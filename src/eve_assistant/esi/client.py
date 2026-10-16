"""A small ESI client that follows CCP's rules.

- Sends the pinned X-Compatibility-Date and a descriptive User-Agent.
- Serves from its own cache while ESI's Cache-Control says the data is fresh,
  and otherwise asks with If-None-Match, so unchanged data costs less.
- Follows X-Pages for paged routes.
- Stops on rate limits (429 / 420) instead of retrying blindly, and watches the
  legacy error budget so a burst of errors cannot get the app blocked.
"""

import json
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from eve_assistant.esi import COMPATIBILITY_DATE, ESI_BASE_URL

ERROR_BUDGET_FLOOR = 20


class EsiError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class EsiRateLimited(EsiError):
    def __init__(self, message: str, retry_after: float):
        super().__init__(message, status=429)
        self.retry_after = retry_after


@dataclass(frozen=True)
class EsiResult:
    data: Any
    from_cache: bool  # True when ESI was not asked or answered 304


def _max_age(headers: httpx.Headers) -> float:
    match = re.search(r"max-age=(\d+)", headers.get("cache-control", ""))
    return float(match.group(1)) if match else 0.0


class EsiClient:
    def __init__(
        self,
        http: httpx.Client,
        db: sqlite3.Connection,
        token_for: Callable[[int], str] | None = None,
        base_url: str = ESI_BASE_URL,
        clock: Callable[[], float] = time.time,
    ):
        self._http = http
        self._db = db
        self._token_for = token_for
        self._base = base_url.rstrip("/")
        self._clock = clock

    def get(
        self, path: str, character_id: int | None = None, params: dict | None = None
    ) -> EsiResult:
        """GET a route. Pass character_id for routes that need that character's token."""
        query = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        key = f"GET {path}?{query} as {character_id or '-'}"
        cached = self._db.execute(
            "SELECT etag, expires_at, body FROM esi_cache WHERE cache_key = ?", (key,)
        ).fetchone()
        if cached and cached["expires_at"] and cached["expires_at"] > self._clock():
            return EsiResult(json.loads(cached["body"]), from_cache=True)

        headers = {"X-Compatibility-Date": COMPATIBILITY_DATE, "Accept": "application/json"}
        if character_id is not None:
            if self._token_for is None:
                raise EsiError("This route needs a logged-in character.")
            headers["Authorization"] = f"Bearer {self._token_for(character_id)}"
        if cached and cached["etag"]:
            headers["If-None-Match"] = cached["etag"]

        first = self._request(path, params, headers)
        if first.status_code == 304 and cached:
            self._store(key, cached["etag"], first.headers, cached["body"])
            return EsiResult(json.loads(cached["body"]), from_cache=True)

        data = first.json()
        pages = int(first.headers.get("x-pages", "1"))
        if pages > 1:
            page_headers = {k: v for k, v in headers.items() if k != "If-None-Match"}
            for page in range(2, pages + 1):
                response = self._request(path, {**(params or {}), "page": page}, page_headers)
                data.extend(response.json())

        body = json.dumps(data)
        self._store(key, first.headers.get("etag"), first.headers, body)
        return EsiResult(data, from_cache=False)

    def _request(self, path: str, params: dict | None, headers: dict) -> httpx.Response:
        try:
            response = self._http.get(f"{self._base}{path}", params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise EsiError(f"Could not reach ESI: {exc.__class__.__name__}") from None

        remaining = response.headers.get("x-esi-error-limit-remain")
        if remaining is not None and int(remaining) < ERROR_BUDGET_FLOOR:
            reset = float(response.headers.get("x-esi-error-limit-reset", "60"))
            raise EsiRateLimited(
                f"ESI error budget nearly used up ({remaining} left); pausing.", reset
            )
        if response.status_code in (420, 429):
            retry = float(
                response.headers.get("retry-after")
                or response.headers.get("x-esi-error-limit-reset")
                or 60
            )
            raise EsiRateLimited(f"ESI rate limit reached ({response.status_code}).", retry)
        if response.status_code == 304:
            return response
        if response.status_code == 403:
            raise EsiError(
                "ESI refused access (403). The character may be missing a permission; "
                "check `eve-assistant characters`.",
                403,
            )
        if response.status_code >= 400:
            raise EsiError(f"ESI returned {response.status_code} for {path}.", response.status_code)
        return response

    def _store(self, key: str, etag: str | None, headers: httpx.Headers, body: str) -> None:
        expires = self._clock() + _max_age(headers)
        self._db.execute(
            "INSERT INTO esi_cache (cache_key, etag, expires_at, body) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET etag = excluded.etag, "
            "expires_at = excluded.expires_at, body = excluded.body",
            (key, etag, expires, body),
        )
        self._db.commit()
