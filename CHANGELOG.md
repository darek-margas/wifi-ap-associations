# Changelog

## 0.9.0

- **New driver `mikrotik_capsman_ssh` (experimental): MikroTik CAPsMAN controllers**
  (RouterOS 7, `wifi` package), with **one access point per CAP**. On CAPsMAN the CAPs
  don't list their clients, the controller does, naming only the interface each client
  is on. The driver reads three tables from the controller over SSH, once per poll for
  all its CAPs: the remote CAPs (`/interface wifi capsman remote-cap`), the interfaces
  (`/interface wifi`, where a CAP radio's interface carries `cap=<identity>@...` and a
  virtual interface leads to it through `master-interface=`) and the registration
  table. Each client is put on its CAP, with SSID, band, signal and connected time; each
  CAP reports its board name, RouterOS version and uptime. A CAP the controller doesn't
  list, or that isn't in state Ok, is unreadable rather than empty. Settings: the
  controller's address and login plus the CAP's identity (`cap`), the unique setting
  together with the host.
- Built and tested from a CAPsMAN router with two cAP ax on RouterOS 7.24.4
  ([wifi-association-presence#11](https://github.com/darek-margas/wifi-association-presence/issues/11)).
  The legacy CAPsMAN of the `wireless` package (`/caps-man`) isn't covered yet.
- `mikrotik_ssh.run_commands()`: the SSH exec runner, shared by both MikroTik drivers.
- Controller APs (`cisco_wlc.JoinedAp`) can carry firmware and uptime.
- `wifi-ap-probe --cap`.

## 0.8.5

- **MikroTik: clients of the `wireless` package were missed on recent RouterOS**
  ([wifi-association-presence#12](https://github.com/darek-margas/wifi-association-presence/issues/12)).
  RouterOS 7 now ships the `wifi` package built in, so `/interface wifi` answers (with
  nothing) even on a device whose radios all run the `wireless` package, such as a cAP
  lite on 7.24.4. The driver used the first package that answered and so reported no
  clients at all. It now reads both packages and merges their clients; an unused
  package's empty table adds nothing, and a client listed by both counts once.
- MikroTik: a client on a **virtual interface** (an extra SSID on a radio,
  `master-interface=`) gets its band from that radio; before, it had none.

## 0.8.4

- **Collector, `mikrotik` command list: CAPsMAN details.** On a CAPsMAN controller the
  registration table lists the clients of every CAP, and names only the interface each
  client is on (e.g. `ap_upstairs-virtual8`). To learn which CAP an interface belongs to,
  reports now also include `/interface wifi radio print terse` and
  `/interface wifi capsman remote-cap print terse` (the `wifi` package, RouterOS 7) and
  `/caps-man interface print terse` and `/caps-man remote-cap print terse` (the legacy
  `caps-man`, RouterOS 6). Devices without CAPsMAN answer "bad command name", which the
  report shows and nothing else minds. This is the groundwork for one access point per
  CAP, asked for in
  [wifi-association-presence#11](https://github.com/darek-margas/wifi-association-presence/issues/11).
- The registration-table format of that issue (flags, `interface`, `ssid`,
  `mac-address`, `uptime`, `signal`, `band`, an interface name with a space) is what
  `mikrotik_ssh` already parses: the first real MikroTik output seen.

## 0.8.3

Follow-up from a review of the Cisco SNMP signal readings.

- **A missing signal or connected time is no longer carried forward for good.** In
  0.8.2, a value missing from a read was copied from the previous read, even if that
  value had been copied too, so a client whose row stopped appearing in the signal
  table could keep an old signal for as long as it stayed on the AP. Now every measured
  value is stored with when it was measured, and a missing one falls back only to a
  measurement under 150 s old (about two polls) on the same AP; after that it is
  unknown. The connected time is carried forward from that measurement.
- A new measurement always replaces the old one, so after a reconnect the connected time
  shows the new association as soon as the controller reports it.

## 0.8.2

- **`cisco_wlc_snmp` reads signal and connected time** for every client on every read:
  RSSI from the Airespace client statistics (`bsnMobileStationStatsTable`, column 1,
  dBm) and the seconds since the client associated from Cisco's client table
  (`cldcClientTable`, column 15). Two reports from a C9800-CL 13 min 14 s apart
  confirmed them: every client's connected time had grown by 795 s. Which clients exist
  is still decided by the Airespace client table alone; rows found only in the other
  two tables are ignored. A missing reading falls back to the previous one only if the
  client is on the same AP.
- **`cisco_wlc_ssh` is no longer experimental:** confirmed in Home Assistant on that
  controller, with signal. Its description lists the five commands a restricted parser
  view must allow (privilege 1 covers the summaries but not the client detail).
- `cisco_wlc_snmp` stays experimental until it has run with signal.

## 0.8.1

Follow-ups from a review of the Cisco SSH signal readings.

- **Due refreshes can't be starved by new clients.** While readings are due, up to 5 of
  a read's 15 client details go to the oldest of them first. Before, a steady stream of
  new or roaming clients could take every slot until established clients' readings
  expired, and the signal sensors would dip for that reason alone.
- **The 10 s detail budget is a hard limit:** each detail waits at most for the time
  left, not a full 5 s past the budget.
- Documented: a client that disconnects and reconnects between two polls keeps showing
  its earlier, longer connected time until its next reading. (One missing from a read
  already loses its reading.)

## 0.8.0

- **`cisco_wlc_ssh` reads signal and connected time.** They are only in each client's
  own detail (`show wireless client mac-address <mac> detail`), so each read asks for a
  few clients, in turns: up to 15 per read and 10 s, clients without a reading first,
  then clients that moved to another AP, then the oldest readings. A reading under 2
  minutes old isn't asked again, so once everyone has one only the oldest few are read
  each poll; with 23 clients, everyone is refreshed about every 2 minutes.
- A reading counts only while the client stays on the AP it was read on and for 10
  minutes; its connected time is carried forward. A detail that fails or times out is
  skipped and presence still comes from the summary. If the controller refuses the
  command (e.g. a user without the privilege), details are paused for an hour.
- `AssociatedClient.connected_seconds` for both Cisco drivers (SNMP: none yet).
- Built and tested on the C9800-CL detail output from
  [wifi-association-presence#10](https://github.com/darek-margas/wifi-association-presence/issues/10).

## 0.7.2

Follow-ups from the second review of the Cisco drivers.

- **A shared controller read is stopped after 45 s** (a poll's limit). Before, a read
  that never finished could be joined by every following poll, so the APs of that
  controller would stay unavailable without a fresh attempt. Now it fails within one
  cycle and the next poll reads again.
- **The previous read lives with the shared reads and expires with them** (after 180 s,
  three polls). Reads under settings since changed (the key holds the credentials) no
  longer stay in memory for good.
- Documented trade-off: a client caught mid-roam by an SNMP read may show its previous AP
  for one more read, rather than briefly look gone.
- Tests: a read that never answers, an AP poll timing out while another joins the same
  read, and old reads being dropped.

## 0.7.1

Fixes from a review of the Cisco drivers.

- **`cisco_wlc_snmp`: the eight columns are walked at the same time.** A read takes
  about as long as its slowest column, instead of all eight one after another, which on
  a slow controller could come close to the 45 s poll limit. All walks finish before
  the SNMP engine closes, so a failed read leaves no late answers behind.
- **A client caught half-way by a read keeps the previous read's details.** A client
  missing from some columns (it roamed or re-associated while they were read) keeps its
  AP and state for one more read instead of looking gone; one in no column is gone.
- **SNMP version setting** (`snmp_version`: v2c or v3) decides which credentials are
  used, so switching version needs no clearing of the other version's fields. Settings
  without it work as before (v3 if a user is set).
- **AP identity:** `identity()` and `IDENTITY_FIELDS`. Both Cisco drivers are identified
  by the controller's host plus the AP's name, so two controllers may each have an AP01.
- **A failed controller read is shared only with the polls already waiting for it**; the
  next poll reads again instead of reusing the failure for 20 s.
- `wifi-ap-probe --snmp-version`.
- Tests run the real pysnmp against a local SNMP agent serving the 9800's columns (v2c,
  v3 SHA-256/AES, wrong keys).

## 0.7.0

- **New driver `cisco_wlc_snmp` (experimental): Cisco lightweight APs joined to a
  Catalyst 9800 controller, read over SNMP** (v2c with a community, or v3 with a user,
  keys and algorithms), for controllers without SSH access. One access point per Cisco
  AP, by its name, as with `cisco_wlc_ssh`; APs of one controller polled together share
  one read. Each read walks eight columns of the Airespace client and AP tables: the
  client's AP, SSID, policy state (only Run counts) and protocol (the band), and the
  AP's name, location, status and model. Built and tested from a C9800-CL SNMPv3 report;
  no signal yet (it is in another table, added once a report confirms its columns).
- The Cisco controller model is shared by both Cisco drivers (`cisco_wlc`), so they give
  the same results; `poll_result` and `band_for_protocol` stay importable from
  `cisco_wlc_ssh`.
- `DriverField` gains `optional` (may be left empty) and `choices` (the only accepted
  values); drivers gain `BLOCKING_IMPORTS`, modules to import off the event loop before
  the first poll (pysnmp).
- `wifi-ap-probe` reads SNMP settings: `--auth-protocol`, `--priv-protocol`, the
  community from `$AP_COMMUNITY` and the v3 privacy key from `$AP_PRIV_KEY`, and only asks
  for the secrets the chosen SNMP version needs.

## 0.6.1

- **SNMP host types** for the collector: `--profile c9800` (Cisco Catalyst 9800) or
  `--profile cisco_wlc` (any Cisco wireless controller) with `--snmp` walks only that
  controller's tables instead of the whole vendor subtree, each up to 50000 values, so
  client tables aren't cut off by the limit. `c9800` is the narrow set: client table,
  client statistics (signal), AP table and Cisco's client MIB; `cisco_wlc` adds Cisco's AP
  and WLAN MIBs and may grow as other controllers report in.
  `async_collect_snmp_report(profile=...)` for programs; `--list-profiles` lists them.
- SNMP collector on Cisco controllers: also walks the Airespace client statistics table
  (`bsnMobileStationStatsTable`, `1.3.6.1.4.1.14179.2.1.6`, where per-client RSSI and SNR
  are) and the whole CISCO-LWAPP-DOT11-CLIENT-MIB (`1.3.6.1.4.1.9.9.599.1`) instead of
  only its client table. A C9800-CL report had no signal at all: these tables weren't
  walked, and the Cisco subtree reached its value limit on the flash file table first.

## 0.6.0

- **New driver `cisco_wlc_ssh` (experimental): Cisco lightweight APs joined to a Catalyst
  9800 controller** (IOS-XE), over the controller's SSH console. One access point per
  Cisco AP, set up with the controller's login and the AP's name (`ap_name`, the unique
  setting, since many APs share a controller). APs of one controller polled together
  share a single login. Clients come from `show wireless client summary` (only the Run
  state counts), SSIDs from `show wlan summary`, the band from the protocol column, and
  the AP's model and location from `show ap summary`. No signal or connected time yet.
  An AP the controller doesn't list or that isn't Registered, a refused command and a
  client table shorter than announced are errors, not departures.
- Built from a C9800-CL report (IOS-XE 17.15.5, C9120AXI) in
  [wifi-association-presence#10](https://github.com/darek-margas/wifi-association-presence/issues/10);
  experimental until confirmed on that controller.
- `wifi-ap-probe --ap-name` for drivers that read one AP from a controller.

## 0.5.1

- MikroTik (`wifi` package): a client that is still authenticating (no `A` flag in the
  registration table) isn't counted as associated, as OpenWrt's driver does. In the old
  `wireless` package `A` means "the peer is an AP", so nothing is filtered there.
- The client-table column names were checked against MikroTik's WiFi manual
  (`interface`, `mac-address`, `ssid`, `uptime`, `signal`, `band`; on the interface
  `configuration.ssid`, `channel.band`, including 6 GHz bands such as `6ghz-be`). Still
  experimental until confirmed on a real access point.

## 0.5.0

- **New driver `mikrotik_ssh` (experimental): MikroTik RouterOS** access points, routers
  with Wi-Fi and CAPsMAN controllers, over SSH. One connection per poll, each command as
  its own exec request (no terminal). Reads name, model, firmware, uptime, CPU and memory
  from `/system identity` and `/system resource` (confirmed on RouterOS 7.20), and the
  clients from the `wifi` package's registration table (RouterOS 7) or the `wireless`
  one (RouterOS 6), with SSID and band from the interface list.
- **Experimental** because the registration-table columns come from MikroTik's
  documentation, not yet from a real access point. A report from one (Collect access
  point report → MikroTik RouterOS) will confirm or correct it.

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

