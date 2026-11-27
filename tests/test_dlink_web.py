"""D-Link DAP web restart: the page's crypto, and the requests against a fake DAP."""

from __future__ import annotations

import asyncio
import hashlib
import hmac

import pytest
from aiohttp import web

from wifi_ap_associations import DRIVERS, AccessPointAuthError, AccessPointError
from wifi_ap_associations import dlink_dap_web as w

PASSWORD = "Secret-Pass1"
SALT_DATA, SALT_KEY = "f1llyDHZaItqsgiKoqMy", "CpDz1sIFjWqOVZ7SkM1"


def test_sbox_is_the_pages_table() -> None:
    # The first and last entries of AES_Sbox in the DAP-2610's sys_setting.php.
    assert w.SBOX[:8] == [99, 124, 119, 123, 242, 107, 111, 197]
    assert w.SBOX[-4:] == [176, 84, 187, 22]
    assert sorted(w.SBOX) == list(range(256))
    assert w.SHIFT_ROWS == (0, 5, 10, 15, 4, 9, 14, 3, 8, 13, 2, 7, 12, 1, 6, 11)


def test_login_password_is_the_pages_check() -> None:
    # check(): private_key = hex_hmac_md5(salt1, salt2 + pw), sent = hex_hmac_md5(salt1,
    # private_key), where the page's hex_hmac_md5 takes (data, key).
    private_key, sent = w.login_password(SALT_DATA, SALT_KEY, PASSWORD)
    expected = hmac.new((SALT_KEY + PASSWORD).encode(), SALT_DATA.encode(), hashlib.md5).hexdigest()
    assert private_key == expected
    assert sent == hmac.new(expected.encode(), SALT_DATA.encode(), hashlib.md5).hexdigest()


def test_cookie_round_trip_and_counter() -> None:
    private_key, _ = w.login_password(SALT_DATA, SALT_KEY, PASSWORD)
    cookie = w.encrypt_cookie("AbCdEfGh12345678:1795745587", private_key)
    assert len(cookie) == 88  # 64 bytes, as the DAP's own auth_key
    assert w.decrypt_cookie(cookie, private_key).rstrip("\0") == "AbCdEfGh12345678:1795745587"
    nxt = w.next_auth_key(cookie, private_key)
    assert w.decrypt_cookie(nxt, private_key).rstrip("\0") == "AbCdEfGh12345678:1795745588"


def test_cookie_under_another_key_has_no_counter() -> None:
    private_key, _ = w.login_password(SALT_DATA, SALT_KEY, PASSWORD)
    cookie = w.encrypt_cookie("AbCdEfGh12345678:1795745587", private_key)
    other, _ = w.login_password(SALT_DATA, SALT_KEY, "wrong")
    with pytest.raises(AccessPointError, match="counter"):
        w.next_auth_key(cookie, other)


def test_drivers() -> None:
    assert DRIVERS["dlink_dap_ssh"].RESTARTS is True
    assert DRIVERS["openwrt_ssh"].RESTARTS is False


class FakeDap:
    """The DAP's web interface, as far as the restart needs it."""

    def __init__(self, password: str = PASSWORD) -> None:
        self.private_key, self.expected = w.login_password(SALT_DATA, SALT_KEY, password)
        self.counter = 1795745587
        self.log: list[str] = []
        self.restarted = False

    def cookie(self) -> str:
        return w.encrypt_cookie(f"AbCdEfGh12345678:{self.counter}", self.private_key)

    def app(self) -> web.Application:
        async def page(request: web.Request) -> web.Response:
            self.log.append(f"{request.method} {request.rel_url.raw_path_qs}")
            assert request.cookies.get("session_uid") and len(request.cookies["session_uid"]) == 16
            if request.path == "/login.php" and request.method == "GET":
                body = (
                    "function check(){ private_key = hex_hmac_md5("
                    f'"{SALT_DATA}","{SALT_KEY}"+f.LOGIN_PASSWD.value); }}'
                )
            elif request.path == "/login.php":
                form = await request.post()
                if form.get("LOGIN_PASSWD") != self.expected or form.get("LOGIN_USER") != "admin":
                    return web.Response(text="<META HTTP-EQUIV=Refresh CONTENT='0; url=login_fail.php'>")
                body = "<META HTTP-EQUIV=Refresh CONTENT='0; url=index.php'>"
            elif request.path == "/sys_cfg_valid.xgi":
                sent = w.decrypt_cookie(request.cookies["auth_key"], self.private_key).rstrip("\0")
                assert sent == f"AbCdEfGh12345678:{self.counter + 1}", sent
                assert request.rel_url.raw_query_string == "&exeshell=submit%20REBOOT"
                self.restarted = True
                body = "rebooting"
            else:
                body = "page"
            resp = web.Response(text=body)
            resp.set_cookie("auth_key", self.cookie())
            return resp

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", page)
        return app


async def _serve(fake: FakeDap):
    runner = web.AppRunner(fake.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


def test_restart_sends_reboot_once() -> None:
    async def go() -> FakeDap:
        fake = FakeDap()
        runner, port = await _serve(fake)
        try:
            await w.async_restart("127.0.0.1", "admin", PASSWORD, port=port)
        finally:
            await runner.cleanup()
        return fake

    fake = asyncio.run(go())
    assert fake.restarted
    assert fake.log == [
        "GET /index.php", "GET /login.php", "POST /login.php", "GET /index.php",
        "GET /sys_setting.php", "GET /sys_cfg_valid.xgi?&exeshell=submit%20REBOOT",
    ]
    # Never the factory reset or anything else on that page.
    assert sum("exeshell" in line for line in fake.log) == 1


def test_wrong_password_never_reaches_reboot() -> None:
    async def go() -> FakeDap:
        fake = FakeDap()
        runner, port = await _serve(fake)
        try:
            with pytest.raises(AccessPointAuthError):
                await w.async_restart("127.0.0.1", "admin", "wrong", port=port)
        finally:
            await runner.cleanup()
        return fake

    fake = asyncio.run(go())
    assert not fake.restarted
    assert not any("exeshell" in line for line in fake.log)


def test_unreachable_is_an_access_point_error() -> None:
    with pytest.raises(AccessPointError):
        asyncio.run(w.async_restart("127.0.0.1", "admin", PASSWORD, port=9))
