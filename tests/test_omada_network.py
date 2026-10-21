"""Tests for the TP-Link Omada driver.

Client records are Home Assistant's tplink_omada test fixture (connected-clients.json),
trimmed; the access point record follows its devices.json with "type": "ap".
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
from wifi_ap_associations.omada_network import (
    OmadaNetworkDriver,
    is_access_point,
    parse_clients,
    parse_device,
)

AP = "C0:C9:E3:4B:AF:0E"

DEVICE = {
    "mac": "C0-C9-E3-4B-AF-0E", "type": "ap", "name": "Spare Room", "model": "EAP653",
    "firmwareVersion": "1.0.12 Build 20230203 Rel.36545", "hwVersion": "1.0",
    "uptime": "1day(s) 8h 27m 26s", "uptimeLong": 116846, "cpuUtil": 10, "memUtil": 20,
    "status": 14, "statusCategory": 1,
}

CLIENTS = [
    {"mac": "16-32-50-ED-FB-15", "wireless": True, "active": True, "ssid": "OFFICE_SSID",
     "apName": "Office", "apMac": "E8-48-B8-7E-C7-1A", "radioId": 0, "channel": 1,
     "rssi": -65, "signalLevel": 62, "uptime": 621441},
    {"mac": "2E-DC-E1-C4-37-D3", "wireless": True, "active": True, "ssid": "ROAMING_SSID",
     "apName": "Spare Room", "apMac": "C0-C9-E3-4B-AF-0E", "radioId": 1, "channel": 44,
     "rssi": -63, "signalLevel": 67, "uptime": 2091},
    {"mac": "2C-71-FF-ED-34-83", "wireless": True, "active": True, "ssid": "ROAMING_SSID",
     "apName": "Living Room", "apMac": "C0-C9-E3-4B-A7-FE", "radioId": 1, "channel": 36,
     "rssi": -67, "signalLevel": 57, "uptime": 621461},
    # Not in the fixture: a wired client, an inactive one, a 6 GHz one, a garbled one.
    {"mac": "AA-BB-CC-00-00-01", "wireless": False, "active": True},
    {"mac": "AA-BB-CC-00-00-02", "wireless": True, "active": False, "apMac": "C0-C9-E3-4B-AF-0E"},
    {"mac": "AA-BB-CC-00-00-03", "wireless": True, "active": True, "apMac": "C0-C9-E3-4B-AF-0E",
     "ssid": "Home6", "radioId": 3, "rssi": 58, "uptime": "12"},
    {"mac": "garbage", "wireless": True, "apMac": "C0-C9-E3-4B-AF-0E"},
]


def test_parse_clients_of_one_ap() -> None:
    assert parse_clients(CLIENTS, AP) == [
        AssociatedClient("2E:DC:E1:C4:37:D3", "ROAMING_SSID", "5GHz", -63, 2091),
        AssociatedClient("AA:BB:CC:00:00:03", "Home6", "6GHz", -58, 12),
    ]
    assert [c.band for c in parse_clients(CLIENTS, "E8:48:B8:7E:C7:1A")] == ["2.4GHz"]


def test_parse_device() -> None:
    info = parse_device(DEVICE)
    assert info.name == "Spare Room"
    assert info.model == "EAP653"
    assert info.firmware.startswith("1.0.12")
    assert info.uptime_seconds == 116846  # uptimeLong, not the text
    assert (info.cpu_percent, info.memory_percent) == (10, 20)


def test_is_access_point() -> None:
    assert is_access_point(DEVICE)
    assert not is_access_point({**DEVICE, "type": "switch"})
    assert not is_access_point({**DEVICE, "type": "gateway"})


# --- the controller over HTTP: a fake Omada controller ---

CID = "4c3e8bd0f8a0a1b2c3d4e5f6a7b8c9d0"


class FakeOmada:
    """An Omada controller (v5 API): /api/info, login token, sites, devices, clients in
    pages, errorCode in every answer."""

    def __init__(self, password: str = "pw", page_size_cap: int | None = None, owner: bool = False) -> None:
        self.password = password
        self.owner = owner  # all sites: privilege.sites is empty, /sites lists them
        self.page_size_cap = page_size_cap
        self.logins = 0
        self.expire_once = False
        self.client_pages: list[int] = []

    def app(self):
        from aiohttp import web

        routes = web.RouteTableDef()

        def ok(result):
            return web.json_response({"errorCode": 0, "msg": "Success.", "result": result})

        @routes.get("/api/info")
        async def info(request):
            return ok({"omadacId": CID, "controllerVer": "5.15.8", "apiVer": "3"})

        @routes.post(f"/{CID}/api/v2/login")
        async def login(request):
            body = await request.json()
            if body.get("username") != "ro" or body.get("password") != self.password:
                return web.json_response({"errorCode": -30109, "msg": "Invalid username or password."})
            self.logins += 1
            response = ok({"roleType": 1, "token": f"tok{self.logins}"})
            response.set_cookie("TPOMADA_SESSIONID", "sess")
            return response

        def guarded(handler):
            async def wrapped(request):
                if self.expire_once:
                    self.expire_once = False
                    return web.json_response({"errorCode": -1200, "msg": "Login required."})
                if request.headers.get("Csrf-Token") != f"tok{self.logins}":
                    return web.json_response({"errorCode": -1200, "msg": "Login required."})
                return ok(handler(request))
            return wrapped

        routes.get(f"/{CID}/api/v2/users/current")(guarded(lambda r: {
            "name": "ro", "privilege": {"sites": [], "all": True} if self.owner else {
                "sites": [{"name": "Default", "key": "site1"}, {"name": "Cabin", "key": "site2"}]}}))
        routes.get(f"/{CID}/api/v2/sites")(guarded(lambda r: {
            "totalRows": 2, "currentPage": 1, "currentSize": 2,
            "data": [{"id": "site1", "name": "Default"}, {"id": "site2", "name": "Cabin"}]}))
        routes.get(f"/{CID}/api/v2/sites/{{site}}/devices")(guarded(lambda r: [
            DEVICE, {"mac": "C0-C9-E3-00-00-99", "type": "switch", "name": "Core"}]
            if r.match_info["site"] == "site1" else []))

        def clients(request):
            assert request.query["filters.active"] == "true"
            page = int(request.query["currentPage"])
            size = min(int(request.query["currentPageSize"]), self.page_size_cap or 10**6)
            self.client_pages.append(page)
            rows = CLIENTS[(page - 1) * size: page * size]
            return {"totalRows": len(CLIENTS), "currentPage": page, "currentSize": size, "data": rows}

        routes.get(f"/{CID}/api/v2/sites/{{site}}/clients")(guarded(clients))
        app = web.Application()
        app.add_routes(routes)
        return app


def run_with(fake: FakeOmada, monkeypatch, work):
    from aiohttp.test_utils import TestServer

    from wifi_ap_associations import http_controller

    monkeypatch.setattr(http_controller, "SCHEME", "http")

    async def main():
        server = TestServer(fake.app())
        await server.start_server()
        try:
            settings = {"host": "127.0.0.1", "port": server.port, "username": "ro", "password": "pw",
                        "site": "Default", "ap_mac": AP}
            return await work(settings)
        finally:
            await server.close()

    return asyncio.run(main())


def poll(settings_changes=None):
    async def work(settings):
        driver = OmadaNetworkDriver({**settings, **(settings_changes or {})})
        try:
            return await driver.async_poll()
        finally:
            await driver.async_close()
    return work


def test_registered_and_experimental() -> None:
    assert DRIVERS["omada_network"] is OmadaNetworkDriver
    assert OmadaNetworkDriver.EXPERIMENTAL is True
    assert OmadaNetworkDriver.SOURCE is None and OmadaNetworkDriver.OWN_DEVICE
    assert OmadaNetworkDriver.LISTS_ACCESS_POINTS
    assert "location" not in OmadaNetworkDriver.REPORTS
    keys = [f.key for f in OmadaNetworkDriver.FIELDS]
    assert keys == ["host", "port", "username", "password", "site", "verify_ssl", "ap_mac"]


def test_poll_through_the_controller(monkeypatch) -> None:
    fake = FakeOmada(page_size_cap=3)  # the clients come in three pages
    result = run_with(fake, monkeypatch, poll())
    assert [c.mac for c in result.clients] == ["2E:DC:E1:C4:37:D3", "AA:BB:CC:00:00:03"]
    assert result.info.model == "EAP653"
    assert fake.client_pages == [1, 2, 3] and fake.logins == 1


def test_site_by_name_case_insensitive(monkeypatch) -> None:
    result = run_with(FakeOmada(), monkeypatch, poll({"site": "default"}))
    assert result.info.name == "Spare Room"


def test_unknown_site_lists_the_sites(monkeypatch) -> None:
    with pytest.raises(AccessPointError, match="no site 'Office'.*Default, Cabin"):
        run_with(FakeOmada(), monkeypatch, poll({"site": "Office"}))


def test_wrong_password_is_an_auth_error(monkeypatch) -> None:
    with pytest.raises(AccessPointAuthError, match="rejected the user or password"):
        run_with(FakeOmada(), monkeypatch, poll({"password": "nope"}))


def test_an_expired_session_logs_in_again(monkeypatch) -> None:
    from wifi_ap_associations import http_controller

    monkeypatch.setattr(http_controller, "SHARE_SECONDS", 0)
    fake = FakeOmada()

    async def work(settings):
        driver = OmadaNetworkDriver(settings)
        try:
            await driver.async_poll()
            fake.expire_once = True
            return await driver.async_poll()
        finally:
            await driver.async_close()

    result = run_with(fake, monkeypatch, work)
    assert len(result.clients) == 2 and fake.logins == 2


def test_not_an_omada_controller(monkeypatch) -> None:
    from aiohttp import web

    class NotOmada(FakeOmada):
        def app(self):
            return web.Application()

    with pytest.raises(AccessPointError, match="doesn't answer as an Omada controller"):
        run_with(NotOmada(), monkeypatch, poll())


def test_disconnected_ap(monkeypatch) -> None:
    fake = FakeOmada()
    original = DEVICE["statusCategory"]
    DEVICE["statusCategory"] = 0
    try:
        with pytest.raises(AccessPointError, match="as disconnected"):
            run_with(fake, monkeypatch, poll())
    finally:
        DEVICE["statusCategory"] = original


def test_settings_from_before_0_12_ask_for_the_login() -> None:
    with pytest.raises(AccessPointAuthError, match="Reconfigure"):
        asyncio.run(OmadaNetworkDriver({"ap_mac": AP}).async_poll())


def test_list_access_points(monkeypatch) -> None:
    found = run_with(FakeOmada(), monkeypatch, OmadaNetworkDriver.async_list_access_points)
    assert found == [(AP, "Spare Room (EAP653, C0:C9:E3:4B:AF:0E)")]


def test_an_owner_account_finds_its_sites_in_the_site_list(monkeypatch) -> None:
    # Seen on a 5.15 software controller: the owner's privilege.sites is empty, "all" true.
    result = run_with(FakeOmada(owner=True), monkeypatch, poll())
    assert result.info.name == "Spare Room"
