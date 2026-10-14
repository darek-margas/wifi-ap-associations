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
    # A client no poll has seen yet counts on each line (the integration's debounce makes
    # one poll of them); the header and the blank line count for nothing.
    assert len(changes) == 2
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


# --- what follow-only really prints (RouterOS 7.24.4, #11); MACs made up --------------

M1, M2 = "5C:AD:BA:00:00:01", "5C:AD:BA:00:00:02"
FOOTER = "-- Ctrl-C to quit. Space prints separator. New entries will appear at bottom.\n"


def line(mac: str, interface: str, authorized: bool = True) -> str:
    flags = " A " if authorized else "   "
    return (f"{flags}comment=management interface={interface} ssid=SSID mac-address={mac} "
            f"uptime=0s signal=-61 auth-type=wpa2-psk-sha2 band=2ghz-n vlan-id=33 \n")


# M2 roams downstairs -> upstairs -> back; M1 joins (unauthorized first), roams, and then
# keeps re-authenticating on the same interface, as the tester's capture shows.
ROAMS = [
    line(M2, "ap_downstairs 2"),
    line(M2, "ap_upstairs "),
    line(M1, "ap_upstairs 2", authorized=False),
    line(M1, "ap_upstairs 2"),
    line(M1, "ap_downstairs 2", authorized=False),
    line(M1, "ap_downstairs 2"),
    line(M2, "ap_upstairs 2"),
    line(M2, "ap_downstairs "),
    line(M1, "ap_upstairs 2", authorized=False),
    line(M1, "ap_upstairs 2"),
    *[line(M1, "ap_upstairs 2", authorized=a) for a in (True, False) * 10],
    FOOTER,
]


def watching(present: set[str]) -> MikroTikSsh:
    """A driver whose last poll saw these clients."""
    driver = MikroTikSsh(CONFIG)
    driver._present = frozenset(present)
    return driver


def test_real_capture_polls_for_arrivals_and_roams_not_for_flapping(monkeypatch) -> None:
    install(monkeypatch, {WATCH_COMMANDS["wifi"]: ROAMS, WATCH_COMMANDS["wireless"]: MISSING})
    changes: list[int] = []
    # Both phones are home (the polls see them) while they roam and re-authenticate.
    with pytest.raises(AccessPointError):
        asyncio.run(watching({M1, M2}).async_watch(lambda: changes.append(1)))
    # M2: 4 interfaces; M1: joined, roamed, roamed back; then 20 re-authentications: none.
    assert len(changes) == 7


def test_wireless_lines_have_no_flags_and_count(monkeypatch) -> None:
    plain = "interface=wlan33 mac-address=5C:AD:BA:00:00:03 ap=no wds=no bridge=no \n"
    install(monkeypatch, {WATCH_COMMANDS["wifi"]: [], WATCH_COMMANDS["wireless"]: [plain, plain, plain]})
    changes: list[int] = []
    with pytest.raises(AccessPointError):
        asyncio.run(watching({"5C:AD:BA:00:00:03"}).async_watch(lambda: changes.append(1)))
    assert len(changes) == 1  # a present client three times in a row: one poll


def test_same_client_counts_again_after_a_while(monkeypatch) -> None:
    from wifi_ap_associations import mikrotik_ssh

    flt = mikrotik_ssh._ChangeFilter(present=lambda mac: True)
    one = line(M1, "ap_upstairs 2")
    assert flt.wanted(one, True, 0.0)
    assert not flt.wanted(one, True, 30.0)
    assert flt.wanted(one, True, 30.0 + mikrotik_ssh.WATCH_REPEAT_SECONDS)


# --- review of 0.10.0b3: a reconnect after leaving is an arrival, whatever the timing ---


def test_reconnect_after_the_poll_saw_it_leave_counts_at_once() -> None:
    from wifi_ap_associations import mikrotik_ssh

    home: set[str] = {M1}
    flt = mikrotik_ssh._ChangeFilter(present=lambda mac: mac in home)
    wlan = "interface=wlan33 mac-address=5C:AD:BA:00:00:01 ap=no wds=no bridge=no\n"
    assert flt.wanted(wlan, False, 0.0)  # first line for it
    assert not flt.wanted(wlan, False, 2.0)  # re-authenticating while home
    home.clear()  # it left; the next poll no longer sees it (leaving prints nothing)
    assert flt.wanted(wlan, False, 20.0)  # back 20 s later, same interface: an arrival
    assert flt.wanted(wlan, False, 22.0)  # and until a poll sees it again, each line counts
    home.add(M1)
    assert not flt.wanted(wlan, False, 24.0)


def test_reconnect_between_two_polls_counts_within_the_repeat_window() -> None:
    from wifi_ap_associations import mikrotik_ssh

    flt = mikrotik_ssh._ChangeFilter(present=lambda mac: True)  # no poll noticed it left
    wlan = "interface=wlan33 mac-address=5C:AD:BA:00:00:01 ap=no wds=no bridge=no\n"
    assert flt.wanted(wlan, False, 0.0)
    assert not flt.wanted(wlan, False, 30.0)
    assert flt.wanted(wlan, False, mikrotik_ssh.WATCH_REPEAT_SECONDS)


def test_the_driver_remembers_who_its_last_poll_saw(monkeypatch) -> None:
    from wifi_ap_associations import mikrotik_ssh

    driver = MikroTikSsh(CONFIG)

    async def run_all(self):
        return {
            "identity": "  name: cAP\n",
            "resource": "  uptime: 1h\n  version: 7.24.4 (stable)\n  board-name: RBcAPL-2nD\n",
            "wifi_interfaces": "", "wifi_clients": "",
            "wireless_interfaces": "", "wireless_stats": "",
            "wireless_clients": " 0 interface=wlan33 mac-address=5C:AD:BA:00:00:01 ap=no wds=no\n",
        }

    monkeypatch.setattr(mikrotik_ssh.MikroTikSsh, "_run_all", run_all)
    asyncio.run(driver.async_poll())
    assert driver._present == frozenset({M1})


def test_lines_arrive_one_by_one_over_a_real_ssh_exec_stream() -> None:
    """A real SSH server streams follow-only lines with pauses; each must reach the
    callback when it is sent, not when the stream ends (our side doesn't buffer)."""
    import time

    import asyncssh

    gap = 0.5
    sent_at: list[float] = []
    received_at: list[float] = []

    async def handle(process) -> None:
        if "wireless" in process.command:
            process.stdout.write("bad command name wireless (line 1 column 12)\n")
            process.exit(0)
            return
        for n in range(3):
            await asyncio.sleep(gap)
            sent_at.append(time.monotonic())
            process.stdout.write(
                f" A interface=wifi1 ssid=Home mac-address=5C:AD:BA:00:01:0{n} uptime=0s\r\n"
            )
        await asyncio.sleep(gap)
        process.exit(0)

    class Server(asyncssh.SSHServer):
        def begin_auth(self, username: str) -> bool:
            return True

        def password_auth_supported(self) -> bool:
            return True

        def validate_password(self, username: str, password: str) -> bool:
            return password == "x"

    async def main() -> None:
        server = await asyncssh.create_server(
            Server, "127.0.0.1", 0,
            server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
            process_factory=handle,
        )
        port = server.sockets[0].getsockname()[1]
        try:
            driver = MikroTikSsh({"host": "127.0.0.1", "port": port, "username": "ha", "password": "x"})
            with pytest.raises(AccessPointError, match="ended"):
                await driver.async_watch(lambda: received_at.append(time.monotonic()))
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(main())
    assert len(received_at) == 3
    # Each line within a fraction of the gap after it was sent.
    assert all(received - sent < gap * 0.8 for sent, received in zip(sent_at, received_at))
