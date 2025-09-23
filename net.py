# -*- coding: utf-8 -*-

"""Shared HTTP utilities for Nightscout requests.

Features:
- Centralized base URL and auth headers (NS_TOKEN preferred; NS_API_SECRET/NIGHTSCOUT_API_SECRET fallback via SHA1)
- Requests session with urllib3 Retry (backoff, retry on common 5xx/429)
- Configurable connect/read timeouts and SSL verification via env

Environment variables:
- NIGHTSCOUT_URL (required): Base URL, e.g., https://example.com
- NS_TOKEN (recommended): Nightscout token for Bearer auth
- NS_API_SECRET or NIGHTSCOUT_API_SECRET: API secret; will be SHA1 hashed for header 'api-secret'
- NS_TIMEOUT_CONNECT_SECONDS (default 5)
- NS_TIMEOUT_READ_SECONDS (default 30)
- NS_RETRIES (default 3)
- NS_RETRY_BACKOFF_SECONDS (default 0.5)
- NS_VERIFY_SSL (default false; set to true/1/yes to verify SSL)
"""

from __future__ import annotations

import os
import hashlib
from typing import Any, Dict, Optional, Tuple, Union

import requests
import urllib3
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from dotenv import load_dotenv
import sys
import os


# Resolve application base directory (EXE folder when compiled, else source folder)
def _env_candidates() -> list:
    """Return candidate directories to look for .env in priority order.
    1) Directory of the running binary (EXE in compiled builds)
    2) Current working directory
    3) Directory of this source file
    """
    cand = []
    try:
        if getattr(sys, 'frozen', False):
            cand.append(os.path.dirname(sys.executable))
    except Exception:
        pass
    try:
        cand.append(os.getcwd())
    except Exception:
        pass
    cand.append(os.path.dirname(__file__))
    # Deduplicate while preserving order
    seen = set(); out = []
    for p in cand:
        if p and p not in seen:
            out.append(p); seen.add(p)
    return out

def _resolve_env_path(prefer_existing: bool = True) -> str:
    for base in _env_candidates():
        env_path = os.path.join(base, '.env')
        if prefer_existing and os.path.exists(env_path):
            return env_path
    # default to first candidate (EXE dir or cwd) if nothing exists yet
    base = _env_candidates()[0]
    return os.path.join(base, '.env')

# Load .env from app dir and relax SSL warnings by default (common for self-hosted Nightscout)
load_dotenv(dotenv_path=_resolve_env_path(prefer_existing=True))
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Base configuration
BASE: Optional[str] = os.getenv("NIGHTSCOUT_URL")
TOKEN: Optional[str] = os.getenv("NS_TOKEN")
_SECRET: Optional[str] = os.getenv("NS_API_SECRET") or os.getenv("NIGHTSCOUT_API_SECRET")
SECRET_SHA1: Optional[str] = hashlib.sha1(_SECRET.encode("utf-8")).hexdigest() if _SECRET else None


def _as_bool(val: Optional[str], default: bool = False) -> bool:
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")


_CONNECT_TIMEOUT: float = float(os.getenv("NS_TIMEOUT_CONNECT_SECONDS", "5"))
_READ_TIMEOUT: float = float(os.getenv("NS_TIMEOUT_READ_SECONDS", "30"))
_RETRIES: int = int(os.getenv("NS_RETRIES", "3"))
_BACKOFF: float = float(os.getenv("NS_RETRY_BACKOFF_SECONDS", "0.5"))
_VERIFY_SSL: bool = _as_bool(os.getenv("NS_VERIFY_SSL"), default=False)

_SESSION: Optional[requests.Session] = None


def headers() -> Dict[str, str]:
    h: Dict[str, str] = {}
    # Prefer query parameter for token; many Nightscout servers don't accept Bearer
    # Only send api-secret header if no token is provided
    if not TOKEN and SECRET_SHA1:
        h["api-secret"] = SECRET_SHA1
    h["Accept"] = "application/json"
    return h


def _build_session() -> requests.Session:
    sess = requests.Session()
    # Configure retries on idempotent requests; we primarily use GET.
    retry = Retry(
        total=_RETRIES,
        read=_RETRIES,
        connect=_RETRIES,
        backoff_factor=_BACKOFF,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD", "OPTIONS"),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)
    return sess


def session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = _build_session()
    return _SESSION


def refresh_env() -> None:
    """Re-read .env and process env vars; reset HTTP session so new auth takes effect."""
    global BASE, TOKEN, _SECRET, SECRET_SHA1, _SESSION
    load_dotenv(dotenv_path=_resolve_env_path(prefer_existing=True), override=True)
    BASE = os.getenv("NIGHTSCOUT_URL")
    TOKEN = os.getenv("NS_TOKEN")
    _SECRET = os.getenv("NS_API_SECRET") or os.getenv("NIGHTSCOUT_API_SECRET")
    SECRET_SHA1 = hashlib.sha1(_SECRET.encode("utf-8")).hexdigest() if _SECRET else None
    _SESSION = None  # will be lazily rebuilt


def normalize_base(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    return url.rstrip("/")


def _ensure_base() -> str:
    base = normalize_base(BASE)
    if not base:
        raise RuntimeError("NIGHTSCOUT_URL not configured in .env")
    return base


TimeoutType = Union[None, float, Tuple[float, float]]


def _norm_timeout(timeout: TimeoutType) -> Tuple[float, float]:
    if timeout is None:
        return (_CONNECT_TIMEOUT, _READ_TIMEOUT)
    if isinstance(timeout, (int, float)):
        return (_CONNECT_TIMEOUT, float(timeout))
    # assume tuple(connect, read)
    c, r = timeout
    return (float(c), float(r))


def get_json(endpoint: str, params: Optional[Dict[str, Any]] = None, timeout: TimeoutType = None):
    base = _ensure_base()
    url = f"{base}{endpoint}"
    # Merge params and add token as query parameter for Nightscout servers that expect it
    merged_params: Dict[str, Any] = dict(params or {})
    if TOKEN:
        merged_params.setdefault("token", TOKEN)
        # Some Nightscout deployments also accept access_token
        merged_params.setdefault("access_token", TOKEN)
    elif SECRET_SHA1:
        # Fallback: some servers accept secret as query
        merged_params.setdefault("secret", SECRET_SHA1)
    resp = session().get(
        url,
        params=merged_params,
        headers=headers(),
        verify=_VERIFY_SSL,
        timeout=_norm_timeout(timeout),
    )
    resp.raise_for_status()
    return resp.json()


__all__ = [
    "BASE",
    "headers",
    "get_json",
    "session",
    "refresh_env",
    "TOKEN",
    "SECRET_SHA1",
]
