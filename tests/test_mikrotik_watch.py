"""Instant arrivals on MikroTik: `registration-table print follow-only` as a change feed."""

from __future__ import annotations

import asyncio

import pytest

from wifi_ap_associations import (
    DRIVERS,
    AccessPointAuthError,
    AccessPointError,
)
from wifi_ap_associations.mikrotik_capsman_ssh import MikroTikCapsmanSsh
from wifi_ap_associations.mikrotik_ssh import WATCH_COMMANDS, MikroTikSsh

CONFIG = {"host": "192.0.2.1", "port": 22, "username": "ha", "password": "x"}
FOREVER = object()  # a feed that never ends by itself


class FakeConnection:
    """asyncssh.connect() stand-in; each command's feed is a list of lines (or FOREVER)."""

    def __init__(self, feeds: dict[str, list]) -> None:
        self.feeds = feeds
        self.started: list[str] = []
        self.connect_kwargs: dict = {}

    def connect(self, *args, **kwargs):
        self.connect_kwargs = kwargs
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def create_process(self, command, **kwargs):
        self.started.append(command)
        lines = list(self.feeds[command])

        class Stdout:
            async def readline(self) -> str:
                if not lines:
                    return ""
                line = lines.pop(0)
                if line is FOREVER:
                    await asyncio.Event().wait()
                return line

        class Process:
            stdout = Stdout()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        return Process()


def install(monkeypatch: pytest.MonkeyPatch, feeds: dict[str, list]) -> FakeConnection:
    import asyncssh

    fake = FakeConnection(feeds)
    monkeypatch.setattr(asyncssh, "connect", fake.connect)
    return fake


MISSING = ["bad command name wireless (line 1 column 12)\n"]
JOIN = " 3 A interface=wifi1 ssid=Home mac-address=5C:AD:BA:00:00:09 uptime=0s signal=-60\n"


def test_each_client_line_asks_for_a_poll_until_the_feed_ends(monkeypatch) -> None:
    fake = install(monkeypatch, {
        WATCH_COMMANDS["wifi"]: [JOIN, "\n", "Flags: A - AUTHORIZED\n", JOIN],
        WATCH_COMMANDS["wireless"]: MISSING,
    })
    changes: list[int] = []
    with pytest.raises(AccessPointError, match="ended"):
        asyncio.run(MikroTikSsh(CONFIG).async_watch(lambda: changes.append(1)))
    assert len(changes) == 2  # the two client lines, not the header or blank line
    assert set(fake.started) == set(WATCH_COMMANDS.values())
    # A dead connection is noticed without waiting for TCP to give up.
    assert fake.connect_kwargs["keepalive_interval"] == 30


def test_no_registration_table_at_all_is_an_error(monkeypatch) -> None:
    install(monkeypatch, {command: MISSING for command in WATCH_COMMANDS.values()})
    with pytest.raises(AccessPointError, match="nothing|no Wi-Fi"):
        asyncio.run(MikroTikSsh(CONFIG).async_watch(lambda: None))


def test_watch_runs_until_cancelled(monkeypatch) -> None:
    install(monkeypatch, {
        WATCH_COMMANDS["wifi"]: [JOIN, FOREVER],
        WATCH_COMMANDS["wireless"]: [FOREVER],
    })
    changes: list[int] = []

    async def main():
        task = asyncio.ensure_future(MikroTikSsh(CONFIG).async_watch(lambda: changes.append(1)))
        await asyncio.sleep(0.05)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert changes == [1]


def test_rejected_login(monkeypatch) -> None:
    import asyncssh

    def connect(*args, **kwargs):
        raise asyncssh.PermissionDenied("no")

    monkeypatch.setattr(asyncssh, "connect", connect)
    with pytest.raises(AccessPointAuthError):
        asyncio.run(MikroTikSsh(CONFIG).async_watch(lambda: None))


def test_capsman_watches_the_controller_once_for_all_caps(monkeypatch) -> None:
    upstairs = MikroTikCapsmanSsh({**CONFIG, "cap": "ap_upstairs"})
    downstairs = MikroTikCapsmanSsh({**CONFIG, "cap": "ap_downstairs"})
    assert upstairs.WATCHES and upstairs.watch_key() == downstairs.watch_key()
    assert MikroTikCapsmanSsh({**CONFIG, "host": "192.0.2.2", "cap": "x"}).watch_key() != upstairs.watch_key()
    fake = install(monkeypatch, {WATCH_COMMANDS["wifi"]: [JOIN]})
    with pytest.raises(AccessPointError):
        asyncio.run(upstairs.async_watch(lambda: None))
    assert fake.started == [WATCH_COMMANDS["wifi"]]  # CAPsMAN is the wifi package


def test_only_watching_drivers_watch() -> None:
    assert MikroTikSsh.WATCHES is True
    assert DRIVERS["openwrt_ssh"].WATCHES is False
    plain = MikroTikSsh(CONFIG)
    assert plain.watch_key() != MikroTikSsh({**CONFIG, "host": "192.0.2.2"}).watch_key()
    openwrt = DRIVERS["openwrt_ssh"](CONFIG)
    with pytest.raises(NotImplementedError):
        asyncio.run(openwrt.async_watch(lambda: None))
