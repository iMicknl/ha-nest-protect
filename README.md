![Detail page of a Nest Protect device](https://github.com/iMicknl/ha-nest-protect/assets/1424596/8fd15c57-2a9c-4c20-8c8f-65a526573d1e)

[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/release/iMicknl/ha-nest-protect.svg)](https://GitHub.com/iMicknl/ha-nest-protect/releases/)
[![HA integration usage](https://img.shields.io/badge/dynamic/json?color=41BDF5&logo=home-assistant&label=integration%20usage&suffix=%20installs&cacheSeconds=15600&url=https://analytics.home-assistant.io/custom_integrations.json&query=$.nest_protect.total)](https://analytics.home-assistant.io/custom_integrations.json)

# Nest Protect integration for Home Assistant

Custom component for Home Assistant to interact with Nest Protect devices via an undocumented and unofficial Nest API. Unfortunately, Google SDM doesn't support Nest Protect devices and thus the core [Nest integration](https://www.home-assistant.io/integrations/nest/) won't work for Nest Protect.

This integration will add the most important sensors of your Nest Protect device (CO, heat and smoke) and the occupancy if your device is wired (to main power). In addition, it will expose several diagnostic and configuration entities. All sensor values will be updated real-time.

## Known limitations

- Only Google Accounts are supported, there is no plan to support legacy Nest accounts
- When Nest Protect (wired) occupancy is triggered, it will stay 'on' for 10 minutes. (API limitation)
- Only _cookie authentication_ is supported as Google removed the API key authentication method. This means that you need to login to the Nest website at least once to generate a cookie. This cookie will be used to authenticate with the Nest API. The cookie will be stored in the Home Assistant configuration folder and will be used for future requests. If you logout from your browser or change your password, you need to reautenticate and and replace the current issue_token and cookies.

## Installation

You can install this integration via [HACS](#hacs) or [manually](#manual).

### HACS

Search for the Nest Protect integration and choose install, then reboot Home Assistant. Configure the Nest Protect integration either via the integrations page or press the blue button below.


[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=nest_protect)

### Manual

Copy the `custom_components/nest_protect` to your custom_components folder and reboot Home Assistant. Configure the Nest Protect integration either via the integrations page or press the blue button below.


[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=nest_protect)

## Authentication

The values of "issue_token" and "cookies" are specific to your Google Account. You only need to retrieve them once, as long as you stay logged into your Google Account.

### Recommended: Chrome Extension

The easiest way to get your credentials is with the **Nest Auth Helper** Chrome extension:

1. Download `nest-auth-helper.zip` from the [latest release](https://github.com/iMicknl/ha-nest-protect/releases/latest/download/nest-auth-helper.zip) and unzip it.
1. In Chrome, go to `chrome://extensions`, enable **Developer mode**, and click **Load unpacked** to load the unzipped folder.
1. Sign in to [home.nest.com](https://home.nest.com) in a regular browser window.
1. Click the extension icon → **Extract Credentials** → **Copy Code**.
1. Paste the code into the Home Assistant configuration flow.

Keep the extension installed — you may need it again if Home Assistant requests re-authentication.

### Manual: Developer Tools

If you prefer not to use the extension, you can extract the credentials manually using Chrome/Edge DevTools.

> **Important:** Third-party cookies must be enabled for `home.nest.com`, otherwise the site will enter a redirect loop. Do **not** use an Incognito/Private window, as these block third-party cookies by default.
>
> - **Chrome**: Settings → Privacy and Security → Third-party cookies → Allow third-party cookies (or add `home.nest.com` to "Sites that can always use cookies").
> - **Edge**: Settings → Cookies and site permissions → Manage and delete cookies and site data → Disable "Block third-party cookies."

1. Open a Chrome/Edge browser tab (regular window, not incognito).
1. Open Developer Tools (F12 or View → Developer → Developer Tools).
1. Click on the **Network** tab. Make sure **Preserve Log** is checked.
1. In the **Filter** box, enter `issueToken`.
1. Go to [home.nest.com](https://home.nest.com), and click **Sign in with Google**. Log into your account.
1. One network call (beginning with `iframerpc`) will appear in the Dev Tools window. Click on it.
1. In the Headers tab, under General, copy the entire **Request URL** (beginning with `https://accounts.google.com`). This is your `issue_token`.
1. In the **Filter** box, enter `oauth2/iframe`.
1. Several network calls will appear in the Dev Tools window. Click on the last iframe call.
1. In the **Headers** tab, under **Request Headers**, copy the entire **cookie** value (the whole string with many field/value pairs — do not include the `cookie:` name). This is your `cookies`.
1. Do not log out of home.nest.com, as this will invalidate your credentials. Just close the browser tab.

(adapted from [homebridge-nest documentation](https://github.com/chrisjshull/homebridge-nest))

## Advanced

Feel free to [create an issue on GitHub](https://github.com/iMicknl/ha-nest-protect/issues/new/choose) if you find an issue or if you have a suggestion. It is always helpful to download the diagnostics information and to include debug logging.

### Enable debug logging

The [logger](https://www.home-assistant.io/integrations/logger/) integration lets you define the level of logging activities in Home Assistant. Turning on debug mode will show more information about unsupported devices in your logbook.

```
logger:
  default: critical
  logs:
    custom_components.nest_protect: debug
```

## Credits

Based on the research and implementation of [homebridge-nest](https://github.com/chrisjshull/homebridge-nest).
