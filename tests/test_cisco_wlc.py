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
from wifi_ap_associations.cisco_wlc import ClientReading, ControllerClient, ControllerData
from wifi_ap_associations.cisco_wlc_ssh import (
    COMMANDS,
    DETAILS_PER_READ,
    SIGNAL_MAX_AGE,
    CiscoWlcSsh,
    band_for_protocol,
    cisco_mac,
    parse_client_detail,
    parse_controller,
    pick_details,
    poll_result,
    with_readings,
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


DETAIL = (Path(__file__).parent / "fixtures" / "cisco-c9800-cl-17.15.5-client-detail.txt").read_text(
    encoding="utf-8"
)
DETAIL = DETAIL[DETAIL.index("show wireless client"):]  # without the fixture's comments
# Each client's AP in the summary fixture, by its IOS-style MAC.
SUMMARY_AP = {
    line.split()[0]: line.split()[1]
    for line in OUTPUTS["clients"].splitlines()
    if re.match(r"^[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4} ", line)
}


def detail_for(mac: str, signal: int = -61, ap: str | None = None) -> str:
    """The real detail output, rewritten for another client, AP and signal."""
    text = DETAIL.replace("4e62.3300.0009", mac)
    text = text.replace("AP Name: AP01", f"AP Name: {ap or SUMMARY_AP[mac]}")
    return text.replace("Radio Signal Strength Indicator : -61 dBm",
                        f"Radio Signal Strength Indicator : {signal} dBm")


class FakeConsole:
    """An interactive console answering like the 9800 in the fixtures, prompt included.

    `detail(mac)` answers a client's detail command: text, or None to never answer.
    """

    def __init__(self, sent: list[str], detail=detail_for) -> None:
        answers = {command: OUTPUTS[key] for key, command in COMMANDS.items()}
        answers["terminal length 0"] = "terminal length 0\nC9800#"
        console = self

        class Stdin:
            def write(self, data: str) -> None:
                command = data.strip()
                sent.append(command)
                if command.startswith("show wireless client mac-address"):
                    answer = detail(command.split()[4])
                else:
                    answer = answers[command]
                if answer is not None:
                    console.pending.append(answer.replace("\n", "\r\n"))

        class Stdout:
            async def read(self, size: int) -> str:
                if not console.pending:
                    await asyncio.Event().wait()  # a console that never answers
                return console.pending.pop(0)

        self.pending = ["\r\n\r\nC9800#"]
        self.stdin, self.stdout = Stdin(), Stdout()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def fake_connect(monkeypatch: pytest.MonkeyPatch, sent: list[str], detail=detail_for) -> list[dict]:
    import asyncssh

    connects: list[dict] = []

    class Conn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def create_process(self, **kwargs):
            return FakeConsole(sent, detail)

    def connect(*args, **kwargs):
        connects.append(kwargs)
        return Conn()

    monkeypatch.setattr(asyncssh, "connect", connect)
    monkeypatch.setattr(cisco_wlc_ssh, "_shared", cisco_wlc_ssh.SharedReads(cisco_wlc_ssh.SHARE_SECONDS))
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
    assert sent[:4] == ["terminal length 0", *COMMANDS.values()]
    assert len(sent) == 4 + DETAILS_PER_READ  # then the first turn of client details
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
    monkeypatch.setattr(cisco_wlc_ssh, "_shared", cisco_wlc_ssh.SharedReads(cisco_wlc_ssh.SHARE_SECONDS))
    driver = CiscoWlcSsh({**CONFIG, "ap_name": "AP01"})
    with pytest.raises(AccessPointAuthError):
        asyncio.run(driver.async_poll())


# --- signal and connected time from client details, read in turns -------------------


def test_client_detail_of_the_real_controller() -> None:
    mac, reading = parse_client_detail(DETAIL, now=100.0)
    assert mac == "4E:62:33:00:00:09"
    assert (reading.ap_name, reading.signal, reading.connected_seconds) == ("AP01", -61, 36651)


@pytest.mark.parametrize(
    "answer",
    [
        "show wireless client mac-address 4e62.3300.0009 detail\n\nC9800#",  # left meanwhile
        "% Invalid input detected at '^' marker.\nC9800>",
        DETAIL.replace("Radio Signal Strength Indicator : -61 dBm", "Radio Signal Strength Indicator : 17 dBm"),
    ],
)
def test_odd_client_details(answer: str) -> None:
    parsed = parse_client_detail(answer, now=0.0)
    assert parsed is None or parsed[1].signal is None


def test_cisco_mac() -> None:
    assert cisco_mac("4E:62:33:00:00:09") == "4e62.3300.0009"


def _client(mac: str, ap: str = "AP01", state: str = "Run") -> ControllerClient:
    return ControllerClient(mac=mac, ap_name=ap, wlan_id="1", state=state, protocol="11ac")


def test_pick_details_new_then_roamed_then_oldest() -> None:
    now = 1000.0
    clients = [
        _client("00:00:00:00:00:01"),  # fresh reading: skipped
        _client("00:00:00:00:00:02"),  # old reading
        _client("00:00:00:00:00:03", ap="AP02"),  # read on AP01, now on AP02
        _client("00:00:00:00:00:04"),  # no reading yet
        _client("00:00:00:00:00:05", state="IP Learn"),  # not associated yet: skipped
        _client("00:00:00:00:00:06"),  # older reading
    ]
    readings = {
        "00:00:00:00:00:01": ClientReading("AP01", -50, 10, now - 30),
        "00:00:00:00:00:02": ClientReading("AP01", -50, 10, now - 300),
        "00:00:00:00:00:03": ClientReading("AP01", -50, 10, now - 30),
        "00:00:00:00:00:06": ClientReading("AP01", -50, 10, now - 500),
    }
    assert pick_details(clients, readings, now) == [
        "00:00:00:00:00:04", "00:00:00:00:00:03", "00:00:00:00:00:06", "00:00:00:00:00:02",
    ]
    many = [_client(f"00:00:00:00:01:{i:02X}") for i in range(40)]
    assert len(pick_details(many, {}, now)) == DETAILS_PER_READ


def test_reading_applies_only_on_its_ap_and_while_young() -> None:
    now = 1000.0
    data = ControllerData(
        aps={}, ssids={},
        clients=[_client("00:00:00:00:00:01"), _client("00:00:00:00:00:02", ap="AP02"),
                 _client("00:00:00:00:00:03")],
    )
    readings = {
        "00:00:00:00:00:01": ClientReading("AP01", -61, 100, now - 60),
        "00:00:00:00:00:02": ClientReading("AP01", -61, 100, now - 60),  # roamed since
        "00:00:00:00:00:03": ClientReading("AP01", -61, 100, now - SIGNAL_MAX_AGE - 1),
        "00:00:00:00:00:09": ClientReading("AP01", -61, 100, now - 60),  # gone
    }
    result = with_readings(data, readings, 0.0, now)
    by_mac = {c.mac: c for c in result.clients}
    assert (by_mac["00:00:00:00:00:01"].signal, by_mac["00:00:00:00:00:01"].connected_seconds) == (-61, 160)
    assert by_mac["00:00:00:00:00:02"].signal is None
    assert by_mac["00:00:00:00:00:03"].signal is None
    assert set(result.readings) == {"00:00:00:00:00:01", "00:00:00:00:00:02"}


def _poll_all(monkeypatch, sent, detail=detail_for, polls: int = 1):
    """Poll both APs `polls` times in one event loop; each poll is a new controller read."""
    connects = fake_connect(monkeypatch, sent, detail)
    monkeypatch.setattr(cisco_wlc_ssh, "_shared", cisco_wlc_ssh.SharedReads(0))
    aps = [CiscoWlcSsh({**CONFIG, "ap_name": name}) for name in ("AP01", "AP02")]

    async def main():
        results = []
        for _ in range(polls):
            results.append(await asyncio.gather(*(ap.async_poll() for ap in aps)))
        return results

    return asyncio.run(main()), connects


def _details(sent: list[str]) -> list[str]:
    return [c for c in sent if c.startswith("show wireless client mac-address")]


def test_signal_arrives_in_turns_then_only_old_readings_are_read(monkeypatch) -> None:
    sent: list[str] = []
    results, _ = _poll_all(monkeypatch, sent, polls=3)
    first, second, third = results
    with_signal = [sum(c.signal is not None for c in ap.clients) for ap in first]
    assert sum(with_signal) == DETAILS_PER_READ  # 15 of 23 after the first read
    assert all(c.signal == -61 for ap in second for c in ap.clients)  # all after the second
    assert all(c.connected_seconds and c.connected_seconds >= 36651 for ap in second for c in ap.clients)
    assert len(_details(sent)) == 23  # the third read had nothing to refresh yet
    assert all(c.signal == -61 for ap in third for c in ap.clients)


def test_detail_on_another_ap_waits_for_a_new_reading(monkeypatch) -> None:
    sent: list[str] = []
    # The client roamed between the summary and its detail: the detail names AP02.
    results, _ = _poll_all(monkeypatch, sent, lambda mac: detail_for(mac, ap="AP02") if mac == "4e62.3300.0009" else detail_for(mac), polls=2)
    ap01 = {c.mac: c for c in results[-1][0].clients}
    assert ap01["4E:62:33:00:00:09"].signal is None


def test_refused_detail_pauses_details_but_not_presence(monkeypatch) -> None:
    sent: list[str] = []
    refused = lambda mac: f"show wireless client mac-address {mac} detail\n% Authorization failed.\nC9800>"
    results, _ = _poll_all(monkeypatch, sent, refused, polls=2)
    assert len(_details(sent)) == 1  # asked once, then paused
    assert [len(ap.clients) for ap in results[-1]] == [17, 6]
    assert all(c.signal is None for ap in results[-1] for c in ap.clients)


def test_detail_that_never_answers_ends_details_not_the_read(monkeypatch) -> None:
    monkeypatch.setattr(cisco_wlc_ssh, "DETAIL_TIMEOUT", 0.1)
    sent: list[str] = []
    results, _ = _poll_all(monkeypatch, sent, lambda mac: None)
    assert len(_details(sent)) == 1
    assert [len(ap.clients) for ap in results[0]] == [17, 6]
