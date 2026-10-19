"""SNMP traps: decoding, the shared listener, and D-Link instant arrivals.

The traps are built like those a DAP-2610 sent (SNMPv1, community "dmpublic",
enterprise 1.3.6.1.4.1.171.10.37.59.5.7.2, specific 14 = a client joined, 20 = left,
the access point's MAC as the only variable).
"""

from __future__ import annotations

import asyncio
import logging
import socket

import pytest

from wifi_ap_associations import AccessPointError, dlink_dap_ssh, snmp_trap
from wifi_ap_associations.dlink_dap_ssh import DlinkDapSsh, trap_kind
from wifi_ap_associations.snmp_trap import TrapListener, decode

DAP_ENTERPRISE = "1.3.6.1.4.1.171.10.37.59.5.7.2"
AP_MAC_OID = "1.3.6.1.4.1.171.10.37.59.2.1.1.1.1.5"


def v1_trap(specific: int, enterprise: str = DAP_ENTERPRISE, community: str = "dmpublic") -> bytes:
    from pyasn1.codec.ber import encoder
    from pysnmp.proto import api
    from pysnmp.proto.rfc1902 import OctetString

    module = api.PROTOCOL_MODULES[api.SNMP_VERSION_1]
    pdu = module.TrapPDU()
    module.apiTrapPDU.set_defaults(pdu)
    module.apiTrapPDU.set_enterprise(pdu, enterprise)
    module.apiTrapPDU.set_generic_trap(pdu, 6)  # enterpriseSpecific
    module.apiTrapPDU.set_specific_trap(pdu, specific)
    module.apiTrapPDU.set_varbinds(pdu, [(AP_MAC_OID, OctetString("f4:8c:eb:00:00:01"))])
    message = module.Message()
    module.apiMessage.set_defaults(message)
    module.apiMessage.set_community(message, community)
    module.apiMessage.set_pdu(message, pdu)
    return encoder.encode(message)


def v2c_trap(trap_oid: str) -> bytes:
    from pyasn1.codec.ber import encoder
    from pysnmp.proto import api
    from pysnmp.proto.rfc1902 import ObjectName, OctetString

    module = api.PROTOCOL_MODULES[api.SNMP_VERSION_2C]
    pdu = module.SNMPv2TrapPDU()
    module.apiTrapPDU.set_defaults(pdu)
    varbinds = module.apiTrapPDU.get_varbinds(pdu)  # sysUpTime.0, snmpTrapOID.0
    varbinds = [varbinds[0], ("1.3.6.1.6.3.1.1.4.1.0", ObjectName(trap_oid)),
                (AP_MAC_OID, OctetString("f4:8c:eb:00:00:01"))]
    module.apiTrapPDU.set_varbinds(pdu, varbinds)
    message = module.Message()
    module.apiMessage.set_defaults(message)
    module.apiMessage.set_community(message, "public")
    module.apiMessage.set_pdu(message, pdu)
    return encoder.encode(message)


def test_decode_a_dap_v1_trap() -> None:
    trap = decode(v1_trap(14), "192.0.2.31")
    assert trap is not None
    assert (trap.source, trap.version, trap.community) == ("192.0.2.31", "v1", "dmpublic")
    assert (trap.enterprise, trap.generic, trap.specific) == (DAP_ENTERPRISE, 6, 14)
    assert trap.varbinds == ((AP_MAC_OID, "f4:8c:eb:00:00:01"),)
    assert "specific 14" in trap.describe() and "f4:8c:eb:00:00:01" in trap.describe()


def test_decode_a_v2c_trap_into_enterprise_and_number() -> None:
    trap = decode(v2c_trap(DAP_ENTERPRISE + ".0.14"), "192.0.2.31")
    assert trap is not None and trap.version == "v2c"
    assert (trap.enterprise, trap.specific) == (DAP_ENTERPRISE, 14)
    assert trap.varbinds == ((AP_MAC_OID, "f4:8c:eb:00:00:01"),)


def test_garbage_is_not_a_trap() -> None:
    assert decode(b"\x30\x03\x02\x01\x07", "192.0.2.31") is None
    assert decode(b"hello", "192.0.2.31") is None


def test_dap_trap_kinds() -> None:
    assert trap_kind(decode(v1_trap(14), "x")) == "joined"
    assert trap_kind(decode(v1_trap(20), "x")) == "left"
    assert trap_kind(decode(v1_trap(7), "x")) is None
    assert trap_kind(decode(v1_trap(14, enterprise="1.3.6.1.4.1.9.9.1"), "x")) is None  # not D-Link
    assert trap_kind(decode(v1_trap(14, enterprise="1.3.6.1.4.1.171.10.37.37.5.7.2"), "x")) == "joined"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def send(port: int, data: bytes) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(data, ("127.0.0.1", port))


def test_the_listener_passes_traps_from_subscribed_addresses_only(caplog) -> None:
    listener = TrapListener()
    listener.configure(port=free_port(), host="127.0.0.1")
    got: list[int] = []
    caplog.set_level(logging.DEBUG, logger="wifi_ap_associations.snmp_trap")

    async def main() -> None:
        unsubscribe = await listener.subscribe("127.0.0.1", lambda trap: got.append(trap.specific))
        assert listener.listening
        send(listener.port, v1_trap(14))
        await asyncio.sleep(0.1)
        listener.received(v1_trap(20), "192.0.2.99")  # another sender: logged, not passed on
        unsubscribe()
        assert not listener.listening

    asyncio.run(main())
    assert got == [14]
    text = caplog.text
    assert "SNMP trap: 127.0.0.1 SNMPv1 community 'dmpublic' enterprise " + DAP_ENTERPRISE in text
    assert "SNMP trap (no access point at this address): 192.0.2.99" in text


def test_dlink_watch_polls_after_a_join_and_reports_leaves(monkeypatch) -> None:
    listener = TrapListener()
    listener.configure(port=free_port(), host="127.0.0.1")
    monkeypatch.setattr(snmp_trap, "LISTENER", listener)
    monkeypatch.setattr(dlink_dap_ssh, "TRAP_JOIN_DELAY", 0.05)
    events: list[str] = []
    driver = DlinkDapSsh({"host": "127.0.0.1", "port": 22, "username": "admin", "password": "x"})
    assert driver.WATCHES

    async def main() -> None:
        task = asyncio.create_task(
            driver.async_watch(lambda: events.append("change"), on_leave=lambda: events.append("leave"))
        )
        await asyncio.sleep(0.05)
        send(listener.port, v1_trap(20))
        send(listener.port, v1_trap(14))
        await asyncio.sleep(0.02)
        assert events == ["leave"]  # the join's poll waits for the handshake
        await asyncio.sleep(0.1)
        assert events == ["leave", "change"]
        send(listener.port, v1_trap(14))
        await asyncio.sleep(0.01)
        task.cancel()  # a pending poll is cancelled with the watch
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0.1)
        assert not listener.listening

    asyncio.run(main())
    assert events == ["leave", "change"]


def test_dlink_watch_says_why_it_cannot_listen(monkeypatch) -> None:
    listener = TrapListener()
    taken = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    taken.bind(("127.0.0.1", 0))
    listener.configure(port=taken.getsockname()[1], host="127.0.0.1")
    monkeypatch.setattr(snmp_trap, "LISTENER", listener)
    driver = DlinkDapSsh({"host": "127.0.0.1", "port": 22, "username": "admin", "password": "x"})
    try:
        with pytest.raises(AccessPointError, match="cannot listen for SNMP traps on UDP port"):
            asyncio.run(driver.async_watch(lambda: None))
    finally:
        taken.close()


# Real traps from a DAP-2610 (captured with tcpdump), its MAC and IP address replaced
# by documentation values of the same length.
REAL_DAP_TRAPS = {
    14: bytes.fromhex(
        "305a0201000408646d7075626c6963a44b060e2b06010401812b0a253b050702004004c000021f02010602010e430426956ca13027302506102b06010401812b0a253b020101010105041166343a38633a65623a30303a30303a3031"
    ),
    20: bytes.fromhex(
        "305a0201000408646d7075626c6963a44b060e2b06010401812b0a253b050702004004c000021f0201060201144304269676b83027302506102b06010401812b0a253b020101010105041166343a38633a65623a30303a30303a3031"
    ),
    1: bytes.fromhex(
        "305a0201000408646d7075626c6963a44b060e2b06010401812b0a253b050702004004c000021f0201060201014304269658853027302506102b06010401812b0a253b020101010105041166343a38633a65623a30303a30303a3031"
    ),
}


@pytest.mark.parametrize(("specific", "kind"), [(14, "joined"), (20, "left"), (1, None)])
def test_real_dap_traps(specific: int, kind: str | None) -> None:
    trap = decode(REAL_DAP_TRAPS[specific], "192.0.2.31")
    assert trap is not None and trap.version == "v1" and trap.community == "dmpublic"
    assert trap.enterprise == "1.3.6.1.4.1.171.10.37.59.5.7.2.0" and trap.specific == specific
    assert trap.varbinds == ((AP_MAC_OID, "f4:8c:eb:00:00:01"),)
    assert trap_kind(trap) == kind
