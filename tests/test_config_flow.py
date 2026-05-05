"""Tests for the Nest Protect config flow."""

import base64
import json
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nest_protect.const import DOMAIN
from custom_components.nest_protect.pynest.exceptions import BadCredentialsException


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
        return_value=[issue_token, cookies, "user@example.com"],
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
