"""MikroTik RouterOS driver: parsing of identity, resource and registration tables.

The identity and resource outputs are a real CRS310 switch on RouterOS 7.20.4 (no
Wi-Fi). The registration tables follow MikroTik's documentation; they are to be
replaced by a real access point's output once someone sends a report.
"""

from __future__ import annotations

import asyncio

import pytest

from wifi_ap_associations import DRIVERS, AccessPointAuthError, AccessPointError
from wifi_ap_associations.mikrotik_ssh import (
    COMMANDS,
    MikroTikSsh,
    band_name,
    parse_poll,
    parse_size,
    parse_terse,
    parse_uptime,
)

IDENTITY = "  name: MikroTik\n\n"
RESOURCE = """\
                   uptime: 30w6d20h44m17s
                  version: 7.20.4 (stable)
               build-time: 2025-11-05 12:07:41
              free-memory: 172.6MiB
             total-memory: 256.0MiB
                      cpu: ARM
                cpu-load: 2%
               board-name: CRS310-8G+2S+
                 platform: MikroTik
"""
BAD_WIRELESS = "bad command name wireless (line 1 column 12)\n"

# Documentation-based samples (RouterOS 7 wifi package, RouterOS 6 wireless package).
WIFI_INTERFACES = (
    " 0 R  name=wifi1 default-name=wifi1 configuration.mode=ap configuration.ssid=Home "
    "channel.band=5ghz-ax mac-address=04:F4:1C:00:00:10\n"
    " 1 R  name=wifi2 default-name=wifi2 configuration.mode=ap configuration.ssid=Home IoT "
    "channel.band=2ghz-ax mac-address=04:F4:1C:00:00:11\n"
)
WIFI_CLIENTS = (
    " 0 interface=wifi1 ssid=Home mac-address=5C:AD:BA:00:00:01 uptime=1h2m3s "
    "last-activity=0ms signal=-55 auth-type=wpa2-psk band=5ghz-ax tx-rate=867Mbps\n"
    " 1 interface=wifi2 mac-address=5c:ad:ba:00:00:02 uptime=21m48s960ms signal=-71 "
    "auth-type=wpa2-psk\n"
)
WIRELESS_INTERFACES = (
    " 0 R  name=wlan1 mtu=1500 mac-address=04:F4:1C:00:00:20 ssid=Garden band=2ghz-b/g/n "
    "mode=ap-bridge\n"
)
WIRELESS_CLIENTS = (
    " 0 interface=wlan1 mac-address=5C:AD:BA:00:00:03 ap=no wds=no uptime=3d4h "
    "signal-strength=-61@HT20-7 signal-to-noise=40\n"
)


def outputs(**overrides: str) -> dict[str, str]:
    base = {
        "identity": IDENTITY,
        "resource": RESOURCE,
        "wifi_interfaces": "",
        "wifi_clients": "",
        "wireless_interfaces": BAD_WIRELESS,
        "wireless_clients": BAD_WIRELESS,
    }
    return {**base, **overrides}


def test_registered_as_experimental() -> None:
    assert DRIVERS["mikrotik_ssh"] is MikroTikSsh
    assert MikroTikSsh.EXPERIMENTAL is True
    assert set(COMMANDS) == {
        "identity", "resource", "wifi_interfaces", "wifi_clients",
        "wireless_interfaces", "wireless_clients",
    }


def test_switch_without_wifi_reports_details_and_no_clients() -> None:
    result = parse_poll(outputs())
    assert result.clients == []
    info = result.info
    assert info.name == "MikroTik"
    assert info.model == "CRS310-8G+2S+"
    assert info.firmware == "7.20.4"
    assert info.uptime_seconds == 30 * 604800 + 6 * 86400 + 20 * 3600 + 44 * 60 + 17
    assert info.cpu_percent == 2
    assert info.memory_percent == 33  # (256.0 - 172.6) / 256.0


def test_wifi_package_clients_with_band_and_ssid() -> None:
    result = parse_poll(outputs(wifi_interfaces=WIFI_INTERFACES, wifi_clients=WIFI_CLIENTS))
    first, second = result.clients
    assert (first.mac, first.ssid, first.band, first.signal) == (
        "5C:AD:BA:00:00:01", "Home", "5GHz", -55,
    )
    assert first.connected_seconds == 3723
    # No ssid/band in the table: taken from the interface list (an SSID with a space).
    assert (second.mac, second.ssid, second.band, second.signal) == (
        "5C:AD:BA:00:00:02", "Home IoT", "2.4GHz", -71,
    )
    assert second.connected_seconds == 21 * 60 + 48


def test_wireless_package_used_when_wifi_menu_is_missing() -> None:
    result = parse_poll(
        outputs(
            wifi_interfaces="bad command name wifi (line 1 column 12)\n",
            wifi_clients="bad command name wifi (line 1 column 12)\n",
            wireless_interfaces=WIRELESS_INTERFACES,
            wireless_clients=WIRELESS_CLIENTS,
        )
    )
    (client,) = result.clients
    assert (client.mac, client.ssid, client.band, client.signal) == (
        "5C:AD:BA:00:00:03", "Garden", "2.4GHz", -61,
    )
    assert client.connected_seconds == 3 * 86400 + 4 * 3600


def test_unreadable_registration_table_is_an_error_not_an_empty_list() -> None:
    with pytest.raises(AccessPointError):
        parse_poll(outputs(wifi_clients="something unexpected\n"))


def test_no_resource_answer_is_an_error() -> None:
    with pytest.raises(AccessPointError):
        parse_poll(outputs(resource=""))


def test_terse_values_may_contain_spaces() -> None:
    (record,) = parse_terse(
        " 0 RS comment=Uplink to Dlink 24 ports name=Dlink 24 default-name=ether1 type=ether\n"
    )
    assert record["comment"] == "Uplink to Dlink 24 ports"
    assert record["name"] == "Dlink 24"
    assert record["type"] == "ether"


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("17s", 17), ("1h2m3s", 3723), ("2w", 1209600), ("21m48s960ms", 1308), ("", None), ("abc", None)],
)
def test_uptime(text: str, seconds: int | None) -> None:
    assert parse_uptime(text) == seconds


def test_size_and_band() -> None:
    assert parse_size("256.0MiB") == 256 * 1024**2
    assert parse_size("1024") == 1024
    assert band_name("2ghz-b/g/n") == "2.4GHz"
    assert band_name("5ghz-ac") == "5GHz"
    assert band_name("6ghz-ax") == "6GHz"
    assert band_name(None) is None


def test_runs_each_command_on_one_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncssh

    calls: list[str] = []
    answers = outputs(wifi_interfaces=WIFI_INTERFACES, wifi_clients=WIFI_CLIENTS)
    by_command = {command: answers[key] for key, command in COMMANDS.items()}

    class Result:
        def __init__(self, stdout: str) -> None:
            self.stdout, self.stderr = stdout, ""

    class Conn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def run(self, command, **kwargs):
            calls.append(command)
            return Result(by_command[command].replace("\n", "\r\n"))

    connects = []

    def connect(*args, **kwargs):
        connects.append(kwargs)
        return Conn()

    monkeypatch.setattr(asyncssh, "connect", connect)
    driver = MikroTikSsh({"host": "192.0.2.1", "port": 22, "username": "admin", "password": "x"})
    result = asyncio.run(driver.async_poll())
    assert len(connects) == 1
    assert calls == list(COMMANDS.values())
    assert len(result.clients) == 2 and result.info.model == "CRS310-8G+2S+"


def test_rejected_login(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncssh

    def connect(*args, **kwargs):
        raise asyncssh.PermissionDenied("no")

    monkeypatch.setattr(asyncssh, "connect", connect)
    driver = MikroTikSsh({"host": "192.0.2.1", "port": 22, "username": "admin", "password": "x"})
    with pytest.raises(AccessPointAuthError):
        asyncio.run(driver.async_poll())
