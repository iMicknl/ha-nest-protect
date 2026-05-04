# Native Auth Flow: Chrome Extension + Config Flow Integration

## Overview

Replace the current manual DevTools credential extraction with a guided flow: a Chrome extension extracts authentication credentials and presents them as a single copyable code, which users paste into the HA config flow.

**Goal:** Make initial setup and reauth accessible to non-technical users — no DevTools, no Python, no terminal.

## User Experience

### Setup Flow (happy path)

1. User adds "Nest Protect" integration in HA
2. Config flow asks: "How would you like to authenticate?" — Extension (recommended) or Manual
3. User selects Extension → config flow shows instructions with a download link for the extension zip
4. User installs extension (unzip → chrome://extensions → Developer Mode → Load Unpacked)
5. User signs in to home.nest.com
6. User clicks extension icon → clicks "Extract" → extension shows a code string
7. User copies the code, pastes it into the HA config flow field
8. HA decodes, validates, saves → done

### Reauth Flow

Same as setup: config flow shows method choice, user re-runs the extension and pastes a fresh code. Extension remains installed between reauths.

## Architecture

### Component 1: Chrome Extension

Location: `chrome_extension/`

```
chrome_extension/
├── manifest.json
├── popup.html
├── popup.js
├── content_script.js
└── icons/
    ├── icon-48.png
    └── icon-128.png
```

**manifest.json (MV3):**
- `permissions`: `cookies`, `activeTab`
- `host_permissions`: `https://home.nest.com/*`, `https://*.google.com/*`
- `content_scripts`: matches `https://home.nest.com/*`, injects `content_script.js`
- `action`: popup

**content_script.js:**
- Injected into `home.nest.com`
- Listens for messages from the popup
- Executes `gapi.auth2.getAuthInstance().currentUser.get().getAuthResponse(true)` in the page context (via a script element injected into the page, since content scripts are isolated)
- Returns `login_hint` to the popup

**popup.js:**
- On "Extract" click:
  1. Sends message to content script → gets `login_hint`
  2. Calls `chrome.cookies.getAll({ domain: ".google.com" })` → filters for SID, HSID, SSID, APISID, SAPISID
  3. Constructs `issue_token` URL:
     ```
     https://accounts.google.com/o/oauth2/iframerpc
       ?action=issueToken
       &response_type=token%20id_token
       &login_hint=<login_hint>
       &client_id=733249279899-44tchle2kaa9afr5v9ov7jbuojfr9lrq.apps.googleusercontent.com
       &origin=https%3A%2F%2Fhome.nest.com
       &scope=openid%20profile%20email%20https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fnest-account
       &ss_domain=https%3A%2F%2Fhome.nest.com
     ```
  4. Encodes `{ "issue_token": "<url>", "cookies": "SID=x; HSID=x; SSID=x; APISID=x; SAPISID=x" }` as base64
  5. Displays the code with a "Copy" button

**Error states:**
- Tab is not on `home.nest.com` → "Please navigate to home.nest.com and sign in first"
- User not signed in (no `login_hint`) → "Please sign in to your Google account on home.nest.com"
- Missing cookies → "Missing cookies: X, Y. Please sign in to home.nest.com and try again"

### Component 2: Config Flow Changes

File: `custom_components/nest_protect/config_flow.py`

**New steps:**

1. `async_step_user` — unchanged (account type selection)
2. `async_step_auth_method` — **new**: radio/select choice between "extension" and "manual"
3. `async_step_extension` — **new**: single text field ("Paste the code from the extension")
4. `async_step_account_link` — unchanged (manual: issue_token + cookies fields)

**`async_step_auth_method`:**
```python
async def async_step_auth_method(self, user_input=None) -> FlowResult:
    if user_input:
        if user_input["method"] == "extension":
            return await self.async_step_extension()
        return await self.async_step_account_link()

    return self.async_show_form(
        step_id="auth_method",
        data_schema=vol.Schema({
            vol.Required("method", default="extension"): vol.In({
                "extension": "Use the Chrome Extension (recommended)",
                "manual": "Enter credentials manually",
            }),
        }),
        description_placeholders=DESCRIPTION_PLACEHOLDERS,
    )
```

**`async_step_extension`:**
```python
async def async_step_extension(self, user_input=None) -> FlowResult:
    errors = {}

    if user_input:
        try:
            decoded = json.loads(base64.b64decode(user_input["auth_code"]))
            issue_token = decoded["issue_token"]
            cookies = decoded["cookies"]
        except (ValueError, KeyError):
            errors["auth_code"] = "invalid_code"

        if not errors:
            if not self._validate_issue_token(issue_token):
                errors["auth_code"] = "invalid_code"
            elif not self._validate_cookies(cookies):
                errors["auth_code"] = "invalid_code"

        if not errors:
            # Validate full auth chain (same logic as account_link)
            validation_input = {
                CONF_ISSUE_TOKEN: issue_token,
                CONF_COOKIES: cookies,
                CONF_ACCOUNT_TYPE: self._default_account_type,
            }
            try:
                [issue_token, cookies, email] = await self.async_validate_input(validation_input)
            except (TimeoutError, ClientError):
                errors["base"] = "cannot_connect"
            except BadCredentialsException:
                errors["base"] = "invalid_auth"
            except Exception:
                errors["base"] = "unknown"

        if not errors:
            data = {
                CONF_ISSUE_TOKEN: issue_token,
                CONF_COOKIES: cookies,
                CONF_ACCOUNT_TYPE: self._default_account_type,
            }
            # Handle reauth vs new entry (same as account_link)
            if self._config_entry:
                self.hass.config_entries.async_update_entry(
                    self._config_entry, data={**self._config_entry.data, **data}
                )
                self.hass.async_create_task(
                    self.hass.config_entries.async_reload(self._config_entry.entry_id)
                )
                return self.async_abort(reason="reauth_successful")

            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=f"Nest Protect ({email})", data=data)

    return self.async_show_form(
        step_id="extension",
        data_schema=vol.Schema({vol.Required("auth_code"): str}),
        description_placeholders=DESCRIPTION_PLACEHOLDERS,
        errors=errors,
    )
```

**`async_step_reauth`:**
Updated to go through `async_step_auth_method` so users can choose either path.

### Component 3: Strings

Updated `strings.json` with new step descriptions:

```json
{
  "auth_method": {
    "title": "Authentication Method",
    "description": "Choose how to connect your Nest account.",
    "data": {
      "method": "Method"
    }
  },
  "extension": {
    "title": "Connect with Chrome Extension",
    "description": "1. [Download the Nest Auth Helper extension]({extension_download_url})\n2. Unzip and load it in Chrome (chrome://extensions → Developer Mode → Load Unpacked)\n3. Sign in to [home.nest.com]({nest_url})\n4. Click the extension icon → click **Extract**\n5. Copy the code and paste it below",
    "data": {
      "auth_code": "Authentication Code"
    },
    "data_description": {
      "auth_code": "The code displayed by the Nest Auth Helper extension"
    }
  }
}
```

### Component 4: GitHub Actions — Extension Packaging

File: `.github/workflows/release.yml` (extend existing)

On release publish:
1. Zip contents of `chrome_extension/` as `nest-auth-helper.zip`
2. Attach to the release as an asset

This gives a stable download URL:
```
https://github.com/iMicknl/ha-nest-protect/releases/latest/download/nest-auth-helper.zip
```

This URL is used as `{extension_download_url}` in the config flow description placeholders.

### Component 5: Extension README

File: `chrome_extension/README.md`

Install instructions, usage, permissions explanation, and note that the extension should remain installed for future reauth.

## Data Format

**Code string:** `base64(json)`

```json
{
  "issue_token": "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken&...",
  "cookies": "SID=xxx; HSID=xxx; SSID=xxx; APISID=xxx; SAPISID=xxx"
}
```

**Stored in config entry** (unchanged from current format):
- `issue_token`: the full URL string
- `cookies`: the cookie header string
- `account_type`: production/field_test

## Backwards Compatibility

- Config entry version stays at 3 — stored data format is identical
- Manual flow preserved as-is for advanced users
- Existing entries continue working without any migration

## What Stays Unchanged

- `NestClient` and all auth logic
- `NestSessionManager` and session persistence
- All entity/platform code
- Token refresh chain (issue_token + cookies → access_token → JWT → session)

## Error Handling

| Scenario | Where caught | User sees |
|----------|-------------|-----------|
| Extension: tab not on home.nest.com | Extension popup | "Navigate to home.nest.com first" |
| Extension: not signed in | Extension popup | "Sign in to your Google account" |
| Extension: cookies missing | Extension popup | "Missing cookies: X. Sign in again" |
| Config flow: invalid base64/JSON | `async_step_extension` | "Invalid code format" |
| Config flow: auth chain fails | `async_step_extension` | "Authentication failed — session may have expired" |
| Config flow: network error | `async_step_extension` | "Cannot connect" |

## Security Considerations

- The code string contains sensitive session cookies — the config flow description should note "do not share this code"
- Cookies are stored in HA's config entry (encrypted at rest by HA's storage)
- Extension only reads cookies scoped to `.google.com` — no broader access
- Extension does not transmit data anywhere — display only
