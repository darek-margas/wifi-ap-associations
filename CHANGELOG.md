# Changelog

## 0.14.0

**Breaking change:** `unifi_network` and `omada_network` need the controller's login in
their settings, and no longer take a data source as the constructor's second argument;
`UnifiSource`, `OmadaSource` and `AccessPointDriver.SOURCE` are removed.

- **UniFi and TP-Link Omada log in to their controller themselves.** `unifi_network` and
  `omada_network` no longer take a data source from the program (Home Assistant's UniFi
  and Omada integrations, whose logged-in sessions a custom integration may not borrow);
  their settings are now the controller's `host`, `port`, `username`, `password`, `site`
  and `verify_ssl`, plus the AP's `ap_mac` as before. The types keep their names, so
  saved settings only need the login added; without it a poll raises
  `AccessPointAuthError` saying so. `UnifiSource`, `OmadaSource` and `SOURCE` are gone.
- **New module `http_controller`:** one HTTPS session (aiohttp, a new dependency) per
  controller and login, shared by its access points, kept between polls and logged in
  again when the controller drops it; one read per 20 seconds serves them all.
- **UniFi:** tells a UniFi OS console (`/api/auth/login`, `/proxy/network`, CSRF token)
  from a self-hosted Network application (`/api/login`), the latter also while it is
  still starting; finds the site by its short name or description; keeps one event
  websocket per controller for all its access points' watches. Checked against a
  self-hosted Network application 10.0.162.
- **Omada:** the controller's v2 API (`/api/info`, login token, sites, devices, active
  clients in pages); an account with every site (the owner) finds them in the site list,
  as a 5.15 software controller answers. Checked against a software controller
  5.15.24; still experimental (no access points yet).
- **`AccessPointDriver.async_close()`** releases what a driver keeps open (programs call
  it when they stop using one); **`LISTS_ACCESS_POINTS` / `async_list_access_points()`**
  list a controller's access points to pick from.

## 0.13.1

- **New driver `tplink_eap_snmp`, experimental:** TP-Link EAP access points running
  standalone, without an Omada controller, over SNMP v2c or v3. Their command line has no
  way to list clients, but TP-Link's own MIB does: a client count
  (`1.3.6.1.4.1.11863.10.1.1.1.0`) and a table of the clients' MACs as text
  (`...10.1.1.2.1.2`). Both matched the clients connected in two reports of an EAP670
  (firmware 1.3.13), 9 and then 11. Presence and which AP only: the table has no SSID,
  band, signal or connected time. Name and model from the system group. A device
  without the table is an error, not an AP that always looks empty, and so is a
  positive count with no readable MAC (an empty AP would send every client away).
  Checked with the real pysnmp against a simulated EAP670 (v2c), also one counting 11
  clients with no MAC rows.
- **`AccessPointDriver.CLIENT_DETAILS`:** which of a client's band and signal a driver
  fills in (both by default; none for `tplink_eap_snmp`), so a program can leave out
  per-band counts and signal sensors that would always read zero.
- The Cisco SNMP driver's credentials (`SNMP_FIELDS`, `SnmpCredentials`) and column walk
  are shared with it; the Cisco driver is unchanged.

## 0.13.0

- **Instant arrivals for D-Link DAP, through SNMP traps.** `dlink_dap_ssh` has
  `WATCHES = True`: a DAP with its trap server set sends an SNMPv1 trap the moment a
  client joins (specific trap 14) or leaves (20), enterprise
  `1.3.6.1.4.1.171.10.37.<model>.5.7.2`, the AP's own MAC as the only variable (seen on a
  DAP-2610, checked against its log to the second). A join calls `on_change()` 3 s
  later, after the client's key handshake. Its syslog was tried too: one line every
  ~21 s from a queue and lines dropped, so no use.
- **New module `snmp_trap`:** one shared UDP listener (port 162 by default,
  `LISTENER.configure(port=...)`) decoding SNMPv1 and v2c traps; subscribers get the
  traps of their own address only. Every trap received is logged at debug in full
  (sender, version, community, enterprise, trap numbers, variables), from any sender, to
  read a new model's traps from the log.
- **Leaves:** `async_watch(on_change, on_leave=None)`. `on_leave()` means a client left
  the access point (not which one): OpenWrt `deauth` / `disassoc`, UniFi disconnects and
  roams away, D-Link trap 20; MikroTik reports none. `watch_forever(..., on_leave=...)`
  passes it on (to drivers that take it).
- **Fast departure (opt-in):** `PresenceTracker(..., fast_departure=...)` and
  `note_leave(access_point_id, at)`. A device that was on an access point which then
  reported a leave, and which the next successful poll of it didn't see, is away
  `fast_departure` after the report (never under 10 s) instead of after the grace period.
  `away_since(mac, now)` gives when a device counted as away. The evidence (a round that
  read the access point after a leave and didn't see the device) is taken with that
  round's own access point states and kept until the device is seen again: the access
  point failing or recovering later, or another client's leave, changes nothing; it is
  saved with the sightings. A device back before the deadline never left; back after it,
  it starts a new visit. `update(..., started=...)` takes when the round began, so a
  leave reported while a slow round was still running isn't taken for older than the
  read that saw the device. Each access point keeps all recent leaves, the earliest
  since the device was last seen counting. `pending_departures(now)` lists the deadlines still
  ahead, for a program to book its polls at after `restore()`; saved deadlines are
  ignored when restoring with fast departure off.
- D-Link declares the trap decoder's modules in `BLOCKING_IMPORTS`, so programs load them
  before their event loop runs rather than on the first trap.

## 0.12.3

From a TP-Link EAP670's reports (standalone, without a controller;
[discussion #4](https://github.com/darek-margas/wifi-association-presence/discussions/4)):

- **Collector, SNMP: MACs written as text are MACs.** TP-Link keeps a table of MACs as
  text (`AA-BB-CC-DD-EE-FF`); the report took them for ordinary text and, outside the
  recognised client tables, showed only their length. They are now recognised: the table
  is listed as a likely client table and its MACs shown redacted with the vendor prefix
  kept (enough to tell clients from the access point's own radios).
- **Collector, SSH: "Connection reset by peer" suggests "Allow old SSH algorithms"**, as
  an algorithm mismatch does: the EAP670 drops the connection instead of saying no
  algorithm matched.

## 0.12.2

From a review of 0.12.1, all in the OpenWrt driver:

- **A poll that finds no hostapd interface is a failure, not an empty AP.** During a
  Wi-Fi reload hostapd's ubus objects are gone for a few seconds, and the poll returned
  no clients, as if everyone had left. It now raises `AccessPointError` ("No Wi-Fi
  interface on ubus"), so the AP is unreadable for that poll and its clients keep their
  sightings. Checked live on OpenWrt 25.12.5 and 22.03.7 with the AP radio down. A
  radio with nobody on it is still an empty answer.
- **Only stations hostapd reports as associated and authorized (`true`) count.** A
  missing or null flag counted before; hostapd always sends both as true/false.
- **Instant arrivals on hostapd without `sta-authorized` (22.03):** the delayed poll
  after `assoc` is skipped when the client left again within the delay, and pending
  ones are cancelled when the watch ends or reconnects.

## 0.12.1

- **Collector: "legacy SSH" no longer drops modern algorithms.** It replaced the default
  algorithm list with old ones only, without any ECDSA host key, so an access point that
  offers only ECDSA (a TP-Link EAP670: `ecdsa-sha2-nistp521`) could not be reached with
  it on. It now adds the old algorithms to the modern ones. Test: a real SSH server with
  only an ECDSA P-521 host key, collected from with the option off and on.

## 0.12.0

- **The presence rules move into the library**, from the Home Assistant integration, so
  other programs (an MQTT bridge first) decide home and away the same way:
  - `wifi_ap_associations.presence`: merging a poll round into sightings (stronger
    signal wins), grace period, visits, retention, storage. Moved unchanged, except
    that `AccessPointRead` names its first field `access_point_id` (was `subentry_id`).
  - `wifi_ap_associations.coverage`: roams, short drops and late roams per device and
    day, client signal per access point. Moved unchanged.
  - `wifi_ap_associations.tracker` (new): `PresenceTracker`, the state the
    integration's coordinator kept between polls (sightings with visits, access point
    states, coverage, riding out a short total outage, storage and restore);
    `poll_all()`; `watch_forever()`, the instant-arrival retry loop.
- The integration's tests of those rules moved here with them (70), plus 13 for the
  tracker and the loops. Stored data keeps its format: what the integration saved
  before loads unchanged.

## 0.11.2

- **New driver `omada_network`, experimental: TP-Link Omada access points** through an
  Omada controller connection passed in as `OmadaSource` (as for UniFi). Clients from
  the controller's connected client list: AP (`apMac`), SSID, band (`radioId` 0 =
  2.4 GHz, 1 and 2 = 5 GHz, 3 = 6 GHz), signal (`rssi`, dBm) and connected time
  (`uptime`); the AP's name, model, firmware, uptime (`uptimeLong`), CPU and memory
  from its device record; an AP the controller doesn't show as connected is
  unavailable. Written from Home Assistant's tplink_omada test fixtures and
  tplink-omada-client 1.5, not yet run on a real controller.

## 0.11.1

- **Instant arrivals for OpenWrt.** `openwrt_ssh` has `WATCHES = True`: `async_watch()`
  follows hostapd's ubus notifications (`ubus subscribe` on every `hostapd.*` object,
  one SSH connection) and calls `on_change()` when a client is authorized
  (`sta-authorized`), or 2 s after `assoc` on hostapd builds without that event
  (OpenWrt 22.03). Probes and leaving count for nothing.
- A Wi-Fi reload gives hostapd's objects new ids and leaves a running subscribe silent
  without ending it; the watch also follows `ubus listen ubus.object.add
  ubus.object.remove` and subscribes again (with one `on_change()`, as clients rejoin).
- A ubus command whose SSH session ends keeps running on the router, so each runs in a
  shell that prints an empty line every 15 s and stops it once that write fails:
  nothing is left behind by a dropped connection.
- Built and checked against OpenWrt 25.12.5 and 22.03.7 in VMs with simulated radios:
  joins, leaves, an AP reload, and nothing left running afterwards. Dropbear answers the
  SSH keepalive, so it stays on.

## 0.11.0

- **Instant arrivals for UniFi.** `unifi_network` has `WATCHES = True`: `async_watch()`
  calls `on_change()` when the controller reports a client joining the access point,
  roaming to it or changing radio on it (`EVT_WU_Connected`, `EVT_WU_Roam`,
  `EVT_WU_RoamRadio` and the guest `EVT_WG_` ones). The events come through a new
  `UnifiSource.async_watch_events(mac, callback)`, which hands over the controller's raw
  events; `arrives_at(event, ap_mac)` tells which count. Disconnects count for nothing.

## 0.10.5

- **`cisco_wlc_snmp` is no longer experimental.** Confirmed in Home Assistant on a
  C9800-CL (IOS-XE 17.15.5, SNMPv3 SHA-256/AES-128) with signal on every client
  ([wifi-association-presence#10](https://github.com/darek-margas/wifi-association-presence/issues/10)).

## 0.10.4

From a tester's Home Assistant log
([wifi-association-presence#11](https://github.com/darek-margas/wifi-association-presence/issues/11)).

- **The MikroTik watch no longer drops every two minutes.** RouterOS's SSH server
  doesn't answer the SSH keepalive, so asyncssh closed the watch connection after three
  missed replies ("Server not responding to keepalive"), and arrivals fell back to the
  regular poll until it reconnected. The SSH keepalive is now off for the watch;
  instead it runs `/system identity print` on the same connection every 60 s and treats
  no answer within 20 s as a dead connection.
- **No more "Task exception was never retrieved".** When the connection dropped, both
  feeds (`wifi` and `wireless`) failed together and only one failure was read.

## 0.10.3

From a review of 0.10.2 and a tester's report of arrivals taking 5 to 30 s
([wifi-association-presence#11](https://github.com/darek-margas/wifi-association-presence/issues/11)).

- **A reconnect is an arrival, however soon.** Leaving prints nothing, so the 60 s
  filter took a phone that left and came back on the same interface within a minute
  for one re-authenticating, and waited for the next poll. The filter now asks the
  driver whether its last poll saw the client: one it didn't see always counts; one it
  saw counts again only on another interface or after 60 s. `watch_registrations()`
  takes `present`; `MikroTikSsh` remembers the clients of its last poll, and the CAPsMAN
  driver asks the controller's last shared read.
- **Debug logging of the change feed:** each line received, the filter's decision and
  why, a feed ending, and how long each poll command took (logger
  `wifi_ap_associations`).
- `wifi-ap-probe --debug` prints those logs with timestamps; with `--watch` it shows
  when each line arrives against when the phone joined.
- Test: lines of a real AsyncSSH exec stream arrive one by one, not buffered.

## 0.10.2

- **MikroTik watch: only arrivals and roams.** A tester's `print follow-only` capture
  (RouterOS 7.24.4, [wifi-association-presence#11](https://github.com/darek-margas/wifi-association-presence/issues/11))
  showed a line for every join and roam, nothing on leaving, and a line every second or
  two for a client that keeps re-authenticating; in the `wifi` package a joining client
  appears first without the A (authorized) flag. `on_change()` is now called only for
  an authorized client line (`wifi`; every client line of `wireless`, which has no
  flags) of a client that is new, on another interface than last time, or last seen
  over 60 s ago. Before, each line asked for a poll: a flapping client meant one every
  few seconds, and a poll on the unauthorized line came too early to count the client.

## 0.10.1

Fixes from a review of the Cisco SNMP signal readings.

- **Signal and connected time fall back field by field.** They come from two separate
  tables, but a read that got one of them stored both as one reading: the other's last
  value was dropped, and a fresh value made the missing one look recent. Each field now
  keeps its own value and measurement time (`ClientReading.signal_read_at`,
  `connected_read_at`) and falls back on its own, for at most 150 s.
- **A failing signal or connected-time column no longer fails the read.** Only the AP
  and client tables decide presence; an error walking one of the extra columns leaves
  signal or connected time empty for that read instead of making the AP unavailable.
  Rejected keys still fail it.

## 0.10.0

- **Watching for changes (instant arrivals), experimental.** Drivers can say
  `WATCHES = True` and offer `async_watch(on_change)`: it keeps the device's change feed
  open and calls `on_change()` (no arguments: nothing in the event is trusted) whenever
  the clients may have changed, so the caller can poll at once instead of waiting a
  minute. It runs until cancelled and raises `AccessPointError` or
  `AccessPointAuthError` when the feed can't go on. `watch_key()` says which drivers
  share one watch.
- **MikroTik watches**, with `/interface wifi registration-table print follow-only terse`
  and its `wireless` counterpart over the same SSH login (keep-alives every 30 s). Any
  line naming a `mac-address=` counts as a change; a package that isn't installed is
  skipped. A CAPsMAN controller is watched once for all its CAPs (`print follow-only` of
  the `wifi` package), and its CAPs share a read for 3 s instead of 20, so a poll an
  event triggers reads the controller again. The exact `follow-only` output hasn't been
  seen from a device yet, hence experimental.
- `wifi-ap-probe --watch` prints a line with the time and client count for every change
  until Ctrl+C, to try it without Home Assistant.

## 0.9.3

- **`mikrotik_ssh` and `mikrotik_capsman_ssh` are no longer experimental.** Confirmed
  in Home Assistant by a tester on RouterOS 7.24.4: a cAP lite with the `wireless`
  package (clients, SSID, band, signal and connected time) and a CAPsMAN controller
  with two cAP ax on the `wifi` package (each client on its CAP)
  ([wifi-association-presence#11](https://github.com/darek-margas/wifi-association-presence/issues/11),
  [#12](https://github.com/darek-margas/wifi-association-presence/issues/12)).

## 0.9.2

- **MikroTik `wireless` signal, now actually read.** 0.9.1 asked for
  `registration-table print stats terse`, but RouterOS prints each client of it over
  several lines: the first has the MAC, the indented ones after it hold `uptime` and
  `signal-strength`. The terse parser read every line as its own record, so the signal
  was never matched to its client. It now joins indented lines without a row number to
  the record they continue, and drops the quotes around quoted values
  (`rx-rate="6Mbps"`). Tested on the output of a cAP lite on RouterOS 7.24.4
  ([wifi-association-presence#11](https://github.com/darek-margas/wifi-association-presence/issues/11)).

## 0.9.1

Follow-ups from the CAPsMAN tester
([wifi-association-presence#11](https://github.com/darek-margas/wifi-association-presence/issues/11)).

- **MikroTik, `wireless` package: signal and connected time.** On RouterOS 7.24.4
  `/interface wireless registration-table print terse` lists only interface, MAC,
  `ap`, `wds` and `bridge`: no signal, no uptime. `mikrotik_ssh` now also runs
  `print stats terse` and takes `signal-strength` and `uptime` from it, matched by MAC.
  Presence still comes from the plain table; if the device doesn't answer `stats`
  (an error, an older RouterOS), nothing changes. The collector's `mikrotik` list
  includes the command too.
- **`mikrotik_capsman_ssh` is named "MikroTik CAP via CAPsMAN (SSH)"**: it is added once
  per CAP, and MikroTik doesn't call CAPsMAN a controller. Its `TYPE` stays the same, so
  stored configurations are unaffected.

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

