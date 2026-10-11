"""Cisco Catalyst 9800 controller driver: parsing and the controller read shared by APs.

The fixture is a real C9800-CL (IOS-XE 17.15.5) with two C9120AXI APs, from a report
sent with the integration's collector.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import re

import pytest

from wifi_ap_associations import DRIVERS, AccessPointAuthError, AccessPointError
from wifi_ap_associations import cisco_wlc_ssh
from wifi_ap_associations.cisco_wlc_ssh import (
    COMMANDS,
    CiscoWlcSsh,
    band_for_protocol,
    parse_controller,
    poll_result,
)

FIXTURE = (Path(__file__).parent / "fixtures" / "cisco-c9800-cl-17.15.5.txt").read_text(
    encoding="utf-8"
)


def sections(text: str) -> dict[str, str]:
    """Each command's console output (echo, table, prompt), keyed like COMMANDS."""
    parts = re.split(r"^# --- (.+?) ---$", text, flags=re.MULTILINE)
    by_command = dict(zip(parts[1::2], parts[2::2]))
    return {key: by_command[command] for key, command in COMMANDS.items()}


OUTPUTS = sections(FIXTURE)
CONFIG = {"host": "192.0.2.20", "port": 22, "username": "ha", "password": "x"}


def test_registered_as_experimental() -> None:
    assert DRIVERS["cisco_wlc_ssh"] is CiscoWlcSsh
    assert CiscoWlcSsh.EXPERIMENTAL is True
    assert CiscoWlcSsh.UNIQUE_FIELD == "ap_name"
    assert [f.key for f in CiscoWlcSsh.FIELDS] == ["host", "port", "username", "password", "ap_name"]


def test_real_controller_output() -> None:
    data = parse_controller(OUTPUTS)
    assert set(data.aps) == {"AP01", "AP02"}
    assert data.aps["AP01"].model == "C9120AXI-B"
    # Read by pattern: the redacted IPs shifted these columns.
    assert data.aps["AP01"].state == "Registered"
    assert data.aps["AP01"].location is None  # "default location"
    assert data.ssids == {
        "1": "<redacted2.4GHzNet>",
        "2": "<redacted5GHzNet>",
        "3": "<redactedIoTNet>",
        "30": "<redactedOtherNet>",
    }
    assert len(data.clients) == 23


def test_clients_are_split_per_ap() -> None:
    data = parse_controller(OUTPUTS)
    ap01 = poll_result(data, "AP01")
    ap02 = poll_result(data, "AP02")
    assert len(ap01.clients) == 17 and len(ap02.clients) == 6
    assert not {c.mac for c in ap01.clients} & {c.mac for c in ap02.clients}
    first = ap02.clients[0]
    assert (first.mac, first.ssid, first.band, first.signal) == (
        "0C:95:05:00:00:05", "<redactedIoTNet>", "2.4GHz", None
    )
    by_mac = {c.mac: c for c in ap01.clients}
    assert by_mac["A4:F6:E8:00:00:16"].band == "5GHz"  # 11ax(5)
    assert by_mac["38:DE:AD:00:00:08"].band == "5GHz"  # 11ac
    assert by_mac["38:DE:AD:00:00:08"].ssid == "<redacted5GHzNet>"
    assert ap01.info.name == "AP01" and ap01.info.model == "C9120AXI-B"


def test_only_run_state_counts() -> None:
    outputs = dict(OUTPUTS)
    # Same width, so the columns stay in place; "IP Learn" is two words.
    outputs["clients"] = re.sub(
        r"^(0c95\.0500\.0005 .*?WLAN 3    )Run      ",
        r"\1IP Learn ",
        OUTPUTS["clients"],
        count=1,
        flags=re.MULTILINE,
    )
    assert outputs["clients"] != OUTPUTS["clients"]
    ap02 = poll_result(parse_controller(outputs), "AP02")
    assert "0C:95:05:00:00:05" not in {c.mac for c in ap02.clients}
    assert len(ap02.clients) == 5


def test_unknown_or_down_ap_is_an_error() -> None:
    data = parse_controller(OUTPUTS)
    with pytest.raises(AccessPointError, match="not joined"):
        poll_result(data, "AP03")
    outputs = dict(OUTPUTS)
    outputs["aps"] = OUTPUTS["aps"].replace("Registered", "Downloading", 1)
    with pytest.raises(AccessPointError, match="Downloading"):
        poll_result(parse_controller(outputs), "AP02")


def test_truncated_client_table_is_an_error() -> None:
    outputs = dict(OUTPUTS)
    lines = OUTPUTS["clients"].splitlines()
    outputs["clients"] = "\n".join(line for line in lines if not line.startswith("e4a4."))
    with pytest.raises(AccessPointError, match="22 rows"):
        parse_controller(outputs)


def test_refused_command_is_an_error() -> None:
    outputs = dict(OUTPUTS)
    outputs["clients"] = (
        "show wireless client summary\n"
        "                      ^\n"
        "% Invalid input detected at '^' marker.\n\nC9800>"
    )
    with pytest.raises(AccessPointError, match="Invalid input"):
        parse_controller(outputs)


def test_no_clients() -> None:
    outputs = dict(OUTPUTS)
    outputs["clients"] = (
        "show wireless client summary\nNumber of Clients: 0\n\n"
        "Number of Excluded Clients: 0\n\nC9800#"
    )
    assert poll_result(parse_controller(outputs), "AP01").clients == []


@pytest.mark.parametrize(
    ("protocol", "band"),
    [
        ("11n(2.4)", "2.4GHz"),
        ("11ax(2.4)", "2.4GHz"),
        ("11g", "2.4GHz"),
        ("11ac", "5GHz"),
        ("11ax(5)", "5GHz"),
        ("11n(5)", "5GHz"),
        ("11ax(6)", "6GHz"),
        ("", None),
        (None, None),
    ],
)
def test_band_for_protocol(protocol: str | None, band: str | None) -> None:
    assert band_for_protocol(protocol) == band


class FakeConsole:
    """An interactive console answering COMMANDS with the fixture, prompt included."""

    def __init__(self, sent: list[str]) -> None:
        answers = {command: OUTPUTS[key] for key, command in COMMANDS.items()}
        answers["terminal length 0"] = "terminal length 0\nC9800#"
        console = self

        class Stdin:
            def write(self, data: str) -> None:
                command = data.strip()
                sent.append(command)
                console.pending.append(answers[command].replace("\n", "\r\n"))

        class Stdout:
            async def read(self, size: int) -> str:
                return console.pending.pop(0)

        self.pending = ["\r\n\r\nC9800#"]
        self.stdin, self.stdout = Stdin(), Stdout()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def fake_connect(monkeypatch: pytest.MonkeyPatch, sent: list[str]) -> list[dict]:
    import asyncssh

    connects: list[dict] = []

    class Conn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def create_process(self, **kwargs):
            return FakeConsole(sent)

    def connect(*args, **kwargs):
        connects.append(kwargs)
        return Conn()

    monkeypatch.setattr(asyncssh, "connect", connect)
    monkeypatch.setattr(cisco_wlc_ssh, "_shared", {})
    return connects


def test_aps_polled_together_share_one_login(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    connects = fake_connect(monkeypatch, sent)
    ap01 = CiscoWlcSsh({**CONFIG, "ap_name": "AP01"})
    ap02 = CiscoWlcSsh({**CONFIG, "ap_name": "AP02"})

    async def poll_both():
        return await asyncio.gather(ap01.async_poll(), ap02.async_poll())

    first, second = asyncio.run(poll_both())
    assert len(connects) == 1
    assert sent == ["terminal length 0", *COMMANDS.values()]
    assert (len(first.clients), len(second.clients)) == (17, 6)


def test_other_credentials_do_not_share(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    connects = fake_connect(monkeypatch, sent)

    async def poll_both():
        await CiscoWlcSsh({**CONFIG, "ap_name": "AP01"}).async_poll()
        await CiscoWlcSsh({**CONFIG, "password": "y", "ap_name": "AP01"}).async_poll()

    asyncio.run(poll_both())
    assert len(connects) == 2


def test_rejected_login(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncssh

    def connect(*args, **kwargs):
        raise asyncssh.PermissionDenied("no")

    monkeypatch.setattr(asyncssh, "connect", connect)
    monkeypatch.setattr(cisco_wlc_ssh, "_shared", {})
    driver = CiscoWlcSsh({**CONFIG, "ap_name": "AP01"})
    with pytest.raises(AccessPointAuthError):
        asyncio.run(driver.async_poll())
