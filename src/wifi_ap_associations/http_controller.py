"""A logged-in HTTPS session with a Wi-Fi controller, shared by the APs read through it.

The UniFi Network and TP-Link Omada drivers read access points through their
controller, each with its own login (the controller's address, a user and password),
not through another program's connection. One controller session serves every access
point configured with the same controller settings: one login, one client list per
read, shared for SHARE_SECONDS.

Sessions are kept open between polls (logging in every minute would fill the
controller's admin log) and log in again when the controller drops them. A session is
closed when the last access point using it is closed (AccessPointDriver.async_close).
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from collections.abc import Awaitable, Callable, Hashable, Mapping
from typing import Any

from . import AccessPointError

_LOGGER = logging.getLogger(__name__)

# Tests point this at a local plain-HTTP server.
SCHEME = "https"
REQUEST_TIMEOUT = 15  # seconds per request
SHARE_SECONDS = 20  # one read serves every AP of a controller polled within this time


class HttpController[T]:
    """A session with one controller: login, JSON requests, and a shared read."""

    def __init__(self, settings: Mapping[str, Any]) -> None:
        self.host = str(settings["host"]).strip()
        self.port = int(settings.get("port") or 443)
        self.username = str(settings.get("username") or "")
        self.password = str(settings.get("password") or "")
        self.site = str(settings.get("site") or "").strip()
        self.verify_ssl = str(settings.get("verify_ssl") or "no") == "yes"
        self._session: Any = None
        self._login_lock = asyncio.Lock()
        self._logged_in = False
        self._read_lock = asyncio.Lock()
        self._read_at = 0.0
        self._read: T | None = None
        # Bumped by invalidate(); a read counts only if it started after the last bump.
        self._generation = 0
        self._read_generation = -1

    @property
    def base_url(self) -> str:
        return f"{SCHEME}://{self.host}:{self.port}"

    def ssl_context(self) -> bool:
        """Certificate checks only if asked: local controllers mostly use their own."""
        return self.verify_ssl or SCHEME != "https"

    async def session(self) -> Any:
        import aiohttp

        if self._session is None or self._session.closed:
            # unsafe: keep cookies of controllers addressed by IP.
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            )
            self._logged_in = False
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        self._logged_in = False

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        allow_redirects: bool = True,
    ) -> tuple[int, Any, Mapping[str, str]]:
        """One request: (status, JSON body or None, headers). Network errors raise
        AccessPointError with the controller's address."""
        import aiohttp

        session = await self.session()
        try:
            async with session.request(
                method,
                self.base_url + path,
                json=json,
                params=params,
                headers=headers,
                ssl=self.ssl_context(),
                allow_redirects=allow_redirects,
            ) as response:
                try:
                    body = await response.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    body = None
                return response.status, body, response.headers
        except ssl.SSLCertVerificationError as err:
            raise AccessPointError(
                f"{self.host}: the controller's certificate isn't trusted ({err.reason}); "
                "turn off certificate verification for a controller with its own certificate"
            ) from err
        except (aiohttp.ClientError, TimeoutError, OSError) as err:
            raise AccessPointError(f"{self.host}:{self.port}: {err or type(err).__name__}") from err

    async def ensure_login(self) -> None:
        async with self._login_lock:
            if not self._logged_in:
                await self.login()
                self._logged_in = True

    def logged_out(self) -> None:
        """The controller dropped the session: log in again on the next request."""
        self._logged_in = False

    async def login(self) -> None:
        """Log in (and learn what the controller needs); raise AccessPointAuthError if
        the user or password is rejected."""
        raise NotImplementedError

    async def fetch(self) -> T:
        """Everything a read needs (logged in)."""
        raise NotImplementedError

    def invalidate(self) -> None:
        """The controller reported a change (e.g. a client joined): the next read asks it
        anew, also if a read was already under way when the change came."""
        self._generation += 1

    async def read(self) -> T:
        """The controller's data, shared by the APs polled within SHARE_SECONDS (and
        not invalidated since that read began)."""
        async with self._read_lock:
            if (
                self._read is not None
                and self._read_generation == self._generation
                and time.monotonic() - self._read_at < SHARE_SECONDS
            ):
                return self._read
            generation = self._generation
            await self.ensure_login()
            self._read = await self.fetch()
            self._read_at = time.monotonic()
            self._read_generation = generation
            return self._read


class ControllerPool[T]:
    """One controller session per distinct settings, released by its last user."""

    def __init__(self, factory: Callable[[Mapping[str, Any]], HttpController[T]]) -> None:
        self._factory = factory
        self._items: dict[Hashable, tuple[HttpController[T], set[int]]] = {}

    @staticmethod
    def key(settings: Mapping[str, Any]) -> Hashable:
        return tuple(
            str(settings.get(k) or "")
            for k in ("host", "port", "username", "password", "site", "verify_ssl")
        )

    def acquire(self, settings: Mapping[str, Any], user: object) -> HttpController[T]:
        key = self.key(settings)
        if key not in self._items:
            self._items[key] = (self._factory(settings), set())
        controller, users = self._items[key]
        users.add(id(user))
        return controller

    async def release(self, settings: Mapping[str, Any], user: object) -> None:
        key = self.key(settings)
        if key not in self._items:
            return
        controller, users = self._items[key]
        users.discard(id(user))
        if not users:
            del self._items[key]
            await controller.close()


async def retry_once_logged_out[T](
    controller: HttpController[Any], call: Callable[[], Awaitable[T]]
) -> T:
    """Run a request; if the controller had dropped the session, log in and run it again."""
    try:
        return await call()
    except LoggedOut:
        controller.logged_out()
        await controller.ensure_login()
        try:
            return await call()
        except LoggedOut as err:
            raise AccessPointError(
                f"{controller.host}: {err.reason or 'the controller refuses the request after a new login'}"
            ) from err


class LoggedOut(Exception):
    """The controller answered that the session isn't logged in (any more), or with an
    error that a new login may cure; reason says what it answered."""

    def __init__(self, reason: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
