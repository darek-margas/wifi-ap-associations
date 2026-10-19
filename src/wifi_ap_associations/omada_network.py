"""TP-Link Omada access points, read through Home Assistant's TP-Link Omada integration.

Like the UniFi driver, this one doesn't log in anywhere: it asks the Omada controller
(software controller, hardware OC200/OC300, or a router with a built-in controller)
which wireless clients are connected to one of its access points, over the connection
Home Assistant's own TP-Link Omada integration already holds.

The controller's connected client list holds one record per client, as in Home
Assistant's tplink_omada test fixtures (tplink-omada-client 1.5):

    {"mac": "2E-DC-E1-C4-37-D3", "wireless": true, "active": true,
     "apMac": "C0-C9-E3-4B-AF-0E", "apName": "Spare Room", "ssid": "Home",
     "radioId": 1, "channel": 44, "rssi": -63, "uptime": 2091, ...}

radioId is 0 for 2.4 GHz, 1 and 2 for 5 GHz (tri-band APs have two), 3 for 6 GHz.
Each access point is a device record ("type": "ap") with "statusCategory" 1 while it is
connected to the controller, and "uptimeLong" in seconds ("uptime" is text).

The source of that data is injected (see OmadaSource), so this module keeps the
package free of Home Assistant imports and can be tested on its own. Experimental:
written from those fixtures and the client library, not yet run on a real controller.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from . import (
    AccessPointDriver,
    AccessPointError,
    AccessPointInfo,
    AssociatedClient,
    DriverField,
    PollResult,
    normalize_mac,
    register,
)

CONF_AP_MAC = "ap_mac"

RADIO_BANDS = {0: "2.4GHz", 1: "5GHz", 2: "5GHz", 3: "6GHz"}

DEVICE_STATES = {
    0: "disconnected",
    1: "connected",
    2: "pending",
    3: "heartbeat missed",
    4: "isolated",
}
STATE_CONNECTED = 1


class OmadaSource(Protocol):
    """Where the driver gets controller data (Home Assistant's TP-Link Omada integration)."""

    async def async_get_device(self, mac: str) -> Mapping[str, Any] | None:
        """The raw device record of the access point with this MAC, if known."""

    async def async_get_clients(self, mac: str) -> list[Mapping[str, Any]]:
        """Raw records of the clients connected to the controller (site) that manages
        the access point with this MAC."""


def is_access_point(device: Mapping[str, Any]) -> bool:
    """Whether an Omada device record is an access point (an EAP)."""
    return device.get("type") == "ap"


def _int(value: Any) -> int | None:
    """An int from a number or numeric text; None for anything else."""
    if isinstance(value, bool):
        return None
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return None


def parse_clients(raw_clients: list[Mapping[str, Any]], ap_mac: str) -> list[AssociatedClient]:
    """The wireless clients connected to the access point with this MAC."""
    clients: list[AssociatedClient] = []
    for raw in raw_clients:
        if raw.get("wireless") is not True or raw.get("active") is False:
            continue
        try:
            if normalize_mac(raw.get("apMac") or "") != ap_mac:
                continue
            mac = normalize_mac(raw["mac"])
        except (KeyError, ValueError):
            continue
        signal = _int(raw.get("rssi"))
        clients.append(
            AssociatedClient(
                mac=mac,
                ssid=raw.get("ssid") or None,
                band=RADIO_BANDS.get(_int(raw.get("radioId"))),  # type: ignore[arg-type]
                # dBm is negative; guard against a controller reporting it positive.
                signal=-abs(signal) if signal is not None else None,
                connected_seconds=_int(raw.get("uptime")),
            )
        )
    return clients


def parse_device(device: Mapping[str, Any]) -> AccessPointInfo:
    """What the controller reports about the access point."""
    return AccessPointInfo(
        name=device.get("name") or None,
        model=device.get("model") or None,
        firmware=device.get("firmwareVersion") or device.get("version") or None,
        hardware=device.get("hwVersion") or None,
        uptime_seconds=_int(device.get("uptimeLong")),
        cpu_percent=_int(device.get("cpuUtil")),
        memory_percent=_int(device.get("memUtil")),
    )


@register
class OmadaNetworkDriver(AccessPointDriver):
    """One TP-Link Omada access point, read through the TP-Link Omada integration."""

    TYPE = "omada_network"
    NAME = "TP-Link Omada (via the TP-Link Omada integration)"
    MANUFACTURER = "TP-Link"
    FIELDS = (DriverField(CONF_AP_MAC),)
    SIGNAL_UNIT = "dBm"
    UNIQUE_FIELD = CONF_AP_MAC
    # Reads through Home Assistant's TP-Link Omada integration (see the integration's
    # sources.py), which also lists the access points to pick from.
    SOURCE = "omada"
    POLL_TIMEOUT = 20
    # The Omada integration registers the AP as a device already; its area is what the
    # tracked devices report.
    OWN_DEVICE = False
    # Written from Home Assistant's fixtures and the client library; no real controller
    # has run it yet.
    EXPERIMENTAL = True
    # Omada has no location setting the controller reports.
    REPORTS = frozenset(
        {"name", "model", "firmware", "hardware", "uptime_seconds", "cpu_percent", "memory_percent"}
    )

    def device_mac(self) -> str | None:
        """The MAC of the device the Omada integration registered for this AP."""
        return self.ap_mac

    def __init__(self, config: dict[str, Any], source: OmadaSource | None = None) -> None:
        """Store the settings and the controller data source."""
        super().__init__(config)
        self.source = source
        self.ap_mac = normalize_mac(config[CONF_AP_MAC])

    async def async_get_associated_clients(self) -> list[AssociatedClient]:
        """Clients connected to this access point."""
        return (await self.async_poll()).clients

    async def async_poll(self) -> PollResult:
        """Clients and device details, as the controller reports them."""
        if self.source is None:
            raise AccessPointError("Omada access points can only be read inside Home Assistant")
        device = await self.source.async_get_device(self.ap_mac)
        if device is None:
            raise AccessPointError(
                f"{self.ap_mac} is not an access point of any loaded TP-Link Omada integration"
            )
        state = device.get("statusCategory")
        if state != STATE_CONNECTED:
            raise AccessPointError(
                f"the controller reports the access point as {DEVICE_STATES.get(state, state)}"
            )
        raw_clients = await self.source.async_get_clients(self.ap_mac)
        return PollResult(parse_clients(raw_clients, self.ap_mac), parse_device(device))
