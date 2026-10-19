"""Cisco Catalyst 9800 wireless controllers over SNMP (experimental).

For controllers that can be read over SNMP but not over SSH. As with the SSH driver,
one access point is one AP joined to the controller, and the APs of a controller polled
together share one read. Each read walks ten columns, eight of the Airespace wireless MIB and one of
Cisco's client MIB, which the 9800 serves (confirmed on a C9800-CL with IOS-XE 17.15.5):

    bsnMobileStationTable 1.3.6.1.4.1.14179.2.1.4.1, one row per client, indexed by its MAC:
      .4  AP MAC            the AP's base radio MAC, the index of bsnAPTable
      .7  SSID
      .23 policy state      "Run" once the client is through (as `show wireless client`)
      .25 protocol          an enumeration, see PROTOCOL_BANDS
    bsnAPTable 1.3.6.1.4.1.14179.2.2.1.1, one row per AP, indexed by its base radio MAC:
      .3  name    .4 location    .6 operation status (1 = associated)    .16 model
    bsnMobileStationStatsTable 1.3.6.1.4.1.14179.2.1.6.1, per client:
      .1  RSSI in dBm
    cldcClientTable 1.3.6.1.4.1.9.9.599.1.3.1.1, per client:
      .15 seconds since the client (re)associated; in two reports 13 min 14 s apart it
          had grown by 795 for every client. (.14 holds when, but on a clock a day off.)

Signal and connected time come for every client on every read; one a read misses is
taken from its own last measurement on the same AP while under
READING_FALLBACK_SECONDS old (see parse_columns), then it is unknown. They are extras:
an error walking their columns leaves them empty instead of failing the read, which
only the AP and client tables can. Which clients exist is
decided by the client table alone: the other two tables also hold rows for devices
that aren't wireless clients, and those are ignored. SNMP v2c with a community, or v3
with a user and keys, as the snmp_version setting says.

The columns are walked at the same time, so a read takes about as long as its
slowest column and the columns describe nearly the same moment. A client that joins,
roams or leaves during a read can still be missing from some of them; such a client
keeps what the previous read said about it (see parse_columns) rather than vanishing.
The price: a client caught mid-roam may show its previous AP for one more read, which
is better for presence than briefly showing it gone.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from . import (
    AccessPointAuthError,
    AccessPointDriver,
    AccessPointError,
    AssociatedClient,
    DriverField,
    PollResult,
    register,
)
from .cisco_wlc import (
    CONF_AP_NAME,
    STATE_REGISTERED,
    ClientReading,
    ControllerClient,
    ControllerData,
    JoinedAp,
    SharedReads,
    ap_location,
    poll_result,
)
from .collect import _SNMP_AUTH_ERRORS, SNMP_AUTH, SNMP_PRIV

CLIENT_TABLE = "1.3.6.1.4.1.14179.2.1.4.1"
AP_TABLE = "1.3.6.1.4.1.14179.2.2.1.1"
CLIENT_COLUMNS = {
    "ap": f"{CLIENT_TABLE}.4",
    "ssid": f"{CLIENT_TABLE}.7",
    "state": f"{CLIENT_TABLE}.23",
    "protocol": f"{CLIENT_TABLE}.25",
}
# Per-client extras from other tables; they never decide which clients exist.
EXTRA_COLUMNS = {
    "signal": "1.3.6.1.4.1.14179.2.1.6.1.1",
    "connected": "1.3.6.1.4.1.9.9.599.1.3.1.1.15",
}
AP_COLUMNS = {
    "name": f"{AP_TABLE}.3",
    "location": f"{AP_TABLE}.4",
    "status": f"{AP_TABLE}.6",
    "model": f"{AP_TABLE}.16",
}
# bsnMobileStationProtocol. 3, 6, 10, 11 and 12 were matched to `show wireless client
# summary` on a 9800 (11g, 11n(2.4), 11ac, 11ax(5), 11ax(2.4)); 1, 2 and 7 are the
# MIB's 802.11a, b and n (5 GHz). Other values give no band.
PROTOCOL_BANDS = {
    1: "5GHz",
    2: "2.4GHz",
    3: "2.4GHz",
    6: "2.4GHz",
    7: "5GHz",
    10: "5GHz",
    11: "5GHz",
    12: "2.4GHz",
}
# bsnAPOperationStatus: associated(1), disassociating(2), downloading(3).
AP_STATES = {1: STATE_REGISTERED, 2: "Disassociating", 3: "Downloading"}

SNMP_TIMEOUT = 2  # seconds per request
SNMP_RETRIES = 2
SHARE_SECONDS = 20
SNMP_VERSIONS = ("v2c", "v3")
# A signal or connected time missing from a read is taken from the last one actually
# measured on the same AP, if it is at most this old (about two polls); never from a
# value that was itself carried over.
READING_FALLBACK_SECONDS = 150

# Also keeps each controller's last read, for clients a read catches half-way.
_shared = SharedReads(SHARE_SECONDS)


def _text(value: Any) -> str | None:
    """A text column's value as a string (None if missing or empty)."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if value is None:
        return None
    return str(value).rstrip("\0").strip() or None


def _mac(value: Any) -> str | None:
    """A 6-byte MAC column's value as AA:BB:CC:DD:EE:FF."""
    if isinstance(value, bytes) and len(value) == 6:
        return ":".join(f"{byte:02X}" for byte in value)
    return None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_columns(
    aps: dict[str, dict[str, Any]],
    clients: dict[str, dict[str, Any]],
    previous: ControllerData | None = None,
    now: float | None = None,
) -> ControllerData:
    """All APs and clients from walked columns: {column: {row MAC: value}}.

    Keys are those of AP_COLUMNS and CLIENT_COLUMNS; values are bytes for text and MACs,
    ints for numbers. A client listed in some columns but not others (it joined, roamed
    or left while they were read) takes the missing details from the previous read: a
    client already known keeps its AP and state for one more read instead of looking
    gone, and one that just left disappears a read later, once no column lists it. A
    client the previous read didn't know is left out until a read has all its details.

    Signal and connected time are stored as readings with the moment they were measured
    (`now`, time.monotonic() by default). One missing from a read falls back to the last
    measured reading on the same AP while it is under READING_FALLBACK_SECONDS old, its
    connected time grown by the reading's age; after that it is unknown. A measured value
    always replaces the reading, so a reconnect shows its new, shorter time as soon as
    the column answers again.
    """
    now = time.monotonic() if now is None else now
    readings = dict(previous.readings) if previous else {}
    joined: dict[str, JoinedAp] = {}
    name_by_mac: dict[str, str] = {}
    for ap_mac, raw_name in aps.get("name", {}).items():
        if not (name := _text(raw_name)):
            continue
        status = _int(aps.get("status", {}).get(ap_mac))
        joined[name] = JoinedAp(
            name=name,
            model=_text(aps.get("model", {}).get(ap_mac)),
            state=AP_STATES.get(status, f"in status {status}") if status is not None else "unknown",
            location=ap_location(_text(aps.get("location", {}).get(ap_mac))),
        )
        name_by_mac[ap_mac] = name
    known = {client.mac: client for client in previous.clients} if previous else {}
    found = []
    listed = {mac for key in CLIENT_COLUMNS for mac in clients.get(key, {})}
    for mac in sorted(listed):
        ap_name = name_by_mac.get(_mac(clients.get("ap", {}).get(mac)) or "")
        state = _text(clients.get("state", {}).get(mac))
        before = known.get(mac)
        if ap_name is None or state is None:
            if before is None:
                continue
            ap_name = ap_name or before.ap_name
            state = state or before.state
        ssid = _text(clients.get("ssid", {}).get(mac))
        band = PROTOCOL_BANDS.get(_int(clients.get("protocol", {}).get(mac)) or 0)
        signal = _int(clients.get("signal", {}).get(mac))
        if signal is not None and not -120 <= signal <= 0:
            signal = None
        connected = _int(clients.get("connected", {}).get(mac))
        if connected is not None and connected < 0:
            connected = None
        # Each field falls back on its own, to its own last measurement on this AP: a
        # fresh signal neither drops nor rejuvenates a connected time the read missed.
        last = readings.get(mac)
        if last is not None and last.ap_name != ap_name:
            last = None
        signal_at: float | None = now if signal is not None else None
        if signal is None and last is not None and last.signal is not None:
            if last.signal_age(now) <= READING_FALLBACK_SECONDS:
                signal, signal_at = last.signal, now - last.signal_age(now)
        connected_at: float | None = now if connected is not None else None
        if connected is None and last is not None and last.connected_seconds is not None:
            if last.connected_age(now) <= READING_FALLBACK_SECONDS:
                connected, connected_at = last.connected_seconds, now - last.connected_age(now)
        if signal_at is None and connected_at is None:
            readings.pop(mac, None)
        else:
            readings[mac] = ClientReading(
                ap_name,
                signal,
                connected,
                max(at for at in (signal_at, connected_at) if at is not None),
                signal_read_at=signal_at,
                connected_read_at=connected_at,
            )
        if connected is not None and connected_at is not None:
            connected += int(now - connected_at)  # carried forward from its measurement
        found.append(
            ControllerClient(
                mac=mac,
                ap_name=ap_name,
                wlan_id=None,
                state=state,
                protocol=None,
                ssid=ssid or (before.ssid if before else None),
                band=band or (before.band if before else None),
                signal=signal,
                connected_seconds=connected,
            )
        )
    # Readings of clients no longer listed, or too old to fall back to, are dropped.
    kept = {
        mac: reading
        for mac, reading in readings.items()
        if mac in listed and now - reading.read_at <= READING_FALLBACK_SECONDS
    }
    return ControllerData(aps=joined, ssids={}, clients=found, readings=kept)


def _row_mac(oid: str, column: str) -> str | None:
    """The MAC in a row's index: the six numbers after the column's OID."""
    parts = oid[len(column) + 1 :].split(".")
    if len(parts) != 6 or not all(p.isdigit() and int(p) <= 255 for p in parts):
        return None
    return ":".join(f"{int(p):02X}" for p in parts)


def _python_value(value: Any) -> Any:
    """pysnmp value as bytes (strings, MACs) or int (numbers)."""
    try:
        return bytes(value.asOctets())
    except AttributeError:
        return _int(value)


@register
class CiscoWlcSnmp(AccessPointDriver):
    """A lightweight AP joined to a Cisco Catalyst 9800 controller, read over SNMP."""

    TYPE = "cisco_wlc_snmp"
    NAME = "Cisco Catalyst 9800 controller (SNMP)"
    MANUFACTURER = "Cisco"
    SIGNAL_UNIT = "dBm"
    REPORTS = frozenset({"name", "location", "model"})
    UNIQUE_FIELD = CONF_AP_NAME
    IDENTITY_FIELDS = ("host", CONF_AP_NAME)
    BLOCKING_IMPORTS = ("pysnmp.hlapi.v3arch.asyncio",)
    FIELDS = (
        DriverField("host"),
        DriverField("port", default=161),
        DriverField(CONF_AP_NAME),
        # Which credentials count; the other version's may stay stored, unused, so
        # switching version needs no clearing of fields.
        DriverField("snmp_version", default="v2c", choices=SNMP_VERSIONS),
        DriverField("community", secret=True, optional=True),
        DriverField("username", optional=True),
        DriverField("auth_key", secret=True, optional=True),
        DriverField("auth_protocol", default="sha", choices=tuple(SNMP_AUTH)),
        DriverField("priv_key", secret=True, optional=True),
        DriverField("priv_protocol", default="aes", choices=tuple(SNMP_PRIV)),
    )

    async def async_get_associated_clients(self) -> list[AssociatedClient]:
        """Clients associated to this AP."""
        return (await self.async_poll()).clients

    async def async_poll(self) -> PollResult:
        """This AP's clients and details, from a controller read shared with its other APs."""
        key = tuple(
            str(self.config.get(field.key) or "")
            for field in self.FIELDS
            if field.key != CONF_AP_NAME
        )
        data = await _shared.read(key, lambda: self._read_controller(key))
        return poll_result(data, str(self.config[CONF_AP_NAME]).strip())

    def _version(self) -> str:
        """The SNMP version; settings saved before it was a setting: v3 if a user is set."""
        version = self.config.get("snmp_version")
        if version in SNMP_VERSIONS:
            return version
        return "v3" if (self.config.get("username") or "").strip() else "v2c"

    def _auth(self, hlapi: Any) -> Any:
        """v2c or v3 credentials, as snmp_version says; rejects missing ones."""
        if self._version() == "v3":
            user = (self.config.get("username") or "").strip()
            if not user:
                raise AccessPointAuthError("SNMPv3 needs a user")
            auth_key = self.config.get("auth_key") or ""
            auth_protocol = self.config.get("auth_protocol") or "sha"
            priv_protocol = self.config.get("priv_protocol") or "aes"
            if not auth_key:
                raise AccessPointAuthError("SNMPv3 needs an authentication key")
            if auth_protocol not in SNMP_AUTH or priv_protocol not in SNMP_PRIV:
                raise AccessPointError(
                    f"unknown SNMPv3 algorithm {auth_protocol!r} / {priv_protocol!r}"
                )
            return hlapi.UsmUserData(
                user,
                authKey=auth_key,
                privKey=self.config.get("priv_key") or auth_key,
                authProtocol=getattr(hlapi, SNMP_AUTH[auth_protocol]),
                privProtocol=getattr(hlapi, SNMP_PRIV[priv_protocol]),
            )
        if community := self.config.get("community"):
            return hlapi.CommunityData(community, mpModel=1)
        raise AccessPointAuthError("SNMP v2c needs a community")

    async def _read_controller(self, key: tuple[str, ...]) -> ControllerData:
        import pysnmp.hlapi.v3arch.asyncio as hlapi
        from pysnmp.error import PySnmpError

        host = self.config["host"]
        auth = self._auth(hlapi)
        # Creating an engine loads MIB files from disk each time: not on the event loop.
        engine = await asyncio.get_running_loop().run_in_executor(None, hlapi.SnmpEngine)
        try:
            target = await hlapi.UdpTransportTarget.create(
                (host, int(self.config.get("port") or 161)),
                timeout=SNMP_TIMEOUT,
                retries=SNMP_RETRIES,
            )
            context = hlapi.ContextData()
            columns = [*AP_COLUMNS.values(), *CLIENT_COLUMNS.values(), *EXTRA_COLUMNS.values()]
            # All at once: one engine tells concurrent requests apart by request ID.
            # Every walk is let finish, failed or not, before the engine closes: closing
            # with requests out makes their late answers fail inside pysnmp.
            walked = await asyncio.gather(
                *(_walk(hlapi, engine, auth, target, context, oid) for oid in columns),
                return_exceptions=True,
            )
        except (OSError, PySnmpError) as err:  # e.g. a host name that doesn't resolve
            raise AccessPointError(f"{host}: {err}") from err
        finally:
            engine.close_dispatcher()
        # Presence needs the AP and client tables: an error there fails the read. Signal
        # and connected time are extras: an error walking them only leaves them empty.
        required = {*AP_COLUMNS.values(), *CLIENT_COLUMNS.values()}
        errors = [
            result
            for oid, result in zip(columns, walked)
            if isinstance(result, BaseException)
            and (oid in required or isinstance(result, AccessPointAuthError))
        ]
        if errors:
            # Rejected keys first: they tell the user what to fix.
            auth = [e for e in errors if isinstance(e, AccessPointAuthError)]
            raise (auth or errors)[0]
        values = {
            oid: {} if isinstance(result, BaseException) else result
            for oid, result in zip(columns, walked)
        }
        data = parse_columns(
            {name: values[oid] for name, oid in AP_COLUMNS.items()},
            {name: values[oid] for name, oid in {**CLIENT_COLUMNS, **EXTRA_COLUMNS}.items()},
            _shared.last(key),
        )
        return data


async def _walk(
    hlapi: Any, engine: Any, auth: Any, target: Any, context: Any, column: str
) -> dict[str, Any]:
    """One column as {row MAC: value}; any SNMP error raises, so no table is partial."""
    rows: dict[str, Any] = {}
    async for error, status, _index, binds in hlapi.bulk_walk_cmd(
        engine, auth, target, context, 0, 25,
        hlapi.ObjectType(hlapi.ObjectIdentity(column)),
        lexicographicMode=False, lookupMib=False,
    ):
        if error:
            text = str(error)
            if any(marker in text.lower() for marker in _SNMP_AUTH_ERRORS):
                raise AccessPointAuthError(f"SNMPv3 user or keys rejected: {text}")
            raise AccessPointError(
                f"SNMP: {text}; check the address, the community or user, and that SNMP "
                "is enabled on the controller"
            )
        if status:
            raise AccessPointError(f"SNMP error: {status.prettyPrint()}")
        for oid, value in binds:
            if (mac := _row_mac(str(oid), column)) is not None:
                rows[mac] = _python_value(value)
    return rows
