![Detail page of a Nest Protect device](https://user-images.githubusercontent.com/1424596/158192051-c4a49665-2675-4299-9abf-c0848623445a.png)

[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/release/iMicknl/ha-nest-protect.svg)](https://GitHub.com/iMicknl/ha-nest-protect/releases/)
[![HA integration usage](https://img.shields.io/badge/dynamic/json?color=41BDF5&logo=home-assistant&label=integration%20usage&suffix=%20installs&cacheSeconds=15600&url=https://analytics.home-assistant.io/custom_integrations.json&query=$.nest_protect.total)](https://analytics.home-assistant.io/custom_integrations.json)

# Nest Protect integration for Home Assistant

Custom component for Home Assistant to interact with Nest Protect devices via an undocumented and unofficial Nest API. Unfortunately, Google SDM doesn't support Nest Protect devices and thus the core [Nest integration](https://www.home-assistant.io/integrations/nest/) won't work for Nest Protect.

This integration will add the most important sensors of your Nest Protect device (CO, heat and smoke) and the occupancy if your device is wired (to main power). In addition, it will expose several diagnostic and configuration entities. All sensor values will be updated real-time.

## Known limitations

- Only Google Accounts are supported, there is no plan to support legacy Nest accounts
- When Nest Protect (wired) occupancy is triggered, it will stay 'on' for 10 minutes. (API limitation)
- Only *cookie authentication* is supported as Google removed the API key authentication method. This means that you need to login to the Nest website at least once to generate a cookie. This cookie will be used to authenticate with the Nest API. The cookie will be stored in the Home Assistant configuration folder and will be used for future requests.

## Installation

You can install this integration via [HACS](#hacs) or [manually](#manual).

### HACS

Search for the Nest Protect integration and choose install. Reboot Home Assistant and configure the Nest Protect integration via the integrations page or press the blue button below.

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=nest_protect)


### Manual

Copy the `custom_components/nest_protect` to your custom_components folder. Reboot Home Assistant and configure the Nest Protect integration via the integrations page or press the blue button below.

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=nest_protect)


## Retrieving `issue_token` and `cookies`

(adapted from [homebridge-nest documentation](https://github.com/chrisjshull/homebridge-nest))

The values of "issue_token" and "cookies" are specific to your Google Account. To get them, follow these steps (only needs to be done once, as long as you stay logged into your Google Account).

1. Open a Chrome browser tab in Incognito Mode (or clear your cache).
2. Open Developer Tools (View/Developer/Developer Tools).
3. Click on **Network** tab. Make sure 'Preserve Log' is checked.
4. In the **Filter** box, enter *issueToken*
5. Go to home.nest.com, and click **Sign in with Google**. Log into your account.
6. One network call (beginning with iframerpc) will appear in the Dev Tools window. Click on it.
7. In the Headers tab, under General, copy the entire Request URL (beginning with https://accounts.google.com). This is your `issue_token` in the configuration form.
8. In the **Filter** box, enter *oauth2/iframe*.
9. Several network calls will appear in the Dev Tools window. Click on the last iframe call.
10. In the **Headers** tab, under **Request Headers**, copy the entire cookie (include the whole string which is several lines long and has many field/value pairs - do not include the cookie: name). This is your `cookies` in the configuration form.
11. Do not log out of home.nest.com, as this will invalidate your credentials. Just close the browser tab.

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
