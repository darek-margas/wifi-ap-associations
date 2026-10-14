"""MikroTik CAPsMAN controller: clients put on their CAP, one read shared by the CAPs.

The fixture is a real CAPsMAN router with two cAP ax on RouterOS 7.24.4, from
darek-margas/wifi-association-presence#11.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import re

import pytest

from wifi_ap_associations import DRIVERS, AccessPointAuthError, AccessPointError
from wifi_ap_associations import mikrotik_capsman_ssh
from wifi_ap_associations.cisco_wlc import SharedReads, poll_result
from wifi_ap_associations.mikrotik_capsman_ssh import (
    COMMANDS,
    MikroTikCapsmanSsh,
    cap_identity,
    interface_caps,
    parse_controller,
)
from wifi_ap_associations.mikrotik_ssh import MikroTikSsh, parse_terse

FIXTURE = (Path(__file__).parent / "fixtures" / "mikrotik-capsman-7.24.4.txt").read_text(
    encoding="utf-8"
)


def sections(text: str) -> dict[str, str]:
    parts = re.split(r"^# --- (\w+): .* ---$", text, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2]))


OUTPUTS = sections(FIXTURE)
CONFIG = {"host": "192.0.2.1", "port": 22, "username": "ha", "password": "x"}


def test_registered_as_experimental_per_cap() -> None:
    assert DRIVERS["mikrotik_capsman_ssh"] is MikroTikCapsmanSsh
    assert MikroTikCapsmanSsh.EXPERIMENTAL is True
    assert [f.key for f in MikroTikCapsmanSsh.FIELDS] == ["host", "port", "username", "password", "cap"]
    assert set(COMMANDS.values()) == {
        "/interface wifi capsman remote-cap print terse",
        "/interface wifi print terse",
        "/interface wifi registration-table print terse",
    }


def test_real_controller_puts_clients_on_their_cap() -> None:
    data = parse_controller(OUTPUTS)
    upstairs, downstairs = poll_result(data, "ap_upstairs"), poll_result(data, "ap_downstairs")
    assert (len(upstairs.clients), len(downstairs.clients)) == (14, 11)
    assert all(client.ap_name for client in data.clients)  # every client found its CAP
    # Row 0: on "ap_upstairs -virtual8", whose master is "ap_upstairs 2" (2.4 GHz radio).
    first = next(c for c in upstairs.clients if c.mac == "02:00:00:00:03:00")
    assert (first.ssid, first.band, first.signal) == ("SSID31", "2.4GHz", -29)
    assert first.connected_seconds == 2 * 604800 + 6 * 86400 + 22 * 3600 + 37 * 60 + 35
    # Row 4: straight on the 5 GHz radio interface "ap_downstairs " (a trailing space).
    fourth = next(c for c in downstairs.clients if c.mac == "02:00:00:00:03:04")
    assert (fourth.band, fourth.signal) == ("5GHz", -46)


def test_cap_details_from_the_remote_cap_list() -> None:
    info = poll_result(parse_controller(OUTPUTS), "ap_upstairs").info
    assert (info.name, info.model, info.firmware) == ("ap_upstairs", "cAPGi-5HaxD2HaxD", "7.24.4")
    assert info.uptime_seconds == 2 * 604800 + 6 * 86400 + 22 * 3600 + 40 * 60 + 4


def test_virtual_interfaces_lead_to_their_cap_through_master() -> None:
    caps = interface_caps(parse_terse(OUTPUTS["interfaces"]))
    assert caps["ap_upstairs -virtual8"] == "ap_upstairs"
    assert caps["ap_downstairs 2"] == "ap_downstairs"
    loop = [{"name": "a", "master-interface": "b"}, {"name": "b", "master-interface": "a"}]
    assert interface_caps(loop) == {}


def test_cap_identity() -> None:
    assert cap_identity("ap_upstairs@MAC%*34") == "ap_upstairs"
    assert cap_identity("name@with@at@02:00:00:00:00:01%*1") == "name@with@at"
    assert cap_identity("ap_hall") == "ap_hall"


def test_unknown_or_disconnected_cap_is_an_error() -> None:
    with pytest.raises(AccessPointError, match="not joined"):
        poll_result(parse_controller(OUTPUTS), "ap_garage")
    outputs = dict(OUTPUTS)
    outputs["caps"] = OUTPUTS["caps"].replace("state=Ok", "state=Disconnected", 1)
    with pytest.raises(AccessPointError, match="Disconnected"):
        poll_result(parse_controller(outputs), "ap_upstairs")


def test_client_still_authenticating_is_skipped() -> None:
    outputs = dict(OUTPUTS)
    outputs["clients"] = OUTPUTS["clients"].replace(" 0 A interface=", " 0   interface=", 1)
    outputs["clients"] = outputs["clients"].replace(" 1 A interface=", " 1 G interface=", 1)
    macs = {c.mac for c in poll_result(parse_controller(outputs), "ap_upstairs").clients}
    assert "02:00:00:00:03:00" in macs  # no flags printed at all: kept
    assert "02:00:00:00:03:01" not in macs  # flags without A: still joining


@pytest.mark.parametrize("key", list(COMMANDS))
def test_no_capsman_is_an_error(key: str) -> None:
    outputs = dict(OUTPUTS)
    outputs[key] = "bad command name capsman (line 1 column 16)\n"
    with pytest.raises(AccessPointError, match="CAPsMAN"):
        parse_controller(outputs)


def test_unreadable_registration_table_is_an_error() -> None:
    outputs = dict(OUTPUTS)
    outputs["clients"] = "failure: timeout\n"
    with pytest.raises(AccessPointError, match="registration table"):
        parse_controller(outputs)


def test_identity_is_controller_and_cap() -> None:
    a = MikroTikCapsmanSsh.identity({"host": "192.0.2.1", "cap": "ap_upstairs"})
    assert a == ("192.0.2.1", "ap_upstairs")
    assert MikroTikCapsmanSsh.identity({"host": "192.0.2.1", "cap": "ap_downstairs"}) != a
    # The router added as a plain MikroTik access point is a different entry.
    assert MikroTikSsh.identity({"host": "192.0.2.1"}) != a


def _fake_ssh(monkeypatch: pytest.MonkeyPatch) -> tuple[list[dict], list[str]]:
    import asyncssh

    connects: list[dict] = []
    calls: list[str] = []
    by_command = {command: OUTPUTS[key] for key, command in COMMANDS.items()}

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

    def connect(*args, **kwargs):
        connects.append(kwargs)
        return Conn()

    monkeypatch.setattr(asyncssh, "connect", connect)
    monkeypatch.setattr(mikrotik_capsman_ssh, "_shared", SharedReads(20))
    return connects, calls


def test_caps_polled_together_share_one_login(monkeypatch: pytest.MonkeyPatch) -> None:
    connects, calls = _fake_ssh(monkeypatch)

    async def poll_both():
        return await asyncio.gather(
            MikroTikCapsmanSsh({**CONFIG, "cap": "ap_upstairs"}).async_poll(),
            MikroTikCapsmanSsh({**CONFIG, "cap": "ap_downstairs"}).async_poll(),
        )

    upstairs, downstairs = asyncio.run(poll_both())
    assert len(connects) == 1 and calls == list(COMMANDS.values())
    assert (len(upstairs.clients), len(downstairs.clients)) == (14, 11)


def test_rejected_login(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncssh

    def connect(*args, **kwargs):
        raise asyncssh.PermissionDenied("no")

    monkeypatch.setattr(asyncssh, "connect", connect)
    monkeypatch.setattr(mikrotik_capsman_ssh, "_shared", SharedReads(20))
    with pytest.raises(AccessPointAuthError):
        asyncio.run(MikroTikCapsmanSsh({**CONFIG, "cap": "ap_upstairs"}).async_poll())
