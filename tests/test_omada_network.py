"""Tests for the TP-Link Omada driver.

Client records are Home Assistant's tplink_omada test fixture (connected-clients.json),
trimmed; the access point record follows its devices.json with "type": "ap".
"""

from __future__ import annotations

import asyncio

import pytest

from wifi_ap_associations import DRIVERS, AccessPointError, AssociatedClient
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


class FakeSource:
    def __init__(self, device=DEVICE, clients=CLIENTS) -> None:
        self.device = device
        self.clients = clients

    async def async_get_device(self, mac):
        return self.device if mac == AP else None

    async def async_get_clients(self, mac):
        return self.clients


def test_registered_and_experimental() -> None:
    assert DRIVERS["omada_network"] is OmadaNetworkDriver
    assert OmadaNetworkDriver.EXPERIMENTAL is True
    assert OmadaNetworkDriver.SOURCE == "omada"
    assert OmadaNetworkDriver.OWN_DEVICE is False
    assert "location" not in OmadaNetworkDriver.REPORTS


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


def test_poll() -> None:
    result = asyncio.run(OmadaNetworkDriver({"ap_mac": "c0-c9-e3-4b-af-0e"}, FakeSource()).async_poll())
    assert [c.mac for c in result.clients] == ["2E:DC:E1:C4:37:D3", "AA:BB:CC:00:00:03"]
    assert result.info.name == "Spare Room"


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (None, "inside Home Assistant"),
        (FakeSource(device=None), "not an access point"),
        (FakeSource(device={**DEVICE, "statusCategory": 0}), "disconnected"),
        (FakeSource(device={**DEVICE, "statusCategory": 3}), "heartbeat missed"),
    ],
)
def test_poll_unavailable(source, message) -> None:
    with pytest.raises(AccessPointError, match=message):
        asyncio.run(OmadaNetworkDriver({"ap_mac": AP}, source).async_poll())
