"""Cisco Catalyst 9800 over SNMP: parsing the walked columns, credentials, shared reads.

The fixture holds the columns the driver reads, from a real C9800-CL (IOS-XE 17.15.5)
SNMPv3 report sent with the integration's collector.
"""

from __future__ import annotations

import ast
import asyncio
import socket
import time
from pathlib import Path

import pytest

from wifi_ap_associations import DRIVERS, AccessPointAuthError, AccessPointError
from wifi_ap_associations import cisco_wlc_snmp
from wifi_ap_associations.cisco_wlc import SharedReads, poll_result
from wifi_ap_associations.cisco_wlc_snmp import (
    AP_COLUMNS,
    CLIENT_COLUMNS,
    CiscoWlcSnmp,
    parse_columns,
)

FIXTURE = Path(__file__).parent / "fixtures" / "cisco-c9800-cl-17.15.5-snmp.txt"
COLUMN_KEYS = {
    **{oid: ("aps", key) for key, oid in AP_COLUMNS.items()},
    **{oid: ("clients", key) for key, oid in CLIENT_COLUMNS.items()},
}


def _value(text: str):
    kind, _, value = text.partition(": ")
    if kind == "MAC":
        return bytes.fromhex(value.replace(":", ""))
    if kind == "STRING":
        return ast.literal_eval(value).encode()
    return int(value)


def fixture_rows() -> list[tuple[str, str, object]]:
    """(column OID, row MAC, value) for every value line of the fixture."""
    rows = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        oid, value = line.split(" = ", 1)
        column, mac = oid.split(".[")
        rows.append((column, mac.rstrip("]"), _value(value)))
    return rows


def fixture_columns() -> tuple[dict, dict]:
    tables: dict[str, dict] = {"aps": {}, "clients": {}}
    for column, mac, value in fixture_rows():
        table, key = COLUMN_KEYS[column]
        tables[table].setdefault(key, {})[mac] = value
    return tables["aps"], tables["clients"]


CONFIG = {"host": "192.0.2.20", "port": 161, "community": "public-ro"}


def test_registered_as_experimental_with_optional_credentials() -> None:
    assert DRIVERS["cisco_wlc_snmp"] is CiscoWlcSnmp
    assert CiscoWlcSnmp.EXPERIMENTAL is True
    assert CiscoWlcSnmp.UNIQUE_FIELD == "ap_name"
    assert CiscoWlcSnmp.BLOCKING_IMPORTS == ("pysnmp.hlapi.v3arch.asyncio",)
    fields = {f.key: f for f in CiscoWlcSnmp.FIELDS}
    assert all(fields[k].optional for k in ("community", "username", "auth_key", "priv_key"))
    assert all(fields[k].secret for k in ("community", "auth_key", "priv_key"))
    assert "sha256" in fields["auth_protocol"].choices
    assert fields["priv_protocol"].default == "aes"


def test_real_controller_columns() -> None:
    data = parse_columns(*fixture_columns())
    assert set(data.aps) == {"AP01", "AP02"}
    assert data.aps["AP01"].model == "C9120AXI-B"
    assert data.aps["AP01"].state == "Registered"
    assert data.aps["AP01"].location is None  # "default location"
    ap01, ap02 = poll_result(data, "AP01"), poll_result(data, "AP02")
    # As the reporter's Home Assistant showed at the same time: 16 and 7.
    assert (len(ap01.clients), len(ap02.clients)) == (16, 7)
    by_mac = {c.mac: c for c in [*ap01.clients, *ap02.clients]}
    # Protocol codes matched against the same clients' `show wireless client summary`.
    assert by_mac["38:DE:AD:00:00:04"].band == "5GHz"  # 10 = 11ac
    assert by_mac["A4:F6:E8:00:00:11"].band == "5GHz"  # 11 = 11ax(5)
    assert by_mac["B0:A4:60:00:00:15"].band == "2.4GHz"  # 12 = 11ax(2.4)
    assert by_mac["50:02:91:00:00:06"].band == "2.4GHz"  # 3 = 11g
    assert by_mac["38:DE:AD:00:00:04"].ssid == "<redacted5GHzNet>"
    assert all(c.signal is None for c in by_mac.values())
    assert ap01.info.name == "AP01" and ap01.info.model == "C9120AXI-B"


def test_client_missing_from_a_column_is_left_out_of_this_read() -> None:
    aps, clients = fixture_columns()
    del clients["ap"]["38:DE:AD:00:00:04"]  # joined between the column walks
    data = parse_columns(aps, clients)
    assert "38:DE:AD:00:00:04" not in {c.mac for c in data.clients}
    assert len(data.clients) == 22


def test_only_run_state_counts() -> None:
    aps, clients = fixture_columns()
    clients["state"]["38:DE:AD:00:00:04"] = b"IP Learn"
    assert "38:DE:AD:00:00:04" not in {c.mac for c in poll_result(parse_columns(aps, clients), "AP01").clients}


def test_unknown_band_code_gives_no_band() -> None:
    aps, clients = fixture_columns()
    clients["protocol"]["38:DE:AD:00:00:04"] = 99
    client = next(c for c in parse_columns(aps, clients).clients if c.mac == "38:DE:AD:00:00:04")
    assert client.band is None


def test_unknown_or_not_associated_ap_is_an_error() -> None:
    aps, clients = fixture_columns()
    with pytest.raises(AccessPointError, match="not joined"):
        poll_result(parse_columns(aps, clients), "AP03")
    ap01_mac = next(mac for mac, name in aps["name"].items() if name == b"AP01")
    aps["status"][ap01_mac] = 3
    with pytest.raises(AccessPointError, match="Downloading"):
        poll_result(parse_columns(aps, clients), "AP01")


def test_no_clients() -> None:
    aps, _clients = fixture_columns()
    assert poll_result(parse_columns(aps, {}), "AP01").clients == []


@pytest.mark.parametrize(
    ("config", "error"),
    [
        ({"host": "h"}, "community"),
        ({"host": "h", "username": "ro"}, "authentication key"),
    ],
)
def test_missing_credentials_are_auth_errors(config: dict, error: str) -> None:
    import pysnmp.hlapi.v3arch.asyncio as hlapi

    with pytest.raises(AccessPointAuthError, match=error):
        CiscoWlcSnmp(config)._auth(hlapi)


def test_v3_user_and_algorithms() -> None:
    import pysnmp.hlapi.v3arch.asyncio as hlapi

    auth = CiscoWlcSnmp(
        {"host": "h", "username": "ro", "auth_key": "authsecret1",
         "auth_protocol": "sha256", "priv_protocol": "aes"}
    )._auth(hlapi)
    assert isinstance(auth, hlapi.UsmUserData)
    assert auth.authentication_protocol == hlapi.usmHMAC192SHA256AuthProtocol
    assert auth.privacy_protocol == hlapi.usmAesCfb128Protocol
    assert isinstance(CiscoWlcSnmp(CONFIG)._auth(hlapi), hlapi.CommunityData)


class FakeAgent:
    """pysnmp's hlapi, answering walks from the fixture; counts engines and walks."""

    def __init__(self, error: str | None = None) -> None:
        import pysnmp.hlapi.v3arch.asyncio as hlapi

        self.real = hlapi
        self.error = error
        self.engines = 0
        self.closed = 0
        self.walks: list[str] = []
        self.by_column: dict[str, list[tuple[str, object]]] = {}
        for column, mac, value in fixture_rows():
            index = ".".join(str(int(part, 16)) for part in mac.split(":"))
            self.by_column.setdefault(column, []).append((f"{column}.{index}", value))

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent = self
        hlapi = self.real

        class Engine:
            def __init__(self) -> None:
                agent.engines += 1

            def close_dispatcher(self) -> None:
                agent.closed += 1

        class Target:
            @staticmethod
            async def create(address, **kwargs):
                return address

        class Wrapped:
            def __init__(self, raw) -> None:
                self.raw = raw

            def asOctets(self):
                if isinstance(self.raw, bytes):
                    return self.raw
                raise AttributeError

            def __int__(self) -> int:
                return self.raw

        async def bulk_walk_cmd(engine, auth, target, context, _n, _r, column, **kwargs):
            agent.walks.append(column)
            if agent.error:
                yield agent.error, 0, 0, []
                return
            for oid, value in agent.by_column.get(column, []):
                yield None, 0, 0, [(oid, Wrapped(value))]

        monkeypatch.setattr(hlapi, "SnmpEngine", Engine)
        monkeypatch.setattr(hlapi, "UdpTransportTarget", Target)
        monkeypatch.setattr(hlapi, "ObjectIdentity", lambda oid: oid)
        monkeypatch.setattr(hlapi, "ObjectType", lambda identity: identity)
        monkeypatch.setattr(hlapi, "bulk_walk_cmd", bulk_walk_cmd)
        monkeypatch.setattr(cisco_wlc_snmp, "_shared", cisco_wlc_snmp.SharedReads(20))


def test_aps_polled_together_share_one_read(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = FakeAgent()
    agent.install(monkeypatch)

    async def poll_both():
        return await asyncio.gather(
            CiscoWlcSnmp({**CONFIG, "ap_name": "AP01"}).async_poll(),
            CiscoWlcSnmp({**CONFIG, "ap_name": "AP02"}).async_poll(),
        )

    ap01, ap02 = asyncio.run(poll_both())
    assert (len(ap01.clients), len(ap02.clients)) == (16, 7)
    assert agent.engines == 1 and agent.closed == 1
    assert sorted(agent.walks) == sorted([*AP_COLUMNS.values(), *CLIENT_COLUMNS.values()])


@pytest.mark.parametrize(
    ("indication", "error"),
    [
        ("Wrong SNMP PDU digest", AccessPointAuthError),
        ("No SNMP response received before timeout", AccessPointError),
    ],
)
def test_snmp_errors(monkeypatch: pytest.MonkeyPatch, indication: str, error: type) -> None:
    agent = FakeAgent(error=indication)
    agent.install(monkeypatch)
    with pytest.raises(error):
        asyncio.run(CiscoWlcSnmp({**CONFIG, "ap_name": "AP01"}).async_poll())
    assert agent.closed == agent.engines == 1  # the engine is released on errors too


def test_no_answer_on_real_pysnmp(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real pysnmp, nothing listening: a plain AccessPointError, engine closed."""
    monkeypatch.setattr(cisco_wlc_snmp, "SNMP_TIMEOUT", 0.2)
    monkeypatch.setattr(cisco_wlc_snmp, "SNMP_RETRIES", 0)
    monkeypatch.setattr(cisco_wlc_snmp, "_shared", cisco_wlc_snmp.SharedReads(20))
    driver = CiscoWlcSnmp({"host": "127.0.0.1", "port": 9, "community": "x", "ap_name": "AP01"})
    with pytest.raises(AccessPointError, match="No SNMP response"):
        asyncio.run(driver.async_poll())


# --- review follow-ups: identity, version, snapshots, timing, failed reads ----------


def test_identity_is_controller_and_ap_name() -> None:
    from wifi_ap_associations.cisco_wlc_ssh import CiscoWlcSsh

    a = CiscoWlcSnmp.identity({"host": "192.0.2.20", "ap_name": "AP01"})
    assert a == ("192.0.2.20", "ap01")
    # Another controller's AP01 is another AP.
    assert CiscoWlcSnmp.identity({"host": "192.0.2.30", "ap_name": "AP01"}) != a
    # The same AP over SSH is the same AP (port and credentials don't matter).
    assert CiscoWlcSsh.identity({"host": "192.0.2.20 ", "port": 22, "ap_name": "AP01"}) == a
    assert CiscoWlcSnmp.identity({"host": "192.0.2.20"}) is None  # incomplete


def test_snmp_version_decides_which_credentials_count() -> None:
    import pysnmp.hlapi.v3arch.asyncio as hlapi

    stale_v3 = {"host": "h", "username": "ro", "auth_key": "authsecret1", "community": "ro-c"}
    # Switched to v2c: the stored v3 user is ignored, no clearing needed.
    auth = CiscoWlcSnmp({**stale_v3, "snmp_version": "v2c"})._auth(hlapi)
    assert isinstance(auth, hlapi.CommunityData)
    auth = CiscoWlcSnmp({**stale_v3, "snmp_version": "v3"})._auth(hlapi)
    assert isinstance(auth, hlapi.UsmUserData)
    # Saved before snmp_version existed: v3 if a user is set, as before.
    assert isinstance(CiscoWlcSnmp(stale_v3)._auth(hlapi), hlapi.UsmUserData)
    with pytest.raises(AccessPointAuthError, match="user"):
        CiscoWlcSnmp({"host": "h", "snmp_version": "v3", "auth_key": "k"})._auth(hlapi)


def test_half_read_client_keeps_its_previous_ap_for_one_read() -> None:
    aps, clients = fixture_columns()
    previous = parse_columns(aps, clients)
    roaming = "38:DE:AD:00:00:04"
    # Its row vanished from the AP and state columns while they were walked.
    del clients["ap"][roaming]
    del clients["state"][roaming]
    data = parse_columns(aps, clients, previous)
    assert roaming in {c.mac for c in poll_result(data, "AP01").clients}
    # Gone from every column: gone.
    for column in clients.values():
        column.pop(roaming, None)
    assert roaming not in {c.mac for c in parse_columns(aps, clients, data).clients}


def test_client_without_previous_read_waits_for_complete_details() -> None:
    aps, clients = fixture_columns()
    new = "38:DE:AD:00:00:04"
    del clients["state"][new]  # joined between the column walks
    assert new not in {c.mac for c in parse_columns(aps, clients).clients}


def test_columns_are_walked_at_the_same_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Eight walks of 0.3 s each: together about 0.3 s, not 2.4 s."""
    agent = FakeAgent()
    agent.install(monkeypatch)
    import pysnmp.hlapi.v3arch.asyncio as hlapi

    walk = hlapi.bulk_walk_cmd

    async def slow_walk(*args, **kwargs):
        await asyncio.sleep(0.3)
        async for answer in walk(*args, **kwargs):
            yield answer

    monkeypatch.setattr(hlapi, "bulk_walk_cmd", slow_walk)
    started = time.monotonic()
    asyncio.run(CiscoWlcSnmp({**CONFIG, "ap_name": "AP01"}).async_poll())
    assert time.monotonic() - started < 1.2


def test_failed_read_is_not_reused_by_the_next_poll() -> None:
    reads: list[int] = []

    async def failing_read():
        reads.append(1)
        raise AccessPointError("no answer")

    async def main():
        shared = SharedReads(20)
        for _ in range(2):
            with pytest.raises(AccessPointError):
                await shared.read("controller", failing_read)

    asyncio.run(main())
    assert len(reads) == 2  # the second poll tried again


def test_concurrent_polls_share_a_failure() -> None:
    reads: list[int] = []

    async def failing_read():
        reads.append(1)
        await asyncio.sleep(0.05)
        raise AccessPointError("no answer")

    async def main():
        shared = SharedReads(20)
        return await asyncio.gather(
            shared.read("c", failing_read),
            shared.read("c", failing_read),
            return_exceptions=True,
        )

    results = asyncio.run(main())
    assert all(isinstance(r, AccessPointError) for r in results) and len(reads) == 1


# --- end to end: the real pysnmp against a local agent serving the fixture ----------


def _agent_rows():
    from pysnmp.proto.api import v2c

    rows = []
    for column, mac, value in fixture_rows():
        index = tuple(int(part, 16) for part in mac.split(":"))
        oid = tuple(int(part) for part in column.split(".")) + index
        snmp_value = v2c.OctetString(value) if isinstance(value, bytes) else v2c.Integer(value)
        rows.append((oid, snmp_value))
    return sorted(rows)


async def _local_agent():
    """An SNMP agent on 127.0.0.1 answering GET/GETNEXT/GETBULK from the fixture.

    v2c community "public"; v3 user "ro" with SHA-256 "authsecret1" and AES "privsecret1".
    """
    from pysnmp.carrier.asyncio.dgram import udp
    from pysnmp.entity import config, engine
    from pysnmp.entity.rfc3413 import cmdrsp, context
    from pysnmp.proto import rfc1905
    from pysnmp.proto.api import v2c
    from pysnmp.smi import instrum

    data = _agent_rows()

    class Table(instrum.AbstractMibInstrumController):
        def read_variables(self, *var_binds, **ctx):
            return [(name, rfc1905.noSuchInstance) for name, _ in var_binds]

        def read_next_variables(self, *var_binds, **ctx):
            answers = []
            for name, _ in var_binds:
                found = next(((o, v) for o, v in data if o > tuple(name)), None)
                answers.append(
                    (v2c.ObjectIdentifier(found[0]), found[1])
                    if found
                    else (name, rfc1905.endOfMibView)
                )
            return answers

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    agent = engine.SnmpEngine()
    config.add_transport(
        agent, udp.DOMAIN_NAME, udp.UdpTransport().open_server_mode(("127.0.0.1", port))
    )
    config.add_v1_system(agent, "area", "public")
    config.add_vacm_user(agent, 2, "area", "noAuthNoPriv", (1, 3, 6))
    config.add_v3_user(
        agent, "ro", config.USM_AUTH_HMAC192_SHA256, "authsecret1",
        config.USM_PRIV_CFB128_AES, "privsecret1",
    )
    config.add_vacm_user(agent, 3, "ro", "authPriv", (1, 3, 6))
    snmp_context = context.SnmpContext(agent)
    snmp_context.unregister_context_name(b"")
    snmp_context.register_context_name(b"", Table())
    for responder in (
        cmdrsp.GetCommandResponder, cmdrsp.NextCommandResponder, cmdrsp.BulkCommandResponder
    ):
        responder(agent, snmp_context)
    return agent, port


V3 = {
    "snmp_version": "v3", "username": "ro", "auth_key": "authsecret1",
    "auth_protocol": "sha256", "priv_key": "privsecret1", "priv_protocol": "aes",
}


@pytest.mark.parametrize(
    "credentials", [{"snmp_version": "v2c", "community": "public"}, V3], ids=["v2c", "v3"]
)
def test_real_pysnmp_against_a_local_agent(
    monkeypatch: pytest.MonkeyPatch, credentials: dict
) -> None:
    monkeypatch.setattr(cisco_wlc_snmp, "_shared", cisco_wlc_snmp.SharedReads(20))

    async def main():
        agent, port = await _local_agent()
        try:
            config = {"host": "127.0.0.1", "port": port, **credentials}
            return await asyncio.gather(
                CiscoWlcSnmp({**config, "ap_name": "AP01"}).async_poll(),
                CiscoWlcSnmp({**config, "ap_name": "AP02"}).async_poll(),
            )
        finally:
            agent.close_dispatcher()

    ap01, ap02 = asyncio.run(main())
    assert (len(ap01.clients), len(ap02.clients)) == (16, 7)
    assert ap01.info.model == "C9120AXI-B"
    assert {c.band for c in ap01.clients} == {"2.4GHz", "5GHz"}


def test_real_pysnmp_wrong_key_is_an_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cisco_wlc_snmp, "_shared", cisco_wlc_snmp.SharedReads(20))

    async def main():
        agent, port = await _local_agent()
        try:
            await CiscoWlcSnmp(
                {"host": "127.0.0.1", "port": port, "ap_name": "AP01", **V3,
                 "auth_key": "wrongsecret"}
            ).async_poll()
        finally:
            agent.close_dispatcher()

    with pytest.raises(AccessPointAuthError):
        asyncio.run(main())


# --- SharedReads lifecycle: stuck reads, timed-out callers, old reads -----------------


def _data(name: str = "AP01"):
    from wifi_ap_associations.cisco_wlc import ControllerData, JoinedAp

    return ControllerData(
        aps={name: JoinedAp(name=name, model=None, state="Registered", location=None)},
        ssids={},
        clients=[],
    )


def test_stuck_read_fails_within_its_timeout_and_the_next_poll_reads_again() -> None:
    """A read that never answers can't be joined poll after poll."""
    reads: list[str] = []

    async def stuck():
        reads.append("stuck")
        await asyncio.Event().wait()  # never set

    async def answers():
        reads.append("fresh")
        return _data()

    async def main():
        shared = SharedReads(20, timeout=0.2)
        # This poll gives up first (its own, shorter limit); the read goes on, shielded.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(shared.read("c", stuck), 0.05)
        # Another poll joins the still-running read and sees it stopped at its timeout.
        with pytest.raises(AccessPointError, match="over 0.2 s"):
            await shared.read("c", stuck)
        # The next poll starts a fresh read rather than reusing the failure.
        return await shared.read("c", answers)

    assert asyncio.run(main()) == _data()
    assert reads == ["stuck", "fresh"]


def test_last_read_is_kept_for_a_while_then_dropped() -> None:
    async def main():
        shared = SharedReads(0, keep=0.1)

        async def first():
            return _data("AP01")

        await shared.read("old-credentials", first)
        assert shared.last("old-credentials") == _data("AP01")
        await asyncio.sleep(0.15)
        assert shared.last("old-credentials") is None
        # Any later read drops what is too old, also under keys no longer used.
        await shared.read("new-credentials", first)
        return shared

    shared = asyncio.run(main())
    assert set(shared._last) == {"new-credentials"}
    assert set(shared._reads) == {"new-credentials"}


def test_failed_read_leaves_the_last_good_one() -> None:
    async def main():
        shared = SharedReads(0)

        async def good():
            return _data()

        async def bad():
            raise AccessPointError("no answer")

        await shared.read("c", good)
        with pytest.raises(AccessPointError):
            await shared.read("c", bad)
        return shared.last("c")

    assert asyncio.run(main()) == _data()
