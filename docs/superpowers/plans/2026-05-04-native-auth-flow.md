# Native Auth Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the manual DevTools credential extraction with a guided Chrome extension + single-paste config flow.

**Architecture:** A Chrome extension (MV3) extracts `login_hint` via a content script on `home.nest.com` and reads HttpOnly cookies via `chrome.cookies` API. It encodes both as a base64 JSON blob. The HA config flow adds a method-choice step, then decodes the blob and validates credentials through the existing `NestClient` auth chain.

**Tech Stack:** Chrome Extension Manifest V3 (JS), Home Assistant config flow (Python), GitHub Actions (YAML)

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Rewrite | `chrome_extension/manifest.json` | MV3 manifest with content_scripts, cookies, activeTab |
| Rewrite | `chrome_extension/popup.html` | Extension popup UI |
| Rewrite | `chrome_extension/popup.js` | Extract cookies, build code, display to user |
| Create | `chrome_extension/content_script.js` | Extract login_hint from home.nest.com page context |
| Rewrite | `chrome_extension/README.md` | Install/usage instructions |
| Modify | `custom_components/nest_protect/config_flow.py` | Add auth_method + extension steps |
| Modify | `custom_components/nest_protect/strings.json` | Add strings for new steps |
| Modify | `custom_components/nest_protect/const.py` | Add CONF_AUTH_CODE constant |
| Create | `tests/test_config_flow.py` | Config flow tests |
| Modify | `.github/workflows/release.yml` | Add zip + upload job |

---

### Task 1: Chrome Extension — Content Script

**Files:**
- Create: `chrome_extension/content_script.js`

This script is injected into `home.nest.com` pages. It listens for messages from the popup and extracts `login_hint` from the page's `gapi.auth2` instance by injecting a script element into the page context (content scripts run in an isolated world).

- [ ] **Step 1: Create content_script.js**

```javascript
// content_script.js
// Injected into home.nest.com — extracts login_hint from the page's gapi.auth2 instance.

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action !== "extractLoginHint") return false;

  const script = document.createElement("script");
  script.textContent = `
    (function() {
      try {
        var auth = gapi.auth2.getAuthInstance();
        var user = auth.currentUser.get();
        var resp = user.getAuthResponse(true);
        if (!resp || !resp.access_token) {
          window.postMessage({type: "NEST_AUTH_RESULT", error: "not_signed_in"}, "*");
          return;
        }
        window.postMessage({
          type: "NEST_AUTH_RESULT",
          login_hint: resp.login_hint,
          email: user.getBasicProfile().getEmail()
        }, "*");
      } catch(e) {
        window.postMessage({type: "NEST_AUTH_RESULT", error: e.message}, "*");
      }
    })();
  `;
  document.documentElement.appendChild(script);
  script.remove();

  const handler = (event) => {
    if (event.data && event.data.type === "NEST_AUTH_RESULT") {
      window.removeEventListener("message", handler);
      sendResponse(event.data);
    }
  };
  window.addEventListener("message", handler);

  // Timeout after 3 seconds
  setTimeout(() => {
    window.removeEventListener("message", handler);
    sendResponse({ error: "timeout" });
  }, 3000);

  return true; // Keep message channel open for async sendResponse
});
```

- [ ] **Step 2: Verify file exists**

Run: `cat chrome_extension/content_script.js | head -5`
Expected: Shows the first 5 lines of the file.

- [ ] **Step 3: Commit**

```bash
git add chrome_extension/content_script.js
git commit -m "feat(extension): add content script for login_hint extraction"
```

---

### Task 2: Chrome Extension — Manifest & Popup Rewrite

**Files:**
- Rewrite: `chrome_extension/manifest.json`
- Rewrite: `chrome_extension/popup.html`
- Rewrite: `chrome_extension/popup.js`

The manifest adds `content_scripts` for home.nest.com, and the popup is rewritten to extract cookies + login_hint and display a single base64 code.

- [ ] **Step 1: Rewrite manifest.json**

```json
{
  "manifest_version": 3,
  "name": "Nest Auth Helper",
  "version": "1.0.0",
  "description": "Extract authentication credentials for the ha-nest-protect Home Assistant integration",
  "permissions": [
    "cookies",
    "activeTab"
  ],
  "host_permissions": [
    "https://home.nest.com/*",
    "https://*.google.com/*"
  ],
  "content_scripts": [
    {
      "matches": ["https://home.nest.com/*"],
      "js": ["content_script.js"],
      "run_at": "document_idle"
    }
  ],
  "action": {
    "default_popup": "popup.html",
    "default_icon": {
      "48": "icons/icon-48.png",
      "128": "icons/icon-128.png"
    }
  },
  "icons": {
    "48": "icons/icon-48.png",
    "128": "icons/icon-128.png"
  }
}
```

- [ ] **Step 2: Rewrite popup.html**

```html
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
body {
  width: 380px;
  padding: 20px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  font-size: 14px;
  color: #333;
}
h2 { margin: 0 0 12px; font-size: 16px; }
p { margin: 8px 0; line-height: 1.5; }
.status {
  padding: 12px;
  border-radius: 8px;
  margin: 12px 0;
}
.status.info { background: #e3f2fd; color: #1565c0; }
.status.success { background: #e8f5e9; color: #2e7d32; }
.status.error { background: #fbe9e7; color: #c62828; }
.output-area {
  margin-top: 12px;
}
.output-area label {
  display: block;
  font-weight: 500;
  font-size: 13px;
  margin-bottom: 4px;
}
.output-area textarea {
  width: 100%;
  height: 100px;
  padding: 8px;
  border: 1px solid #ccc;
  border-radius: 4px;
  box-sizing: border-box;
  font-family: monospace;
  font-size: 11px;
  resize: vertical;
  word-break: break-all;
}
button {
  margin-top: 12px;
  width: 100%;
  padding: 10px;
  background: #1976d2;
  color: white;
  border: none;
  border-radius: 6px;
  font-size: 14px;
  cursor: pointer;
}
button:hover { background: #1565c0; }
button:disabled { background: #bbb; cursor: not-allowed; }
button.copy-btn {
  background: #43a047;
  margin-top: 8px;
}
button.copy-btn:hover { background: #388e3c; }
.warning {
  font-size: 12px;
  color: #e65100;
  margin-top: 8px;
}
</style>
</head>
<body>
  <h2>Nest Auth Helper</h2>
  <p>Extract credentials for the <strong>ha-nest-protect</strong> Home Assistant integration.</p>

  <button id="extract_btn">Extract Credentials</button>

  <div id="status_area"></div>

  <div id="output_area" class="output-area" style="display:none;">
    <label for="auth_code">Your Authentication Code</label>
    <textarea id="auth_code" readonly></textarea>
    <button id="copy_btn" class="copy-btn">Copy Code</button>
    <p class="warning">Do not share this code — it contains your Google session credentials.</p>
  </div>

  <script src="popup.js"></script>
</body>
</html>
```

- [ ] **Step 3: Rewrite popup.js**

```javascript
const extractBtn = document.getElementById("extract_btn");
const copyBtn = document.getElementById("copy_btn");
const statusArea = document.getElementById("status_area");
const outputArea = document.getElementById("output_area");
const authCodeField = document.getElementById("auth_code");

const REQUIRED_COOKIES = ["SID", "HSID", "SSID", "APISID", "SAPISID"];
const CLIENT_ID =
  "733249279899-44tchle2kaa9afr5v9ov7jbuojfr9lrq.apps.googleusercontent.com";

function setStatus(msg, type) {
  statusArea.innerHTML = `<div class="status ${type}">${msg}</div>`;
}

extractBtn.addEventListener("click", async () => {
  extractBtn.disabled = true;
  outputArea.style.display = "none";
  setStatus("Extracting credentials...", "info");

  try {
    // Step 1: Get the active tab — must be home.nest.com
    const [tab] = await chrome.tabs.query({
      active: true,
      currentWindow: true,
    });

    if (!tab || !tab.url || !tab.url.startsWith("https://home.nest.com")) {
      setStatus(
        "Please navigate to <b>home.nest.com</b> and sign in first, then try again.",
        "error"
      );
      extractBtn.disabled = false;
      return;
    }

    // Step 2: Ask content script for login_hint
    const authResult = await chrome.tabs.sendMessage(tab.id, {
      action: "extractLoginHint",
    });

    if (!authResult || authResult.error) {
      const errorMsg =
        authResult && authResult.error === "not_signed_in"
          ? "You are not signed in. Please sign in to your Google account on home.nest.com first."
          : `Could not extract credentials: ${authResult ? authResult.error : "no response"}. Make sure you are signed in on home.nest.com.`;
      setStatus(errorMsg, "error");
      extractBtn.disabled = false;
      return;
    }

    const loginHint = authResult.login_hint;

    // Step 3: Read cookies
    const cookies = await chrome.cookies.getAll({ domain: ".google.com" });
    const relevant = cookies.filter((c) => REQUIRED_COOKIES.includes(c.name));

    const missing = REQUIRED_COOKIES.filter(
      (name) => !relevant.some((c) => c.name === name)
    );

    if (missing.length > 0) {
      setStatus(
        `Missing cookies: <b>${missing.join(", ")}</b>. Please make sure you are signed in to home.nest.com and try again.`,
        "error"
      );
      extractBtn.disabled = false;
      return;
    }

    // Step 4: Build issue_token URL
    const params = new URLSearchParams({
      action: "issueToken",
      response_type: "token id_token",
      login_hint: loginHint,
      client_id: CLIENT_ID,
      origin: "https://home.nest.com",
      scope:
        "openid profile email https://www.googleapis.com/auth/nest-account",
      ss_domain: "https://home.nest.com",
    });
    const issueToken = `https://accounts.google.com/o/oauth2/iframerpc?${params.toString()}`;

    // Step 5: Build cookie string
    const cookieString = relevant
      .map((c) => `${c.name}=${c.value}`)
      .join("; ");

    // Step 6: Encode as base64
    const payload = JSON.stringify({
      issue_token: issueToken,
      cookies: cookieString,
    });
    const code = btoa(payload);

    // Step 7: Display
    authCodeField.value = code;
    outputArea.style.display = "block";
    setStatus(
      `Credentials extracted for <b>${authResult.email || "your account"}</b>. Copy the code below and paste it into Home Assistant.`,
      "success"
    );
  } catch (err) {
    setStatus(`Error: ${err.message}`, "error");
  } finally {
    extractBtn.disabled = false;
  }
});

copyBtn.addEventListener("click", () => {
  navigator.clipboard.writeText(authCodeField.value).then(() => {
    copyBtn.textContent = "Copied!";
    setTimeout(() => {
      copyBtn.textContent = "Copy Code";
    }, 2000);
  });
});
```

- [ ] **Step 4: Verify all extension files**

Run: `ls chrome_extension/`
Expected: `content_script.js  icons  manifest.json  popup.html  popup.js  README.md`

- [ ] **Step 5: Commit**

```bash
git add chrome_extension/manifest.json chrome_extension/popup.html chrome_extension/popup.js
git commit -m "feat(extension): rewrite popup and manifest for code-based auth extraction"
```

---

### Task 3: Chrome Extension — README

**Files:**
- Rewrite: `chrome_extension/README.md`

- [ ] **Step 1: Rewrite README.md**

```markdown
# Nest Auth Helper — Chrome Extension

A Chrome extension that extracts the authentication credentials needed to configure the **ha-nest-protect** Home Assistant integration. It produces a single code string you paste into the HA setup flow.

## Installation

1. Download the latest `nest-auth-helper.zip` from the [Releases page](https://github.com/iMicknl/ha-nest-protect/releases/latest/download/nest-auth-helper.zip)
2. Unzip the file
3. Open Chrome and navigate to `chrome://extensions`
4. Enable **Developer mode** (toggle in the top-right corner)
5. Click **Load unpacked** and select the unzipped folder
6. The extension icon will appear in your toolbar

## Usage

1. Sign in to [home.nest.com](https://home.nest.com) in Chrome
2. Click the extension icon in your toolbar
3. Click **Extract Credentials**
4. Click **Copy Code**
5. Paste the code into the Home Assistant config flow

## Permissions

This extension requests the minimum permissions needed:

- **cookies** — to read Google authentication cookies (SID, HSID, SSID, APISID, SAPISID)
- **activeTab** — to communicate with the content script on home.nest.com
- **Host access** — scoped to `home.nest.com` and `*.google.com` only

The extension does not transmit any data externally. All values are displayed locally for you to copy.

## Keep Installed

Keep this extension installed — you may need it again if Home Assistant requests re-authentication.
```

- [ ] **Step 2: Commit**

```bash
git add chrome_extension/README.md
git commit -m "docs(extension): update README for code-based auth flow"
```

---

### Task 4: Config Flow — Add `async_step_auth_method`

**Files:**
- Modify: `custom_components/nest_protect/config_flow.py`
- Modify: `custom_components/nest_protect/strings.json`

- [ ] **Step 1: Write failing test for auth_method step**

Create `tests/test_config_flow.py`:

```python
"""Tests for the Nest Protect config flow."""

import base64
import json
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.nest_protect.const import DOMAIN


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect -- uv run pytest tests/test_config_flow.py::test_step_user_leads_to_auth_method -v`
Expected: FAIL — `async_step_user` currently calls `async_step_account_link` directly, not `async_step_auth_method`.

- [ ] **Step 3: Modify config_flow.py — add auth_method step**

In `config_flow.py`, change `async_step_user` to call `async_step_auth_method` instead of `async_step_account_link`, and add the new step.

Add to imports at the top:

```python
import base64
import json
```

Change `async_step_user` body when `user_input` is truthy:

```python
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a flow initialized by the user."""
        errors = {}

        if user_input:
            self._default_account_type = user_input[CONF_ACCOUNT_TYPE]
            return await self.async_step_auth_method()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ACCOUNT_TYPE, default=self._default_account_type
                    ): vol.In(
                        {key: env.name for key, env in NEST_ENVIRONMENTS.items()}
                    ),
                }
            ),
            errors=errors,
        )
```

Add new `async_step_auth_method`:

```python
    async def async_step_auth_method(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle auth method selection."""
        if user_input:
            if user_input["method"] == "extension":
                return await self.async_step_extension()
            return await self.async_step_account_link()

        return self.async_show_form(
            step_id="auth_method",
            data_schema=vol.Schema(
                {
                    vol.Required("method", default="extension"): vol.In(
                        {
                            "extension": "Use the Chrome Extension (recommended)",
                            "manual": "Enter credentials manually",
                        }
                    ),
                }
            ),
            description_placeholders=DESCRIPTION_PLACEHOLDERS,
        )
```

- [ ] **Step 4: Update strings.json — add auth_method step**

Add to `"config" > "step"`:

```json
"auth_method": {
    "title": "Authentication Method",
    "description": "Choose how to connect your Nest account. The Chrome Extension method is recommended for most users.",
    "data": {
        "method": "Method"
    }
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect -- uv run pytest tests/test_config_flow.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/test_config_flow.py custom_components/nest_protect/config_flow.py custom_components/nest_protect/strings.json
git commit -m "feat(config_flow): add auth method selection step"
```

---

### Task 5: Config Flow — Add `async_step_extension`

**Files:**
- Modify: `custom_components/nest_protect/config_flow.py`
- Modify: `custom_components/nest_protect/strings.json`
- Modify: `custom_components/nest_protect/const.py`
- Modify: `tests/test_config_flow.py`

- [ ] **Step 1: Add CONF_AUTH_CODE to const.py**

Add after the existing CONF constants:

```python
CONF_AUTH_CODE: Final = "auth_code"
```

- [ ] **Step 2: Update DESCRIPTION_PLACEHOLDERS in config_flow.py**

```python
DESCRIPTION_PLACEHOLDERS = {
    "nest_url": "https://home.nest.com",
    "issue_token_prefix": "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken",
    "accounts_url": "https://accounts.google.com/",
    "extension_download_url": "https://github.com/iMicknl/ha-nest-protect/releases/latest/download/nest-auth-helper.zip",
}
```

- [ ] **Step 3: Write failing test for extension step — success path**

Add to `tests/test_config_flow.py`:

```python
async def test_extension_step_creates_entry(hass: HomeAssistant) -> None:
    """Test extension step decodes code and creates entry on success."""
    issue_token = "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken&response_type=token%20id_token&login_hint=hint123&client_id=733249279899-44tchle2kaa9afr5v9ov7jbuojfr9lrq.apps.googleusercontent.com&origin=https%3A%2F%2Fhome.nest.com&scope=openid+profile+email+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fnest-account&ss_domain=https%3A%2F%2Fhome.nest.com"
    cookies = "SID=abc; HSID=def; SSID=ghi; APISID=jkl; SAPISID=mno"
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
```

- [ ] **Step 4: Write failing test for extension step — invalid code**

Add to `tests/test_config_flow.py`:

```python
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
```

- [ ] **Step 5: Write failing test for extension step — auth failure**

Add to `tests/test_config_flow.py`:

```python
from custom_components.nest_protect.pynest.exceptions import BadCredentialsException


async def test_extension_step_auth_failure(hass: HomeAssistant) -> None:
    """Test extension step shows error when auth chain fails."""
    issue_token = "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken&response_type=token%20id_token&login_hint=hint123&client_id=733249279899-44tchle2kaa9afr5v9ov7jbuojfr9lrq.apps.googleusercontent.com&origin=https%3A%2F%2Fhome.nest.com&scope=openid+profile+email+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fnest-account&ss_domain=https%3A%2F%2Fhome.nest.com"
    cookies = "SID=abc; HSID=def; SSID=ghi; APISID=jkl; SAPISID=mno"
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
        side_effect=BadCredentialsException("expired"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"auth_code": code},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "extension"
    assert result["errors"]["base"] == "invalid_auth"
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect -- uv run pytest tests/test_config_flow.py::test_extension_step_creates_entry tests/test_config_flow.py::test_extension_step_invalid_code tests/test_config_flow.py::test_extension_step_auth_failure -v`
Expected: FAIL — `async_step_extension` does not exist yet.

- [ ] **Step 7: Implement async_step_extension in config_flow.py**

Add import of `CONF_AUTH_CODE` from `.const` and add the step method to the `ConfigFlow` class:

```python
    async def async_step_extension(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle authentication via Chrome extension code."""
        errors = {}

        if user_input:
            issue_token = ""
            cookies = ""

            try:
                decoded = json.loads(
                    base64.b64decode(user_input[CONF_AUTH_CODE]).decode()
                )
                issue_token = decoded["issue_token"]
                cookies = decoded["cookies"]
            except (ValueError, KeyError, json.JSONDecodeError):
                errors[CONF_AUTH_CODE] = "invalid_code"

            if not errors:
                if not self._validate_issue_token(issue_token):
                    errors[CONF_AUTH_CODE] = "invalid_code"
                elif not self._validate_cookies(cookies):
                    errors[CONF_AUTH_CODE] = "invalid_code"

            if not errors:
                validation_input = {
                    CONF_ISSUE_TOKEN: issue_token,
                    CONF_COOKIES: cookies,
                    CONF_ACCOUNT_TYPE: self._default_account_type,
                }
                try:
                    [issue_token, cookies, email] = await self.async_validate_input(
                        validation_input
                    )
                except (TimeoutError, ClientError):
                    errors["base"] = "cannot_connect"
                except BadCredentialsException:
                    errors["base"] = "invalid_auth"
                except Exception as exception:  # pylint: disable=broad-except
                    errors["base"] = "unknown"
                    LOGGER.exception(exception)

            if not errors:
                data = {
                    CONF_ISSUE_TOKEN: issue_token,
                    CONF_COOKIES: cookies,
                    CONF_ACCOUNT_TYPE: self._default_account_type,
                }

                if self._config_entry:
                    self.hass.config_entries.async_update_entry(
                        self._config_entry,
                        data={**self._config_entry.data, **data},
                    )
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(
                            self._config_entry.entry_id
                        )
                    )
                    return self.async_abort(reason="reauth_successful")

                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Nest Protect ({email})", data=data
                )

        return self.async_show_form(
            step_id="extension",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_AUTH_CODE): str,
                }
            ),
            description_placeholders=DESCRIPTION_PLACEHOLDERS,
            errors=errors,
        )
```

- [ ] **Step 8: Add strings for extension step**

Add to `"config" > "step"` in `strings.json`:

```json
"extension": {
    "title": "Connect with Chrome Extension",
    "description": "1. [Download the Nest Auth Helper extension]({extension_download_url})\n2. Unzip and load it in Chrome (chrome://extensions → Developer Mode → Load Unpacked)\n3. Sign in to [home.nest.com]({nest_url})\n4. Click the extension icon → click **Extract Credentials**\n5. Copy the code and paste it below\n\n⚠️ Do not share this code — it contains your session credentials.",
    "data": {
        "auth_code": "Authentication Code"
    },
    "data_description": {
        "auth_code": "The code displayed by the Nest Auth Helper extension"
    }
}
```

- [ ] **Step 9: Add error string for invalid_code**

Add to `"config" > "error"` in `strings.json`:

```json
"invalid_code": "Invalid authentication code. Make sure you copied the complete code from the extension."
```

- [ ] **Step 10: Run tests to verify they pass**

Run: `devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect -- uv run pytest tests/test_config_flow.py -v`
Expected: All 6 tests PASS.

- [ ] **Step 11: Commit**

```bash
git add custom_components/nest_protect/config_flow.py custom_components/nest_protect/const.py custom_components/nest_protect/strings.json tests/test_config_flow.py
git commit -m "feat(config_flow): add extension step with code decoding and validation"
```

---

### Task 6: Config Flow — Update Reauth to Use `async_step_auth_method`

**Files:**
- Modify: `custom_components/nest_protect/config_flow.py`
- Modify: `tests/test_config_flow.py`

- [ ] **Step 1: Write failing test for reauth**

Add to `tests/test_config_flow.py`:

```python
from pytest_homeassistant_custom_component.common import MockConfigEntry


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect -- uv run pytest tests/test_config_flow.py::test_reauth_shows_auth_method -v`
Expected: FAIL — current reauth goes directly to `account_link`.

- [ ] **Step 3: Update async_step_reauth**

Replace the `async_step_reauth` method:

```python
    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle reauth."""
        self._config_entry = cast(
            ConfigEntry,
            self.hass.config_entries.async_get_entry(self.context["entry_id"]),
        )

        self._default_account_type = self._config_entry.data[CONF_ACCOUNT_TYPE]

        return await self.async_step_auth_method(user_input)
```

- [ ] **Step 4: Run all config flow tests**

Run: `devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect -- uv run pytest tests/test_config_flow.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/nest_protect/config_flow.py tests/test_config_flow.py
git commit -m "feat(config_flow): route reauth through auth method selection"
```

---

### Task 7: GitHub Actions — Extension Zip on Release

**Files:**
- Modify: `.github/workflows/release.yml`

- [ ] **Step 1: Add extension packaging job to release.yml**

Add a new job after the existing `bump-version` job:

```yaml
  package-extension:
    runs-on: ubuntu-latest

    permissions:
      contents: write

    steps:
      - uses: actions/checkout@v6

      - name: Create extension zip
        run: |
          cd chrome_extension
          zip -r ../nest-auth-helper.zip .

      - name: Upload to release
        uses: softprops/action-gh-release@v2
        with:
          files: nest-auth-helper.zip
```

- [ ] **Step 2: Verify YAML is valid**

Run: `devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect -- python -c "import yaml; yaml.safe_load(open('.github/workflows/release.yml'))"`
Expected: No error output.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/release.yml
git commit -m "ci: package chrome extension zip on release"
```

---

### Task 8: Run Full Test Suite & Final Verification

**Files:** None (verification only)

- [ ] **Step 1: Run entire test suite**

Run: `devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect -- uv run pytest tests/ -v`
Expected: All tests PASS, including existing tests in `test_init.py`, `test_session.py`, `pynest/test_client.py`, `pynest/test_models.py`.

- [ ] **Step 2: Run linting**

Run: `devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect -- uv run ruff check custom_components/ tests/`
Expected: No errors.

- [ ] **Step 3: Run formatting**

Run: `devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect -- uv run ruff format --check custom_components/ tests/`
Expected: All files formatted correctly.

- [ ] **Step 4: Verify extension files are complete**

Run: `find chrome_extension -type f | sort`
Expected:
```
chrome_extension/content_script.js
chrome_extension/icons/icon-128.png
chrome_extension/icons/icon-48.png
chrome_extension/manifest.json
chrome_extension/popup.html
chrome_extension/popup.js
chrome_extension/README.md
```

- [ ] **Step 5: Verify JSON validity of extension manifest**

Run: `devcontainer exec --workspace-folder /Users/mick/Projects/ha-nest-protect -- python -c "import json; json.load(open('chrome_extension/manifest.json'))"`
Expected: No error.
