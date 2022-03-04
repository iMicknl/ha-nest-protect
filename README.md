![Detail page of a Nest Protect device](https://user-images.githubusercontent.com/1424596/149627841-e5611c04-f0e7-4b66-9b10-a9ec0b5c37f8.png)

[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/custom-components/hacs)
[![GitHub release](https://img.shields.io/github/release/iMicknl/ha-nest-protect.svg)](https://GitHub.com/iMicknl/ha-nest-protect/releases/)
[![Open in Visual Studio Code](https://open.vscode.dev/badges/open-in-vscode.svg)](https://open.vscode.dev/iMicknl/ha-nest-protect/)

# Nest Protect integration for Home Assistant (work in progress)

Custom component for Home Assistant to interact with Nest Protect devices via the original Nest platform. Currently Google SDM doesn't support Nest Protect devices and thus the official [Nest integration](https://www.home-assistant.io/integrations/nest/) won't work for Nest Protect.

This integration will add the main sensors of your Nest Protect device (CO, heat and smoke) and the occupancy if your device is wired.

Since this integration is still a work in progress, there are some limitations and bugs. Please have a look at the known limitations and issues below, feel free to create an issue if you find another one or if you have a suggestion.

## Known limitations and issues

- Only Nest Protect devices are supported
- Only Google Accounts are supported
- After the occupancy is triggered, it will stay 'on' for 10 minutes. (device limitation)
- Config sensors are shown, but you cannot change the settings (yet)

## Installation

You can install this integration via [HACS](#hacs) or [manually](#manual).

### HACS

Add the repository url below to HACS, search for the Nest Protect integration and choose install.

> https://github.com/imicknl/ha-nest-protect

Reboot Home Assistant and configure the Nest Protect integration via the integrations page or press the blue button below.

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=nest_protect)


### Manual

Copy the `custom_components/nest_protect` to your custom_components folder. Reboot Home Assistant and configure the Nest Protect integration via the integrations page or press the blue button below.

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=nest_protect)



## Advanced

### Enable debug logging

The [logger](https://www.home-assistant.io/integrations/logger/) integration lets you define the level of logging activities in Home Assistant. Turning on debug mode will show more information about unsupported devices in your logbook.

```
logger:
  default: critical
  logs:
    custom_components.nest_protect: debug
```

## Credits

Based on the great work of [homebridge-nest](https://github.com/chrisjshull/homebridge-nest).