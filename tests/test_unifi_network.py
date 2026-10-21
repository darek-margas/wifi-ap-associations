"""Tests for the UniFi driver, with records shaped like a UniFi Network controller's.

The controller itself is a fake served over local HTTP (classic and UniFi OS).
"""

from __future__ import annotations

import asyncio

import pytest

from wifi_ap_associations import (
    DRIVERS,
    AccessPointAuthError,
    AccessPointError,
    AssociatedClient,
)
from wifi_ap_associations.unifi_network import (
    UnifiNetworkDriver,
    arrives_at,
    is_access_point,
    parse_clients,
    parse_device,
)

AP = "78:8A:20:00:00:10"
OTHER_AP = "78:8A:20:00:00:20"

DEVICE = {
    "mac": "78:8a:20:00:00:10",
    "type": "uap",
    "model": "U7PG2",
    "name": "Hallway AP",
    "version": "6.6.77.15402",
    "state": 1,
    "uptime": 86400,
    "system-stats": {"cpu": "7.3", "mem": "44.9", "uptime": "86400"},
}

CLIENTS = [
    {
        "mac": "5c:ad:ba:00:00:01",
        "is_wired": False,
        "ap_mac": "78:8a:20:00:00:10",
        "essid": "Home",
        "radio": "na",
        "signal": -58,
        "uptime": 5321,
        "_uptime_by_uap": 5300,
    },
    {
        "mac": "4c:fc:aa:00:00:02",
        "is_wired": False,
        "ap_mac": "78:8a:20:00:00:10",
        "essid": "Home IoT",
        "radio": "ng",
        "signal": -71,
        "uptime": 120,
    },
    # Associated to another access point.
    {"mac": "4c:fc:aa:00:00:03", "is_wired": False, "ap_mac": "78:8a:20:00:00:20",
     "essid": "Home", "radio": "6e", "signal": -50},
    # Wired, behind a switch; never a Wi-Fi association.
    {"mac": "00:11:32:00:00:04", "is_wired": True, "sw_mac": "78:8a:20:00:00:30"},
    # Malformed records are skipped.
    {"is_wired": False, "ap_mac": "78:8a:20:00:00:10"},
    {"mac": "nonsense", "is_wired": False, "ap_mac": "78:8a:20:00:00:10"},
]


def test_parse_clients() -> None:
    assert parse_clients(CLIENTS, AP) == [
        AssociatedClient("5C:AD:BA:00:00:01", "Home", "5GHz", -58, 5300),
        AssociatedClient("4C:FC:AA:00:00:02", "Home IoT", "2.4GHz", -71, 120),
    ]
    assert parse_clients(CLIENTS, OTHER_AP) == [
        AssociatedClient("4C:FC:AA:00:00:03", "Home", "6GHz", -50, None),
    ]


def test_parse_clients_positive_or_missing_signal() -> None:
    raw = [{"mac": "5c:ad:ba:00:00:01", "ap_mac": AP, "signal": 63, "radio": "xx"},
           {"mac": "5c:ad:ba:00:00:02", "ap_mac": AP},
           {"mac": "5c:ad:ba:00:00:03", "ap_mac": AP, "signal": 0}]
    first, second, third = parse_clients(raw, AP)
    assert (first.signal, first.band, first.ssid) == (-63, None, None)
    assert second.signal is None
    assert third.signal == 0  # zero is a value, not "unknown"


def test_parse_device() -> None:
    info = parse_device(DEVICE)
    assert info.name == "Hallway AP"
    assert info.model == "U7PG2"
    assert info.firmware == "6.6.77.15402"
    assert info.uptime_seconds == 86400
    assert (info.cpu_percent, info.memory_percent) == (7, 45)
    assert parse_device({"mac": AP}).name is None


def test_is_access_point() -> None:
    assert is_access_point(DEVICE)
    assert is_access_point({"type": "udm", "radio_table": [{"radio": "na"}]})
    assert not is_access_point({"type": "usw"})
    assert not is_access_point({"type": "ugw", "radio_table": []})


# --- instant arrivals: the controller's events ---

PHONE = "5c:ad:ba:00:00:01"


def test_arrival_events() -> None:
    joined = {"key": "EVT_WU_Connected", "user": PHONE, "ap": "78:8a:20:00:00:10"}
    assert arrives_at(joined, AP)
    assert not arrives_at(joined, OTHER_AP)
    roam = {"key": "EVT_WU_Roam", "user": PHONE, "ap_from": "78:8a:20:00:00:20",
            "ap_to": "78:8a:20:00:00:10"}
    assert arrives_at(roam, AP)
    assert not arrives_at(roam, OTHER_AP)  # it left that one: the poll will tell
    assert arrives_at({"key": "EVT_WU_RoamRadio", "user": PHONE, "ap": "78:8a:20:00:00:10"}, AP)
    assert arrives_at({"key": "EVT_WG_Connected", "guest": PHONE, "ap": "78:8a:20:00:00:10"}, AP)
    assert not arrives_at({"key": "EVT_WU_Disconnected", "user": PHONE, "ap": "78:8a:20:00:00:10"}, AP)
    assert not arrives_at({"key": "EVT_AP_Connected", "ap": "78:8a:20:00:00:10"}, AP)
    assert not arrives_at({"key": "EVT_WU_Connected", "ap": "garbage"}, AP)


def test_leave_events() -> None:
    from wifi_ap_associations.unifi_network import leaves

    assert leaves({"key": "EVT_WU_Disconnected", "user": PHONE, "ap": "78:8a:20:00:00:10"}, AP)
    assert leaves({"key": "EVT_WG_Disconnected", "guest": PHONE, "ap": "78:8a:20:00:00:10"}, AP)
    assert leaves({"key": "EVT_WU_Roam", "ap_from": "78:8a:20:00:00:10", "ap_to": "78:8a:20:00:00:20"}, AP)
    assert not leaves({"key": "EVT_WU_Roam", "ap_from": "78:8a:20:00:00:20", "ap_to": "78:8a:20:00:00:10"}, AP)
    assert not leaves({"key": "EVT_WU_Connected", "ap": "78:8a:20:00:00:10"}, AP)
    assert not leaves({"key": "EVT_WU_Disconnected", "ap": "78:8a:20:00:00:20"}, AP)


# --- the controller over HTTP: a fake UniFi Network controller ---


class FakeUnifi:
    """A UniFi Network controller: classic (/api/login) or UniFi OS (/api/auth/login,
    /proxy/network), one site, cookies, an optional dropped session and events."""

    def __init__(self, unifi_os: bool = False, password: str = "pw", events=(), starting: bool = False) -> None:
        self.unifi_os = unifi_os
        self.starting = starting  # the Network application answers "/" with its status while it starts
        self.password = password
        self.events = list(events)
        self.logins = 0
        self.requests: list[str] = []
        self.drop_session_once = False
        self.devices = [DEVICE, {"mac": "78:8a:20:00:00:20", "type": "usw", "name": "Switch"}]
        self.clients = CLIENTS

    def app(self):
        from aiohttp import web

        prefix = "/proxy/network" if self.unifi_os else ""
        routes = web.RouteTableDef()

        def logged_in(request) -> bool:
            return request.cookies.get("session") == "ok"

        @routes.get("/")
        async def root(request):
            if self.starting:
                return web.json_response({"meta": {"rc": "ok", "up": False}, "data": []})
            if self.unifi_os:
                return web.Response(text="UniFi OS")
            raise web.HTTPFound("/manage")

        @routes.post("/api/auth/login" if self.unifi_os else "/api/login")
        async def login(request):
            body = await request.json()
            if body.get("username") != "ro" or body.get("password") != self.password:
                return web.json_response({"meta": {"rc": "error", "msg": "api.err.Invalid"}}, status=400)
            self.logins += 1
            response = web.json_response({"meta": {"rc": "ok"}, "data": []})
            response.set_cookie("session", "ok")
            if self.unifi_os:
                response.headers["X-CSRF-Token"] = "csrf1"
            return response

        def guarded(handler):
            async def wrapped(request):
                self.requests.append(request.path)
                if self.drop_session_once:
                    self.drop_session_once = False
                    return web.json_response({"meta": {"rc": "error", "msg": "api.err.LoginRequired"}}, status=401)
                if not logged_in(request):
                    return web.json_response({"meta": {"rc": "error", "msg": "api.err.LoginRequired"}}, status=401)
                return web.json_response({"meta": {"rc": "ok"}, "data": handler(request)})
            return wrapped

        routes.get(f"{prefix}/api/self/sites")(guarded(lambda r: [{"name": "default", "desc": "Default"}, {"name": "x7k2", "desc": "Cabin"}]))
        routes.get(prefix + "/api/s/{site}/stat/device")(guarded(lambda r: self.devices))
        routes.get(prefix + "/api/s/{site}/stat/sta")(guarded(lambda r: self.clients if r.match_info["site"] == "default" else []))

        @routes.get(prefix + "/wss/s/{site}/events")
        async def events(request):
            if not logged_in(request):
                raise web.HTTPUnauthorized()
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_str("not json")
            await ws.send_json({"meta": {"rc": "ok", "message": "sta:sync"}, "data": [{}]})
            await ws.send_json({"meta": {"rc": "ok", "message": "events"}, "data": self.events})
            await ws.close()
            return ws

        app = web.Application()
        app.add_routes(routes)
        return app


def run_with(fake: FakeUnifi, monkeypatch, work):
    """Run work(settings) against the fake controller on a local port."""
    from aiohttp.test_utils import TestServer

    from wifi_ap_associations import http_controller

    monkeypatch.setattr(http_controller, "SCHEME", "http")

    async def main():
        server = TestServer(fake.app())
        await server.start_server()
        try:
            settings = {"host": "127.0.0.1", "port": server.port, "username": "ro", "password": "pw",
                        "site": "default", "ap_mac": AP}
            return await work(settings)
        finally:
            await server.close()

    return asyncio.run(main())


def test_registered() -> None:
    assert DRIVERS["unifi_network"] is UnifiNetworkDriver
    assert UnifiNetworkDriver.UNIQUE_FIELD == "ap_mac"
    assert UnifiNetworkDriver.SOURCE is None and UnifiNetworkDriver.OWN_DEVICE
    assert UnifiNetworkDriver.LISTS_ACCESS_POINTS and UnifiNetworkDriver.WATCHES
    keys = [f.key for f in UnifiNetworkDriver.FIELDS]
    assert keys == ["host", "port", "username", "password", "site", "verify_ssl", "ap_mac"]


def test_invalid_mac_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        UnifiNetworkDriver({"ap_mac": "not-a-mac"})


@pytest.mark.parametrize("unifi_os", [False, True])
def test_poll_through_the_controller(monkeypatch, unifi_os) -> None:
    fake = FakeUnifi(unifi_os=unifi_os)

    async def work(settings):
        driver = UnifiNetworkDriver(settings)
        try:
            first = await driver.async_poll()
            again = await driver.async_poll()  # within SHARE_SECONDS: the same read
            return first, again
        finally:
            await driver.async_close()

    first, again = run_with(fake, monkeypatch, work)
    assert [c.mac for c in first.clients] == ["5C:AD:BA:00:00:01", "4C:FC:AA:00:00:02"]
    assert first.info.name == "Hallway AP" and again == first
    assert fake.logins == 1
    prefix = "/proxy/network" if unifi_os else ""
    assert fake.requests.count(f"{prefix}/api/s/default/stat/sta") == 1


def test_site_by_description(monkeypatch) -> None:
    async def work(settings):
        driver = UnifiNetworkDriver({**settings, "site": "cabin"})
        try:
            return await driver.async_poll()
        finally:
            await driver.async_close()

    fake = FakeUnifi()
    result = run_with(fake, monkeypatch, work)
    assert result.clients == []
    assert "/api/s/x7k2/stat/sta" in fake.requests


def test_unknown_site_lists_the_sites(monkeypatch) -> None:
    async def work(settings):
        driver = UnifiNetworkDriver({**settings, "site": "office"})
        try:
            await driver.async_poll()
        finally:
            await driver.async_close()

    with pytest.raises(AccessPointError, match="no site 'office'.*Cabin \\(x7k2\\)"):
        run_with(FakeUnifi(), monkeypatch, work)


def test_wrong_password_is_an_auth_error(monkeypatch) -> None:
    async def work(settings):
        driver = UnifiNetworkDriver({**settings, "password": "nope"})
        try:
            await driver.async_poll()
        finally:
            await driver.async_close()

    with pytest.raises(AccessPointAuthError, match="rejected the user or password"):
        run_with(FakeUnifi(), monkeypatch, work)


def test_a_dropped_session_logs_in_again(monkeypatch) -> None:
    from wifi_ap_associations import http_controller

    monkeypatch.setattr(http_controller, "SHARE_SECONDS", 0)
    fake = FakeUnifi()

    async def work(settings):
        driver = UnifiNetworkDriver(settings)
        try:
            await driver.async_poll()
            fake.drop_session_once = True
            return await driver.async_poll()
        finally:
            await driver.async_close()

    result = run_with(fake, monkeypatch, work)
    assert len(result.clients) == 2 and fake.logins == 2


def test_an_ap_the_controller_does_not_have(monkeypatch) -> None:
    async def work(settings):
        driver = UnifiNetworkDriver({**settings, "ap_mac": "78:8A:20:00:00:99"})
        try:
            await driver.async_poll()
        finally:
            await driver.async_close()

    with pytest.raises(AccessPointError, match="not a device of site default"):
        run_with(FakeUnifi(), monkeypatch, work)


def test_settings_from_before_0_12_ask_for_the_login() -> None:
    # Saved when the driver read through Home Assistant's UniFi integration: no host.
    driver = UnifiNetworkDriver({"ap_mac": AP})
    with pytest.raises(AccessPointAuthError, match="Reconfigure"):
        asyncio.run(driver.async_poll())


def test_list_access_points(monkeypatch) -> None:
    found = run_with(FakeUnifi(unifi_os=True), monkeypatch, UnifiNetworkDriver.async_list_access_points)
    assert found == [(AP, "Hallway AP (U7PG2, 78:8A:20:00:00:10)")]  # not the switch


def test_watch_shares_one_event_stream(monkeypatch) -> None:
    fake = FakeUnifi(events=[
        {"key": "EVT_WU_Connected", "user": PHONE, "ap": "78:8a:20:00:00:10"},
        {"key": "EVT_WU_Disconnected", "user": PHONE, "ap": "78:8a:20:00:00:20"},
        {"key": "EVT_WU_Roam", "user": PHONE, "ap_from": "78:8a:20:00:00:10", "ap_to": "78:8a:20:00:00:20"},
    ])
    seen: list[str] = []

    async def work(settings):
        hall = UnifiNetworkDriver(settings)
        den = UnifiNetworkDriver({**settings, "ap_mac": OTHER_AP})
        try:
            results = await asyncio.gather(
                hall.async_watch(lambda: seen.append("hall change"), lambda: seen.append("hall leave")),
                den.async_watch(lambda: seen.append("den change"), lambda: seen.append("den leave")),
                return_exceptions=True,
            )
            return results
        finally:
            await hall.async_close()
            await den.async_close()

    results = run_with(fake, monkeypatch, work)
    # The stream ends (the fake closes it): each watch says so, to be retried.
    assert all(isinstance(r, AccessPointError) for r in results)
    assert sorted(seen) == ["den change", "den leave", "hall change", "hall leave"]
    assert fake.logins == 1


def test_a_starting_controller_is_not_taken_for_unifi_os(monkeypatch) -> None:
    async def work(settings):
        driver = UnifiNetworkDriver(settings)
        try:
            return await driver.async_poll()
        finally:
            await driver.async_close()

    # Seen on the Network application 10.0: "/" answers 200 with JSON while it starts.
    assert len(run_with(FakeUnifi(starting=True), monkeypatch, work).clients) == 2
