"""Instant arrivals on OpenWrt: hostapd's ubus notifications as a change feed.

The lines are as OpenWrt 25.12.5 and 22.03.7 printed them in VMs with simulated radios.
"""

from __future__ import annotations

import asyncio

import pytest

from wifi_ap_associations import AccessPointError
from wifi_ap_associations import openwrt_ssh
from wifi_ap_associations.openwrt_ssh import (
    WATCH_LIST_COMMAND,
    WATCH_OBJECTS_COMMAND,
    OpenWrtSsh,
    _Arrivals,
)

CONFIG = {"host": "192.0.2.1", "port": 22, "username": "root", "password": "x"}
STA = "02:00:00:00:01:00"
PROBE = '{ "probe": {"address":"02:00:00:00:01:00","ifname":"phy0-ap0","target":"ff:ff:ff:ff:ff:ff","signal":-50,"freq":2437} }\n'
AUTH = '{ "auth": {"address":"02:00:00:00:01:00","ifname":"phy0-ap0","target":"02:00:00:00:00:00","signal":-14,"freq":2437} }\n'
ASSOC = '{ "assoc": {"address":"02:00:00:00:01:00","ifname":"phy0-ap0","target":"02:00:00:00:00:00","signal":-14,"freq":2437} }\n'
AUTHORIZED = '{ "sta-authorized": {"address":"02:00:00:00:01:00","ifname":"phy0-ap0","auth-alg":"open"} }\n'
DEAUTH = '{ "deauth": {"address":"02:00:00:00:01:00","ifname":"phy0-ap0"} }\n'
DISASSOC = '{ "disassoc": {"address":"02:00:00:00:01:00","ifname":"phy0-ap0"} }\n'
ASSOC_22 = '{ "assoc": {"address":"02:00:00:00:01:00","target":"02:00:00:00:00:00","signal":-14,"freq":2437} }\n'
REMOVED = '{ "ubus.object.remove": {"id":1834731444,"path":"hostapd.phy0-ap0"} }\n'
ADDED = '{ "ubus.object.add": {"id":-821137894,"path":"hostapd.phy0-ap0"} }\n'


@pytest.fixture(autouse=True)
def short_waits(monkeypatch):
    monkeypatch.setattr(openwrt_ssh, "WATCH_ASSOC_DELAY", 0.05)
    monkeypatch.setattr(openwrt_ssh, "WATCH_SETTLE_SECONDS", 0.01)


def test_authorized_counts_at_once_and_its_assoc_not_again() -> None:
    changes: list[int] = []

    async def main():
        arrivals = _Arrivals(lambda: changes.append(1))
        for line in (PROBE, AUTH, ASSOC, AUTHORIZED, "Command failed\n", "{}\n"):
            arrivals.line(line)
        assert changes == [1]
        await asyncio.sleep(0.1)  # the assoc's delayed check sees it authorized

    asyncio.run(main())
    assert changes == [1]


def test_without_authorized_events_assoc_counts_a_moment_later() -> None:
    changes: list[int] = []

    async def main():
        arrivals = _Arrivals(lambda: changes.append(1))
        arrivals.line(ASSOC_22)
        assert changes == []  # not yet: the key handshake follows the assoc
        await asyncio.sleep(0.1)

    asyncio.run(main())
    assert changes == [1]


def test_leaving_and_probes_count_for_nothing() -> None:
    changes: list[int] = []

    async def main():
        arrivals = _Arrivals(lambda: changes.append(1))
        for line in (PROBE, DEAUTH, DISASSOC, AUTH):
            arrivals.line(line)
        await asyncio.sleep(0.1)

    asyncio.run(main())
    assert changes == []


class Feed:
    """A process whose stdout yields queued lines; "" ends it."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.closed = False
        feed = self

        class Stdout:
            async def readline(self) -> str:
                return await feed.queue.get()

        self.stdout = Stdout()

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, objects: list[str]) -> None:
        self.objects = objects
        self.listen = Feed()
        self.subscribes: list[tuple[str, Feed]] = []

    def connect(self, *args, **kwargs):
        self.connect_kwargs = kwargs
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def run(self, command, **kwargs):
        assert command == WATCH_LIST_COMMAND
        return type("Result", (), {"stdout": "\n".join(self.objects) + "\n"})()

    async def create_process(self, command, **kwargs):
        # Each runs in the self-cleaning shell; the ubus command comes first.
        ubus = command.split(" & ", 1)[0]
        assert "trap" in command and "echo" in command
        if ubus == WATCH_OBJECTS_COMMAND:
            return self.listen
        feed = Feed()
        self.subscribes.append((ubus, feed))
        return feed


def install(monkeypatch, objects: list[str]) -> FakeConnection:
    import asyncssh

    fake = FakeConnection(objects)
    monkeypatch.setattr(asyncssh, "connect", fake.connect)
    return fake


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0.02)


def test_subscribes_to_every_interface_and_polls_for_a_join(monkeypatch) -> None:
    fake = install(monkeypatch, ["hostapd.phy0-ap0", "hostapd.phy1-ap0"])
    changes: list[int] = []

    async def main():
        task = asyncio.ensure_future(OpenWrtSsh(CONFIG).async_watch(lambda: changes.append(1)))
        await settle()
        assert [command for command, _ in fake.subscribes] == [
            "ubus subscribe hostapd.phy0-ap0 hostapd.phy1-ap0"
        ]
        feed = fake.subscribes[0][1]
        for line in (PROBE, AUTH, ASSOC, AUTHORIZED):
            feed.queue.put_nowait(line)
        await settle()
        assert changes == [1]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert fake.connect_kwargs["keepalive_interval"] == 30  # dropbear answers it


def test_a_wifi_reload_subscribes_again_and_polls(monkeypatch) -> None:
    # hostapd's objects get new ids on a reload and the old subscribe goes silent
    # without ending: the object events make the watch subscribe again.
    fake = install(monkeypatch, ["hostapd.phy0-ap0"])
    changes: list[int] = []

    async def main():
        task = asyncio.ensure_future(OpenWrtSsh(CONFIG).async_watch(lambda: changes.append(1)))
        await settle()
        fake.listen.queue.put_nowait(REMOVED)
        fake.listen.queue.put_nowait(ADDED)
        await settle()
        assert len(fake.subscribes) == 2
        assert fake.subscribes[0][1].closed  # the silent one is let go
        assert changes == [1]  # clients rejoin after a reload: one poll
        fake.subscribes[1][1].queue.put_nowait(AUTHORIZED)
        await settle()
        assert changes == [1, 1]
        assert not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())


def test_no_hostapd_yet_waits_for_one(monkeypatch) -> None:
    fake = install(monkeypatch, [])

    async def main():
        task = asyncio.ensure_future(OpenWrtSsh(CONFIG).async_watch(lambda: None))
        await settle()
        assert fake.subscribes == [] and not task.done()
        fake.objects.append("hostapd.wlan0")
        fake.listen.queue.put_nowait('{ "ubus.object.add": {"id":1,"path":"hostapd.wlan0"} }\n')
        await settle()
        assert [command for command, _ in fake.subscribes] == ["ubus subscribe hostapd.wlan0"]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())


def test_an_ending_feed_ends_the_watch(monkeypatch) -> None:
    fake = install(monkeypatch, ["hostapd.phy0-ap0"])

    async def main():
        task = asyncio.ensure_future(OpenWrtSsh(CONFIG).async_watch(lambda: None))
        await settle()
        fake.subscribes[0][1].queue.put_nowait("")
        with pytest.raises(AccessPointError, match="event feed ended"):
            await asyncio.wait_for(task, 1)

    asyncio.run(main())


def test_an_ending_object_feed_ends_the_watch(monkeypatch) -> None:
    fake = install(monkeypatch, ["hostapd.phy0-ap0"])

    async def main():
        task = asyncio.ensure_future(OpenWrtSsh(CONFIG).async_watch(lambda: None))
        await settle()
        fake.listen.queue.put_nowait("")
        with pytest.raises(AccessPointError, match="object feed ended"):
            await asyncio.wait_for(task, 1)

    asyncio.run(main())


def test_openwrt_watches() -> None:
    assert OpenWrtSsh.WATCHES is True
    assert OpenWrtSsh(CONFIG).watch_key() != OpenWrtSsh({**CONFIG, "host": "192.0.2.2"}).watch_key()


def test_heartbeat_lines_count_for_nothing(monkeypatch) -> None:
    fake = install(monkeypatch, ["hostapd.phy0-ap0"])
    changes: list[int] = []

    async def main():
        task = asyncio.ensure_future(OpenWrtSsh(CONFIG).async_watch(lambda: changes.append(1)))
        await settle()
        fake.listen.queue.put_nowait("\n")
        fake.subscribes[0][1].queue.put_nowait("\n")
        await settle()
        assert changes == [] and len(fake.subscribes) == 1 and not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert fake.listen.closed and fake.subscribes[0][1].closed

    asyncio.run(main())


def test_assoc_then_leaving_within_the_delay_polls_for_nothing() -> None:
    changes: list[int] = []

    async def main():
        arrivals = _Arrivals(lambda: changes.append(1))
        arrivals.line(ASSOC_22)
        arrivals.line(DEAUTH)
        await asyncio.sleep(0.1)

    asyncio.run(main())
    assert changes == []


def test_pending_assoc_checks_are_cancelled_when_the_watch_ends() -> None:
    changes: list[int] = []

    async def main():
        arrivals = _Arrivals(lambda: changes.append(1))
        arrivals.line(ASSOC_22)
        arrivals.close()  # the feed failed before the delay ran out
        await asyncio.sleep(0.1)
        arrivals.line(ASSOC_22)  # a new one after closing still works
        await asyncio.sleep(0.1)

    asyncio.run(main())
    assert changes == [1]


def test_a_watch_that_ends_leaves_no_delayed_poll(monkeypatch) -> None:
    fake = install(monkeypatch, ["hostapd.wlan0"])
    changes: list[int] = []

    async def main():
        task = asyncio.ensure_future(OpenWrtSsh(CONFIG).async_watch(lambda: changes.append(1)))
        await settle()
        feed = fake.subscribes[0][1]
        feed.queue.put_nowait(ASSOC_22)
        feed.queue.put_nowait("")  # the feed ends right after the assoc
        with pytest.raises(AccessPointError):
            await asyncio.wait_for(task, 1)
        await asyncio.sleep(0.1)  # past the assoc delay

    asyncio.run(main())
    assert changes == []
