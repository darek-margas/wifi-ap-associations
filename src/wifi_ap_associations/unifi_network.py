"""UniFi access points, read through their UniFi Network controller.

The driver logs in to the UniFi Network controller (UniFi OS console, Cloud Key or
self-hosted Network application) with a local account and asks it which wireless
clients are associated to one of its access points. One login serves all access points
of a controller (see http_controller). A UniFi OS console is told from a self-hosted
application by its answer at "/" (200 rather than a redirect): its login is
/api/auth/login and the Network API is under /proxy/network.

The controller's active client list (stat/sta) holds only currently connected
clients, one record per client:

    {"mac": "5c:ad:ba:00:00:01", "is_wired": false, "ap_mac": "78:8a:20:00:00:10",
     "essid": "Home", "radio": "na", "signal": -58, "uptime": 5321, ...}

and each access point is a device record (stat/device, "type": "uap", or a console or
gateway with built-in radios), with "state" 1 while it is connected to the controller.

The controller also pushes events over its websocket (wss/s/<site>/events), e.g. a
client joining

    {"key": "EVT_WU_Connected", "user": "5c:ad:ba:00:00:01", "ap": "78:8a:20:00:00:10",
     "ssid": "Home", "radio": "na", ...}

or roaming ({"key": "EVT_WU_Roam", "ap_from": ..., "ap_to": ...}); guests have "EVT_WG_..."
keys and "guest" instead of "user". Those are the instant arrivals (async_watch); one
websocket per controller serves the watches of all its access points.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

from . import (
    AccessPointAuthError,
    AccessPointDriver,
    AccessPointError,
    AccessPointInfo,
    AssociatedClient,
    DriverField,
    PollResult,
    http_controller,
    normalize_mac,
    register,
)
from .http_controller import (
    ControllerPool,
    HttpController,
    LoggedOut,
    retry_once_logged_out,
)

CONF_AP_MAC = "ap_mac"

# The controller settings, shared with the Omada driver.
CONTROLLER_FIELDS = (
    DriverField("host"),
    DriverField("port", default=443),
    DriverField("username"),
    DriverField("password", secret=True),
)
VERIFY_SSL = DriverField("verify_ssl", default="no", choices=("no", "yes"))

# UniFi radio codes: ng = 2.4 GHz, na = 5 GHz, 6e = 6 GHz, ad = 60 GHz.
RADIO_BANDS = {"ng": "2.4GHz", "na": "5GHz", "6e": "6GHz", "ad": "60GHz"}

DEVICE_STATES = {
    0: "disconnected",
    1: "connected",
    2: "pending adoption",
    4: "upgrading",
    5: "provisioning",
    6: "heartbeat missed",
    7: "adopting",
    9: "adoption failed",
    10: "isolated",
    11: "adoption failed",
}
STATE_CONNECTED = 1

# Controller events of a client leaving the access point (clients and guests).
LEAVE_EVENTS = frozenset({"EVT_WU_Disconnected", "EVT_WG_Disconnected"})

# Controller events that put a client on an access point: joining, roaming to it, and
# moving between its radios (clients and guests).
ARRIVAL_EVENTS = frozenset({
    "EVT_WU_Connected", "EVT_WU_Roam", "EVT_WU_RoamRadio",
    "EVT_WG_Connected", "EVT_WG_Roam", "EVT_WG_RoamRadio",
})

NEEDS_LOGIN = (
    "this access point needs its controller's address and login since 0.12: open its "
    "settings (Reconfigure) and enter them"
)

_LOGGER = logging.getLogger(__name__)


def leaves(event: Mapping[str, Any], ap_mac: str) -> bool:
    """Whether a controller event is a client leaving this access point (disconnecting,
    or roaming away from it)."""
    key = event.get("key")
    if key in LEAVE_EVENTS:
        where = event.get("ap")
    elif key in ("EVT_WU_Roam", "EVT_WG_Roam"):
        where = event.get("ap_from")
    else:
        return False
    try:
        return normalize_mac(where or "") == ap_mac
    except ValueError:
        return False


def arrives_at(event: Mapping[str, Any], ap_mac: str) -> bool:
    """Whether a controller event puts a client on this access point."""
    if event.get("key") not in ARRIVAL_EVENTS:
        return False
    try:
        # A roam names "ap_from" and "ap_to"; a join and a radio change name "ap".
        return normalize_mac(event.get("ap_to") or event.get("ap") or "") == ap_mac
    except ValueError:
        return False


def is_access_point(device: Mapping[str, Any]) -> bool:
    """Whether a UniFi device record has Wi-Fi radios (an AP, or a console with radios)."""
    return device.get("type") == "uap" or bool(device.get("radio_table"))


def _int(value: Any) -> int | None:
    """An int from a number or numeric text; None for anything else."""
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return None


def parse_clients(
    raw_clients: list[Mapping[str, Any]], ap_mac: str
) -> list[AssociatedClient]:
    """The wireless clients associated to the access point with this MAC."""
    clients: list[AssociatedClient] = []
    for raw in raw_clients:
        if raw.get("is_wired", False):
            continue
        try:
            if normalize_mac(raw.get("ap_mac") or "") != ap_mac:
                continue
            mac = normalize_mac(raw["mac"])
        except (KeyError, ValueError):
            continue
        signal = _int(raw.get("signal"))
        clients.append(
            AssociatedClient(
                mac=mac,
                ssid=raw.get("essid") or None,
                band=RADIO_BANDS.get(raw.get("radio") or ""),
                # Some controllers report signal as a positive number; dBm is negative.
                signal=-abs(signal) if signal is not None else None,
                connected_seconds=_int(raw.get("_uptime_by_uap", raw.get("uptime"))),
            )
        )
    return clients


def parse_device(device: Mapping[str, Any]) -> AccessPointInfo:
    """What the controller reports about the access point."""
    stats = device.get("system-stats") or {}
    return AccessPointInfo(
        name=device.get("name") or None,
        model=device.get("model") or None,
        firmware=device.get("version") or None,
        uptime_seconds=_int(device.get("uptime")),
        cpu_percent=_int(stats.get("cpu")),
        memory_percent=_int(stats.get("mem")),
    )


def device_label(device: Mapping[str, Any], mac: str) -> str:
    """How an access point is listed when picking one."""
    name = device.get("name") or mac
    model = device.get("model")
    return f"{name} ({model}, {mac})" if model else f"{name} ({mac})"


def pick_site(sites: list[Mapping[str, Any]], wanted: str) -> str:
    """The site's short name (as in the controller's URLs), from that or its description."""
    wanted = wanted.strip() or "default"
    for site in sites:
        if str(site.get("name") or "") == wanted:
            return wanted
    for site in sites:
        if str(site.get("desc") or "").casefold() == wanted.casefold():
            return str(site["name"])
    known = ", ".join(
        f"{s.get('desc') or s.get('name')} ({s.get('name')})" for s in sites
    ) or "none this user may see"
    raise AccessPointError(f"no site {wanted!r} on the controller; its sites: {known}")


UnifiData = tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]  # devices, clients


class UnifiController(HttpController[UnifiData]):
    """A session with one UniFi Network controller."""

    def __init__(self, settings: Mapping[str, Any]) -> None:
        super().__init__(settings)
        self.unifi_os: bool | None = None
        self.csrf: str | None = None
        self.site_name: str | None = None
        self._watchers: set[Callable[[Mapping[str, Any]], None]] = set()
        self._events: asyncio.Task[None] | None = None

    @property
    def prefix(self) -> str:
        return "/proxy/network" if self.unifi_os else ""

    def _headers(self) -> dict[str, str] | None:
        return {"x-csrf-token": self.csrf} if self.csrf else None

    async def login(self) -> None:
        if self.unifi_os is None:
            status, body, _headers = await self.request("GET", "/", allow_redirects=False)
            # A UniFi OS console answers its own page; the Network application redirects
            # (or, while it starts, answers its status as JSON).
            self.unifi_os = status == 200 and not (isinstance(body, dict) and "meta" in body)
        try:
            await self._login()
        except AccessPointError:
            self.unifi_os = None  # tell again next time (e.g. it was still starting)
            raise

    async def _login(self) -> None:
        if self.unifi_os:
            path = "/api/auth/login"
            payload = {"username": self.username, "password": self.password, "rememberMe": True}
        else:
            path = "/api/login"
            payload = {"username": self.username, "password": self.password, "remember": True}
        status, body, headers = await self.request("POST", path, json=payload)
        if status in (400, 401, 403):
            raise AccessPointAuthError(
                f"{self.host}: the controller rejected the user or password (use a local "
                "account, not a UniFi cloud account with two-factor sign-in)"
            )
        if status == 429:
            raise AccessPointError(f"{self.host}: the controller refuses logins for a while (too many)")
        if status != 200:
            raise AccessPointError(f"{self.host}: login failed (HTTP {status})")
        self.csrf = headers.get("x-csrf-token") or self.csrf
        status, body, _headers = await self.request(
            "GET", f"{self.prefix}/api/self/sites", headers=self._headers()
        )
        if status != 200 or not isinstance(body, dict):
            raise AccessPointError(f"{self.host}: can't list the controller's sites (HTTP {status})")
        self.site_name = pick_site(body.get("data") or [], self.site)

    async def get(self, path: str) -> list[Mapping[str, Any]]:
        """A site API call's data list (logged in, once more after a dropped session)."""

        async def call() -> list[Mapping[str, Any]]:
            status, body, headers = await self.request(
                "GET", f"{self.prefix}/api/s/{self.site_name}/{path}", headers=self._headers()
            )
            meta = body.get("meta") if isinstance(body, dict) else None
            if status == 401 or (meta or {}).get("msg") == "api.err.LoginRequired":
                raise LoggedOut
            if status != 200 or not isinstance(body, dict):
                raise AccessPointError(f"{self.host}: {path} failed (HTTP {status})")
            if (meta or {}).get("rc", "ok") != "ok":
                raise AccessPointError(f"{self.host}: {path}: {(meta or {}).get('msg')}")
            self.csrf = headers.get("x-updated-csrf-token") or self.csrf
            return list(body.get("data") or [])

        await self.ensure_login()
        return await retry_once_logged_out(self, call)

    async def fetch(self) -> UnifiData:
        devices, clients = await asyncio.gather(self.get("stat/device"), self.get("stat/sta"))
        return devices, clients

    async def close(self) -> None:
        if self._events is not None:
            self._events.cancel()
            await asyncio.gather(self._events, return_exceptions=True)
            self._events = None
        await super().close()

    async def watch(self, callback: Callable[[Mapping[str, Any]], None]) -> None:
        """Hand each controller event to callback until cancelled; raises AccessPointError
        when the event stream ends. All watchers share one websocket."""
        self._watchers.add(callback)
        try:
            if self._events is None or self._events.done():
                self._events = asyncio.create_task(self._run_events())
            await asyncio.shield(self._events)
            raise AccessPointError(f"{self.host}: the controller closed its event stream")
        finally:
            self._watchers.discard(callback)
            if not self._watchers and self._events is not None and not self._events.done():
                self._events.cancel()

    async def _run_events(self) -> None:
        import aiohttp

        await self.ensure_login()
        scheme = "wss" if http_controller.SCHEME == "https" else "ws"
        url = f"{scheme}://{self.host}:{self.port}{self.prefix}/wss/s/{self.site_name}/events"
        session = await self.session()
        try:
            async with session.ws_connect(
                url,
                ssl=self.ssl_context(),
                heartbeat=30,
                params={"clients": "v2"} if self.unifi_os else None,
            ) as ws:
                _LOGGER.debug("UniFi %s: event stream open", self.host)
                async for message in ws:
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        body = json.loads(message.data)
                    except ValueError:
                        continue
                    if (body.get("meta") or {}).get("message") != "events":
                        continue
                    for event in body.get("data") or []:
                        if event.get("key") in ARRIVAL_EVENTS | LEAVE_EVENTS:
                            # The poll this asks for (or a fast departure's deadline poll)
                            # must see the change: not a read from before it (shared for
                            # SHARE_SECONDS otherwise).
                            self.invalidate()
                        for watcher in list(self._watchers):
                            watcher(event)
        except aiohttp.WSServerHandshakeError as err:
            if err.status == 401:
                self.logged_out()
            raise AccessPointError(f"{self.host}: event stream refused (HTTP {err.status})") from err
        except (aiohttp.ClientError, TimeoutError, OSError) as err:
            raise AccessPointError(f"{self.host}: event stream lost ({err or type(err).__name__})") from err


_pool: ControllerPool[UnifiData] = ControllerPool(UnifiController)


def has_controller_settings(config: Mapping[str, Any]) -> bool:
    """Settings saved before the driver logged in itself have no controller address."""
    return bool(str(config.get("host") or "").strip())


@register
class UnifiNetworkDriver(AccessPointDriver):
    """One UniFi access point, read through its UniFi Network controller."""

    # Stable identifier: settings saved by earlier versions (which read through Home
    # Assistant's UniFi integration) keep it and only need the controller login added.
    TYPE = "unifi_network"
    NAME = "UniFi Network controller"
    # As the UniFi integration names it.
    MANUFACTURER = "Ubiquiti Networks"
    FIELDS = (
        *CONTROLLER_FIELDS,
        DriverField("site", default="default"),
        VERIFY_SSL,
        DriverField(CONF_AP_MAC),
    )
    SIGNAL_UNIT = "dBm"
    UNIQUE_FIELD = CONF_AP_MAC
    MAC_FIELDS = (CONF_AP_MAC,)
    LISTS_ACCESS_POINTS = True
    # Login and two requests to a local controller.
    POLL_TIMEOUT = 30
    WATCHES = True

    def device_mac(self) -> str | None:
        """The AP's MAC (another integration's device for it may carry an area)."""
        return self.ap_mac

    def __init__(self, config: dict[str, Any]) -> None:
        """Store the settings."""
        super().__init__(config)
        self.ap_mac = normalize_mac(config[CONF_AP_MAC])

    def _controller(self) -> UnifiController:
        if not has_controller_settings(self.config):
            raise AccessPointAuthError(NEEDS_LOGIN)
        return _pool.acquire(self.config, self)  # type: ignore[return-value]

    @classmethod
    async def async_list_access_points(cls, config: dict[str, Any]) -> list[tuple[str, str]]:
        """The controller's access points: (MAC, "name (model, MAC)")."""
        controller = UnifiController(config)
        try:
            await controller.ensure_login()
            devices = await controller.get("stat/device")
        finally:
            await controller.close()
        found = []
        for device in devices:
            if not is_access_point(device):
                continue
            try:
                mac = normalize_mac(device.get("mac") or "")
            except ValueError:
                continue
            found.append((mac, device_label(device, mac)))
        return sorted(found, key=lambda item: item[1].casefold())

    async def async_close(self) -> None:
        """Release the controller session (closed with its last access point)."""
        await _pool.release(self.config, self)

    async def async_get_associated_clients(self) -> list[AssociatedClient]:
        """Clients associated to this access point."""
        return (await self.async_poll()).clients

    async def async_poll(self) -> PollResult:
        """Clients and device details, as the controller reports them."""
        devices, clients = await self._controller().read()
        device = next(
            (d for d in devices if _same_mac(d.get("mac"), self.ap_mac)), None
        )
        if device is None:
            raise AccessPointError(
                f"{self.ap_mac} is not a device of site {self.config.get('site') or 'default'} "
                "on this controller"
            )
        state = device.get("state")
        if state != STATE_CONNECTED:
            raise AccessPointError(
                f"the controller reports the access point as {DEVICE_STATES.get(state, state)}"
            )
        return PollResult(parse_clients(clients, self.ap_mac), parse_device(device))

    async def async_watch(
        self, on_change: Callable[[], None], on_leave: Callable[[], None] | None = None
    ) -> None:
        """Call on_change() when the controller reports a client joining this access
        point, roaming to it or changing radio on it; on_leave() when one leaves it."""

        def event(raw: Mapping[str, Any]) -> None:
            if arrives_at(raw, self.ap_mac):
                _LOGGER.debug(
                    "UniFi %s: %s for %s, polling",
                    self.ap_mac,
                    raw.get("key"),
                    raw.get("user") or raw.get("guest"),
                )
                on_change()
            elif on_leave is not None and leaves(raw, self.ap_mac):
                _LOGGER.debug(
                    "UniFi %s: %s for %s, a client left",
                    self.ap_mac,
                    raw.get("key"),
                    raw.get("user") or raw.get("guest"),
                )
                on_leave()

        await self._controller().watch(event)


def _same_mac(value: Any, mac: str) -> bool:
    try:
        return normalize_mac(str(value or "")) == mac
    except ValueError:
        return False
