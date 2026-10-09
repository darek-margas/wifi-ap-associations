"""Collector: the rules that keep a shared report safe (read-only commands, redaction)."""

from __future__ import annotations

import asyncio

import asyncssh
import pytest

from wifi_ap_associations import AccessPointAuthError, AccessPointError, collect
from wifi_ap_associations.collect import PROFILES, Redactor, _REFUSED, redact


@pytest.mark.parametrize(
    "command",
    ["reboot", "set ssid x", "factory reset", "save", "Apply", "  delete 1", "passwd", "ping 1.1.1.1"],
)
def test_changing_commands_are_refused(command: str) -> None:
    assert _REFUSED.match(command)


@pytest.mark.parametrize("command", ["show version", "get clientinfo", "ubus call system board", "mca-dump"])
def test_reading_commands_are_allowed(command: str) -> None:
    assert not _REFUSED.match(command)


def test_profiles_contain_no_refused_command() -> None:
    for name, commands in PROFILES.items():
        for command in commands:
            assert not _REFUSED.match(command), f"{name}: {command}"


def test_mac_keeps_vendor_prefix_and_is_consistent() -> None:
    redactor = Redactor([])
    first = redactor.text("client 5c:ad:ba:12:34:56 joined")
    again = redactor.text("5C:AD:BA:12:34:56")
    other = redactor.text("5c:ad:ba:65:43:21")
    assert first == "client 5C:AD:BA:XX:XX:01 joined"
    assert again == "5C:AD:BA:XX:XX:01"
    assert other == "5C:AD:BA:XX:XX:02"


def test_ipv4_keeps_first_and_last_octet() -> None:
    assert Redactor([]).text("gateway 192.168.1.23") == "gateway 192.x.x.23"


def test_typed_secret_is_removed() -> None:
    assert "hunter2" not in Redactor(["hunter2"]).text("login ok, password hunter2 accepted")


def test_secret_looking_setting_is_blanked() -> None:
    report = redact("ssid=Home\nwpa_passphrase=correct horse battery")
    assert "correct horse" not in report
    assert "ssid=Home" in report


# --- async_collect_ssh_report (the API the Home Assistant integration uses) ---------


def _run(**kwargs):
    return asyncio.run(
        collect.async_collect_ssh_report("192.0.2.10", "admin", "hunter2", **kwargs)
    )


def test_report_is_redacted_with_warning_on_top(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    async def fake_collect(args, password):
        seen["args"], seen["password"] = args, password
        return "# --- get clientinfo ---\n5c:ad:ba:12:34:56  -61  password=hunter2\nwpa_passphrase=topsecret"

    monkeypatch.setattr(collect, "collect_ssh", fake_collect)
    report = _run(profile="dlink_dap", commands=["show station", " "], legacy_ssh=True)
    assert report.startswith(collect.REVIEW_WARNING)
    assert "5C:AD:BA:XX:XX:01" in report
    assert "hunter2" not in report and "topsecret" not in report
    assert seen["password"] == "hunter2"
    assert seen["args"].profile == "dlink_dap"
    assert seen["args"].command == ["show station"]
    assert seen["args"].legacy_ssh is True
    assert seen["args"].port == 22


def test_unknown_profile_is_rejected() -> None:
    with pytest.raises(ValueError):
        _run(profile="nope")


def test_rejected_login_is_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_collect(args, password):
        raise asyncssh.PermissionDenied("bad password")
    monkeypatch.setattr(collect, "collect_ssh", fake_collect)
    with pytest.raises(AccessPointAuthError):
        _run()


def test_algorithm_mismatch_suggests_legacy_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_collect(args, password):
        raise asyncssh.KeyExchangeFailed("No matching key exchange algorithm found")

    monkeypatch.setattr(collect, "collect_ssh", fake_collect)
    with pytest.raises(AccessPointError, match="try legacy SSH"):
        _run()


def test_unreachable_is_access_point_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_collect(args, password):
        raise OSError("Connection refused")
    monkeypatch.setattr(collect, "collect_ssh", fake_collect)
    with pytest.raises(AccessPointError) as err:
        _run()
    assert not isinstance(err.value, AccessPointAuthError)
    assert "legacy" not in str(err.value)


# --- async_collect_snmp_report --------------------------------------------------------


def _snmp(**kwargs):
    return asyncio.run(collect.async_collect_snmp_report("192.0.2.10", **kwargs))


def test_snmp_v2c_report(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    async def fake_collect(args, secrets):
        seen["args"], seen["secrets"] = args, secrets
        return ["# mode: SNMP v2c", "sysName = ap"]

    monkeypatch.setattr(collect, "collect_snmp", fake_collect)
    report = _snmp(community="public", max_values=100)
    assert report.startswith(collect.REVIEW_WARNING)
    assert report.endswith("# mode: SNMP v2c\nsysName = ap\n")
    assert seen["secrets"] == {"community": "public"}
    assert seen["args"].snmp_user is None
    assert seen["args"].snmp_max == 100
    assert seen["args"].port == 161
    assert seen["args"].no_redact is False and seen["args"].snmp_full_values is False


def test_snmp_v3_privacy_key_defaults_to_auth_key(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    async def fake_collect(args, secrets):
        seen["args"], seen["secrets"] = args, secrets
        return []

    monkeypatch.setattr(collect, "collect_snmp", fake_collect)
    _snmp(user="ro", auth_key="authsecret", auth_protocol="sha256", priv_protocol="aes256")
    assert seen["secrets"] == {"auth": "authsecret", "priv": "authsecret"}
    assert seen["args"].snmp_user == "ro"
    assert (seen["args"].snmp_auth, seen["args"].snmp_priv) == ("sha256", "aes256")


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"user": "ro"}, {"user": "ro", "auth_key": "k", "auth_protocol": "nope"}],
)
def test_snmp_missing_or_unknown_settings(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        _snmp(**kwargs)


def test_snmp_v3_rejected_keys_are_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_collect(args, secrets):
        raise RuntimeError("SNMP request failed: Wrong SNMP PDU digest")

    monkeypatch.setattr(collect, "collect_snmp", fake_collect)
    with pytest.raises(AccessPointAuthError):
        _snmp(user="ro", auth_key="k")


def test_snmp_no_response_is_access_point_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_collect(args, secrets):
        raise RuntimeError("SNMP request failed: No SNMP response received before timeout")

    monkeypatch.setattr(collect, "collect_snmp", fake_collect)
    with pytest.raises(AccessPointError) as err:
        _snmp(community="wrong")
    assert not isinstance(err.value, AccessPointAuthError)
    assert "community" in str(err.value)


def test_snmp_protocol_names_exist_in_installed_pysnmp() -> None:
    import pysnmp.hlapi.v3arch.asyncio as hlapi

    for name in [*collect.SNMP_AUTH.values(), *collect.SNMP_PRIV.values()]:
        assert hasattr(hlapi, name), name
    for name in ("CommunityData", "UsmUserData", "UdpTransportTarget", "bulk_walk_cmd", "get_cmd"):
        assert hasattr(hlapi, name), name
