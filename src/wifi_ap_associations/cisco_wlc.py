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
from dataclasses import dataclass, field
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
    firmware: str | None = None
    uptime_seconds: int | None = None


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
    connected_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class ClientReading:
    """One client's signal and connection time, read on one AP at one moment."""

    ap_name: str
    signal: int | None
    connected_seconds: int | None
    read_at: float
    """time.monotonic() of the read (the latest, if the fields were measured apart)."""
    signal_read_at: float | None = None
    """When the signal was measured, if not at read_at (fields read separately)."""
    connected_read_at: float | None = None
    """When connected_seconds was measured, if not at read_at."""

    def signal_age(self, now: float) -> float:
        return now - (self.signal_read_at if self.signal_read_at is not None else self.read_at)

    def connected_age(self, now: float) -> float:
        at = self.connected_read_at if self.connected_read_at is not None else self.read_at
        return now - at


@dataclass(frozen=True, slots=True)
class ControllerData:
    """What one read of a controller says about all of its APs."""

    aps: dict[str, JoinedAp]
    ssids: dict[str, str]
    clients: list[ControllerClient]
    readings: dict[str, ClientReading] = field(default_factory=dict)
    """Per-client readings gathered over several reads (drivers that read details in
    turns), carried from one read to the next."""
    details_paused_until: float = 0.0
    """time.monotonic() until which per-client details aren't asked for (refused)."""


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
            connected_seconds=client.connected_seconds,
        )
        for client in data.clients
        if client.ap_name == ap_name and client.state == STATE_RUN
    ]
    info = AccessPointInfo(
        name=ap.name,
        location=ap.location,
        model=ap.model,
        firmware=ap.firmware,
        uptime_seconds=ap.uptime_seconds,
    )
    return PollResult(clients, info)


class SharedReads:
    """Controller reads in progress or just finished, shared by the APs of a controller.

    Home Assistant polls all APs at the same moment: the first starts the read, the
    others join it. A successful read is reused for `seconds`, then the next poll reads
    again. A failed one is shared only with the polls already waiting for it: the next
    poll tries again (e.g. right after fixing a setting). Reads are shielded: one AP's
    poll timing out doesn't cancel the read the others wait for. Each read is stopped
    after `timeout` (a poll's limit), so a stuck one can't be joined poll after poll.

    The last successful read of each controller is kept for `keep` seconds, for drivers
    whose next read needs it (see last()). Older reads, also of settings since changed
    (the key holds the credentials), are dropped.
    """

    def __init__(self, seconds: float, timeout: float = 45, keep: float = 180) -> None:
        self.seconds = seconds
        self.timeout = timeout
        self.keep = keep
        self._reads: dict[Hashable, tuple[float, asyncio.Task[ControllerData]]] = {}
        self._last: dict[Hashable, tuple[float, ControllerData]] = {}

    def last(self, key: Hashable) -> ControllerData | None:
        """The last successful read of this controller, if within `keep` seconds."""
        entry = self._last.get(key)
        if entry is None or time.monotonic() - entry[0] > self.keep:
            return None
        return entry[1]

    def _forget_old(self) -> None:
        now = time.monotonic()
        for key in [k for k, (at, _data) in self._last.items() if now - at > self.keep]:
            del self._last[key]
        for key in [
            k for k, (at, task) in self._reads.items() if task.done() and now - at > self.keep
        ]:
            del self._reads[key]

    async def _run(
        self, key: Hashable, read: Callable[[], Awaitable[ControllerData]]
    ) -> ControllerData:
        try:
            data = await asyncio.wait_for(read(), self.timeout)
        except TimeoutError as err:
            raise AccessPointError(f"Controller read took over {self.timeout:g} s") from err
        self._last[key] = (time.monotonic(), data)
        return data

    async def read(
        self, key: Hashable, read: Callable[[], Awaitable[ControllerData]]
    ) -> ControllerData:
        loop = asyncio.get_running_loop()
        self._forget_old()
        entry = self._reads.get(key)
        if (
            entry is None
            or entry[1].get_loop() is not loop
            or (
                entry[1].done()
                and (
                    entry[1].cancelled()
                    or entry[1].exception() is not None
                    or time.monotonic() - entry[0] > self.seconds
                )
            )
        ):
            entry = (time.monotonic(), loop.create_task(self._run(key, read)))
            self._reads[key] = entry
        return await asyncio.shield(entry[1])
