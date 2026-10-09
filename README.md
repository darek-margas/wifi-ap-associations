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
inventory, an exporter for Prometheus, your own scripts.

## Supported access points

| `TYPE` | Access point | How it reads | Signal |
|---|---|---|---|
| `dlink_dap_ssh` | D-Link DAP (e.g. DAP-2610) | SSH console | `%` |
| `openwrt_ssh` | OpenWrt 22.03 and newer | SSH, `ubus` (hostapd) | `dBm` |
| `unifi_network` | UniFi | through a UniFi Network controller connection you pass in (see below) | `dBm` |

More can be added as drivers; see
[Help add your access point](https://github.com/darek-margas/wifi-association-presence#help-add-your-access-point).

## Install

```bash
pip install wifi-ap-associations
```

Python 3.12 or newer. The SSH drivers use [asyncssh](https://pypi.org/project/asyncssh/).

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
- Every driver lists its settings in `FIELDS` (key, whether it is secret, default), its
  display name in `NAME`, and the scale of `signal` in `SIGNAL_UNIT` (`"%"` or `"dBm"`).
- MAC addresses are upper case with colons; `normalize_mac()` converts other spellings.
- Errors: `AccessPointAuthError` for rejected credentials, `AccessPointError` for
  everything else (unreachable, timeout, unexpected output).

### UniFi

The UniFi driver does not log in by itself. It reads from an object you pass as the
second argument, which implements `UnifiSource`
(`async_get_device(mac)` and `async_get_clients(mac)`, returning the controller's device
and client records). In Home Assistant that is the UniFi Network integration's
connection; elsewhere, wrap your own controller client.

## Command-line tools

Installed with the library:

```bash
# Read an access point that has a driver, print its associated clients
wifi-ap-probe --type openwrt_ssh --host 192.168.1.2 --username root
wifi-ap-probe --list-types

# Collect read-only, redacted data from an access point that has no driver yet
wifi-ap-collect --host 192.168.1.2 --username admin
wifi-ap-collect --host 192.168.1.2 --snmp            # needs: pip install pysnmp
wifi-ap-collect --list-profiles
```

Passwords are asked for interactively (or taken from `$AP_PASSWORD`). `wifi-ap-collect`
runs only commands that display information (anything that could change settings is
refused), redacts MAC addresses (vendor prefix kept), IP and email addresses, the
secrets you typed and secret-looking settings, and writes `ap-report-<host>.txt`.
**Read the report before sharing it**: automatic redaction can't recognise everything.
Attach it to a
[New access point model](https://github.com/darek-margas/wifi-association-presence/issues/new?template=new_access_point.yml)
issue. `wifi-ap-collect --help` lists all options (profiles, extra commands, legacy SSH,
SNMPv3).

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

Releases: set `__version__` in `src/wifi_ap_associations/__init__.py`, add a section to
`CHANGELOG.md`, run the *publish* workflow by hand to upload to TestPyPI and check it,
then push a tag `v<version>` to upload to PyPI and create the GitHub release.

## License

GPL-3.0, like the integration.
