# Nest Auth Helper – Firefox Extension

This is a Firefox port of the [Nest Auth Helper Chrome extension](../chrome_extension/) for the [ha-nest-protect](https://github.com/iMicknl/ha-nest-protect) Home Assistant integration.

## Differences from the Chrome version

| Feature | Chrome (MV3) | Firefox (MV2) |
|---|---|---|
| Manifest version | 3 | 2 |
| Background | Service Worker (`service_worker`) | Persistent background script (`scripts`) |
| Toolbar action | `action` | `browser_action` |
| API namespace | `chrome.*` | `browser.*` (Promise-based) |
| `cookies` / `tabs` permissions | Declared separately in `host_permissions` | Declared directly in `permissions` |
| `extraHeaders` flag | Required for cookie access | Not needed (omitted) |
| Auto open popup on capture | `chrome.action.openPopup()` | Not supported — badge turns ✓, click toolbar icon manually |
| Browser ID | — | `browser_specific_settings.gecko.id` required |

## Installation (Temporary / Developer Mode)

1. Open Firefox and navigate to `about:debugging`
2. Click **This Firefox** in the left sidebar
3. Click **Load Temporary Add-on...**
4. Navigate to this `firefox_extension/` directory and select `manifest.json`

The extension will be active until Firefox is restarted. For a persistent install, the extension would need to be signed via [addons.mozilla.org](https://addons.mozilla.org).

## Usage

1. Click the **Nest Auth Helper** icon in the Firefox toolbar
2. Click **Extract Credentials**
3. A new tab opens to `home.nest.com` — sign in if prompted
4. When the toolbar badge shows **✓**, click the toolbar icon to open the popup
5. Copy the authentication code and paste it into the Home Assistant integration setup

## Packaging as a .xpi

To create an installable `.xpi` file:

```bash
cd firefox_extension
zip -r ../nest-auth-helper-firefox.xpi . -x "*.DS_Store"
```

The `.xpi` can then be installed in Firefox via `about:addons` → gear icon → **Install Add-on From File**.
