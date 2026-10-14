"""MikroTik CAPsMAN controllers (RouterOS 7, wifi package) over SSH (experimental).

With CAPsMAN, the access points (CAPs) don't list their clients: the controller does,
for all of them. So one access point here is one CAP, set up with the controller's login
and the CAP's identity, and every CAP of a controller is read from the same three
commands, one login per poll shared by its CAPs (as the Cisco 9800 drivers do).

Seen on a CAPsMAN router with two cAP ax (cAPGi-5HaxD2HaxD), all on RouterOS 7.24.4:

    /interface wifi capsman remote-cap print terse
    0 address=...%vlan33-mgmt identity=ap_upstairs board-name=cAPGi-5HaxD2HaxD
      version=7.24.4 state=Ok uptime=2w6d22h40m4s ...

    /interface wifi print terse
    14 MDB comment=operated by CAP ... cap=ap_upstairs@...%*34 name=ap_upstairs radio-mac=...
    22  DB comment=operated by CAP ... name=ap_upstairs -virtual8 master-interface=ap_upstairs 2
         configuration.ssid=... channel.band=2ghz-ax

    /interface wifi registration-table print terse
    0 A interface=ap_upstairs -virtual8 ssid=... mac-address=... uptime=2w6d22h37m35s
        signal=-29 band=2ghz-n

A client's interface is a CAP radio's interface (it carries `cap=<identity>@<address>`)
or a virtual interface (an extra SSID) whose `master-interface=` leads to one. Clients
without the A (authorized) flag are still joining and don't count. Interface names may
contain spaces ("ap_upstairs 2").

The legacy CAPsMAN of the `wireless` package (`/caps-man`, RouterOS 6) isn't covered.
Confirmed on that controller in Home Assistant
(darek-margas/wifi-association-presence#11).
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from typing import Any

from . import (
    AccessPointDriver,
    AccessPointError,
    AssociatedClient,
    DriverField,
    PollResult,
    normalize_mac,
    register,
)
from .cisco_wlc import (
    STATE_REGISTERED,
    STATE_RUN,
    ControllerClient,
    ControllerData,
    JoinedAp,
    SharedReads,
    poll_result,
)
from .mikrotik_ssh import (
    _LEADING_INT,
    MIKROTIK_SSH_POLICY,
    WATCH_COMMANDS,
    band_name,
    is_missing_menu,
    parse_terse,
    parse_uptime,
    run_commands,
    watch_registrations,
)

CONF_CAP = "cap"
# Short: the CAPs of one poll read together anyway, and a poll an instant-arrival event
# triggers seconds after the last one must read the controller again.
SHARE_SECONDS = 3
COMMANDS = {
    "caps": "/interface wifi capsman remote-cap print terse",
    "interfaces": "/interface wifi print terse",
    "clients": "/interface wifi registration-table print terse",
}
# A remote CAP the controller manages normally.
STATE_OK = "Ok"

_shared = SharedReads(SHARE_SECONDS)


def cap_identity(value: str) -> str:
    """The CAP's identity from `cap=ap_upstairs@<address>%<interface>`."""
    return value.rsplit("@", 1)[0].strip() if "@" in value else value.strip()


def interface_caps(records: list[dict[str, str]]) -> dict[str, str]:
    """Interface name -> the identity of the CAP it belongs to.

    A radio's interface names its CAP (`cap=`); a virtual interface leads there through
    `master-interface=`, possibly over several steps (never in a loop). Interfaces of
    the controller's own radios have no CAP and aren't listed.
    """
    own: dict[str, str] = {}
    masters: dict[str, str] = {}
    for record in records:
        name = record.get("name")
        if not name:
            continue
        if cap := record.get("cap"):
            own[name] = cap_identity(cap)
        if master := record.get("master-interface"):
            masters[name] = master
    caps = {}
    for name in {*own, *masters}:
        seen = {name}
        current = name
        while current not in own and (master := masters.get(current)) and master not in seen:
            seen.add(master)
            current = master
        if current in own:
            caps[name] = own[current]
    return caps


def parse_controller(outputs: dict[str, str]) -> ControllerData:
    """All CAPs and clients from the outputs of COMMANDS (keyed like COMMANDS).

    No CAPsMAN on the device, or a table that answers but can't be read, raises: the
    CAPs' clients can't be known, which is different from "nobody is connected".
    """
    for key in COMMANDS:
        if is_missing_menu(outputs.get(key, "")):
            raise AccessPointError(
                "No CAPsMAN of the wifi package on this device (RouterOS 7 is needed)"
            )
    cap_records = parse_terse(outputs.get("caps", ""))
    if outputs.get("caps", "").strip() and not cap_records:
        raise AccessPointError("Could not read the CAPsMAN remote CAP list")
    aps = {}
    for record in cap_records:
        if not (name := (record.get("identity") or "").strip()):
            continue
        state = record.get("state") or "unknown"
        version = record.get("version")
        aps[name] = JoinedAp(
            name=name,
            model=record.get("board-name") or None,
            state=STATE_REGISTERED if state == STATE_OK else state,
            location=None,
            firmware=version.split()[0] if version else None,
            uptime_seconds=parse_uptime(record.get("uptime")),
        )
    caps = interface_caps(parse_terse(outputs.get("interfaces", "")))
    table = outputs.get("clients", "")
    records = parse_terse(table)
    if table.strip() and not records:
        raise AccessPointError("Could not read the wifi registration table")
    clients = []
    for record in records:
        try:
            mac = normalize_mac(record.get("mac-address", ""))
        except ValueError:
            continue
        flags = record.get("_flags", "")
        signal = record.get("signal")
        match = _LEADING_INT.match(signal) if signal else None
        clients.append(
            ControllerClient(
                mac=mac,
                ap_name=caps.get(record.get("interface", ""), ""),
                wlan_id=None,
                # A client still authenticating has flags but no A (authorized).
                state=STATE_RUN if not flags or "A" in flags else "Authenticating",
                protocol=None,
                ssid=record.get("ssid") or None,
                band=band_name(record.get("band")),
                signal=int(match.group(1)) if match else None,
                connected_seconds=parse_uptime(record.get("uptime")),
            )
        )
    return ControllerData(aps=aps, ssids={}, clients=clients)


@register
class MikroTikCapsmanSsh(AccessPointDriver):
    """One CAP of a MikroTik CAPsMAN controller (RouterOS 7, wifi package), over SSH."""

    TYPE = "mikrotik_capsman_ssh"
    # Added once per CAP, so named for the CAP rather than the controller.
    NAME = "MikroTik CAP via CAPsMAN (SSH)"
    MANUFACTURER = "MikroTik"
    SIGNAL_UNIT = "dBm"
    # From the controller's remote CAP list; CAPs report no CPU, memory or location there.
    REPORTS = frozenset({"name", "model", "firmware", "uptime_seconds"})
    # Many CAPs share one controller; the CAP's identity tells them apart.
    UNIQUE_FIELD = CONF_CAP
    IDENTITY_FIELDS = ("host", CONF_CAP)
    SSH_POLICY = MIKROTIK_SSH_POLICY
    # Instant arrivals: one `print follow-only` on the controller for all its CAPs.
    WATCHES = True
    FIELDS = (
        DriverField("host"),
        DriverField("port", default=22),
        DriverField("username", default="admin"),
        DriverField("password", secret=True),
        DriverField(CONF_CAP),
    )

    async def async_get_associated_clients(self) -> list[AssociatedClient]:
        """Clients associated to this CAP."""
        return (await self.async_poll()).clients

    async def async_poll(self) -> PollResult:
        """This CAP's clients and details, from a controller read shared with its other CAPs."""
        key = (
            str(self.config["host"]),
            int(self.config.get("port") or 22),
            str(self.config["username"]),
            str(self.config["password"]),
        )
        data = await _shared.read(key, self._read_controller)
        return poll_result(data, str(self.config[CONF_CAP]).strip())

    def watch_key(self) -> Hashable:
        """One watch per controller, shared by its CAPs."""
        return (
            self.TYPE,
            str(self.config["host"]).strip().casefold(),
            int(self.config.get("port") or 22),
            str(self.config["username"]),
        )

    async def async_watch(self, on_change: Callable[[], None]) -> None:
        """Follow the controller's registration table (all CAPs' clients)."""
        await watch_registrations(
            self.config, [WATCH_COMMANDS["wifi"]], self.SSH_POLICY, on_change
        )

    async def _read_controller(self) -> ControllerData:
        outputs: dict[str, Any] = await run_commands(self.config, COMMANDS, self.SSH_POLICY)
        return parse_controller(outputs)
