# wifi-ap-associations

Ask a Wi-Fi access point which clients are associated to it right now: their MAC, signal,
band, SSID and how long they have been connected, plus what the access point reports about
itself (model, firmware, uptime, CPU and memory).

An associated client is one that has joined the access point's wireless network, as the
access point lists it. That is a better sign of a device being there than an ARP or
switch table entry: a phone stays associated while it sleeps and sends nothing for
minutes.

This is the library behind the Home Assistant integration
[Wi-Fi Association Presence](https://github.com/darek-margas/wifi-association-presence),
but it has no Home Assistant imports and works in any asyncio program: a network
inventory, an exporter for Prometheus, your own scripts. It also carries that
integration's presence rules (who is home, on which access point, since when), so
another program decides home and away exactly as the integration does; an MQTT bridge
built on it, as a container for Node-RED and other MQTT users, is on the integration's
[roadmap](https://github.com/darek-margas/wifi-association-presence#roadmap).

## Supported access points

| `TYPE` | Access point | How it reads | Signal |
|---|---|---|---|
| `dlink_dap_ssh` | D-Link DAP (e.g. DAP-2610) | SSH console | `%` |
| `openwrt_ssh` | OpenWrt 22.03 and newer | SSH, `ubus` (hostapd) | `dBm` |
| `mikrotik_ssh` | MikroTik RouterOS (APs, routers with Wi-Fi) | SSH exec, `print terse` of the `wifi` and `wireless` registration tables (signal and uptime of `wireless` from `print stats terse`) | `dBm` |
| `mikrotik_capsman_ssh` | MikroTik CAP via CAPsMAN: one CAP of a CAPsMAN controller (RouterOS 7, `wifi` package) | SSH exec on the controller: remote CAPs, interfaces and registration table, one read shared by its CAPs; one entry per CAP, by its identity | `dBm` |
| `cisco_wlc_ssh` | Cisco lightweight APs (9120, 9130, ...) joined to a Catalyst 9800 controller | SSH console of the controller, `show wireless client summary`, plus up to 15 clients' detail per read in turns (signal, connected time); one entry per AP, by AP name | `dBm` |
| `cisco_wlc_snmp` | The same APs, read over SNMP (v2c or v3) instead of SSH | SNMP: the Airespace client, client statistics and AP tables the 9800 serves, and Cisco's client table (connected time), every client every read; one entry per AP, by AP name | `dBm` |
| `unifi_network` | UniFi | through its UniFi Network controller (UniFi OS or self-hosted), with a local account: `stat/sta` and `stat/device`, events over its websocket | `dBm` |
| `omada_network` | TP-Link Omada access points (EAP), **experimental** | through its Omada controller (version 5 or later), with a local account: its v2 web API | `dBm` |
| `tplink_eap_snmp` | TP-Link EAP running standalone (no controller), **experimental** | SNMP (v2c or v3): TP-Link's client table, MACs only (no SSID, band or signal) | — |

More can be added as drivers; see
[Help add your access point](https://github.com/darek-margas/wifi-association-presence#help-add-your-access-point).

## Install

```bash
pip install wifi-ap-associations
```

Python 3.12 or newer. It brings [asyncssh](https://pypi.org/project/asyncssh/) (SSH) and
[pysnmp](https://pypi.org/project/pysnmp/) (SNMP collector).

## Use

```python
import asyncio

from wifi_ap_associations import DRIVERS, AccessPointAuthError, AccessPointError


async def main() -> None:
    driver = DRIVERS["openwrt_ssh"](
        {"host": "192.168.1.2", "port": 22, "username": "root", "password": "secret"}
    )
    try:
        result = await driver.async_poll()
    except AccessPointAuthError:
        print("wrong username or password")
        return
    except AccessPointError as err:
        print(f"could not read the access point: {err}")
        return

    for client in result.clients:
        print(client.mac, client.band, client.ssid, client.signal, driver.SIGNAL_UNIT)
    if result.info:
        print(result.info.model, result.info.firmware, result.info.uptime_seconds)


asyncio.run(main())
```

- `async_poll()` returns a `PollResult`: `clients` (a list of `AssociatedClient`) and
  `info` (an `AccessPointInfo`, or `None`). `async_get_associated_clients()` returns the
  clients only.
- Every driver lists its settings in `FIELDS` (key, whether it is secret, default,
  whether it may be left empty, and for some the only accepted values in `choices`), its
  display name in `NAME`, and the scale of `signal` in `SIGNAL_UNIT` (`"%"` or `"dBm"`).
- `identity(config)` tells which physical AP a configuration points at (by
  `IDENTITY_FIELDS`, else `UNIQUE_FIELD`), comparable across drivers: the Cisco drivers
  use the controller's host plus the AP's name, so two controllers may each have an
  AP01, while one AP added over SSH and SNMP is the same AP.
- `BLOCKING_IMPORTS` names modules a driver loads on its first poll that read files from
  disk (pysnmp for the SNMP driver). In an asyncio program that must not block, import
  them in a worker thread first, as Home Assistant does.
- Drivers with `WATCHES = True` (MikroTik, UniFi, OpenWrt, D-Link DAP) also offer
  `async_watch(on_change)`: it keeps the device's change feed open and calls
  `on_change()` whenever the clients may have changed, so a program can poll at once
  (instant arrivals). It runs until cancelled and raises `AccessPointError` when the
  feed ends; nothing in the event is trusted, only the poll. `watch_key()` tells which
  drivers share one watch (the CAPs of one CAPsMAN controller).
- MAC addresses are upper case with colons; `normalize_mac()` converts other spellings.
- Errors: `AccessPointAuthError` for rejected credentials, `AccessPointError` for
  everything else (unreachable, timeout, unexpected output).

### UniFi and TP-Link Omada controllers

These drivers log in to the controller themselves (settings: `host`, `port`, `username`,
`password`, `site`, `verify_ssl` and the AP's `ap_mac`). Access points with the same
controller settings share one HTTPS session (aiohttp) and one read of the controller per
20 seconds; call `await driver.async_close()` when you stop using a driver, and the
session is closed with its last access point. `await UnifiNetworkDriver.async_list_access_points(settings)`
(and the same for Omada) lists a controller's access points as `(mac, label)` for
picking one. The UniFi driver keeps one event websocket per controller for its
access points' watches.

## Command-line tools

Installed with the library:

```bash
# Read an access point that has a driver, print its associated clients
wifi-ap-probe --type openwrt_ssh --host 192.168.1.2 --username root
wifi-ap-probe --type cisco_wlc_snmp --host 192.168.1.20 --ap-name AP01   # SNMP v2c
wifi-ap-probe --type cisco_wlc_snmp --host 192.168.1.20 --ap-name AP01 --username ro --auth-protocol sha256  # v3
wifi-ap-probe --type mikrotik_ssh --host 192.168.1.1 --username admin --watch  # follow changes
wifi-ap-probe --type mikrotik_ssh --host 192.168.1.1 --username admin --watch --debug  # with each line, timed
wifi-ap-probe --list-types

# Collect read-only, redacted data from an access point that has no driver yet
wifi-ap-collect --host 192.168.1.2 --username admin
wifi-ap-collect --host 192.168.1.2 --snmp            # SNMP v2c, asks for the community
wifi-ap-collect --host 192.168.1.2 --snmp --profile c9800  # only a Cisco 9800's tables
wifi-ap-collect --list-profiles
```

Passwords are asked for interactively (or taken from `$AP_PASSWORD`). `wifi-ap-collect`
uses vetted profile commands and validates extra commands: `show` / `get` / `display`
commands, read-only `ubus` calls and the usual client-list commands (`iw dev <if> station
dump`, `iwinfo <if> assoclist`, `wlanconfig <if> list`, `wl assoclist`, MikroTik
`/... print`) are accepted; shell chaining, redirection, expansion and anything else are
refused. It redacts MAC addresses (vendor prefix kept), IP and email addresses, the
secrets you typed and secret-looking settings, and writes `ap-report-<host>.txt`.
**Read the report before sharing it**: automatic redaction can't recognise everything.
Attach it to a
[New access point model](https://github.com/darek-margas/wifi-association-presence/issues/new?template=new_access_point.yml)
issue. `wifi-ap-collect --help` lists all options (profiles, extra commands, legacy SSH,
SNMPv3). Custom commands support `show`, `get`, `display`, known information commands
and read-only `ubus` calls; unsupported commands are skipped. Use a read-only account
where the access point provides one.

From a program, the SSH collector is one call (same commands, read-only filter and
redaction):

```python
from wifi_ap_associations.collect import async_collect_ssh_report

report = await async_collect_ssh_report(
    "192.168.1.2", "admin", "secret", profile="generic", legacy_ssh=False
)
```

It returns the report text with the review warning on top, and raises
`AccessPointAuthError` / `AccessPointError` like the drivers.

## Presence across polls

The Home Assistant integration's presence rules are in this library, so any program can
use them: `wifi_ap_associations.presence` (merging a poll round, the grace period,
visits, storage), `wifi_ap_associations.coverage` (roams, short drops, late roams,
signal per access point) and `wifi_ap_associations.tracker`, which keeps the state
between polls:

```python
import asyncio
from datetime import datetime, timedelta, UTC

from wifi_ap_associations import DRIVERS
from wifi_ap_associations.presence import AccessPointRead
from wifi_ap_associations.tracker import AllAccessPointsFailed, PresenceTracker, poll_all

aps = {
    "hall": DRIVERS["openwrt_ssh"]({"host": "192.168.1.2", "username": "root", "password": "x"}),
    "den": DRIVERS["mikrotik_ssh"]({"host": "192.168.1.3", "username": "ha", "password": "y"}),
}
tracker = PresenceTracker(consider_home=timedelta(minutes=3))


async def main() -> None:
    while True:
        results = await poll_all(list(aps.values()))
        now = datetime.now(UTC)
        reads = [
            AccessPointRead(ap_id, ap_id, driver.SIGNAL_UNIT, result)
            for (ap_id, driver), result in zip(aps.items(), results)
        ]
        try:
            tracker.update(reads, now, datetime.now().date())
        except AllAccessPointsFailed:
            print("no access point readable: presence unknown")
        else:
            phone = tracker.current("5C:AD:BA:00:00:01", now)
            print("phone", f"home on {phone.access_point} since {phone.arrived}" if phone else "away")
        await asyncio.sleep(60)


asyncio.run(main())
```

- `PresenceTracker.update(reads, now, today)` takes one round of every access point
  (each read is a `PollResult` or the exception it raised) and returns the sightings and
  each access point's state. A device on two access points goes to the stronger signal;
  a failing access point keeps its clients until the grace period runs out; a round in
  which none could be read is ridden out within the grace period after the last good
  one, and past it raises `AllAccessPointsFailed` (presence unknown, not everyone away).
- `current(mac, now)` is the device's sighting while it is home: access point, SSID,
  band, signal, `arrived` (start of the visit) and `last_seen`.
- `to_storage(now, clean=...)` and `restore(stored, now, today)` keep visits and today's
  coverage counts across a restart; a clean stop (`clean=True`) keeps visits however
  long the program was down.
- `coverage.counts` and `access_point_coverage` hold the coverage numbers (see
  `wifi_ap_associations.coverage`).
- `poll_all(drivers)` polls them at once within each driver's `POLL_TIMEOUT`;
  `watch_forever(driver, on_change, ...)` keeps an instant-arrival watch open with the
  integration's retry timing and calls back when it pauses and resumes.

## Security

The SSH drivers log in with a password and **do not verify the access point's host key**
(it changes when the access point is reset or reinstalled). Use them on a trusted network
and keep the access points' management access limited to the host that polls them. Each
poll is a new login, which the access point may log.

## Development

```bash
pip install -e . pytest ruff
ruff check --select E9,F src tests
python -m pytest
```

Releases: set `__version__` in `src/wifi_ap_associations/__init__.py`, add a
`## <version>` section to `CHANGELOG.md`, and push to `main`. The *publish* workflow sees
a version without a tag, runs the tests, uploads to PyPI, and creates the tag
`v<version>` and the GitHub release. To try a version on TestPyPI without releasing it,
push it to another branch and run *publish* by hand on that branch (Actions → publish →
Run workflow); a manual run only uploads to TestPyPI.

## License

GPL-3.0, like the integration.

