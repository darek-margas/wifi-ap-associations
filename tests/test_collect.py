"""Collector: the rules that keep a shared report safe (read-only commands, redaction)."""

from __future__ import annotations

import pytest

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
