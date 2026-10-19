"""OpenWrt access points over SSH, reading hostapd through ubus.

Every hostapd radio/SSID on OpenWrt registers a ubus object (hostapd.wlan0 on older
releases, hostapd.phy0-ap0 from 23.05). Its JSON answers, from OpenWrt's hostapd
ubus.c, are:

    ubus call hostapd.phy0-ap0 get_status
    {"status": "ENABLED", "ssid": "Home", "freq": 5180, "channel": 36, "phy": "phy0", ...}

    ubus call hostapd.phy0-ap0 get_clients
    {"freq": 5180, "clients": {"5c:ad:ba:00:00:01": {"auth": true, "assoc": true,
     "authorized": true, "aid": 1, "signal": -52, "bytes": {...}, ...}}}

A station is listed while it authenticates too, so only associated and authorized ones
count (as Home Assistant's own ubus tracker does). "system board" and "system info"
give the name, model, firmware, uptime and memory.

One SSH command per poll runs all of it and marks each section, so the console
dialogue of the D-Link driver isn't needed: OpenWrt's dropbear runs commands directly.

Instant arrivals (async_watch) follow hostapd's ubus notifications. Seen on OpenWrt
25.12.5 and 22.03.7 (VMs with simulated radios), one JSON line per event:

    ubus subscribe hostapd.phy0-ap0
    { "probe": {"address": "02:00:00:00:01:00", "ifname": "phy0-ap0", ...} }
    { "auth": {"address": "02:00:00:00:01:00", ...} }
    { "assoc": {"address": "02:00:00:00:01:00", ...} }
    { "sta-authorized": {"address": "02:00:00:00:01:00", "ifname": "phy0-ap0", ...} }
    { "deauth": {...} }   { "disassoc": {...} }   (leaving)

22.03 sends no "sta-authorized" (and no "ifname"). "probe" comes from any device
scanning nearby. When hostapd restarts (a Wi-Fi reload), its objects get new ids and a
running subscribe goes silent without ending; `ubus listen ubus.object.add
ubus.object.remove` reports that as it happens, and the watch subscribes again.

A ubus subscribe or listen whose SSH session ends keeps running on the router (dropbear
doesn't stop it, and it never writes once its object is gone), so each one runs inside
a shell that prints an empty line every WATCH_HEARTBEAT_SECONDS: once the session is
gone that write fails and the shell stops the ubus command and exits.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
import json
import logging
import re
import time
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

# Current dropbear and OpenSSH algorithms, plus the ones of older OpenWrt releases.
OPENWRT_SSH_POLICY = SshPolicy(
    kex_algs=(
        "curve25519-sha256",
        "curve25519-sha256@libssh.org",
        "ecdh-sha2-nistp256",
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
        "chacha20-poly1305@openssh.com",
        "aes128-ctr",
        "aes256-ctr",
        "aes128-gcm@openssh.com",
        "aes256-gcm@openssh.com",
    ),
    preferred_auth=("password", "keyboard-interactive"),
)

COMMAND_TIMEOUT = 20
_LOGGER = logging.getLogger(__name__)

# Instant arrivals. A client counts once authorized ("sta-authorized"); hostapd builds
# without that event (22.03) only say "assoc", which comes before the key handshake, so
# a poll then could find the client not yet authorized: it waits WATCH_ASSOC_DELAY.
WATCH_LIST_COMMAND = "ubus list 'hostapd.*'"
WATCH_OBJECTS_COMMAND = "ubus listen ubus.object.add ubus.object.remove"
WATCH_ARRIVAL = "sta-authorized"
WATCH_ASSOC = "assoc"
WATCH_LEAVE = frozenset({"deauth", "disassoc"})
WATCH_ASSOC_DELAY = 2.0
# hostapd removes and adds its objects within a few seconds on a reload: wait this long
# after a change before subscribing to what is there then.
WATCH_SETTLE_SECONDS = 2.0
WATCH_HEARTBEAT_SECONDS = 15
# Dropbear answers the SSH keepalive (unlike RouterOS): a dead connection is noticed
# within about a minute and a half.
WATCH_KEEPALIVE = {"keepalive_interval": 30, "keepalive_count_max": 3}
MARK = "@@wap@@"
# One command, every section marked: board, info, then status and clients per radio.
POLL_COMMAND = (
    f"echo '{MARK} board'; ubus call system board; "
    f"echo '{MARK} info'; ubus call system info; "
    "for o in $(ubus list 'hostapd.*'); do "
    f'echo "{MARK} status $o"; ubus call "$o" get_status; '
    f'echo "{MARK} clients $o"; ubus call "$o" get_clients; '
    "done"
)
_MARK_LINE = re.compile(rf"^{re.escape(MARK)} (\w+)(?: (\S+))?\s*$")


def band_for_frequency(freq: Any) -> str | None:
    """2.4GHz / 5GHz / 6GHz from a channel frequency in MHz."""
    if not isinstance(freq, int) or isinstance(freq, bool):
        return None
    if 2400 <= freq < 2500:
        return "2.4GHz"
    if 4900 <= freq < 5925:
        return "5GHz"
    if 5925 <= freq < 7125:
        return "6GHz"
    return None


def _signed_dbm(value: Any) -> int | None:
    """Signal in dBm; hostapd adds it as a u32, so -52 may arrive as 4294967244."""
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if value >= 2**31:
        value -= 2**32
    return value if -120 <= value <= 0 else None


def split_sections(output: str) -> list[tuple[str, str | None, str]]:
    """(kind, ubus object, body) for each marked section of the poll output."""
    sections: list[tuple[str, str | None, str]] = []
    kind: str | None = None
    obj: str | None = None
    body: list[str] = []
    for line in output.splitlines():
        if match := _MARK_LINE.match(line):
            if kind is not None:
                sections.append((kind, obj, "\n".join(body)))
            kind, obj, body = match.group(1), match.group(2), []
        elif kind is not None:
            body.append(line)
    if kind is not None:
        sections.append((kind, obj, "\n".join(body)))
    return sections


def _json(body: str) -> dict[str, Any] | None:
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse_clients(get_clients: dict[str, Any], ssid: str | None) -> list[AssociatedClient]:
    """Associated and authorized stations of one hostapd interface."""
    band = band_for_frequency(get_clients.get("freq"))
    stations = get_clients.get("clients")
    if not isinstance(stations, dict):
        return []
    clients: list[AssociatedClient] = []
    for mac, station in stations.items():
        if not isinstance(station, dict):
            continue
        # hostapd reports both flags as true/false for every station; anything else is
        # not affirmatively connected.
        if station.get("assoc") is not True or station.get("authorized") is not True:
            continue
        try:
            normalized = normalize_mac(mac)
        except ValueError:
            continue
        clients.append(
            AssociatedClient(
                mac=normalized,
                ssid=ssid,
                band=band,
                signal=_signed_dbm(station.get("signal")),
            )
        )
    return clients


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= 64 and value.isprintable() else None


def parse_info(board: dict[str, Any] | None, info: dict[str, Any] | None) -> AccessPointInfo:
    """Name, model, firmware, uptime and memory from "system board" and "system info"."""
    board = board or {}
    info = info or {}
    release = board.get("release") if isinstance(board.get("release"), dict) else {}
    memory = info.get("memory") if isinstance(info.get("memory"), dict) else {}
    memory_percent = None
    total = memory.get("total")
    available = memory.get("available", memory.get("free"))
    if isinstance(total, int) and total > 0 and isinstance(available, int):
        memory_percent = max(0, min(100, round(100 * (total - available) / total)))
    uptime = info.get("uptime")
    return AccessPointInfo(
        name=_text(board.get("hostname")),
        model=_text(board.get("model")),
        firmware=_text(release.get("description")) or _text(release.get("version")),
        hardware=_text(board.get("board_name")),
        uptime_seconds=uptime if isinstance(uptime, int) and uptime >= 0 else None,
        memory_percent=memory_percent,
    )


def parse_poll(output: str) -> PollResult:
    """Clients of every radio, and what the AP reports about itself.

    Broken device details or status are optional, but a failed client-table read
    is an AP failure, not evidence that its clients left. So is a poll that found no
    hostapd interface at all: during a Wi-Fi reload hostapd's ubus objects are gone
    for a few seconds, and an AP with Wi-Fi off serves no clients either way.
    """
    board = info = None
    ssids: dict[str, str | None] = {}
    clients: list[AssociatedClient] = []
    valid_system = False
    valid_clients = False
    for kind, obj, body in split_sections(output):
        data = _json(body)
        if kind == "clients" and (
            data is None or not isinstance(data.get("clients"), dict)
        ):
            raise AccessPointError(f"Could not read client table for {obj}")
        if data is None:
            continue
        if kind in ("board", "info"):
            valid_system = True
        if kind == "board":
            board = data
        elif kind == "info":
            info = data
        elif kind == "status" and obj:
            ssids[obj] = _text(data.get("ssid"))
        elif kind == "clients" and obj:
            valid_clients = True
            clients.extend(parse_clients(data, ssids.get(obj)))
    if not valid_system and not valid_clients:
        raise AccessPointError("No valid ubus response from the access point")
    if not valid_clients:
        raise AccessPointError(
            "No Wi-Fi interface on ubus (hostapd not running: Wi-Fi off or restarting)"
        )
    return PollResult(clients, parse_info(board, info))



def _self_cleaning(command: str) -> str:
    """The command, stopped by the router once the SSH session is gone (see above)."""
    return (
        f"{command} & p=$!; trap 'kill $p 2>/dev/null' EXIT; trap exit HUP INT TERM PIPE; "
        f"while kill -0 $p 2>/dev/null; do sleep {WATCH_HEARTBEAT_SECONDS}; echo; done"
    )


@asynccontextmanager
async def _remote(conn: Any, command: str) -> AsyncIterator[Any]:
    """A long-running command; leaving closes its channel without waiting for the
    router, which ends the command at its next heartbeat."""
    process = await conn.create_process(
        _self_cleaning(command), encoding="utf-8", errors="replace"
    )
    try:
        yield process
    finally:
        process.close()


class _Arrivals:
    """Which hostapd notifications are a client joining; calls on_change() for them."""

    def __init__(
        self, on_change: Callable[[], None], on_leave: Callable[[], None] | None = None
    ) -> None:
        self._on_change = on_change
        self._on_leave = on_leave
        self._authorized_at: dict[str, float] = {}
        self._left_at: dict[str, float] = {}
        self._pending: set[asyncio.TimerHandle] = set()

    def close(self) -> None:
        """Cancel the delayed checks still pending (the watch is ending)."""
        for handle in self._pending:
            handle.cancel()
        self._pending.clear()

    def line(self, line: str) -> None:
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict) or len(event) != 1:
            return
        ((kind, data),) = event.items()
        if not isinstance(data, dict):
            return
        mac = str(data.get("address", ""))
        if kind in WATCH_LEAVE:
            self._left_at[mac] = time.monotonic()
            if self._on_leave is not None:
                _LOGGER.debug("hostapd: %s left (%s)", mac, kind)
                self._on_leave()
            return
        if kind not in (WATCH_ARRIVAL, WATCH_ASSOC):
            return
        if kind == WATCH_ARRIVAL:
            self._authorized_at[mac] = time.monotonic()
            _LOGGER.debug("hostapd: %s authorized on %s, polling", mac, data.get("ifname", "?"))
            self._on_change()
            return
        # "assoc": poll a moment later, unless "sta-authorized" came meanwhile or the
        # client left again.
        assoc_at = time.monotonic()
        handle: asyncio.TimerHandle | None = None

        def later() -> None:
            self._pending.discard(handle)  # type: ignore[arg-type]
            self._after_assoc(mac, assoc_at)

        handle = asyncio.get_running_loop().call_later(WATCH_ASSOC_DELAY, later)
        self._pending.add(handle)

    def _after_assoc(self, mac: str, assoc_at: float) -> None:
        if self._authorized_at.get(mac, -1.0) >= assoc_at:
            return
        if self._left_at.get(mac, -1.0) >= assoc_at:
            _LOGGER.debug("hostapd: %s associated and left again, no poll", mac)
            return
        _LOGGER.debug("hostapd: %s associated (no authorized event), polling", mac)
        self._on_change()


async def _watch_hostapd(
    conn: Any,
    host: str,
    on_change: Callable[[], None],
    on_leave: Callable[[], None] | None = None,
) -> None:
    """Follow hostapd's notifications on one connection, subscribing again whenever its
    ubus objects change (a Wi-Fi reload). Returns only by raising."""
    arrivals = _Arrivals(on_change, on_leave)
    object_changed = asyncio.Event()

    async with _remote(conn, WATCH_OBJECTS_COMMAND) as objects:

        async def follow_objects() -> None:
            while line := await objects.stdout.readline():
                if '"hostapd.' in line:
                    _LOGGER.debug("%s: hostapd objects changed: %s", host, line.strip())
                    object_changed.set()
            raise AccessPointError(f"{host}: the ubus object feed ended")

        watcher = asyncio.ensure_future(follow_objects())
        try:
            reloaded = False
            while True:
                object_changed.clear()
                listed = await asyncio.wait_for(
                    conn.run(WATCH_LIST_COMMAND, check=False, encoding="utf-8", errors="replace"),
                    COMMAND_TIMEOUT,
                )
                names = [name for name in (listed.stdout or "").split() if name.startswith("hostapd.")]
                if names:
                    _LOGGER.debug("%s: watching %s", host, ", ".join(names))
                    if reloaded:
                        on_change()  # clients rejoin after a reload; one may be missed
                    await _follow_until(conn, host, names, arrivals, object_changed, watcher)
                else:
                    _LOGGER.debug("%s: no hostapd interface on ubus, waiting for one", host)
                    await _first(object_changed.wait(), watcher)
                reloaded = True
                await asyncio.sleep(WATCH_SETTLE_SECONDS)
        finally:
            arrivals.close()
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)


async def _first(waiting: Any, watcher: asyncio.Future[None]) -> None:
    """Wait for `waiting`, or raise what ends the object watcher first."""
    task = asyncio.ensure_future(waiting)
    try:
        await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if watcher.done():
            watcher.result()  # raises
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _follow_until(
    conn: Any,
    host: str,
    names: list[str],
    arrivals: _Arrivals,
    object_changed: asyncio.Event,
    watcher: asyncio.Future[None],
) -> None:
    """Subscribe to these hostapd objects until they change; raise if the feed ends."""
    command = "ubus subscribe " + " ".join(names)
    async with _remote(conn, command) as process:

        async def follow() -> None:
            while line := await process.stdout.readline():
                if line.strip():
                    if '"probe"' not in line:
                        _LOGGER.debug("%s: hostapd event: %s", host, line.strip())
                    arrivals.line(line)
            if not object_changed.is_set():
                raise AccessPointError(f"{host}: the hostapd event feed ended")

        following = asyncio.ensure_future(follow())
        changed = asyncio.ensure_future(object_changed.wait())
        try:
            await asyncio.wait({following, changed, watcher}, return_when=asyncio.FIRST_COMPLETED)
            for task in (following, watcher):
                if task.done() and not task.cancelled() and task.exception():
                    raise task.exception()  # type: ignore[misc]
        finally:
            for task in (following, changed):
                task.cancel()
            await asyncio.gather(following, changed, return_exceptions=True)


@register
class OpenWrtSsh(AccessPointDriver):
    """OpenWrt (or any hostapd + ubus system) over SSH."""

    TYPE = "openwrt_ssh"
    NAME = "OpenWrt (SSH, ubus)"
    MANUFACTURER = "OpenWrt"
    SIGNAL_UNIT = "dBm"
    # OpenWrt has no location setting, and reports load averages rather than CPU %.
    REPORTS = frozenset(
        {"name", "model", "firmware", "hardware", "uptime_seconds", "memory_percent"}
    )
    SSH_POLICY = OPENWRT_SSH_POLICY
    FIELDS = (
        DriverField("host"),
        DriverField("port", default=22),
        DriverField("username", default="root"),
        DriverField("password", secret=True),
    )

    async def async_get_associated_clients(self) -> list[AssociatedClient]:
        """Clients associated to any radio of the AP."""
        return (await self.async_poll()).clients

    async def async_poll(self) -> PollResult:
        """Clients and device details, from one SSH command."""
        output = await self._run(POLL_COMMAND)
        if "ubus" in output and "not found" in output:
            raise AccessPointError(f"{self.config['host']}: ubus is not available")
        return parse_poll(output)

    WATCHES = True

    async def async_watch(
        self, on_change: Callable[[], None], on_leave: Callable[[], None] | None = None
    ) -> None:
        """Call on_change() when a client joins any hostapd interface of the AP, and
        on_leave() when one leaves (deauth or disassoc)."""
        host = self.config["host"]
        try:
            async with asyncssh.connect(
                host,
                port=int(self.config.get("port") or 22),
                username=self.config["username"],
                password=self.config["password"],
                **self.SSH_POLICY.connect_options(),
                **WATCH_KEEPALIVE,
            ) as conn:
                await _watch_hostapd(conn, host, on_change, on_leave)
        except asyncssh.PermissionDenied as err:
            raise AccessPointAuthError(f"{host}: login rejected") from err
        except (OSError, asyncio.TimeoutError, asyncssh.Error) as err:
            raise AccessPointError(f"{host}: {err!r}") from err

    async def _run(self, command: str) -> str:
        try:
            async with asyncssh.connect(
                self.config["host"],
                port=int(self.config.get("port") or 22),
                username=self.config["username"],
                password=self.config["password"],
                **self.SSH_POLICY.connect_options(),
            ) as conn:
                result = await asyncio.wait_for(
                    conn.run(command, check=False, encoding="utf-8", errors="replace"),
                    COMMAND_TIMEOUT,
                )
        except asyncssh.PermissionDenied as err:
            raise AccessPointAuthError(f"{self.config['host']}: login rejected") from err
        except (OSError, asyncio.TimeoutError, asyncssh.Error) as err:
            raise AccessPointError(f"{self.config['host']}: {err!r}") from err
        return f"{result.stdout or ''}\n{result.stderr or ''}"

