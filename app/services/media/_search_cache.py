"""SQLite-backed cache for provider search-API JSON *responses*.

Distinct from the content-addressed asset caches (cache_images/, cache_videos/,
cache_bgm/) — those cache downloaded *files*; this caches the raw search
responses themselves, so re-querying the same (provider, URL) within the TTL
costs zero API calls. That matters most for AMPT's retry-heavy job lifecycle
(portal automatically retries a failed job as "<title> (2)", "(3)"... — see
CLAUDE.md — often re-issuing the same visual_concepts queries against the
same providers). Ported from vidspeed's footage/cache.py::FootageCache, scoped
down to just the provider-response cache (no embedding cache — AMPT's
relevance scoring already has its own CLIP-based path).

Best-effort throughout: any cache read/write failure falls through to calling
the live fetch function rather than failing the search.
"""
import hashlib
import json
import os
import sqlite3
import threading
import time
from typing import Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from loguru import logger

from app.config import config
from app.utils import utils

_DB_LOCK = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

# Query-param names never included in the cache key (rotated API keys must
# not fragment the cache — the same logical query should hit regardless of
# which key served it) and never worth persisting in the DB either way.
_SECRET_PARAM_NAMES = {"key", "api_key", "apikey", "token", "access_token"}


def _db_path() -> str:
    return os.path.join(utils.storage_dir(), "search_cache.sqlite3")


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = _db_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS search_cache ("
            "cache_key TEXT PRIMARY KEY, response_json TEXT NOT NULL, cached_at REAL NOT NULL"
            ")"
        )
        conn.commit()
        _conn = conn
    return _conn


def _redact_url(url: str) -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    redacted = [(k, "REDACTED" if k.lower() in _SECRET_PARAM_NAMES else v) for k, v in query]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(redacted), parts.fragment))


def _cache_key(provider: str, url: str, header_names: tuple, body: Optional[dict] = None) -> str:
    # Header VALUES may hold secrets (e.g. Authorization) — only the set of
    # header names is part of the key, never their values. `body` is the
    # request JSON body for POST-based providers (e.g. Serper) whose actual
    # query lives there rather than in the URL — without it every POST call
    # to the same fixed endpoint URL would collapse onto one cache key.
    payload = json.dumps(
        [provider, _redact_url(url), sorted(header_names), body], sort_keys=True
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _cached_call(
    provider: str,
    url: str,
    fetch_fn: Callable[[], dict],
    header_names: tuple,
    body: Optional[dict],
) -> dict:
    if not config.app.get("search_cache_enabled", True):
        return fetch_fn()

    ttl = float(config.app.get("search_cache_ttl_seconds", 86400))
    key = _cache_key(provider, url, header_names, body)
    now = time.time()

    try:
        with _DB_LOCK:
            row = _get_conn().execute(
                "SELECT response_json, cached_at FROM search_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        if row is not None and (now - row[1]) < ttl:
            return json.loads(row[0])
    except Exception as exc:
        logger.warning(f"search cache read failed, fetching live: {exc}")

    result = fetch_fn()

    try:
        with _DB_LOCK:
            conn = _get_conn()
            conn.execute(
                "INSERT INTO search_cache (cache_key, response_json, cached_at) VALUES (?, ?, ?) "
                "ON CONFLICT(cache_key) DO UPDATE SET "
                "response_json=excluded.response_json, cached_at=excluded.cached_at",
                (key, json.dumps(result), now),
            )
            conn.commit()
    except Exception as exc:
        logger.warning(f"search cache write failed (result still returned live): {exc}")

    return result


def cached_get_json(
    provider: str,
    url: str,
    fetch_fn: Callable[[], dict],
    header_names: tuple = (),
) -> dict:
    """Return a cached JSON response for (provider, GET url) if present and
    fresh; otherwise call fetch_fn() and cache the result before returning
    it. Controlled by config.toml's search_cache_enabled/_ttl_seconds.
    """
    return _cached_call(provider, url, fetch_fn, header_names, body=None)


def cached_post_json(
    provider: str,
    url: str,
    body: dict,
    fetch_fn: Callable[[], dict],
    header_names: tuple = (),
) -> dict:
    """Same as cached_get_json(), for POST-based providers (e.g. Serper)
    whose query lives in the JSON body rather than the URL — `body` is
    folded into the cache key so distinct queries against the same fixed
    endpoint URL don't collide.
    """
    return _cached_call(provider, url, fetch_fn, header_names, body=body)
