"""TP-Link EAP (standalone) over SNMP.

The walks are those of an EAP670 (firmware 1.3.13 build 20260828) with 11 clients, from
a report in discussion #4: the MACs keep their vendor prefixes, the rest is replaced.
"""

from __future__ import annotations

import asyncio

import pytest

from wifi_ap_associations import (
    DRIVERS,
    AccessPointAuthError,
    AccessPointError,
    tplink_eap_snmp,
)
from wifi_ap_associations.tplink_eap_snmp import (
    CLIENT_COUNT,
    CLIENT_MACS,
    SYS_DESCR,
    SYS_NAME,
    TplinkEapSnmp,
    client_mac,
    model_from_descr,
    parse,
)

PREFIXES = [
    "34-85-18", "8C-4F-00", "B0-F2-F6", "C0-39-37", "C0-39-37", "C8-C9-A3",
    "70-C9-32", "82-03-71", "94-27-70", "94-27-70", "34-FD-70",
]
EAP670 = {
    CLIENT_COUNT: {"0": 11},
    CLIENT_MACS: {str(i): f"{p}-00-00-{i:02X}".encode() for i, p in enumerate(PREFIXES, 1)},
    SYS_DESCR: {"0": b"Linux EAP670 4.4.60 #1 SMP PREEMPT Fri Aug 28 02:47:38 UTC 2026 armv7l"},
    SYS_NAME: {"0": b"EAP670"},
}
CONFIG = {"host": "192.0.2.48", "port": 161, "community": "public-ro"}


def test_registered_as_experimental_snmp_driver() -> None:
    assert DRIVERS["tplink_eap_snmp"] is TplinkEapSnmp
    assert TplinkEapSnmp.EXPERIMENTAL is True
    assert TplinkEapSnmp.UNIQUE_FIELD == "host"
    assert TplinkEapSnmp.REPORTS == {"name", "model"}
    assert TplinkEapSnmp.CLIENT_DETAILS == frozenset()  # MACs only
    assert DRIVERS["cisco_wlc_snmp"].CLIENT_DETAILS == {"band", "signal"}  # the default
    keys = [f.key for f in TplinkEapSnmp.FIELDS]
    assert keys[:2] == ["host", "port"] and "community" in keys and "auth_key" in keys


def test_the_eap670_report() -> None:
    result = parse(EAP670)
    assert len(result.clients) == 11
    assert result.clients[0].mac == "34:85:18:00:00:01"
    assert all(c.signal is None and c.ssid is None for c in result.clients)
    assert (result.info.name, result.info.model) == ("EAP670", "EAP670")


@pytest.mark.parametrize(
    ("value", "mac"),
    [
        (b"34-85-18-aa-bb-cc", "34:85:18:AA:BB:CC"),
        (b"34:85:18:AA:BB:CC", "34:85:18:AA:BB:CC"),
        (b"34:85:18:AA:BB:CC\0", "34:85:18:AA:BB:CC"),
        (bytes.fromhex("348518aabbcc"), "34:85:18:AA:BB:CC"),
        (b"34-85-18:AA:BB:CC", None),
        (b"", None),
        (7, None),
    ],
)
def test_client_macs_as_text_or_bytes(value, mac) -> None:
    assert client_mac(value) == mac


def test_model_from_descr() -> None:
    assert model_from_descr("Linux EAP670 4.4.60 #1 SMP") == "EAP670"
    assert model_from_descr("Linux eap225-outdoor 3.3.8") == "EAP225-OUTDOOR"
    assert model_from_descr("Linux OpenWrt") is None
    assert model_from_descr(None) is None


def test_no_clients_is_an_empty_ap() -> None:
    result = parse({**EAP670, CLIENT_COUNT: {"0": 0}, CLIENT_MACS: {}})
    assert result.clients == []


def test_a_device_without_the_table_is_an_error() -> None:
    with pytest.raises(AccessPointError, match="no TP-Link EAP client table"):
        parse({SYS_DESCR: EAP670[SYS_DESCR], SYS_NAME: EAP670[SYS_NAME]})


class FakeAgent:
    """pysnmp's hlapi, answering walks from EAP670; counts engines and walks."""

    def __init__(self, error: str | None = None) -> None:
        import pysnmp.hlapi.v3arch.asyncio as hlapi

        self.real = hlapi
        self.error = error
        self.engines = self.closed = 0
        self.walks: list[str] = []

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
            for row, value in EAP670.get(column, {}).items():
                yield None, 0, 0, [(f"{column}.{row}", Wrapped(value))]

        monkeypatch.setattr(hlapi, "SnmpEngine", Engine)
        monkeypatch.setattr(hlapi, "UdpTransportTarget", Target)
        monkeypatch.setattr(hlapi, "ObjectIdentity", lambda oid: oid)
        monkeypatch.setattr(hlapi, "ObjectType", lambda identity: identity)
        monkeypatch.setattr(hlapi, "bulk_walk_cmd", bulk_walk_cmd)


def test_a_poll_walks_four_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = FakeAgent()
    agent.install(monkeypatch)
    result = asyncio.run(TplinkEapSnmp(CONFIG).async_poll())
    assert len(result.clients) == 11 and result.info.model == "EAP670"
    assert sorted(agent.walks) == sorted([CLIENT_COUNT, CLIENT_MACS, SYS_DESCR, SYS_NAME])
    assert agent.engines == agent.closed == 1


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
        asyncio.run(TplinkEapSnmp(CONFIG).async_poll())
    assert agent.closed == agent.engines == 1


def test_missing_community_is_an_auth_error() -> None:
    with pytest.raises(AccessPointAuthError, match="community"):
        asyncio.run(TplinkEapSnmp({"host": "192.0.2.48"}).async_poll())


def test_no_answer_on_real_pysnmp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tplink_eap_snmp, "SNMP_TIMEOUT", 0.2)
    monkeypatch.setattr(tplink_eap_snmp, "SNMP_RETRIES", 0)
    driver = TplinkEapSnmp({"host": "127.0.0.1", "port": 9, "community": "x"})
    with pytest.raises(AccessPointError, match="No SNMP response"):
        asyncio.run(driver.async_poll())
