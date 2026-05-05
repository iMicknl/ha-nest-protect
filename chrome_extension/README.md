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

- **cookies** — to read Google authentication cookies (SID, HSID, SSID, APISID, SAPISID) as a fallback
- **webRequest** — to intercept the OAuth token request and capture cookies from request headers
- **tabs** — to open and navigate to home.nest.com for credential extraction
- **Host access** — scoped to `home.nest.com` and `accounts.google.com` only

The extension does not transmit any data externally. All values are displayed locally for you to copy.

## Keep Installed

Keep this extension installed — you may need it again if Home Assistant requests re-authentication.
