"""Tests for the Nest Protect config flow."""

import base64
import json
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nest_protect.config_flow import ConfigFlow
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


class TestCleanCookies:
    """Tests for the _clean_cookies static method."""

    def test_strips_unnecessary_cookies(self) -> None:
        """Test that non-essential cookies are removed."""
        raw = "1P_JAR=value1; CONSENT=YES; SID=abc123; HSID=def456; __Secure-3PSIDTS=extra"
        result = ConfigFlow._clean_cookies(raw)
        assert "SID=abc123" in result
        assert "HSID=def456" in result
        assert "1P_JAR" not in result
        assert "CONSENT" not in result
        assert "__Secure-3PSIDTS" not in result

    def test_keeps_all_required_cookies(self) -> None:
        """Test that all required Google auth cookies are preserved."""
        raw = "NID=nid; __Secure-3PSID=psid; APISID=api; SAPISID=sapi; HSID=h; SSID=s; SID=sid"
        result = ConfigFlow._clean_cookies(raw)
        assert "NID=nid" in result
        assert "__Secure-3PSID=psid" in result
        assert "APISID=api" in result
        assert "SAPISID=sapi" in result
        assert "HSID=h" in result
        assert "SSID=s" in result
        assert "SID=sid" in result

    def test_returns_original_when_no_matches(self) -> None:
        """Test that original cookies are returned when no known cookies found."""
        raw = "unknown1=val1; unknown2=val2"
        result = ConfigFlow._clean_cookies(raw)
        assert result == raw

    def test_handles_whitespace(self) -> None:
        """Test that extra whitespace in cookies is handled."""
        raw = "  SID=abc  ;  HSID=def  ;  APISID=ghi  "
        result = ConfigFlow._clean_cookies(raw)
        assert "SID=abc" in result
        assert "HSID=def" in result
        assert "APISID=ghi" in result


async def test_account_link_cleans_cookies(hass: HomeAssistant) -> None:
    """Test that account_link step cleans cookies before validation."""
    issue_token = "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken&response_type=token%20id_token&login_hint=hint123&client_id=733249279899-44tchle2kaa9afr5v9ov7jbuojfr9lrq.apps.googleusercontent.com&origin=https%3A%2F%2Fhome.nest.com&scope=openid+profile+email+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fnest-account&ss_domain=https%3A%2F%2Fhome.nest.com"
    dirty_cookies = "1P_JAR=extra; CONSENT=YES; SID=abc123456789012345678901234567890; HSID=def1234567890; SSID=ghi1234567890; APISID=jkl1234567890; SAPISID=mno1234567890; __Secure-3PSIDTS=extra"

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

    with patch(
        "custom_components.nest_protect.config_flow.ConfigFlow.async_validate_input",
        new_callable=AsyncMock,
    ) as mock_validate:
        mock_validate.return_value = [issue_token, "cleaned", "user@example.com"]
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"issue_token": issue_token, "cookies": dirty_cookies},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Verify the cookies passed to validate_input were cleaned
    call_args = mock_validate.call_args[0][0]
    assert "1P_JAR" not in call_args["cookies"]
    assert "CONSENT" not in call_args["cookies"]
    assert "__Secure-3PSIDTS" not in call_args["cookies"]
    assert "SID=abc123456789012345678901234567890" in call_args["cookies"]
