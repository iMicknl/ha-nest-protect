# Nest Auth Helper — Chrome Extension

A Chrome extension that extracts the authentication credentials needed to configure the **ha-nest-protect** Home Assistant integration. It produces a single code string you paste into the HA setup flow.

## Installation

1. Download [`nest-auth-helper.zip`](https://github.com/iMicknl/ha-nest-protect/releases/download/v0.4.4/nest-auth-helper.zip)
2. Unzip the file
3. Open Chrome and navigate to `chrome://extensions`
4. Enable **Developer mode** (toggle in the top-right corner)
5. Click **Load unpacked** and select the unzipped folder
6. The extension icon will appear in your toolbar

### Firefox

Firefox 121 or later is required.

1. Download and unzip [`nest-auth-helper.zip`](https://github.com/iMicknl/ha-nest-protect/releases/download/v0.4.4/nest-auth-helper.zip)
2. Open Firefox and navigate to `about:debugging`
3. Click **This Firefox**, then **Load Temporary Add-on**
4. Select `manifest.json` in the unzipped folder

Temporary add-ons are removed when Firefox restarts. A permanent install requires a build signed by Mozilla.

Firefox may not grant host access automatically. If extraction does not work, open the **Extensions** menu (puzzle icon) in the toolbar, or `about:addons` → **Nest Auth Helper** → **Permissions**, and allow access to `home.nest.com` and `accounts.google.com`.

## Usage

1. Sign in to [home.nest.com](https://home.nest.com) in Chrome
2. Click the extension icon in your toolbar
3. Click **Extract Credentials**
4. Click **Copy Code**
5. Paste the code into the Home Assistant config flow

## Permissions

This extension requests the minimum permissions needed:

- **cookies** — to read Google authentication cookies (e.g. SID, HSID, SSID, APISID, SAPISID) as a fallback when they are missing from the request headers
- **webRequest** — to intercept the OAuth token request and capture cookies from request headers
- **tabs** — to open and navigate to home.nest.com for credential extraction
- **Host access** — scoped to `home.nest.com` and `accounts.google.com` only

The extension does not transmit any data externally. All values are displayed locally for you to copy.

## Keep Installed

Keep this extension installed — you may need it again if Home Assistant requests re-authentication.
