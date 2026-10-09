"""MikroTik RouterOS access points over SSH (experimental).

RouterOS runs a command sent as an SSH exec request and returns just its output, with
no terminal, logo or pager (an interactive session sends escape queries, wraps lines at
80 columns and echoes commands). Each command is its own exec request on one
connection: RouterOS checks a whole command line before running it, so a single
unknown menu in a combined line (e.g. `/interface wireless` on a device that has the
`wifi` package) would fail all of it.

System details, as RouterOS 7.20 prints them (confirmed on a CRS310):

    /system identity print        name: MikroTik
    /system resource print        uptime: 30w6d20h44m17s
                                  version: 7.20.4 (stable)
                                  free-memory: 172.6MiB
                                  total-memory: 256.0MiB
                                  cpu-load: 2%
                                  board-name: CRS310-8G+2S+

Clients come from the registration table of the Wi-Fi package the device has:
`/interface wifi` (RouterOS 7, also called wifiwave2) or `/interface wireless`
(RouterOS 6 and older 7 devices). `print terse` gives one client per line as
`key=value` pairs, e.g. (column names from MikroTik's documentation, not yet confirmed
on a real access point, hence EXPERIMENTAL):

    0 interface=wifi1 ssid=Home mac-address=5C:AD:BA:00:00:01 uptime=1h2m3s
      signal=-55 band=5ghz-ax ...
    0 interface=wlan1 mac-address=5C:AD:BA:00:00:01 uptime=1h2m3s
      signal-strength=-61@HT20-7 ...

Where the table lacks the SSID or band, they come from the interface list
(`/interface wifi print terse`: configuration.ssid, channel.band; `/interface wireless
print terse`: ssid, band). A CAPsMAN controller lists the clients of all its CAPs.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import asyncssh

from . import (
    AccessPointAuthError,
    AccessPointDriver,
    AccessPointError,
    AccessPointInfo,
    AssociatedClient,
    DriverField,
    PollResult,
    normalize_mac,
    register,
)
from .ssh import SshPolicy

# RouterOS 7 (ROSSSH) offers modern algorithms; group14-sha1 and ssh-rsa for RouterOS 6.
MIKROTIK_SSH_POLICY = SshPolicy(
    kex_algs=(
        "curve25519-sha256",
        "curve25519-sha256@libssh.org",
        "ecdh-sha2-nistp256",
        "diffie-hellman-group-exchange-sha256",
        "diffie-hellman-group14-sha256",
        "diffie-hellman-group14-sha1",
    ),
    server_host_key_algs=(
        "ssh-ed25519",
        "rsa-sha2-256",
        "rsa-sha2-512",
        "ssh-rsa",
    ),
    encryption_algs=(
        "aes128-ctr",
        "aes256-ctr",
        "aes128-gcm@openssh.com",
        "aes256-gcm@openssh.com",
        "chacha20-poly1305@openssh.com",
    ),
    preferred_auth=("password", "keyboard-interactive"),
)

COMMAND_TIMEOUT = 20

COMMANDS = {
    "identity": "/system identity print",
    "resource": "/system resource print",
    "wifi_interfaces": "/interface wifi print terse",
    "wifi_clients": "/interface wifi registration-table print terse",
    "wireless_interfaces": "/interface wireless print terse",
    "wireless_clients": "/interface wireless registration-table print terse",
}

# RouterOS's answer to a menu that doesn't exist (the package isn't installed).
_BAD_COMMAND = re.compile(r"^(bad command name|syntax error|no such command)", re.IGNORECASE)
# A terse key: letters, digits, dots and dashes, followed by "=", at the start or after a
# space. Values can contain spaces ("comment=Uplink to switch"), so a value runs until
# the next key.
_TERSE_KEY = re.compile(r"(?:(?<=\s)|^)([a-z][a-z0-9.-]*)=")
_UPTIME_PART = re.compile(r"(\d+)(ms|w|d|h|m|s)")
_UPTIME_SECONDS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1, "ms": 0}
_SIZE = re.compile(r"^\s*([\d.]+)\s*([KMGT]?i?B)?\s*$")
_SIZE_FACTOR = {
    "B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4,
    "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4,
}
_LEADING_INT = re.compile(r"^\s*(-?\d+)")


def is_missing_menu(output: str) -> bool:
    """Whether RouterOS answered that the menu doesn't exist on this device."""
    return bool(_BAD_COMMAND.match(output.strip()))


def parse_print(output: str) -> dict[str, str]:
    """`key: value` lines of a plain `print` (identity, resource) as a dict."""
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and " " not in key.strip():
            values[key.strip()] = value.strip()
    return values


def parse_terse(output: str) -> list[dict[str, str]]:
    """One dict per `print terse` record (leading row number and flags dropped)."""
    records = []
    for line in output.splitlines():
        keys = list(_TERSE_KEY.finditer(line))
        if not keys:
            continue
        record = {}
        for match, following in zip(keys, [*keys[1:], None]):
            end = following.start() if following else len(line)
            record[match.group(1)] = line[match.end() : end].strip()
        records.append(record)
    return records


def parse_uptime(text: str | None) -> int | None:
    """RouterOS durations such as 30w6d20h44m17s or 1h2m3s (ms ignored) in seconds."""
    if not text:
        return None
    parts = _UPTIME_PART.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text.strip():
        return None
    return sum(int(number) * _UPTIME_SECONDS[unit] for number, unit in parts)


def parse_size(text: str | None) -> float | None:
    """172.6MiB, 256.0MiB, 1024 (bytes) as bytes."""
    if not text or not (match := _SIZE.match(text)):
        return None
    return float(match.group(1)) * _SIZE_FACTOR.get(match.group(2) or "B", 1)


def parse_percent(text: str | None) -> int | None:
    """`12%` as 12."""
    if text and (match := _LEADING_INT.match(text.rstrip("%"))):
        return int(match.group(1))
    return None


def band_name(band: str | None) -> str | None:
    """RouterOS bands (2ghz-ax, 2ghz-b/g/n, 5ghz-ac, 6ghz-ax) as 2.4GHz / 5GHz / 6GHz."""
    if not band:
        return None
    band = band.lower()
    for prefix, name in (("2ghz", "2.4GHz"), ("5ghz", "5GHz"), ("6ghz", "6GHz")):
        if band.startswith(prefix):
            return name
    return None


def parse_info(identity: dict[str, str], resource: dict[str, str]) -> AccessPointInfo:
    """Name, model, firmware, uptime, CPU and memory from identity and resource."""
    total = parse_size(resource.get("total-memory"))
    free = parse_size(resource.get("free-memory"))
    memory = round((total - free) * 100 / total) if total and free is not None else None
    version = resource.get("version")
    return AccessPointInfo(
        name=identity.get("name") or None,
        model=resource.get("board-name") or None,
        firmware=version.split()[0] if version else None,
        uptime_seconds=parse_uptime(resource.get("uptime")),
        cpu_percent=parse_percent(resource.get("cpu-load")),
        memory_percent=memory,
    )


def parse_interfaces(records: list[dict[str, str]]) -> dict[str, tuple[str | None, str | None]]:
    """Interface name -> (SSID, band) from `/interface wifi|wireless print terse`."""
    interfaces = {}
    for record in records:
        name = record.get("name")
        if not name:
            continue
        ssid = record.get("configuration.ssid") or record.get("ssid") or None
        band = record.get("channel.band") or record.get("band")
        interfaces[name] = (ssid, band_name(band))
    return interfaces


def parse_clients(
    records: list[dict[str, str]], interfaces: dict[str, tuple[str | None, str | None]]
) -> list[AssociatedClient]:
    """Registration table records as associated clients (records without a MAC skipped)."""
    clients = []
    for record in records:
        try:
            mac = normalize_mac(record.get("mac-address", ""))
        except ValueError:
            continue
        ssid, band = interfaces.get(record.get("interface", ""), (None, None))
        signal = record.get("signal") or record.get("signal-strength")
        match = _LEADING_INT.match(signal) if signal else None
        clients.append(
            AssociatedClient(
                mac=mac,
                ssid=record.get("ssid") or ssid,
                band=band_name(record.get("band")) or band,
                signal=int(match.group(1)) if match else None,
                connected_seconds=parse_uptime(record.get("uptime")),
            )
        )
    return clients


def parse_poll(outputs: dict[str, str]) -> PollResult:
    """Clients and device details from the outputs of COMMANDS (keyed like COMMANDS).

    The `wifi` package is used when present, else `wireless`. A device with neither
    (a switch) has no clients. A registration table that exists but can't be read
    raises, so a failed read isn't taken for "everyone left".
    """
    identity = parse_print(outputs.get("identity", ""))
    resource = parse_print(outputs.get("resource", ""))
    if not resource:
        raise AccessPointError("No answer to /system resource print")
    clients: list[AssociatedClient] = []
    for package in ("wifi", "wireless"):
        table = outputs.get(f"{package}_clients", "")
        if is_missing_menu(table):
            continue
        records = parse_terse(table)
        if table.strip() and not records:
            raise AccessPointError(f"Could not read the {package} registration table")
        interfaces = parse_interfaces(parse_terse(outputs.get(f"{package}_interfaces", "")))
        clients = parse_clients(records, interfaces)
        break
    return PollResult(clients, parse_info(identity, resource))


@register
class MikroTikSsh(AccessPointDriver):
    """MikroTik RouterOS (access point, router with Wi-Fi or CAPsMAN controller) over SSH."""

    TYPE = "mikrotik_ssh"
    NAME = "MikroTik RouterOS (SSH)"
    MANUFACTURER = "MikroTik"
    SIGNAL_UNIT = "dBm"
    EXPERIMENTAL = True
    # RouterOS has no location setting in /system identity.
    REPORTS = frozenset(
        {"name", "model", "firmware", "uptime_seconds", "cpu_percent", "memory_percent"}
    )
    SSH_POLICY = MIKROTIK_SSH_POLICY
    FIELDS = (
        DriverField("host"),
        DriverField("port", default=22),
        DriverField("username", default="admin"),
        DriverField("password", secret=True),
    )

    async def async_get_associated_clients(self) -> list[AssociatedClient]:
        """Clients associated to the device's radios."""
        return (await self.async_poll()).clients

    async def async_poll(self) -> PollResult:
        """Clients and device details, one exec request per command on one connection."""
        return parse_poll(await self._run_all())

    async def _run_all(self) -> dict[str, str]:
        host = self.config["host"]
        try:
            async with asyncssh.connect(
                host,
                port=int(self.config.get("port") or 22),
                username=self.config["username"],
                password=self.config["password"],
                **self.SSH_POLICY.connect_options(),
            ) as conn:
                outputs: dict[str, Any] = {}
                for key, command in COMMANDS.items():
                    result = await asyncio.wait_for(
                        conn.run(command, check=False, encoding="utf-8", errors="replace"),
                        COMMAND_TIMEOUT,
                    )
                    outputs[key] = f"{result.stdout or ''}{result.stderr or ''}".replace("\r", "")
        except asyncssh.PermissionDenied as err:
            raise AccessPointAuthError(f"{host}: login rejected") from err
        except (OSError, asyncio.TimeoutError, asyncssh.Error) as err:
            raise AccessPointError(f"{host}: {err!r}") from err
        return outputs
