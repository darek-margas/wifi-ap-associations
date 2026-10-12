"""Cisco Catalyst 9800 over SNMP: parsing the walked columns, credentials, shared reads.

The fixture holds the columns the driver reads, from a real C9800-CL (IOS-XE 17.15.5)
SNMPv3 report sent with the integration's collector.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from wifi_ap_associations import DRIVERS, AccessPointAuthError, AccessPointError
from wifi_ap_associations import cisco_wlc_snmp
from wifi_ap_associations.cisco_wlc import poll_result
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
    assert agent.walks == [*AP_COLUMNS.values(), *CLIENT_COLUMNS.values()]


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
