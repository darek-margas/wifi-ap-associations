"""Cisco Catalyst 9800 wireless controllers (IOS-XE) over SSH (experimental).

Lightweight Cisco APs (9120, 9130, 9166, ...) don't list their clients; their controller
does, for all of its APs at once. So one access point here is one AP joined to a
controller: the settings are the controller's login plus the AP's name, and every AP
of a controller is read from the same `show` output. APs polled together (Home Assistant
polls all of them at the same moment) share a single controller login.

Seen on a C9800-CL with IOS-XE 17.15.5 (interactive console, `terminal length 0`):

    show ap summary
    AP Name                          Slots AP Model             Ethernet MAC   Radio MAC      CC   RD   IP Address                                State        Location
    ---------------------------------------------------------------------------------------------
    AP01                             2     C9120AXI-B           c884.a100.0003 c884.a100.0004 US   -B   192.0.2.21                                Registered   default location

    show wlan summary
    ID   Profile Name                     SSID                             Status 2.4GHz/5GHz Security
    ---------------------------------------------------------------------------------------------
    1    Home                             Home                             UP     [WPA2][PSK][AES]

    show wireless client summary
    Number of Clients: 23
    MAC Address    AP Name                                        Type ID   State             Protocol   Method     Role
    ---------------------------------------------------------------------------------------------
    74f4.4100.0012 AP01                                           WLAN 1    Run               11ax(2.4)  None       Local

Only clients in the Run state count as associated (the others are still joining:
Associated, Authenticating, IP Learn, Webauth Pending). The band comes from the protocol
column. These tables have no signal or connected time; `show wireless client summary
detail` has the connected time, but in a much wider layout that isn't used yet.
EXPERIMENTAL until a tester confirms it on a real controller.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import re
import time
from typing import Any

import asyncssh

from . import (
    AccessPointAuthError,
    AccessPointDriver,
    AccessPointError,
    AssociatedClient,
    DriverField,
    PollResult,
    normalize_mac,
    register,
)
from .cisco_wlc import (  # noqa: F401  band_for_protocol and poll_result: public API
    CONF_AP_NAME,
    ControllerClient,
    ControllerData,
    JoinedAp,
    SharedReads,
    ap_location,
    band_for_protocol,
    poll_result,
)
from .ssh import SshPolicy

# IOS-XE 17 offers ECDH and group14/16 key exchange, RSA host keys and AES (CTR, GCM).
CISCO_SSH_POLICY = SshPolicy(
    kex_algs=(
        "curve25519-sha256",
        "ecdh-sha2-nistp256",
        "ecdh-sha2-nistp384",
        "ecdh-sha2-nistp521",
        "diffie-hellman-group16-sha512",
        "diffie-hellman-group14-sha256",
        "diffie-hellman-group14-sha1",
    ),
    server_host_key_algs=(
        "ssh-ed25519",
        "ecdsa-sha2-nistp256",
        "rsa-sha2-256",
        "rsa-sha2-512",
        "ssh-rsa",
    ),
    encryption_algs=(
        "aes128-gcm@openssh.com",
        "aes256-gcm@openssh.com",
        "aes128-ctr",
        "aes192-ctr",
        "aes256-ctr",
    ),
    preferred_auth=("password", "keyboard-interactive"),
)

COMMAND_TIMEOUT = 20
# APs polled within this many seconds of a controller read use that read.
SHARE_SECONDS = 20

COMMANDS = {
    "aps": "show ap summary",
    "wlans": "show wlan summary",
    "clients": "show wireless client summary",
}

# "C9800#" or "C9800>" alone on the last line.
_PROMPT = re.compile(r"(?:^|\n)[\w.\-]+[#>]\s*$")
_MORE = re.compile(r"--More--\s*$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# IOS answers a command it refuses with "% Invalid input ..." or "% Authorization failed".
_IOS_ERROR = re.compile(r"^\s*% ", re.MULTILINE)
_CISCO_MAC = re.compile(r"^[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\b")
_CLIENT_COUNT = re.compile(r"^Number of Clients:\s*(\d+)", re.MULTILINE)
# A row of `show ap summary`, matched by pattern rather than by column position.
_AP_ROW = re.compile(
    r"^(?P<name>\S+)\s+\d+\s+(?P<model>\S+)\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+"
    r"(?P<state>\S+)(?:\s+(?P<location>.*?))?\s*$"
)
def table(output: str, first_column: str, columns: tuple[str, ...]) -> list[dict[str, str]]:
    """Rows of a fixed-width IOS table as dicts, sliced at the header's column positions.

    The header is the line starting with first_column and naming all columns, in order;
    its next line is a row of dashes, and the rows end at the first empty line. The last
    column runs to the end of the line.
    """
    names = (first_column, *columns)
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(first_column) and (starts := _column_starts(line, names)):
            break
    else:
        return []
    rows = []
    for line in lines[index + 1 :]:
        if not line.strip():
            if rows:
                break
            continue
        if set(line.strip()) == {"-"}:
            continue
        rows.append(
            {
                name: line[start:end].strip()
                for name, start, end in zip(names, starts, [*starts[1:], None])
            }
        )
    return rows


def _column_starts(header: str, names: tuple[str, ...]) -> list[int] | None:
    """Where each column starts in a header line, or None if one is missing."""
    starts = []
    position = 0
    for name in names:
        found = header.find(name, position)
        if found < 0:
            return None
        starts.append(found)
        position = found + len(name)
    return starts


def parse_aps(output: str) -> dict[str, JoinedAp]:
    """`show ap summary` as AP name -> JoinedAp."""
    aps = {}
    for row in table(output, "AP Name", ()):
        if match := _AP_ROW.match(row["AP Name"]):
            aps[match.group("name")] = JoinedAp(
                name=match.group("name"),
                model=match.group("model"),
                state=match.group("state"),
                location=ap_location(match.group("location")),
            )
    if "AP Name" in output and not aps and not re.search(r"Number of APs:\s*0\b", output):
        raise AccessPointError("Could not read the controller's AP list")
    return aps


def parse_ssids(output: str) -> dict[str, str]:
    """`show wlan summary` as WLAN ID -> SSID."""
    return {
        row["ID"]: row["SSID"]
        for row in table(output, "ID", ("Profile Name", "SSID", "Status"))
        if row["ID"].isdigit() and row["SSID"]
    }


def parse_clients(output: str) -> list[ControllerClient]:
    """`show wireless client summary` rows (every state; Run is filtered later).

    A row that starts with a MAC but can't be read, or fewer rows than the "Number of
    Clients" line announces, raise: a partial table must not look like departures.
    """
    count = _CLIENT_COUNT.search(output)
    if count is None:
        raise AccessPointError("No answer to show wireless client summary")
    rows = table(
        output, "MAC Address", ("AP Name", "Type", "ID", "State", "Protocol", "Method", "Role")
    )
    clients = []
    for row in rows:
        if not _CISCO_MAC.match(row["MAC Address"]):
            continue
        try:
            mac = normalize_mac(row["MAC Address"])
        except ValueError as err:
            raise AccessPointError(f"Unexpected client row: {row['MAC Address']!r}") from err
        if not row["AP Name"] or not row["State"]:
            raise AccessPointError(f"Unexpected client row for {mac}")
        clients.append(
            ControllerClient(
                mac=mac,
                ap_name=row["AP Name"],
                wlan_id=row["ID"] or None,
                state=row["State"],
                protocol=row["Protocol"] or None,
            )
        )
    if len(clients) != int(count.group(1)):
        raise AccessPointError(
            f"Client table has {len(clients)} rows, the controller announced {count.group(1)}"
        )
    return clients


def parse_controller(outputs: dict[str, str]) -> ControllerData:
    """All APs, SSIDs and clients from the outputs of COMMANDS (keyed like COMMANDS)."""
    for key, command in COMMANDS.items():
        if match := _IOS_ERROR.search(outputs.get(key, "")):
            line = outputs[key][match.start() :].strip().splitlines()[0]
            raise AccessPointError(f"{command}: {line}")
    return ControllerData(
        aps=parse_aps(outputs.get("aps", "")),
        ssids=parse_ssids(outputs.get("wlans", "")),
        clients=parse_clients(outputs.get("clients", "")),
    )


_shared = SharedReads(SHARE_SECONDS)


@register
class CiscoWlcSsh(AccessPointDriver):
    """A lightweight AP joined to a Cisco Catalyst 9800 controller, read over SSH."""

    TYPE = "cisco_wlc_ssh"
    NAME = "Cisco Catalyst 9800 controller (SSH)"
    MANUFACTURER = "Cisco"
    # No signal in the tables read; dBm is what the controller's detail commands report.
    SIGNAL_UNIT = "dBm"
    EXPERIMENTAL = True
    REPORTS = frozenset({"name", "location", "model"})
    # Many APs share one controller host; the AP name tells them apart.
    UNIQUE_FIELD = CONF_AP_NAME
    IDENTITY_FIELDS = ("host", CONF_AP_NAME)
    SSH_POLICY = CISCO_SSH_POLICY
    FIELDS = (
        DriverField("host"),
        DriverField("port", default=22),
        DriverField("username"),
        DriverField("password", secret=True),
        DriverField(CONF_AP_NAME),
    )

    async def async_get_associated_clients(self) -> list[AssociatedClient]:
        """Clients associated to this AP."""
        return (await self.async_poll()).clients

    async def async_poll(self) -> PollResult:
        """This AP's clients and details, from a controller read shared with its other APs."""
        return poll_result(await self._shared_read(), str(self.config[CONF_AP_NAME]).strip())

    async def _shared_read(self) -> ControllerData:
        """Join a read of this controller that is running or just finished, or start one."""
        key = (
            str(self.config["host"]),
            int(self.config.get("port") or 22),
            str(self.config["username"]),
            str(self.config["password"]),
        )
        return await _shared.read(key, self._read_controller)

    async def _read_controller(self) -> ControllerData:
        async with self._console() as process:
            await _run(process, "terminal length 0")
            outputs = {key: await _run(process, command) for key, command in COMMANDS.items()}
        return parse_controller(outputs)

    @asynccontextmanager
    async def _console(self) -> AsyncIterator[asyncssh.SSHClientProcess]:
        """Logged-in console session, with errors mapped to AccessPointError."""
        host = self.config["host"]
        try:
            async with asyncssh.connect(
                host,
                port=int(self.config.get("port") or 22),
                username=self.config["username"],
                password=self.config["password"],
                **self.SSH_POLICY.connect_options(),
            ) as conn:
                async with conn.create_process(
                    term_type="vt100",
                    term_size=(200, 1000),
                    encoding="utf-8",
                    errors="replace",
                ) as process:
                    await _read_until_prompt(process)
                    yield process
        except asyncssh.PermissionDenied as err:
            raise AccessPointAuthError(f"{host}: login rejected") from err
        except (OSError, asyncio.TimeoutError, asyncssh.Error) as err:
            raise AccessPointError(f"{host}: {err!r}") from err


async def _run(process: Any, command: str) -> str:
    """Send a console command and return its output up to the next prompt."""
    process.stdin.write(command + "\n")
    return await _read_until_prompt(process)


async def _read_until_prompt(process: Any) -> str:
    """Read console output until the prompt, paging past --More-- if paging is still on."""
    deadline = time.monotonic() + COMMAND_TIMEOUT
    buffer = ""
    while not _PROMPT.search(buffer):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AccessPointError(f"no CLI prompt within {COMMAND_TIMEOUT}s")
        chunk = await asyncio.wait_for(process.stdout.read(4096), remaining)
        if not chunk:
            raise AccessPointError("console closed the connection")
        buffer = _ANSI.sub("", buffer + chunk).replace("\r", "")
        if _MORE.search(buffer):
            buffer = _MORE.sub("", buffer)
            process.stdin.write(" ")
    return buffer
