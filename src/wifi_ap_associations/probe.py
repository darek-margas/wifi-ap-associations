"""Read one access point's association table and print it (no Home Assistant needed).

Usage:
    wifi-ap-probe --type dlink_dap_ssh --host 192.168.1.231 --username admin
    wifi-ap-probe --type cisco_wlc_ssh --host 192.168.1.20 --username ha --ap-name AP01
    wifi-ap-probe --type cisco_wlc_snmp --host 192.168.1.20 --ap-name AP01   # SNMP v2c
    wifi-ap-probe --type cisco_wlc_snmp --host 192.168.1.20 --ap-name AP01 --snmp-version v3 --username ro --auth-protocol sha256
    wifi-ap-probe --list-types

Secrets are asked for interactively, or taken from the environment: the password and
an SNMPv3 authentication key from $AP_PASSWORD, an SNMP community from $AP_COMMUNITY,
an SNMPv3 privacy key from $AP_PRIV_KEY (empty: same as the authentication key).
Installed with the library as the command "wifi-ap-probe" (or python -m wifi_ap_associations.probe).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from wifi_ap_associations import DRIVERS, AccessPointError


# Where each secret comes from, and whether an optional one is needed: an SNMP
# community only without a v3 user, the v3 keys only with one.
_SECRET_ENV = {"community": "AP_COMMUNITY", "priv_key": "AP_PRIV_KEY"}
_V3_ONLY = {"auth_key", "priv_key"}


def _secret(key: str, optional: bool, snmp_v3: bool) -> str | None:
    if optional and (key in _V3_ONLY) != snmp_v3:
        return None
    if value := os.environ.get(_SECRET_ENV.get(key, "AP_PASSWORD")):
        return value
    hint = " (empty = same as the authentication key)" if key == "priv_key" else ""
    value = getpass.getpass(f"{key}{hint}: ")
    return value or None if optional else value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--type", default="dlink_dap_ssh", help="driver type")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--username")
    parser.add_argument("--ap-name", help="AP name on the controller (cisco_wlc_*)")
    parser.add_argument("--snmp-version", help="v2c or v3 (default: v3 if --username)")
    parser.add_argument("--auth-protocol", help="SNMPv3 authentication, e.g. sha256")
    parser.add_argument("--priv-protocol", help="SNMPv3 privacy, e.g. aes")
    parser.add_argument("--list-types", action="store_true")
    args = parser.parse_args()

    if args.list_types:
        for type_, cls in DRIVERS.items():
            print(f"{type_:20} {cls.NAME}")
        return 0
    if args.type not in DRIVERS:
        parser.error(f"unknown type {args.type!r}; see --list-types")
    if not args.host:
        parser.error("--host is required")

    driver_cls = DRIVERS[args.type]
    if args.snmp_version is None and args.username:
        args.snmp_version = "v3"  # an SNMP user means v3; ignored by non-SNMP drivers
    config: dict[str, object] = {}
    for field in driver_cls.FIELDS:  # before asking for any secret
        value = getattr(args, field.key, None)
        if value is not None and field.choices and value not in field.choices:
            parser.error(f"--{field.key.replace('_', '-')}: one of {', '.join(field.choices)}")
    for field in driver_cls.FIELDS:
        value = getattr(args, field.key, None)
        if value is None and field.secret:
            value = _secret(field.key, field.optional, snmp_v3=args.snmp_version == "v3")
        config[field.key] = value if value is not None else field.default

    try:
        clients = asyncio.run(driver_cls(config).async_get_associated_clients())
    except AccessPointError as err:
        print(f"FAILED: {err}", file=sys.stderr)
        return 1

    print(f"{len(clients)} associated client(s) on {args.host}")
    print(f"{'MAC':17}  {'band':6}  {'signal':>6}  {'connected':>9}  ssid")
    for c in sorted(clients, key=lambda c: (c.band or "", c.ssid or "", c.mac)):
        print(
            f"{c.mac:17}  {c.band or '?':6}  {c.signal if c.signal is not None else '?':>6}"
            f"  {c.connected_seconds if c.connected_seconds is not None else '?':>8}s"
            f"  {c.ssid or '?'}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
