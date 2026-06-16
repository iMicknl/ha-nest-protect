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
- Google removed API key authentication, so you must sign in with Google at least once during setup. The integration supports three methods (see below): **App token (recommended)**, the Chrome extension, and manual cookie paste.
- The **App token** method mints a long-lived credential (an OAuth refresh token, the same kind the Nest mobile app holds). Home Assistant refreshes it automatically and it only stops working if you **change your Google password** or revoke access — no browser needs to stay running.
- The **cookie / extension** methods rely on **Google session cookies that expire on a server-side schedule** (typically every few hours). When Google returns `USER_LOGGED_OUT`, you must re-authenticate. Prefer the App token method to avoid this. See [docs/authentication.md](docs/authentication.md) for the full auth landscape, OAuth details, and realistic expectations.

## Installation

You can install this integration via [HACS](#hacs) or [manually](#manual).

### HACS

Search for the Nest Protect integration and choose install, then reboot Home Assistant. Configure the Nest Protect integration either via the integrations page or press the blue button below.


[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=nest_protect)

### Manual

Copy the `custom_components/nest_protect` to your custom_components folder and reboot Home Assistant. Configure the Nest Protect integration either via the integrations page or press the blue button below.


[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=nest_protect)

## Authentication

During setup you choose an authentication method:

1. **App token (recommended)** — creates a long-lived login that does not expire after a few hours. Select "App token" in the config flow, open the provided "Sign in with Google" link in any browser, approve access, then copy the address your browser tries (and fails) to open — it starts with `com.googleusercontent.apps...` — and paste it back into Home Assistant. This only needs re-doing if you change your Google password.
2. **Chrome Extension** — captures `issue_token` + cookies from a live Google session. Cookies expire every few hours (see limitations).
3. **Manual** — paste `issue_token` and `cookies` yourself (see below). Cookies expire every few hours.

## Retrieving `issue_token` and `cookies` (manual / extension methods)

(adapted from [homebridge-nest documentation](https://github.com/chrisjshull/homebridge-nest))

The values of "issue_token" and "cookies" are specific to your Google Account. To get them, follow these steps (only needs to be done once, as long as you stay logged into your Google Account).

1. Open a Chrome/Edge browser tab in Incognito Mode.
1. Allow third-party cookies in your browser settings to prevent the Nest website from entering a redirect loop. Follow these steps:

   - **In Chrome**: Go to Settings, select Privacy and Security -> Third-party cookies. Enable "Allow third-party cookies."
   - **In Edge**: Go to Settings, select Cookies and site permissions -> Manage and delete cookies and site data. Disable "Block third-party cookies."

1. Open Developer Tools (View/Developer/Developer Tools).
1. Click on **Network** tab. Make sure 'Preserve Log' is checked.
1. In the **Filter** box, enter `issueToken`
1. Go to home.nest.com, and click **Sign in with Google**. Log into your account.
1. One network call (beginning with iframerpc) will appear in the Dev Tools window. Click on it.
1. In the Headers tab, under General, copy the entire Request URL (beginning with https://accounts.google.com). This is your _'issue_token'_ in the configuration form.
1. In the **Filter** box, enter `oauth2/iframe`
1. Several network calls will appear in the Dev Tools window. Click on the last iframe call.
1. In the **Headers** tab, under **Request Headers**, copy the entire cookie (include the whole string which is several lines long and has many field/value pairs - do not include the cookie: name). This is your _'cookies'_ in the configuration form.
1. Do not log out of home.nest.com, as this will invalidate your credentials. Just close the browser tab.

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
