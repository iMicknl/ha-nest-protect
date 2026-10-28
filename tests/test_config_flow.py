"""Tests for the Nest Protect config flow."""

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nest_protect.config_flow import (
    ConfigFlow,
    ConfigFlowValidationResult,
)
from custom_components.nest_protect.const import (
    CONF_AUTH_GENERATION,
    CONF_PREVIOUS_AUTH_GENERATION,
    DOMAIN,
)
from custom_components.nest_protect.pynest.exceptions import (
    BadCredentialsException,
    BadGatewayException,
    NestServiceException,
    NotAuthenticatedException,
    PynestException,
)


async def test_step_user_leads_to_auth_method(hass: HomeAssistant) -> None:
    """Test that selecting account type leads to auth_method step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"account_type": "production"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "auth_method"


async def test_auth_method_extension_leads_to_extension_step(
    hass: HomeAssistant,
) -> None:
    """Test selecting extension method leads to extension step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"account_type": "production"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"method": "extension"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "extension"


async def test_auth_method_manual_leads_to_account_link(
    hass: HomeAssistant,
) -> None:
    """Test selecting manual method leads to account_link step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"account_type": "production"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"method": "manual"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "account_link"


async def test_account_link_stores_refreshed_cookies(
    hass: HomeAssistant,
) -> None:
    """Test manual auth stores cookies refreshed during validation."""
    issue_token = "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken&response_type=token%20id_token&login_hint=hint123&client_id=733249279899-44tchle2kaa9afr5v9ov7jbuojfr9lrq.apps.googleusercontent.com&origin=https%3A%2F%2Fhome.nest.com&scope=openid+profile+email+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fnest-account&ss_domain=https%3A%2F%2Fhome.nest.com"
    cookies = "SID=abc123456789012345678901234567890; HSID=def1234567890; SSID=ghi1234567890; APISID=jkl1234567890; SAPISID=mno1234567890"
    refreshed_cookies = f"{cookies}; __Secure-1PSIDTS=fresh"

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"account_type": "production"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"method": "manual"},
    )

    client = MagicMock()
    client.refreshed_cookies = refreshed_cookies
    client.get_access_token_from_cookies = AsyncMock(
        return_value=MagicMock(access_token="google-token")
    )
    client.authenticate = AsyncMock(
        return_value=MagicMock(access_token="nest-token", userid="user1", user="user.1")
    )
    client.get_first_data = AsyncMock(
        return_value=MagicMock(
            updated_buckets=[
                MagicMock(object_key="user.1", value={"email": "user@example.com"})
            ]
        )
    )

    with patch(
        "custom_components.nest_protect.config_flow.NestClient",
        return_value=client,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"issue_token": issue_token, "cookies": cookies},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["cookies"] == refreshed_cookies


async def test_account_link_reuses_rotation_after_nest_validation_failure(
    hass: HomeAssistant,
) -> None:
    """A downstream failure must not make the next attempt replay stale cookies."""
    issue_token = (
        "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken&retry=true"
    )
    cookies = (
        "SID=abc123456789012345678901234567890; HSID=def1234567890; "
        "SSID=ghi1234567890; APISID=jkl1234567890; SAPISID=mno1234567890"
    )
    refreshed_cookies = f"{cookies}; __Secure-1PSIDTS=fresh"

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"account_type": "production"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"method": "manual"},
    )

    client = MagicMock(refreshed_cookies=refreshed_cookies)
    client.get_access_token_from_cookies = AsyncMock(
        return_value=MagicMock(access_token="google-token")
    )
    client.authenticate = AsyncMock(
        side_effect=[
            PynestException("Nest unavailable"),
            MagicMock(
                access_token="nest-token",
                userid="user1",
                user="user.1",
            ),
        ]
    )
    client.get_first_data = AsyncMock(
        return_value=MagicMock(
            updated_buckets=[
                MagicMock(object_key="user.1", value={"email": "user@example.com"})
            ]
        )
    )
    submitted = {"issue_token": issue_token, "cookies": cookies}

    with patch(
        "custom_components.nest_protect.config_flow.NestClient",
        return_value=client,
    ):
        first_result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=dict(submitted)
        )
        second_result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=dict(submitted)
        )

    assert first_result["type"] is FlowResultType.FORM
    assert second_result["type"] is FlowResultType.CREATE_ENTRY
    assert [
        call.args[1] for call in client.get_access_token_from_cookies.await_args_list
    ] == [cookies, refreshed_cookies]


@pytest.mark.parametrize("generation_changed", [False, True])
async def test_reauth_rotation_survives_a_flow_process_restart(
    hass: HomeAssistant,
    generation_changed: bool,
) -> None:
    """Pending rotation survives a restart but cannot cross login generations."""
    issue_token = (
        "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken&retry=true"
    )
    cookies = "SID=old; HSID=old; SSID=old; APISID=old; SAPISID=old"
    refreshed_cookies = "SID=new; HSID=old; SSID=old; APISID=old; SAPISID=old"
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "issue_token": issue_token,
            "cookies": cookies,
            "account_type": "production",
            CONF_AUTH_GENERATION: "old-generation",
        },
    )
    entry.add_to_hass(hass)
    user_input = {
        "issue_token": issue_token,
        "cookies": cookies,
        "account_type": "production",
    }

    first_client = MagicMock(refreshed_cookies=None)

    async def rotate_cookies(*_):
        first_client.refreshed_cookies = refreshed_cookies
        return MagicMock(access_token="google-token")

    first_client.get_access_token_from_cookies = AsyncMock(side_effect=rotate_cookies)
    first_client.authenticate = AsyncMock(
        side_effect=PynestException("Nest unavailable")
    )
    first_flow = ConfigFlow()
    first_flow.hass = hass
    first_flow.context = {"source": SOURCE_REAUTH, "entry_id": entry.entry_id}

    with (
        patch(
            "custom_components.nest_protect.config_flow.NestClient",
            return_value=first_client,
        ),
        pytest.raises(PynestException, match="Nest unavailable"),
    ):
        await first_flow.async_validate_input(user_input)

    if generation_changed:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_AUTH_GENERATION: "new-generation"},
        )

    restarted_client = MagicMock(refreshed_cookies=None)
    restarted_client.get_access_token_from_cookies = AsyncMock(
        side_effect=PynestException("stop after capture")
    )
    if generation_changed:
        restarted_flow = first_flow
    else:
        restarted_flow = ConfigFlow()
        restarted_flow.hass = hass
        restarted_flow.context = {"source": SOURCE_REAUTH, "entry_id": entry.entry_id}

    with (
        patch(
            "custom_components.nest_protect.config_flow.NestClient",
            return_value=restarted_client,
        ),
        pytest.raises(PynestException, match="stop after capture"),
    ):
        await restarted_flow.async_validate_input(user_input)

    assert restarted_client.get_access_token_from_cookies.await_args.args[1] == (
        cookies if generation_changed else refreshed_cookies
    )


async def test_config_validation_falls_back_after_app_launch_rejection(
    hass: HomeAssistant,
) -> None:
    """Config validation must use the complete runtime provider pipeline."""
    flow = ConfigFlow()
    flow.hass = hass
    client = MagicMock(refreshed_cookies=None)
    client.get_access_token_from_cookies = AsyncMock(
        return_value=MagicMock(access_token="cookie-google-token")
    )
    client.get_access_token_from_refresh_token = AsyncMock(
        return_value=MagicMock(access_token="fallback-google-token")
    )
    client.authenticate = AsyncMock(
        side_effect=[
            MagicMock(access_token="cookie-nest-token", userid="user1"),
            MagicMock(
                access_token="fallback-nest-token",
                userid="user1",
                user="user.1",
            ),
        ]
    )
    client.get_first_data = AsyncMock(
        side_effect=[
            NotAuthenticatedException("401"),
            MagicMock(
                updated_buckets=[
                    MagicMock(object_key="user.1", value={"email": "user@example.com"})
                ]
            ),
        ]
    )

    with (
        patch(
            "custom_components.nest_protect.config_flow.NestClient",
            return_value=client,
        ),
        patch.object(flow, "async_set_unique_id", new_callable=AsyncMock),
    ):
        validated = await flow.async_validate_input(
            {
                "issue_token": "https://accounts.google.com/issue",
                "cookies": "SID=current",
                "refresh_token": "legacy-refresh-token",
                "account_type": "production",
            }
        )

    assert validated.email == "user@example.com"
    client.get_access_token_from_refresh_token.assert_awaited_once_with(
        "legacy-refresh-token"
    )


async def test_future_provider_field_changes_reset_pending_updates(
    hass: HomeAssistant,
) -> None:
    """Pending rotations must be scoped by every provider-owned input field."""
    flow = ConfigFlow()
    flow.hass = hass
    seen_credentials = []

    class FutureProvider:
        name = "future"
        credential_fields = frozenset({"future_token"})

        def __init__(self, client, save_credentials):
            self._client = client
            self._save_credentials = save_credentials

        @property
        def available(self):
            return bool(getattr(self._client, "future_token", None))

        async def async_get_access_token(self):
            seen_credentials.append(self._client.future_token)
            if len(seen_credentials) == 1:
                await self._save_credentials({"future_token": "rotated-a"})
            raise PynestException(f"stop {len(seen_credentials)}")

    def create_future_provider(client, save_credentials):
        return (FutureProvider(client, save_credentials),)

    with patch(
        "custom_components.nest_protect.config_flow.create_credential_providers",
        side_effect=create_future_provider,
    ):
        with pytest.raises(PynestException, match="stop 1"):
            await flow.async_validate_input(
                {"future_token": "future-a", "account_type": "production"}
            )
        with pytest.raises(PynestException, match="stop 2"):
            await flow.async_validate_input(
                {"future_token": "future-b", "account_type": "production"}
            )

    assert seen_credentials == ["future-a", "future-b"]


async def test_extension_step_creates_entry(hass: HomeAssistant) -> None:
    """Test extension step decodes code and creates entry on success."""
    issue_token = "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken&response_type=token%20id_token&login_hint=hint123&client_id=733249279899-44tchle2kaa9afr5v9ov7jbuojfr9lrq.apps.googleusercontent.com&origin=https%3A%2F%2Fhome.nest.com&scope=openid+profile+email+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fnest-account&ss_domain=https%3A%2F%2Fhome.nest.com"
    cookies = "SID=abc123456789012345678901234567890; HSID=def1234567890; SSID=ghi1234567890; APISID=jkl1234567890; SAPISID=mno1234567890"
    code = base64.b64encode(
        json.dumps({"issue_token": issue_token, "cookies": cookies}).encode()
    ).decode()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"account_type": "production"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"method": "extension"},
    )
    assert result["step_id"] == "extension"

    with patch(
        "custom_components.nest_protect.config_flow.ConfigFlow.async_validate_input",
        new_callable=AsyncMock,
        return_value=ConfigFlowValidationResult(
            credentials={"issue_token": issue_token, "cookies": cookies},
            credential_fields=frozenset({"issue_token", "cookies", "refresh_token"}),
            email="user@example.com",
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"auth_code": code},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Nest Protect (user@example.com)"
    assert result["data"]["issue_token"] == issue_token
    assert result["data"]["cookies"] == cookies
    assert result["data"]["account_type"] == "production"


async def test_extension_step_stores_refreshed_cookies(
    hass: HomeAssistant,
) -> None:
    """Test extension step stores cookies refreshed during validation."""
    issue_token = "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken&response_type=token%20id_token&login_hint=hint123&client_id=733249279899-44tchle2kaa9afr5v9ov7jbuojfr9lrq.apps.googleusercontent.com&origin=https%3A%2F%2Fhome.nest.com&scope=openid+profile+email+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fnest-account&ss_domain=https%3A%2F%2Fhome.nest.com"
    cookies = "SID=abc123456789012345678901234567890; HSID=def1234567890; SSID=ghi1234567890; APISID=jkl1234567890; SAPISID=mno1234567890"
    refreshed_cookies = f"{cookies}; __Secure-1PSIDTS=fresh"
    code = base64.b64encode(
        json.dumps({"issue_token": issue_token, "cookies": cookies}).encode()
    ).decode()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"account_type": "production"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"method": "extension"},
    )

    client = MagicMock()
    client.refreshed_cookies = refreshed_cookies
    client.get_access_token_from_cookies = AsyncMock(
        return_value=MagicMock(access_token="google-token")
    )
    client.authenticate = AsyncMock(
        return_value=MagicMock(access_token="nest-token", userid="user1", user="user.1")
    )
    client.get_first_data = AsyncMock(
        return_value=MagicMock(
            updated_buckets=[
                MagicMock(object_key="user.1", value={"email": "user@example.com"})
            ]
        )
    )

    with patch(
        "custom_components.nest_protect.config_flow.NestClient",
        return_value=client,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"auth_code": code},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["cookies"] == refreshed_cookies


async def test_extension_step_invalid_code(hass: HomeAssistant) -> None:
    """Test extension step shows error for invalid base64."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"account_type": "production"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"method": "extension"},
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"auth_code": "not-valid-base64!!!"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "extension"
    assert result["errors"]["auth_code"] == "invalid_code"


async def test_extension_step_auth_failure(hass: HomeAssistant) -> None:
    """Test extension step shows error when auth chain fails."""
    issue_token = "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken&response_type=token%20id_token&login_hint=hint123&client_id=733249279899-44tchle2kaa9afr5v9ov7jbuojfr9lrq.apps.googleusercontent.com&origin=https%3A%2F%2Fhome.nest.com&scope=openid+profile+email+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fnest-account&ss_domain=https%3A%2F%2Fhome.nest.com"
    cookies = "SID=abc123456789012345678901234567890; HSID=def1234567890; SSID=ghi1234567890; APISID=jkl1234567890; SAPISID=mno1234567890"
    code = base64.b64encode(
        json.dumps({"issue_token": issue_token, "cookies": cookies}).encode()
    ).decode()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"account_type": "production"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"method": "extension"},
    )

    with patch(
        "custom_components.nest_protect.config_flow.ConfigFlow.async_validate_input",
        new_callable=AsyncMock,
        side_effect=BadCredentialsException("expired"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"auth_code": code},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "extension"
    assert result["errors"]["base"] == "invalid_auth"


async def test_reauth_shows_auth_method(hass: HomeAssistant) -> None:
    """Test reauth flow shows auth method selection."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "issue_token": "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken&fake=true",
            "cookies": "SID=old; HSID=old; SSID=old; APISID=old; SAPISID=old",
            "account_type": "production",
        },
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "auth_method"


async def test_successful_reauth_stages_new_durable_generation(
    hass: HomeAssistant,
) -> None:
    """New user credentials must not be shadowed by old stored rotations."""
    old_issue_token = (
        "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken&old=true"
    )
    new_issue_token = (
        "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken&new=true"
    )
    old_cookies = (
        "SID=old123456789012345678901234567890; HSID=old1234567890; "
        "SSID=old1234567890; APISID=old1234567890; SAPISID=old1234567890"
    )
    new_cookies = (
        "SID=new123456789012345678901234567890; HSID=new1234567890; "
        "SSID=new1234567890; APISID=new1234567890; SAPISID=new1234567890"
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user.1",
        data={
            "issue_token": old_issue_token,
            "cookies": old_cookies,
            "refresh_token": "legacy-refresh-token",
            "account_type": "production",
            CONF_AUTH_GENERATION: "old-generation",
        },
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"method": "manual"}
    )

    persistence_events = []
    original_update_entry = hass.config_entries.async_update_entry

    async def save_handoff(data):
        persistence_events.append("store")

    def update_entry(*args, **kwargs):
        persistence_events.append("entry")
        return original_update_entry(*args, **kwargs)

    with (
        patch(
            "custom_components.nest_protect.config_flow.NestClient",
            return_value=_mock_client("user.1"),
        ),
        patch(
            "custom_components.nest_protect.config_flow.Store.async_save",
            new_callable=AsyncMock,
            side_effect=save_handoff,
        ) as save_store,
        patch(
            "custom_components.nest_protect.config_flow.Store.async_remove",
            new_callable=AsyncMock,
            side_effect=OSError("cleanup failed"),
        ) as remove_pending_store,
        patch.object(
            hass.config_entries,
            "async_update_entry",
            side_effect=update_entry,
        ),
        patch.object(hass.config_entries, "async_schedule_reload") as reload_entry,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"issue_token": new_issue_token, "cookies": new_cookies},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert persistence_events == ["store", "entry"]
    assert entry.data[CONF_AUTH_GENERATION] != "old-generation"
    durable_handoff = save_store.await_args.args[0]
    assert durable_handoff[CONF_AUTH_GENERATION] == entry.data[CONF_AUTH_GENERATION]
    assert durable_handoff[CONF_PREVIOUS_AUTH_GENERATION] == "old-generation"
    assert durable_handoff["credentials"]["cookies"] == new_cookies
    assert "refresh_token" not in durable_handoff["credentials"]
    assert "refresh_token" not in entry.data
    remove_pending_store.assert_awaited_once_with()
    reload_entry.assert_called_once_with(entry.entry_id)


@pytest.mark.parametrize(
    "error",
    [NestServiceException("503 error"), BadGatewayException("502 error")],
)
@pytest.mark.parametrize("source", ["user", "reauth"])
@pytest.mark.parametrize("method", ["extension", "manual"])
async def test_transient_service_error_shows_cannot_connect(
    hass: HomeAssistant, error: Exception, source: str, method: str
) -> None:
    """Test a temporary Nest outage is reported as cannot_connect."""
    issue_token = "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken&response_type=token%20id_token&login_hint=hint123&client_id=733249279899-44tchle2kaa9afr5v9ov7jbuojfr9lrq.apps.googleusercontent.com&origin=https%3A%2F%2Fhome.nest.com&scope=openid+profile+email+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fnest-account&ss_domain=https%3A%2F%2Fhome.nest.com"
    cookies = "SID=abc123456789012345678901234567890; HSID=def1234567890; SSID=ghi1234567890; APISID=jkl1234567890; SAPISID=mno1234567890"

    if source == "reauth":
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                "issue_token": issue_token,
                "cookies": cookies,
                "account_type": "production",
            },
        )
        entry.add_to_hass(hass)
        result = await entry.start_reauth_flow(hass)
    else:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"account_type": "production"},
        )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"method": method},
    )

    if method == "extension":
        user_input = {
            "auth_code": base64.b64encode(
                json.dumps({"issue_token": issue_token, "cookies": cookies}).encode()
            ).decode()
        }
    else:
        user_input = {"issue_token": issue_token, "cookies": cookies}

    with patch(
        "custom_components.nest_protect.config_flow.ConfigFlow.async_validate_input",
        new_callable=AsyncMock,
        side_effect=error,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=user_input,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "cannot_connect"


VALID_ISSUE_TOKEN = "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken&response_type=token%20id_token&login_hint=hint123&client_id=733249279899-44tchle2kaa9afr5v9ov7jbuojfr9lrq.apps.googleusercontent.com&origin=https%3A%2F%2Fhome.nest.com&scope=openid+profile+email+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fnest-account&ss_domain=https%3A%2F%2Fhome.nest.com"
VALID_COOKIES = "SID=abc123456789012345678901234567890; HSID=def1234567890; SSID=ghi1234567890; APISID=jkl1234567890; SAPISID=mno1234567890"


def _mock_client(user: str = "user.1", email: str = "user@example.com") -> MagicMock:
    """Return a NestClient mock that authenticates as the given account."""
    client = MagicMock()
    client.refreshed_cookies = None
    client.get_access_token_from_cookies = AsyncMock(
        return_value=MagicMock(access_token="google-token")
    )
    client.authenticate = AsyncMock(
        return_value=MagicMock(
            access_token="nest-token", userid=user.removeprefix("user."), user=user
        )
    )
    client.get_first_data = AsyncMock(
        return_value=MagicMock(
            updated_buckets=[MagicMock(object_key=user, value={"email": email})]
        )
    )
    return client


def _existing_entry(hass: HomeAssistant, unique_id: str | None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        unique_id=unique_id,
        title="Nest Protect (user@example.com)",
        data={
            "issue_token": "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken&fake=true",
            "cookies": "SID=old; HSID=old; SSID=old; APISID=old; SAPISID=old",
            "account_type": "production",
        },
    )
    entry.add_to_hass(hass)
    return entry


async def _submit_manual_credentials(
    hass: HomeAssistant,
    result: dict,
    client: MagicMock,
    cookies: str = VALID_COOKIES,
) -> dict:
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"method": "manual"}
    )
    assert result["step_id"] == "account_link"

    with (
        patch(
            "custom_components.nest_protect.config_flow.NestClient",
            return_value=client,
        ),
        patch(
            "custom_components.nest_protect.async_setup_entry", return_value=True
        ) as mock_setup_entry,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"issue_token": VALID_ISSUE_TOKEN, "cookies": cookies},
        )
        await hass.async_block_till_done()

    result["setup_entry_calls"] = len(mock_setup_entry.mock_calls)
    return result


async def test_reauth_same_account_updates_and_reloads(hass: HomeAssistant) -> None:
    """Test reauth with the same account updates the entry and reloads it."""
    entry = _existing_entry(hass, "user.1")

    result = await entry.start_reauth_flow(hass)
    result = await _submit_manual_credentials(hass, result, _mock_client("user.1"))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    # Translated centrally, strings.json has no key for it.
    assert result["translation_domain"] == "homeassistant"
    assert entry.data["issue_token"] == VALID_ISSUE_TOKEN
    assert entry.data["cookies"] == VALID_COOKIES
    assert entry.data["account_type"] == "production"
    assert result["setup_entry_calls"] == 1


async def test_reauth_extension_same_account_updates_entry(
    hass: HomeAssistant,
) -> None:
    """Test reauth through the extension step updates the entry."""
    entry = _existing_entry(hass, "user.1")
    code = base64.b64encode(
        json.dumps(
            {"issue_token": VALID_ISSUE_TOKEN, "cookies": VALID_COOKIES}
        ).encode()
    ).decode()

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"method": "extension"}
    )
    with (
        patch(
            "custom_components.nest_protect.config_flow.NestClient",
            return_value=_mock_client("user.1"),
        ),
        patch("custom_components.nest_protect.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"auth_code": code}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["cookies"] == VALID_COOKIES


async def test_reauth_different_account_aborts(hass: HomeAssistant) -> None:
    """Test reauth with another Google account leaves the entry untouched."""
    entry = _existing_entry(hass, "user.1")
    original_data = dict(entry.data)

    result = await entry.start_reauth_flow(hass)
    result = await _submit_manual_credentials(hass, result, _mock_client("user.2"))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert dict(entry.data) == original_data
    assert entry.unique_id == "user.1"
    assert result["setup_entry_calls"] == 0


async def test_reauth_extension_different_account_aborts(
    hass: HomeAssistant,
) -> None:
    """Test reauth through the extension step rejects another account."""
    entry = _existing_entry(hass, "user.1")
    original_data = dict(entry.data)
    code = base64.b64encode(
        json.dumps(
            {"issue_token": VALID_ISSUE_TOKEN, "cookies": VALID_COOKIES}
        ).encode()
    ).decode()

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"method": "extension"}
    )
    with patch(
        "custom_components.nest_protect.config_flow.NestClient",
        return_value=_mock_client("user.2"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"auth_code": code}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert dict(entry.data) == original_data


async def test_reauth_legacy_unique_id_is_replaced(hass: HomeAssistant) -> None:
    """Test reauth on an entry keyed by a legacy token adopts the account ID."""
    entry = _existing_entry(hass, "legacy-refresh-token")

    result = await entry.start_reauth_flow(hass)
    result = await _submit_manual_credentials(hass, result, _mock_client("user.1"))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.unique_id == "user.1"
    assert entry.data["cookies"] == VALID_COOKIES


async def test_reauth_missing_unique_id_is_set(hass: HomeAssistant) -> None:
    """Test reauth on an entry without unique ID stores the account ID."""
    entry = _existing_entry(hass, None)

    result = await entry.start_reauth_flow(hass)
    result = await _submit_manual_credentials(hass, result, _mock_client("user.1"))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.unique_id == "user.1"


async def test_reauth_legacy_entry_with_account_of_other_entry_aborts(
    hass: HomeAssistant,
) -> None:
    """Test a legacy entry can't take over an account owned by another entry."""
    _existing_entry(hass, "user.1")
    legacy_entry = _existing_entry(hass, "legacy-refresh-token")
    original_data = dict(legacy_entry.data)

    result = await legacy_entry.start_reauth_flow(hass)
    result = await _submit_manual_credentials(hass, result, _mock_client("user.1"))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert legacy_entry.unique_id == "legacy-refresh-token"
    assert dict(legacy_entry.data) == original_data


async def test_reconfigure_same_account_updates_and_reloads(
    hass: HomeAssistant,
) -> None:
    """Test reconfigure reuses the credential steps and updates the entry."""
    entry = _existing_entry(hass, "user.1")

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "auth_method"

    result = await _submit_manual_credentials(hass, result, _mock_client("user.1"))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert result["translation_domain"] == "homeassistant"
    assert entry.data["issue_token"] == VALID_ISSUE_TOKEN
    assert entry.data["cookies"] == VALID_COOKIES
    assert result["setup_entry_calls"] == 1


async def test_reconfigure_different_account_aborts(hass: HomeAssistant) -> None:
    """Test reconfigure with another Google account leaves the entry untouched."""
    entry = _existing_entry(hass, "user.1")
    original_data = dict(entry.data)

    result = await entry.start_reconfigure_flow(hass)
    result = await _submit_manual_credentials(hass, result, _mock_client("user.2"))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert dict(entry.data) == original_data


async def test_reconfigure_keeps_account_type(hass: HomeAssistant) -> None:
    """Test reconfigure validates against the entry's environment."""
    entry = _existing_entry(hass, "user.1")
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "account_type": "fieldtest"}
    )

    result = await entry.start_reconfigure_flow(hass)
    result = await _submit_manual_credentials(hass, result, _mock_client("user.1"))

    assert result["reason"] == "reconfigure_successful"
    assert entry.data["account_type"] == "fieldtest"


async def test_account_link_normalizes_pasted_cookies(hass: HomeAssistant) -> None:
    """Test a Cookie: prefix and line wraps in pasted cookies are tolerated."""
    pasted = (
        "Cookie: SID=abc12345678901234567\n8901234567890;\nHSID=def1234567890; "
        "SSID=ghi1234567890;\r\n APISID=jkl1234567890; SAPISID=mno1234567890\n"
    )
    client = _mock_client("user.1")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"account_type": "production"}
    )
    result = await _submit_manual_credentials(hass, result, client, cookies=pasted)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["cookies"] == VALID_COOKIES
    client.get_access_token_from_cookies.assert_awaited_once_with(
        VALID_ISSUE_TOKEN, VALID_COOKIES
    )
    assert result["result"].unique_id == "user.1"
