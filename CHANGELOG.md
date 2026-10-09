# Changelog

## 0.2.0

The two command-line tools moved here from the integration's `scripts/` folder, unchanged
apart from their usage text. They install with the library:

- **`wifi-ap-collect`** (was `scripts/collect.py`): collects read-only, redacted data
  from an access point that has no driver yet, over SSH or SNMP, for a "New access point
  model" report. SNMP mode also needs `pip install pysnmp`.
- **`wifi-ap-probe`** (was `scripts/probe.py`): reads one access point with a driver and
  prints its associated clients.

Both also run as `python -m wifi_ap_associations.collect` / `.probe`. New tests cover the
collector's safety rules: commands that could change settings are refused, and MACs,
IPv4 addresses, typed secrets and secret-looking settings are redacted.

The drivers are unchanged.

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
