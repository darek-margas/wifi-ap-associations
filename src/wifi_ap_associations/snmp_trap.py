"""A shared SNMP trap listener: access points that push a trap when a client joins.

One UDP socket (port 162 by default) serves every access point that watches through
it; each subscribes by its IP address and gets the traps that address sends, decoded.
The socket opens with the first subscriber and closes with the last.

Nothing in a trap is trusted: it only asks for a poll (see AccessPointDriver.async_watch),
and only traps from a subscribed address are passed on. Every trap received is logged at
debug level with all its fields (sender, version, community, enterprise, trap numbers,
variables), from any sender, so the traps of a new model can be read from the log.

Port 162 is privileged: the process needs root or CAP_NET_BIND_SERVICE, or another port
(configure()) with the traffic forwarded to it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import logging
import socket
from typing import Any

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 162


@dataclass(frozen=True, slots=True)
class Trap:
    """One decoded trap (SNMPv1 or v2c)."""

    source: str  # sender's IP address
    version: str  # "v1" or "v2c"
    community: str
    # v1: the enterprise OID, generic and specific trap numbers. v2c: the trap OID
    # (snmpTrapOID.0); enterprise and specific are derived from it when it has the
    # RFC 3584 form <enterprise>.0.<specific>.
    enterprise: str = ""
    generic: int | None = None
    specific: int | None = None
    trap_oid: str = ""
    varbinds: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def describe(self) -> str:
        """One line with everything, for the log."""
        what = (
            f"enterprise {self.enterprise} generic {self.generic} specific {self.specific}"
            if self.version == "v1"
            else f"trap {self.trap_oid}"
        )
        values = ", ".join(f"{oid} = {value}" for oid, value in self.varbinds)
        return f"{self.source} SNMP{self.version} community {self.community!r} {what}; {values or 'no variables'}"


_SNMP_TRAP_OID = "1.3.6.1.6.3.1.1.4.1.0"
_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"


def _text(value: Any) -> str:
    try:
        return value.prettyPrint()
    except Exception:  # noqa: BLE001 - a value that can't print is still logged
        return repr(value)


def decode(data: bytes, source: str) -> Trap | None:
    """Decode one datagram; None if it isn't an SNMPv1/v2c trap."""
    from pyasn1.codec.ber import decoder
    from pysnmp.proto import api

    try:
        version = int(api.decodeMessageVersion(data))
        module = api.PROTOCOL_MODULES[version]
        message, _rest = decoder.decode(data, asn1Spec=module.Message())
        community = _text(module.apiMessage.get_community(message))
        pdu = module.apiMessage.get_pdu(message)
    except Exception as err:  # noqa: BLE001 - anything malformed is not a trap
        _LOGGER.debug("SNMP trap from %s not decoded (%s): %s", source, err, data[:64].hex())
        return None
    if version == api.SNMP_VERSION_1:
        if not pdu.isSameTypeWith(module.TrapPDU()):
            return None
        trap = module.apiTrapPDU
        return Trap(
            source=source,
            version="v1",
            community=community,
            enterprise=_text(trap.get_enterprise(pdu)),
            generic=int(trap.get_generic_trap(pdu)),
            specific=int(trap.get_specific_trap(pdu)),
            varbinds=tuple((str(oid), _text(value)) for oid, value in trap.get_varbinds(pdu)),
        )
    if not pdu.isSameTypeWith(module.SNMPv2TrapPDU()):
        return None
    varbinds = tuple((str(oid), _text(value)) for oid, value in module.apiPDU.get_varbinds(pdu))
    trap_oid = next((value for oid, value in varbinds if oid == _SNMP_TRAP_OID), "")
    enterprise, specific = "", None
    head, _, tail = trap_oid.rpartition(".0.")
    if head and tail.isdigit():
        enterprise, specific = head, int(tail)
    return Trap(
        source=source,
        version="v2c",
        community=community,
        enterprise=enterprise,
        specific=specific,
        trap_oid=trap_oid,
        varbinds=tuple(vb for vb in varbinds if vb[0] not in (_SNMP_TRAP_OID, _SYS_UPTIME)),
    )


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, listener: TrapListener) -> None:
        self.listener = listener

    def datagram_received(self, data: bytes, addr: Any) -> None:
        self.listener.received(data, str(addr[0]))


class TrapListener:
    """The shared socket and who listens to which sender."""

    def __init__(self) -> None:
        self.host = "0.0.0.0"
        self.port = DEFAULT_PORT
        self._transport: asyncio.DatagramTransport | None = None
        self._subscribers: dict[str, list[Callable[[Trap], None]]] = {}
        self._lock = asyncio.Lock()

    def configure(self, port: int = DEFAULT_PORT, host: str = "0.0.0.0") -> None:
        """Where to listen; takes effect when the socket next opens."""
        self.port, self.host = port, host

    @property
    def listening(self) -> bool:
        return self._transport is not None

    async def subscribe(self, address: str, callback: Callable[[Trap], None]) -> Callable[[], None]:
        """Pass traps from this IP address to callback; returns the unsubscribe function.

        Raises OSError when the port can't be opened (in use, or not allowed).
        """
        async with self._lock:
            if self._transport is None:
                loop = asyncio.get_running_loop()
                transport, _protocol = await loop.create_datagram_endpoint(
                    lambda: _Protocol(self), local_addr=(self.host, self.port), family=socket.AF_INET
                )
                self._transport = transport
                _LOGGER.debug("Listening for SNMP traps on UDP %s:%s", self.host, self.port)
            self._subscribers.setdefault(address, []).append(callback)

        def unsubscribe() -> None:
            callbacks = self._subscribers.get(address, [])
            if callback in callbacks:
                callbacks.remove(callback)
            if not callbacks:
                self._subscribers.pop(address, None)
            if not self._subscribers and self._transport is not None:
                self._transport.close()
                self._transport = None
                _LOGGER.debug("Stopped listening for SNMP traps")

        return unsubscribe

    def received(self, data: bytes, source: str) -> None:
        trap = decode(data, source)
        if trap is None:
            return
        callbacks = list(self._subscribers.get(source, ()))
        _LOGGER.debug("SNMP trap%s: %s", "" if callbacks else " (no access point at this address)",
                      trap.describe())
        for callback in callbacks:
            try:
                callback(trap)
            except Exception:  # a subscriber's bug must not stop the others
                _LOGGER.exception("Handling an SNMP trap from %s failed", source)


LISTENER = TrapListener()
