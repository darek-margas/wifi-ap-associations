"""TP-Link Omada access points, read through their Omada controller.

The driver logs in to the Omada controller (software controller, hardware OC200/OC300,
or a router with a built-in controller; version 5 or later) with a local account and
asks it which wireless clients are connected to one of its access points. One login
serves all access points of a controller (see http_controller). The web API it uses:

    GET  /api/info                                   -> result.omadacId
    POST /<omadacId>/api/v2/login  {username, password}  -> result.token
    GET  /<omadacId>/api/v2/users/current            -> result.privilege.sites [{name, key}]
                                                        (empty with "all": true, then:)
    GET  /<omadacId>/api/v2/sites?currentPage=..     -> result.data [{name, id}]
    GET  /<omadacId>/api/v2/sites/<key>/devices      -> result [device, ...]
    GET  /<omadacId>/api/v2/sites/<key>/clients?filters.active=true&currentPage=..
                                                     -> result.data [client, ...]

with the token in a Csrf-Token header and the session cookie; every answer has an
errorCode, 0 for success. Client records look like (as in tplink-omada-client 1.5):

    {"mac": "2E-DC-E1-C4-37-D3", "wireless": true, "active": true,
     "apMac": "C0-C9-E3-4B-AF-0E", "apName": "Spare Room", "ssid": "Home",
     "radioId": 1, "channel": 44, "rssi": -63, "uptime": 2091, ...}

radioId is 0 for 2.4 GHz, 1 and 2 for 5 GHz (tri-band APs have two), 3 for 6 GHz.
Each access point is a device record ("type": "ap") with "statusCategory" 1 while it is
connected to the controller, and "uptimeLong" in seconds ("uptime" is text).

Experimental: written from that client library's requests and Home Assistant's test
fixtures, and checked against a software controller without access points; not yet
run with real access points.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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
from .http_controller import (
    ControllerPool,
    HttpController,
    LoggedOut,
    retry_once_logged_out,
)
from .unifi_network import (
    CONTROLLER_FIELDS,
    NEEDS_LOGIN,
    VERIFY_SSL,
    has_controller_settings,
)

CONF_AP_MAC = "ap_mac"

RADIO_BANDS = {0: "2.4GHz", 1: "5GHz", 2: "5GHz", 3: "6GHz"}

DEVICE_STATES = {
    0: "disconnected",
    1: "connected",
    2: "pending",
    3: "heartbeat missed",
    4: "isolated",
}
STATE_CONNECTED = 1
PAGE_SIZE = 1000

# errorCode of a login that the controller refused for its user or password.
LOGIN_REJECTED = {-30109, -30110, -30111, -30112}


def is_access_point(device: Mapping[str, Any]) -> bool:
    """Whether an Omada device record is an access point (an EAP)."""
    return device.get("type") == "ap"


def _int(value: Any) -> int | None:
    """An int from a number or numeric text; None for anything else."""
    if isinstance(value, bool):
        return None
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return None


def parse_clients(raw_clients: list[Mapping[str, Any]], ap_mac: str) -> list[AssociatedClient]:
    """The wireless clients connected to the access point with this MAC."""
    clients: list[AssociatedClient] = []
    for raw in raw_clients:
        if raw.get("wireless") is not True or raw.get("active") is False:
            continue
        try:
            if normalize_mac(raw.get("apMac") or "") != ap_mac:
                continue
            mac = normalize_mac(raw["mac"])
        except (KeyError, ValueError):
            continue
        signal = _int(raw.get("rssi"))
        clients.append(
            AssociatedClient(
                mac=mac,
                ssid=raw.get("ssid") or None,
                band=RADIO_BANDS.get(_int(raw.get("radioId"))),  # type: ignore[arg-type]
                # dBm is negative; guard against a controller reporting it positive.
                signal=-abs(signal) if signal is not None else None,
                connected_seconds=_int(raw.get("uptime")),
            )
        )
    return clients


def parse_device(device: Mapping[str, Any]) -> AccessPointInfo:
    """What the controller reports about the access point."""
    return AccessPointInfo(
        name=device.get("name") or None,
        model=device.get("model") or None,
        firmware=device.get("firmwareVersion") or device.get("version") or None,
        hardware=device.get("hwVersion") or None,
        uptime_seconds=_int(device.get("uptimeLong")),
        cpu_percent=_int(device.get("cpuUtil")),
        memory_percent=_int(device.get("memUtil")),
    )


def pick_site(sites: list[Mapping[str, Any]], wanted: str) -> str:
    """The site's id, by its name (as shown in the controller) or id."""
    wanted = wanted.strip() or "Default"
    for site in sites:
        if wanted.casefold() in (str(site.get("name") or "").casefold(), str(site.get("id") or "")):
            return str(site["id"])
    known = ", ".join(str(s.get("name")) for s in sites) or "none this user may see"
    raise AccessPointError(f"no site {wanted!r} on the controller; its sites: {known}")


OmadaData = tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]  # devices, clients


class OmadaController(HttpController[OmadaData]):
    """A session with one Omada controller."""

    def __init__(self, settings: Mapping[str, Any]) -> None:
        super().__init__(settings)
        self.controller_id: str | None = None
        self.token: str | None = None
        self.site_id: str | None = None

    def _headers(self) -> dict[str, str] | None:
        return {"Csrf-Token": self.token} if self.token else None

    def _api(self, path: str) -> str:
        return f"/{self.controller_id}/api/v2{path}"

    async def login(self) -> None:
        status, body, _headers = await self.request("GET", "/api/info")
        result = body.get("result") if isinstance(body, dict) else None
        if status != 200 or not isinstance(result, dict) or not result.get("omadacId"):
            raise AccessPointError(
                f"{self.host}:{self.port} doesn't answer as an Omada controller (version 5 "
                f"or later; HTTP {status})"
            )
        self.controller_id = str(result["omadacId"])
        status, body, _headers = await self.request(
            "POST",
            self._api("/login"),
            json={"username": self.username, "password": self.password},
        )
        code = body.get("errorCode") if isinstance(body, dict) else None
        if status in (401, 403) or code in LOGIN_REJECTED:
            raise AccessPointAuthError(f"{self.host}: the controller rejected the user or password")
        token = ((body or {}).get("result") or {}).get("token") if code == 0 else None
        if status != 200 or not token:
            message = (body or {}).get("msg") if isinstance(body, dict) else None
            raise AccessPointError(f"{self.host}: login failed ({message or f'HTTP {status}'})")
        self.token = str(token)
        current = await self._result("/users/current")
        privilege = (current.get("privilege") or {}) if isinstance(current, dict) else {}
        sites = [
            {"name": site.get("name"), "id": site.get("key")} for site in privilege.get("sites") or []
        ]
        if privilege.get("all") or not sites:
            # An account with every site (the owner, an administrator for all sites)
            # gets an empty list there; the site list itself has them (seen on 5.15).
            result = await self._result("/sites", {"currentPage": 1, "currentPageSize": PAGE_SIZE})
            sites = list((result or {}).get("data") or []) if isinstance(result, dict) else []
        self.site_id = pick_site(sites, self.site)

    async def _result(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """An API call's result; LoggedOut when the controller wants a new login."""
        status, body, _headers = await self.request(
            "GET", self._api(path), params=params, headers=self._headers()
        )
        if status == 401 or (isinstance(body, dict) and body.get("errorCode") in (-1200, -1201)):
            raise LoggedOut
        if status != 200 or not isinstance(body, dict):
            raise AccessPointError(f"{self.host}: {path} failed (HTTP {status})")
        if body.get("errorCode") != 0:
            # Any other error may also be an expired session: one new login decides.
            raise LoggedOut(f"{path}: {body.get('msg') or body.get('errorCode')}")
        return body.get("result")

    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        await self.ensure_login()
        return await retry_once_logged_out(self, lambda: self._result(path, params))

    async def clients(self) -> list[Mapping[str, Any]]:
        await self.ensure_login()  # the site id comes with the login
        found: list[Mapping[str, Any]] = []
        page = 1
        while True:
            result = await self.get(
                f"/sites/{self.site_id}/clients",
                {"currentPage": page, "currentPageSize": PAGE_SIZE, "filters.active": "true"},
            )
            rows = (result or {}).get("data") or [] if isinstance(result, dict) else []
            found += rows
            total = _int((result or {}).get("totalRows")) if isinstance(result, dict) else None
            if not rows or total is None or len(found) >= total:
                return found
            page += 1

    async def devices(self) -> list[Mapping[str, Any]]:
        await self.ensure_login()  # the site id comes with the login
        result = await self.get(f"/sites/{self.site_id}/devices")
        return list(result or []) if isinstance(result, list) else []

    async def fetch(self) -> OmadaData:
        return await self.devices(), await self.clients()


_pool: ControllerPool[OmadaData] = ControllerPool(OmadaController)


@register
class OmadaNetworkDriver(AccessPointDriver):
    """One TP-Link Omada access point, read through its Omada controller."""

    # Stable identifier: settings saved by earlier versions (which read through Home
    # Assistant's TP-Link Omada integration) keep it and only need the login added.
    TYPE = "omada_network"
    NAME = "TP-Link Omada controller"
    MANUFACTURER = "TP-Link"
    FIELDS = (
        *CONTROLLER_FIELDS,
        DriverField("site", default="Default"),
        VERIFY_SSL,
        DriverField(CONF_AP_MAC),
    )
    SIGNAL_UNIT = "dBm"
    UNIQUE_FIELD = CONF_AP_MAC
    MAC_FIELDS = (CONF_AP_MAC,)
    LISTS_ACCESS_POINTS = True
    POLL_TIMEOUT = 30
    EXPERIMENTAL = True
    # Omada has no location setting the controller reports.
    REPORTS = frozenset(
        {"name", "model", "firmware", "hardware", "uptime_seconds", "cpu_percent", "memory_percent"}
    )

    def device_mac(self) -> str | None:
        """The AP's MAC (another integration's device for it may carry an area)."""
        return self.ap_mac

    def __init__(self, config: dict[str, Any]) -> None:
        """Store the settings."""
        super().__init__(config)
        self.ap_mac = normalize_mac(config[CONF_AP_MAC])

    def _controller(self) -> OmadaController:
        if not has_controller_settings(self.config):
            raise AccessPointAuthError(NEEDS_LOGIN)
        return _pool.acquire(self.config, self)  # type: ignore[return-value]

    @classmethod
    async def async_list_access_points(cls, config: dict[str, Any]) -> list[tuple[str, str]]:
        """The controller's access points: (MAC, "name (model, MAC)")."""
        controller = OmadaController(config)
        try:
            devices = await controller.devices()
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
            name = device.get("name") or mac
            model = device.get("model")
            found.append((mac, f"{name} ({model}, {mac})" if model else f"{name} ({mac})"))
        return sorted(found, key=lambda item: item[1].casefold())

    async def async_close(self) -> None:
        """Release the controller session (closed with its last access point)."""
        await _pool.release(self.config, self)

    async def async_get_associated_clients(self) -> list[AssociatedClient]:
        """Clients connected to this access point."""
        return (await self.async_poll()).clients

    async def async_poll(self) -> PollResult:
        """Clients and device details, as the controller reports them."""
        devices, clients = await self._controller().read()
        device = next((d for d in devices if _same_mac(d.get("mac"), self.ap_mac)), None)
        if device is None:
            raise AccessPointError(
                f"{self.ap_mac} is not a device of site {self.config.get('site') or 'Default'} "
                "on this controller"
            )
        state = device.get("statusCategory")
        if state != STATE_CONNECTED:
            raise AccessPointError(
                f"the controller reports the access point as {DEVICE_STATES.get(state, state)}"
            )
        return PollResult(parse_clients(clients, self.ap_mac), parse_device(device))


def _same_mac(value: Any, mac: str) -> bool:
    try:
        return normalize_mac(str(value or "")) == mac
    except ValueError:
        return False
