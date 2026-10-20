"""TP-Link EAP access points running standalone (no Omada controller), over SNMP.

EXPERIMENTAL: built from two reports of an EAP670 (firmware 1.3.13), not yet polled by
a real installation. Its command line has no way to list clients, but TP-Link's own part
of the MIB does (sysObjectID 1.3.6.1.4.1.11863.10.1):

    1.3.6.1.4.1.11863.10.1.1.1.0       the number of connected clients
    1.3.6.1.4.1.11863.10.1.1.2.1       one row per client, indexed 1, 2, ...:
      .1  the row number
      .2  the client's MAC, as text ("34-85-18-..." or "34:85:18:...", 17 characters)

The count and the rows matched the clients connected in both reports (9, then 11), and
the MACs' vendors were the house's phones and appliances, not the AP's radios. The
table has nothing else: no SSID, band, signal or connected time, so only presence (and
which AP) comes from it. Name and model come from the standard system group (sysName;
the model is in sysDescr, "Linux EAP670 4.4.60 ..."). SNMP v2c with a community, or v3
with a user and keys, as for the Cisco SNMP driver.

An agent without the count isn't an EAP with this table (another model or firmware):
that is an error, rather than an AP that always looks empty. So is a positive count with
no readable MAC in the table: an empty AP would send every client away.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from . import (
    AccessPointAuthError,
    AccessPointDriver,
    AccessPointError,
    AccessPointInfo,
    AssociatedClient,
    DriverField,
    PollResult,
    register,
)
from .cisco_wlc_snmp import SNMP_FIELDS, SnmpCredentials, _walk

CLIENT_COUNT = "1.3.6.1.4.1.11863.10.1.1.1"
CLIENT_MACS = "1.3.6.1.4.1.11863.10.1.1.2.1.2"
SYS_DESCR = "1.3.6.1.2.1.1.1"
SYS_NAME = "1.3.6.1.2.1.1.5"

SNMP_TIMEOUT = 2  # seconds per request
SNMP_RETRIES = 2

_MAC_TEXT = re.compile(r"[0-9A-Fa-f]{2}([:-])[0-9A-Fa-f]{2}(?:\1[0-9A-Fa-f]{2}){4}")
_MODEL = re.compile(r"\b(EAP\d+[\w-]*)", re.IGNORECASE)


def _row(oid: str, column: str) -> str | None:
    """The row's index after the column's OID (a scalar's is "0")."""
    index = oid[len(column) + 1 :]
    return index if index and all(part.isdigit() for part in index.split(".")) else None


def _text(value: Any) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if value is None:
        return None
    return str(value).rstrip("\0").strip() or None


def client_mac(value: Any) -> str | None:
    """A client column's value as AA:BB:CC:DD:EE:FF: text (- or : separated) or 6 bytes."""
    if isinstance(value, bytes) and len(value) == 6:
        return ":".join(f"{byte:02X}" for byte in value)
    text = _text(value)
    if text is None or not _MAC_TEXT.fullmatch(text):
        return None
    return text.replace("-", ":").upper()


def model_from_descr(descr: str | None) -> str | None:
    """The model in sysDescr ("Linux EAP670 4.4.60 #1 SMP ..." gives EAP670)."""
    match = _MODEL.search(descr or "")
    return match.group(1).upper() if match else None


def parse(columns: dict[str, dict[str, Any]]) -> PollResult:
    """The AP's clients and details from walked columns: {OID: {row: value}}."""
    if not columns.get(CLIENT_COUNT):
        raise AccessPointError(
            "no TP-Link EAP client table (1.3.6.1.4.1.11863.10.1.1) on this device: it may "
            "be another model, or need different firmware"
        )
    macs = {mac for value in columns.get(CLIENT_MACS, {}).values() if (mac := client_mac(value))}
    count = next(iter(columns[CLIENT_COUNT].values()))
    if not macs and isinstance(count, int) and count > 0:
        # Clients the table doesn't show (unreadable rows, or MACs in a form not known
        # here): failing keeps them home within the grace period, where an empty AP would
        # send them all away. (A client that leaves between the two walks fails one read.)
        raise AccessPointError(
            f"the EAP counts {count} client(s) but its client table gave no readable MAC"
        )
    descr = _text(next(iter(columns.get(SYS_DESCR, {}).values()), None))
    name = _text(next(iter(columns.get(SYS_NAME, {}).values()), None))
    return PollResult(
        [AssociatedClient(mac) for mac in sorted(macs)],
        AccessPointInfo(name=name, model=model_from_descr(descr)),
    )


@register
class TplinkEapSnmp(SnmpCredentials, AccessPointDriver):
    """A standalone TP-Link EAP, read over SNMP."""

    TYPE = "tplink_eap_snmp"
    NAME = "TP-Link EAP, standalone (SNMP)"
    MANUFACTURER = "TP-Link"
    SIGNAL_UNIT = "dBm"  # the table has no signal; declared for the interface only
    REPORTS = frozenset({"name", "model"})
    CLIENT_DETAILS = frozenset()  # MACs only
    EXPERIMENTAL = True
    BLOCKING_IMPORTS = ("pysnmp.hlapi.v3arch.asyncio",)
    FIELDS = (
        DriverField("host"),
        DriverField("port", default=161),
        *SNMP_FIELDS,
    )

    async def async_get_associated_clients(self) -> list[AssociatedClient]:
        """Clients associated to this AP."""
        return (await self.async_poll()).clients

    async def async_poll(self) -> PollResult:
        """The EAP's clients, name and model."""
        import pysnmp.hlapi.v3arch.asyncio as hlapi
        from pysnmp.error import PySnmpError

        host = self.config["host"]
        auth = self._auth(hlapi)
        # Creating an engine loads MIB files from disk each time: not on the event loop.
        engine = await asyncio.get_running_loop().run_in_executor(None, hlapi.SnmpEngine)
        columns = [CLIENT_COUNT, CLIENT_MACS, SYS_DESCR, SYS_NAME]
        try:
            target = await hlapi.UdpTransportTarget.create(
                (host, int(self.config.get("port") or 161)),
                timeout=SNMP_TIMEOUT,
                retries=SNMP_RETRIES,
            )
            context = hlapi.ContextData()
            # Every walk is let finish before the engine closes (see cisco_wlc_snmp).
            walked = await asyncio.gather(
                *(_walk(hlapi, engine, auth, target, context, oid, _row) for oid in columns),
                return_exceptions=True,
            )
        except (OSError, PySnmpError) as err:  # e.g. a host name that doesn't resolve
            raise AccessPointError(f"{host}: {err}") from err
        finally:
            engine.close_dispatcher()
        errors = [result for result in walked if isinstance(result, BaseException)]
        if errors:
            # Rejected keys first: they tell the user what to fix.
            auth_errors = [e for e in errors if isinstance(e, AccessPointAuthError)]
            raise (auth_errors or errors)[0]
        return parse(dict(zip(columns, walked)))
