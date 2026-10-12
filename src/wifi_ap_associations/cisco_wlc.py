"""What Cisco wireless controller drivers share: the controller model and per-AP results.

Lightweight Cisco APs don't list their clients; their controller does, for all of its
APs at once. So one access point is one AP joined to a controller, set up with the
controller's address and login plus the AP's name, and every AP of a controller is
answered from the same controller read. The SSH and SNMP drivers read the controller
differently but produce the same ControllerData, so they give the same results.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
import re
import time

from . import AccessPointError, AccessPointInfo, AssociatedClient, PollResult

CONF_AP_NAME = "ap_name"
STATE_REGISTERED = "Registered"
STATE_RUN = "Run"
_DEFAULT_LOCATION = "default location"


@dataclass(frozen=True, slots=True)
class JoinedAp:
    """An AP the controller lists."""

    name: str
    model: str | None
    state: str
    location: str | None


@dataclass(frozen=True, slots=True)
class ControllerClient:
    """A client the controller lists, in any state (Run is filtered per AP)."""

    mac: str
    ap_name: str
    wlan_id: str | None
    state: str
    protocol: str | None
    ssid: str | None = None
    """SSID when the controller names it per client; otherwise looked up by wlan_id."""
    band: str | None = None
    """Band when the controller gives it directly; otherwise from protocol."""
    signal: int | None = None


@dataclass(frozen=True, slots=True)
class ControllerData:
    """What one read of a controller says about all of its APs."""

    aps: dict[str, JoinedAp]
    ssids: dict[str, str]
    clients: list[ControllerClient]


def ap_location(text: str | None) -> str | None:
    """A location set on the controller; Cisco's "default location" counts as none."""
    text = (text or "").strip()
    return None if text.lower() in {"", _DEFAULT_LOCATION} else text


def band_for_protocol(protocol: str | None) -> str | None:
    """11n(2.4) / 11ax(2.4) / 11g / 11b as 2.4GHz, 11ac / 11a / 11ax(5) as 5GHz, (6) as 6GHz."""
    if not protocol:
        return None
    protocol = protocol.lower()
    if match := re.search(r"\(([\d.]+)\)", protocol):
        return {"2.4": "2.4GHz", "5": "5GHz", "6": "6GHz"}.get(match.group(1))
    if protocol in {"11b", "11g"}:
        return "2.4GHz"
    if protocol in {"11a", "11ac"}:
        return "5GHz"
    return None


def poll_result(data: ControllerData, ap_name: str) -> PollResult:
    """One AP's associated clients and details from a controller read.

    An AP the controller doesn't list, or that isn't Registered, raises: its clients
    can't be known, which is different from "nobody is connected".
    """
    ap = data.aps.get(ap_name)
    if ap is None:
        raise AccessPointError(f"AP {ap_name!r} is not joined to the controller")
    if ap.state != STATE_REGISTERED:
        raise AccessPointError(f"AP {ap_name!r} is {ap.state}")
    clients = [
        AssociatedClient(
            mac=client.mac,
            ssid=client.ssid or data.ssids.get(client.wlan_id or ""),
            band=client.band or band_for_protocol(client.protocol),
            signal=client.signal,
        )
        for client in data.clients
        if client.ap_name == ap_name and client.state == STATE_RUN
    ]
    return PollResult(clients, AccessPointInfo(name=ap.name, location=ap.location, model=ap.model))


class SharedReads:
    """Controller reads in progress or just finished, shared by the APs of a controller.

    Home Assistant polls all APs at the same moment: the first starts the read, the
    others join it. A finished read is reused for `seconds`, then the next poll reads
    again. Reads are shielded: one AP's poll timing out doesn't cancel the read the
    others wait for.
    """

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._reads: dict[Hashable, tuple[float, asyncio.Task[ControllerData]]] = {}

    async def read(
        self, key: Hashable, read: Callable[[], Awaitable[ControllerData]]
    ) -> ControllerData:
        loop = asyncio.get_running_loop()
        entry = self._reads.get(key)
        if (
            entry is None
            or entry[1].get_loop() is not loop
            or (entry[1].done() and time.monotonic() - entry[0] > self.seconds)
        ):
            entry = (time.monotonic(), loop.create_task(read()))
            self._reads[key] = entry
        return await asyncio.shield(entry[1])
