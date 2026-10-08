# Changelog

## 0.1.0

First release, split out of the
[Wi-Fi Association Presence](https://github.com/darek-margas/wifi-association-presence)
integration (its `ap_drivers` package, as in integration 0.5.2). The code is unchanged;
only the package name differs (`wifi_ap_associations` instead of `ap_drivers`).

- Drivers: D-Link DAP (SSH console), OpenWrt (SSH, ubus), UniFi (through a UniFi Network
  controller connection passed in).
- `PollResult` with the associated clients (MAC, SSID, band, signal, connected time) and
  the access point's own details (name, location, model, firmware, hardware, uptime,
  CPU, memory).
