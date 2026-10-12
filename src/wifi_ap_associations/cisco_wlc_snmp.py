"""Cisco Catalyst 9800 wireless controllers over SNMP (experimental).

For controllers that can be read over SNMP but not over SSH. As with the SSH driver,
one access point is one AP joined to the controller, and the APs of a controller polled
together share one read. Each read walks eight columns of the Airespace wireless MIB,
which the 9800 serves (confirmed on a C9800-CL with IOS-XE 17.15.5):

    bsnMobileStationTable 1.3.6.1.4.1.14179.2.1.4.1, one row per client, indexed by its MAC:
      .4  AP MAC            the AP's base radio MAC, the index of bsnAPTable
      .7  SSID
      .23 policy state      "Run" once the client is through (as `show wireless client`)
      .25 protocol          an enumeration, see PROTOCOL_BANDS
    bsnAPTable 1.3.6.1.4.1.14179.2.2.1.1, one row per AP, indexed by its base radio MAC:
      .3  name    .4 location    .6 operation status (1 = associated)    .16 model

That is everything presence needs; per-client signal is in another table
(bsnMobileStationStatsTable) and is added once a report confirms its columns. Until
then signal is unknown. SNMP v2c with a community, or v3 with a user and keys.
"""

from __future__ import annotations

import asyncio
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
    aps: dict[str, dict[str, Any]], clients: dict[str, dict[str, Any]]
) -> ControllerData:
    """All APs and clients from walked columns: {column: {row MAC: value}}.

    Keys are those of AP_COLUMNS and CLIENT_COLUMNS; values are bytes for text and MACs,
    ints for numbers. The columns are walked one after another, so a client that joins
    or leaves meanwhile may be missing from some of them: it is left out of this read
    rather than shown with half its details.
    """
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
    found = []
    for mac, raw_state in clients.get("state", {}).items():
        ap_mac = _mac(clients.get("ap", {}).get(mac))
        state = _text(raw_state)
        if ap_mac is None or state is None:
            continue
        found.append(
            ControllerClient(
                mac=mac,
                ap_name=name_by_mac.get(ap_mac, ""),
                wlan_id=None,
                state=state,
                protocol=None,
                ssid=_text(clients.get("ssid", {}).get(mac)),
                band=PROTOCOL_BANDS.get(_int(clients.get("protocol", {}).get(mac)) or 0),
            )
        )
    return ControllerData(aps=joined, ssids={}, clients=found)


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
    # No signal read yet; the controller reports it in dBm.
    SIGNAL_UNIT = "dBm"
    EXPERIMENTAL = True
    REPORTS = frozenset({"name", "location", "model"})
    UNIQUE_FIELD = CONF_AP_NAME
    BLOCKING_IMPORTS = ("pysnmp.hlapi.v3arch.asyncio",)
    FIELDS = (
        DriverField("host"),
        DriverField("port", default=161),
        DriverField(CONF_AP_NAME),
        # v2c: the community. v3: the user and keys (the community stays empty).
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
        data = await _shared.read(key, self._read_controller)
        return poll_result(data, str(self.config[CONF_AP_NAME]).strip())

    def _auth(self, hlapi: Any) -> Any:
        """v3 when a user is set, else v2c; rejects settings that are neither."""
        user = (self.config.get("username") or "").strip()
        if user:
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
        raise AccessPointAuthError("SNMP needs a community (v2c) or a user and keys (v3)")

    async def _read_controller(self) -> ControllerData:
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
            aps = {
                key: await _walk(hlapi, engine, auth, target, context, column)
                for key, column in AP_COLUMNS.items()
            }
            clients = {
                key: await _walk(hlapi, engine, auth, target, context, column)
                for key, column in CLIENT_COLUMNS.items()
            }
        except (OSError, PySnmpError) as err:  # e.g. a host name that doesn't resolve
            raise AccessPointError(f"{host}: {err}") from err
        finally:
            engine.close_dispatcher()
        return parse_columns(aps, clients)


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
