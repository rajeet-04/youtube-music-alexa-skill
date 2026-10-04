"""Optional per-installation YouTube Music clients built from encrypted sessions."""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Callable, Mapping

from .credentials import CredentialError, Credentials
from .music import UserContext

ORIGIN = "https://music.youtube.com"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


def build_auth_headers(cookie: str, account_index: int, locale: str = "en-IN") -> dict[str, str]:
    """Server-built header allowlist. Callers never choose origin, path or other headers.

    The time-sensitive SAPISIDHASH is refreshed by ytmusicapi on every request;
    the placeholder only marks the headers as browser-authenticated.
    """
    return {
        "cookie": cookie,
        "x-goog-authuser": str(int(account_index)),
        "authorization": "SAPISIDHASH 0_0",
        "user-agent": USER_AGENT,
        "accept-language": locale,
        "content-type": "application/json",
        "origin": ORIGIN,
        "x-origin": ORIGIN,
    }


def _make_ytmusic(auth: Mapping[str, str] | None, context: UserContext):
    from ytmusicapi import YTMusic  # lazy: keeps the import graph light for tests

    language = context.locale.split("-")[0] or "en"
    if auth is None:
        return YTMusic(language=language, location=context.region)
    return YTMusic(auth=dict(auth), language=language, location=context.region)


def account_probe(bundle: Mapping[str, object], *, client_builder: Callable[..., Any] = _make_ytmusic) -> bool:
    """Uncached upstream check that the session really is a signed-in account.

    A logged-out cookie can still produce HTTP 200, so success requires an
    account name in the response.
    """
    cookie = str(bundle["cookie"])
    if "__Secure-3PAPISID" not in cookie:
        return False
    from .music import ANONYMOUS

    client = client_builder(build_auth_headers(cookie, int(bundle.get("account_index", 0))), ANONYMOUS)
    info = client.get_account_info()
    return bool(isinstance(info, dict) and info.get("accountName"))


class ClientFactory:
    """``factory(context)``: shared anonymous client, or an installation-scoped one.

    Authenticated clients are cached per (installation, credential generation,
    locale) so a reconnect or disconnect never reuses an old session.
    """

    def __init__(self, credentials: Credentials | None, *, client_builder: Callable[..., Any] = _make_ytmusic,
                 max_clients: int = 64) -> None:
        self._credentials = credentials
        self._build = client_builder
        self._max = max_clients
        self._anonymous: dict[tuple, Any] = {}
        self._clients: OrderedDict[tuple, Any] = OrderedDict()
        self._lock = threading.Lock()

    def __call__(self, context: UserContext):
        if context.installation_id is None or context.personalization_status != "connected" \
                or self._credentials is None:
            key = (context.locale, context.region)
            with self._lock:
                if key not in self._anonymous:
                    self._anonymous[key] = self._build(None, context)
                return self._anonymous[key]
        key = context.cache_key
        with self._lock:
            if key in self._clients:
                self._clients.move_to_end(key)
                return self._clients[key]
        bundle = self._credentials.lookup(context.installation_id)  # may raise CredentialError
        if bundle is None or bundle.generation != context.generation:
            raise CredentialError("credentials changed")
        client = self._build(build_auth_headers(bundle.cookie, bundle.account_index, context.locale), context)
        with self._lock:
            self._clients[key] = client
            while len(self._clients) > self._max:
                self._clients.popitem(last=False)
        return client
