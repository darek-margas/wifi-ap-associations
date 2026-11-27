"""OpenWrt restart: when "reboot" over SSH counts as taken."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import asyncssh
import pytest

from wifi_ap_associations import AccessPointError
from wifi_ap_associations import openwrt_ssh
from wifi_ap_associations.openwrt_ssh import OpenWrtSsh

CONFIG = {"host": "192.0.2.5", "username": "root", "password": "x"}


class FakeConn:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.commands: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def run(self, command: str, **_kwargs):
        self.commands.append(command)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if self.outcome == "hang":
            await asyncio.sleep(3600)
        return self.outcome


def restart(monkeypatch: pytest.MonkeyPatch, outcome) -> FakeConn:
    conn = FakeConn(outcome)
    monkeypatch.setattr(openwrt_ssh.asyncssh, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(openwrt_ssh, "COMMAND_TIMEOUT", 0.2)
    asyncio.run(OpenWrtSsh(dict(CONFIG)).async_restart())
    return conn


def test_returns_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = restart(monkeypatch, SimpleNamespace(exit_status=0, stdout="", stderr=""))
    assert conn.commands == ["reboot"]


def test_session_lost_after_sending_is_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    restart(monkeypatch, asyncssh.ConnectionLost("Connection lost"))


def test_not_allowed_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AccessPointError, match="reboot failed"):
        restart(monkeypatch, SimpleNamespace(exit_status=1, stdout="", stderr="Permission denied"))


def test_hanging_command_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AccessPointError, match="did not return"):
        restart(monkeypatch, "hang")
