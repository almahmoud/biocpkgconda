"""HTTP with a User-Agent, timeouts and retries."""

import hashlib
import http.client
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import Error

USER_AGENT = "biocpkgconda (+https://github.com/almahmoud/biocpkgconda)"
TIMEOUT = 60
RETRIES = 3
sleep = time.sleep

_NETWORK_ERRORS = (urllib.error.URLError, OSError, http.client.HTTPException)


class StatusError(Error):
    """A request that the server answered with a non-retryable HTTP error."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


def _open(url: str, headers: dict, secret_headers: dict, method: str):
    request = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT, **headers})
    for name, value in secret_headers.items():
        # Unredirected headers are not sent on to the target of a redirect.
        request.add_unredirected_header(name, value)
    return urllib.request.urlopen(request, timeout=TIMEOUT)


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


def _lower(headers) -> dict[str, str]:
    return {name.lower(): value for name, value in headers.items()} if headers else {}


def request(url: str, *, headers: dict | None = None, secret_headers: dict | None = None,
            method: str = "GET") -> Response:
    """Send a request and return the final response. Network errors, 429 and 5xx are retried."""
    reason = ""
    for attempt in range(RETRIES + 1):
        if attempt:
            sleep(2 ** attempt)
        try:
            with _open(url, headers or {}, secret_headers or {}, method) as response:
                return Response(response.status, _lower(response.headers), response.read())
        except urllib.error.HTTPError as err:
            with err:
                if not _retryable(err.code):
                    return Response(err.code, _lower(err.headers), err.read())
            reason = f"HTTP {err.code}"
        except _NETWORK_ERRORS as err:
            reason = str(getattr(err, "reason", err))
    raise Error(f"{method} {url} failed after {RETRIES + 1} attempts: {reason}")


def open_stream(url: str, *, secret_headers: dict | None = None):
    """Open a GET response for reading in chunks. Connecting is retried; reading is not."""
    reason = ""
    for attempt in range(RETRIES + 1):
        if attempt:
            sleep(2 ** attempt)
        try:
            return _open(url, {}, secret_headers or {}, "GET")
        except urllib.error.HTTPError as err:
            err.close()
            if not _retryable(err.code):
                raise StatusError(f"GET {url} returned HTTP {err.code}", err.code) from None
            reason = f"HTTP {err.code}"
        except _NETWORK_ERRORS as err:
            reason = str(getattr(err, "reason", err))
    raise Error(f"GET {url} failed after {RETRIES + 1} attempts: {reason}")


def _cache_name(url: str) -> str:
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    base = re.sub(r"[^A-Za-z0-9._-]", "_", url.rstrip("/").rsplit("/", 1)[-1])[:64]
    return f"{digest}-{base}"


def fetch(url: str, cache: Path | None = None) -> bytes:
    """GET a URL and return the body, reusing a copy in ``cache`` when present."""
    path = Path(cache) / _cache_name(url) if cache else None
    if path and path.exists():
        return path.read_bytes()
    response = request(url)
    if response.status != 200:
        raise Error(f"GET {url} returned HTTP {response.status}")
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".partial")
        partial.write_bytes(response.body)
        partial.replace(path)
    return response.body
