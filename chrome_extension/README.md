# Nest Protect Auth Helper — Chrome Extension

A Chrome extension that extracts the authentication cookies needed to configure the **ha-nest-protect** Home Assistant integration.

## Installation

Chrome does not allow installing extensions outside the Chrome Web Store without Developer Mode. Follow these steps:

1. Download this repository (or just the `chrome_extension/` folder)
2. Open Chrome and navigate to `chrome://extensions`
3. Enable **Developer mode** (toggle in the top-right corner)
4. Click **Load unpacked** and select the `chrome_extension/` folder
5. The extension icon will appear in your toolbar

## Usage

1. Sign in to [home.nest.com](https://home.nest.com) in Chrome
2. Click the extension icon in your toolbar
3. Click **Extract Cookies**
4. Copy the `issue_token` and `cookies` values into your Home Assistant configuration

## Permissions

This extension requests the minimum permissions needed:

- **cookies** — to read Google authentication cookies (`SID`, `HSID`, `SSID`, `APISID`, `SAPISID`)
- **Host access to `home.nest.com` and `accounts.google.com`** — scoped to only these domains

The extension does not transmit any data externally. All values are displayed locally for you to copy.

## Uninstall

After configuring Home Assistant, you can remove the extension:

1. Go to `chrome://extensions`
2. Find "Nest Protect Auth Helper" and click **Remove**
