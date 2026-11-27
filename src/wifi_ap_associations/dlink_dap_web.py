"""D-Link DAP web interface, used only to restart the access point.

Neither of the other ways works on a DAP-2610 (firmware v2.06): the SSH CLI's
"reboot" prints "sh: can't open '/dev/null': Permission denied" and the AP stays up,
and SNMP's deviceRestart (1.3.6.1.4.1.171.10.37.<product>.4.2.1.0) takes the value 1
and does nothing. The web interface's own Restart button does reboot it, and this
module sends the same requests a browser does:

1. GET /login.php with a "session_uid" cookie (16 random characters, which the
   browser makes up itself). The page embeds two salts for this session; the
   password is sent as HMAC-MD5(salt1, key=private_key), where
   private_key = HMAC-MD5(salt1, key=salt2 + password). The page's hex_hmac_md5
   takes (data, key). A failed login redirects to login_fail.php.
2. The AP keeps an "auth_key" cookie: 64 bytes encrypted with private_key, holding
   "<value>:<counter>". Before a command the page decrypts it, adds 1 to the
   counter and encrypts it again (a replay guard). Its "AES" never expands the key,
   so it is a single round per 16-byte block: XOR with the key's first 16 bytes,
   S-box, shift rows, XOR with its last 16 bytes. The key is private_key's 32 hex
   characters as ASCII bytes.
3. GET /sys_cfg_valid.xgi?&exeshell=submit REBOOT. The factory reset next to it on
   the same page is a different command; only this exact string is ever sent.

Seen on a DAP-2610 with firmware v2.06; other DAPs with the same web interface
(e.g. the DAP-3662) should behave the same.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import string

import aiohttp
from yarl import URL

from . import AccessPointAuthError, AccessPointError

# Exactly what the page's do_reboot() opens: exe_str("submit REBOOT").
RESTART_PATH = "/sys_cfg_valid.xgi?&exeshell=submit%20REBOOT"
RESTART_TIMEOUT = 30

_SALTS = re.compile(r'private_key\s*=\s*hex_hmac_md5\("([^"]*)","([^"]*)"\+f\.LOGIN_PASSWD')
_USER_AGENT = "Mozilla/5.0 (wifi-ap-associations)"


def hmac_md5(data: str, key: str) -> str:
    """The page's hex_hmac_md5(data, key)."""
    return hmac.new(key.encode("latin-1"), data.encode("latin-1"), hashlib.md5).hexdigest()


def login_password(salt_data: str, salt_key: str, password: str) -> tuple[str, str]:
    """(private_key, the LOGIN_PASSWD to post), as the login page's check() makes them."""
    private_key = hmac_md5(salt_data, salt_key + password)
    return private_key, hmac_md5(salt_data, private_key)


def _rotl8(x: int, shift: int) -> int:
    return ((x << shift) | (x >> (8 - shift))) & 0xFF


def _make_sbox() -> list[int]:
    """The AES S-box (the page carries it as a table; computing it avoids a copy)."""
    sbox = [0] * 256
    p = q = 1
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        sbox[p] = q ^ _rotl8(q, 1) ^ _rotl8(q, 2) ^ _rotl8(q, 3) ^ _rotl8(q, 4) ^ 0x63
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


SBOX = _make_sbox()
SBOX_INV = [0] * 256
for _i, _v in enumerate(SBOX):
    SBOX_INV[_v] = _i
SHIFT_ROWS = (0, 5, 10, 15, 4, 9, 14, 3, 8, 13, 2, 7, 12, 1, 6, 11)
SHIFT_ROWS_INV = tuple(SHIFT_ROWS.index(i) for i in range(16))


def _key_bytes(private_key: str) -> bytes:
    """hexstr2array(str2hexstr(private_key), 32): its characters as bytes, 32 long."""
    return private_key.encode("latin-1")[:32].ljust(32, b"\0")


def _encrypt_block(block: list[int], key: bytes) -> list[int]:
    block = [b ^ k for b, k in zip(block, key[:16])]
    block = [SBOX[b] for b in block]
    block = [block[SHIFT_ROWS[i]] for i in range(16)]
    return [b ^ k for b, k in zip(block, key[16:32])]


def _decrypt_block(block: list[int], key: bytes) -> list[int]:
    block = [b ^ k for b, k in zip(block, key[16:32])]
    block = [block[SHIFT_ROWS_INV[i]] for i in range(16)]
    block = [SBOX_INV[b] for b in block]
    return [b ^ k for b, k in zip(block, key[:16])]


def _crypt(data: bytes, private_key: str, decrypt: bool) -> bytes:
    """AES_Encrypt128 / AES_Decrypt128: 64 bytes (zero-padded or cut), 4 blocks."""
    key = _key_bytes(private_key)
    data = data[:64].ljust(64, b"\0")
    step = _decrypt_block if decrypt else _encrypt_block
    out: list[int] = []
    for i in range(0, 64, 16):
        out += step(list(data[i : i + 16]), key)
    return bytes(out)


def encrypt_cookie(plain: str, private_key: str) -> str:
    """base64encode(hexstr2str(AES_Encrypt128(str2hexstr(plain), str2hexstr(key))))."""
    return base64.b64encode(_crypt(plain.encode("latin-1"), private_key, False)).decode("ascii")


def decrypt_cookie(cookie: str, private_key: str) -> str:
    """The plain "<value>:<counter>" inside an auth_key cookie (trailing NULs kept)."""
    try:
        raw = base64.b64decode(cookie + "=" * (-len(cookie) % 4))
    except ValueError as err:
        raise AccessPointError(f"auth_key cookie is not base64: {err}") from err
    return _crypt(raw, private_key, True).decode("latin-1")


def next_auth_key(cookie: str, private_key: str) -> str:
    """set_auth_key(): the cookie with its counter one up, encrypted again."""
    parts = decrypt_cookie(cookie, private_key).split(":")
    digits = re.match(r"\s*([+-]?\d+)", parts[1]) if len(parts) > 1 else None
    if digits is None:
        raise AccessPointError("auth_key cookie has no counter (login key mismatch?)")
    return encrypt_cookie(f"{parts[0]}:{int(digits.group(1)) + 1}", private_key)


class _Browser:
    """Just enough of a browser: one session's cookies, sent by hand (the AP is
    addressed by IP, which aiohttp's cookie jar won't keep cookies for)."""

    def __init__(self, session: aiohttp.ClientSession, base: str) -> None:
        self._session = session
        self._base = base
        self.cookies: dict[str, str] = {
            "session_uid": "".join(
                secrets.choice(string.ascii_letters + string.digits) for _ in range(16)
            )
        }

    async def request(
        self, method: str, path: str, referer: str, data: dict[str, str] | None = None
    ) -> str:
        headers = {
            "User-Agent": _USER_AGENT,
            "Referer": self._base + referer,
            "Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items()),
        }
        async with self._session.request(
            method, URL(self._base + path, encoded=True), data=data, headers=headers,
            allow_redirects=False,
        ) as resp:
            for name, morsel in resp.cookies.items():
                self.cookies[name] = morsel.value
            body = await resp.text(errors="replace")
            if resp.status >= 400:
                raise AccessPointError(f"{path}: HTTP {resp.status}")
            return body


async def async_restart(host: str, username: str, password: str, port: int = 80) -> None:
    """Log in to the web interface and press its Restart. Returns once the AP took
    the command. Raises AccessPointAuthError for a rejected login, AccessPointError
    for anything else."""
    base = f"http://{host}" + (f":{port}" if port != 80 else "")
    try:
        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=RESTART_TIMEOUT),
        ) as session:
            browser = _Browser(session, base)
            await browser.request("GET", "/index.php", "/")
            page = await browser.request("GET", "/login.php", "/")
            salts = _SALTS.search(page)
            if salts is None:
                raise AccessPointError("login page without its salts (not a DAP web interface?)")
            private_key, sent = login_password(salts.group(1), salts.group(2), password)
            body = await browser.request(
                "POST", "/login.php", "/login.php",
                {"ACTION_POST": "LOGIN", "LOGIN_USER": username, "LOGIN_PASSWD": sent,
                 "f_date": "", "f_time": "", "login": " Login "},
            )
            if "login_fail" in body:
                raise AccessPointAuthError(f"{host}: web login rejected")
            # The pages a browser passes through, each of which renews auth_key.
            await browser.request("GET", "/index.php", "/login.php")
            await browser.request("GET", "/sys_setting.php", "/index.php")
            if "auth_key" not in browser.cookies:
                raise AccessPointError("no auth_key cookie after login")
            browser.cookies["auth_key"] = next_auth_key(browser.cookies["auth_key"], private_key)
            try:
                body = await browser.request("GET", RESTART_PATH, "/sys_setting.php")
            except (aiohttp.ServerDisconnectedError, aiohttp.ClientPayloadError):
                return  # dropped the connection on its way down
            if "login_fail" in body:
                raise AccessPointError("web session rejected the restart")
    except AccessPointError:
        raise
    except (aiohttp.ClientError, TimeoutError) as err:
        raise AccessPointError(f"{host}: {err!r}") from err
