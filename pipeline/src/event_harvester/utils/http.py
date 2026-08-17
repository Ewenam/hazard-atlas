# http_helper.py
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, Union

import requests
from requests import Response
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


Json = Union[Dict[str, Any], list]


class HTTPError(RuntimeError):
    """Raised when an HTTP request fails or returns a non-OK response."""


@dataclass(frozen=True)
class HTTPConfig:
    timeout: Tuple[float, float] = (5.0, 30.0)  # (connect, read)
    max_retries: int = 5
    backoff_factor: float = 0.6
    retry_statuses: Tuple[int, ...] = (429, 500, 502, 503, 504)
    user_agent: str = "event-harvester/1.0 (+https://example.com)"
    accept: str = "application/json, text/plain;q=0.9, */*;q=0.8"
    # If you know some APIs are strict about headers, you can expand these.
    default_headers: Optional[Dict[str, str]] = None


def _make_session(cfg: HTTPConfig) -> requests.Session:
    session = requests.Session()

    retry = Retry(
        total=cfg.max_retries,
        connect=cfg.max_retries,
        read=cfg.max_retries,
        status=cfg.max_retries,
        backoff_factor=cfg.backoff_factor,
        status_forcelist=cfg.retry_statuses,
        allowed_methods=frozenset({"GET", "HEAD"}),
        raise_on_status=False,
        respect_retry_after_header=True,  # honors Retry-After for 429/503, etc.
    )

    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    # Base headers
    base_headers = {
        "User-Agent": cfg.user_agent,
        "Accept": cfg.accept,
        "Accept-Encoding": "gzip, deflate",
    }
    if cfg.default_headers:
        base_headers.update(cfg.default_headers)

    session.headers.update(base_headers)
    return session


def _raise_for_bad_response(resp: Response, url: str) -> None:
    # requests' resp.ok is True for 200–399
    if resp.ok:
        return

    content_type = (resp.headers.get("Content-Type") or "").lower()
    body_preview = ""
    try:
        if "application/json" in content_type:
            body_preview = json.dumps(resp.json())[:800]
        else:
            body_preview = (resp.text or "")[:800]
    except Exception:
        body_preview = "<unavailable>"

    raise HTTPError(
        f"HTTP {resp.status_code} for {url}. "
        f"Content-Type={resp.headers.get('Content-Type')}. "
        f"Body preview: {body_preview}"
    )


def get_json(
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: Optional[Tuple[float, float]] = None,
    session: Optional[requests.Session] = None,
    cfg: Optional[HTTPConfig] = None,
    require_json: bool = True,
) -> Json:
    """
    GET a URL and return parsed JSON.

    - Retries on transient errors (429, 5xx) with exponential backoff.
    - Raises HTTPError on non-OK responses.
    - Raises HTTPError if JSON parsing fails and require_json=True.
    """
    cfg = cfg or HTTPConfig()
    sess = session or _make_session(cfg)

    req_timeout = timeout or cfg.timeout
    merged_headers = {}
    if headers:
        merged_headers.update(headers)

    resp = sess.get(url, params=params, headers=merged_headers, timeout=req_timeout)

    _raise_for_bad_response(resp, resp.url)

    # Parse JSON
    try:
        return resp.json()
    except ValueError as e:
        if not require_json:
            # Return raw text in a JSON-like wrapper if caller wants to handle it.
            return {"_raw": resp.text, "_error": "invalid_json"}  # type: ignore[return-value]
        raise HTTPError(f"Failed to parse JSON from {resp.url}: {e}. Body: {(resp.text or '')[:800]}") from e


def get_text(
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: Optional[Tuple[float, float]] = None,
    session: Optional[requests.Session] = None,
    cfg: Optional[HTTPConfig] = None,
) -> str:
    """GET a URL and return response text (with retries & error handling)."""
    cfg = cfg or HTTPConfig()
    sess = session or _make_session(cfg)

    req_timeout = timeout or cfg.timeout
    merged_headers = {}
    if headers:
        merged_headers.update(headers)

    resp = sess.get(url, params=params, headers=merged_headers, timeout=req_timeout)

    _raise_for_bad_response(resp, resp.url)
    return resp.text


def polite_sleep(min_seconds: float = 0.5, jitter: float = 0.25) -> None:
    """Small randomized delay to be polite to servers."""
    # Avoid importing random at top if you want minimal overhead
    import random
    time.sleep(min_seconds + random.random() * jitter)


if __name__ == "__main__":
    # Quick sanity test
    data = get_json("https://httpbin.org/json")
    print("Keys:", list(data.keys()) if isinstance(data, dict) else type(data))
