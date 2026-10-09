# Changelog

## 0.4.4

- Collector reports end with a timing line: `# finished: 11 commands in 0.8 s` (SSH) or
  `# finished in 12.3 s` (SNMP). Over SSH exec a whole command list can take under a
  second, which looked as if nothing ran.
- `mikrotik` command list: also `/interface wifi print terse` and `/interface wireless
  print terse`, the Wi-Fi interfaces with SSID and band (the client tables only name
  the interface).
- SNMP walk: also the HOST-RESOURCES processor load and storage tables, where most
  devices (MikroTik included) report CPU and memory.

## 0.4.3

- Collector: new **`mikrotik`** command list for RouterOS (access points, routers, CAPsMAN
  controllers): identity, resources, routerboard, packages, interfaces, and the client
  table of each Wi-Fi package (`wireless`, `wifi`, `wifiwave2`, `caps-man`), all with
  `print terse` where it helps parsing.
- With this list each command runs as its own SSH exec request instead of being typed
  into an interactive terminal. RouterOS's terminal sends escape queries, wraps lines at
  80 columns and echoes commands, which made reports hard to read.

## 0.4.2

- Collector: extra commands may also be the usual read-only client-list commands of
  Linux, Broadcom, Atheros and MikroTik based access points (`iw dev <if> station dump`,
  `iwinfo <if> assoclist`, `wlanconfig <if> list`, `wl assoclist`, `/... print`), and
  D-Link's `config wlan <n>`. 0.4.1 refused them, though they are what a contributor
  typically adds. MikroTik lines that would change something (`set`, `add`, `remove`,
  `reset-configuration`, ...) are still refused.
- Removed the unused list of refused command words left over from before 0.4.1.
- A test checks the installed pysnmp engine can be closed (0.4.1 tested it with a fake).

## 0.4.1

- OpenWrt: failed or malformed client-table reads now raise an AP error instead of reporting an empty client list. Missing ubus is detected even with section markers.
- Collector: validate custom SSH commands as display commands; refuse shell chaining, redirection, expansion and unknown command families. Existing built-in profiles are retained.
- SNMP collector: close the engine dispatcher after success, failure or cancellation.

## 0.4.0

- **SNMP comes with the library**: pysnmp (7.1 or newer, below 8) is now a dependency, so
  `wifi-ap-collect --snmp` works without installing anything else.
- **`collect.async_collect_snmp_report()`**: the SNMP collector as a function (v2c with a
  community, v3 with user, keys and algorithms), with the same walk and redaction as
  `wifi-ap-collect --snmp`, and a limit per walked subtree (default 5000 values).
  Rejected v3 keys raise `AccessPointAuthError`, anything else `AccessPointError`.
- A test checks that every SNMPv3 algorithm the collector offers exists in the installed
  pysnmp.

## 0.3.0

- **`collect.async_collect_ssh_report()`**: the SSH collector as a function, for programs
  such as the Home Assistant integration (which now offers it in its UI). It runs the
  same commands with the same read-only filter and redaction as `wifi-ap-collect` and
  returns the report with the review warning on top. A rejected login raises
  `AccessPointAuthError`, any other failure `AccessPointError` (its message suggests
  legacy SSH when the algorithms don't match), an unknown profile `ValueError`.
- The `wifi-ap-collect` command and the drivers are unchanged.

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

