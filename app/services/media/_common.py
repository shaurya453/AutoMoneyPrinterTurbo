"""Shared state and low-level utilities for the media package.

This module is the dependency base layer: it must NOT import from
images.py or videos.py.
"""
import os
import random
import re
import threading
import time
from typing import Optional

import requests
from loguru import logger
from PIL import Image, UnidentifiedImageError

from app.config import config
from app.services.media._search_cache import cached_get_json, cached_post_json
from app.utils import utils

# ---------------------------------------------------------------------------
# Thread-safe API key rotation
# ---------------------------------------------------------------------------

_api_key_counter = 0
_api_key_lock = threading.Lock()

# ---------------------------------------------------------------------------
# HTTP timeouts as (connect, read) tuples, in seconds.
# ---------------------------------------------------------------------------

_HTTP_TIMEOUT_THUMBNAIL = (15, 30)  # small thumbnail downloads for reranking
_HTTP_TIMEOUT_API = (30, 60)        # provider search/JSON endpoints
_HTTP_TIMEOUT_IMAGE = (10, 15)      # full-size image downloads
_HTTP_TIMEOUT_MEDIA = (60, 240)     # video/audio downloads

# ---------------------------------------------------------------------------
# Known-slow or always-irrelevant domains
# ---------------------------------------------------------------------------

# Known-slow or always-irrelevant domains that appear in open-web image search
# results. Downloads from these are skipped without attempting a connection.
_SLOW_IMAGE_DOMAINS = {
    "allegroimg.com",
    "a.allegroimg.com",
    "imgs.699pic.com",
    "assets.699pic.com",
    # Facebook/Instagram "lookaside" crawler endpoints serve an HTML page,
    # never the image — every download "succeeds" then fails raster validation.
    "lookaside.fbsbx.com",
    "lookaside.instagram.com",
    "tiktok.com",
    # Hotlink-protected: every direct download returns 403.
    "stockcake.com",
}

# Hosts that returned HTTP 429 during this process run; populated dynamically.
# Maps host → monotonic unblock timestamp. Entries expire after _BLOCKED_HOST_TTL_SECONDS.
_BLOCKED_HOST_TTL_SECONDS = 300  # 5 minutes
_per_run_blocked_hosts: dict = {}
_blocked_hosts_lock = threading.Lock()

# Per-host download-failure circuit breaker: hotlink-protected publishers
# (403 on every image) and HTML-serving endpoints fail reliably, so after
# _HOST_FAILURE_BLOCK_THRESHOLD failures without an intervening success the
# host joins _per_run_blocked_hosts for the TTL instead of being re-attempted
# for every candidate the search providers surface from it.
_HOST_FAILURE_BLOCK_THRESHOLD = 3
_host_failure_counts: dict = {}

# Image URLs that failed to download (or weren't decodable) this run — a URL
# that 403'd for one worker thread will 403 for every other sentence too, so
# don't let it occupy a candidate slot again.
_failed_image_urls: set = set()

# Search providers on temporary cooldown (quota exhausted, rate-limited, or
# stalling). Maps provider name → monotonic reactivation timestamp.
_provider_cooldowns: dict = {}


def is_host_blocked(host: str) -> bool:
    """True if `host` is currently 429-blocked or failure-blocked."""
    with _blocked_hosts_lock:
        unblock_at = _per_run_blocked_hosts.get(host)
        if unblock_at is None:
            return False
        if time.monotonic() < unblock_at:
            return True
        del _per_run_blocked_hosts[host]
        return False


def block_host(host: str, ttl: float = _BLOCKED_HOST_TTL_SECONDS) -> None:
    with _blocked_hosts_lock:
        _per_run_blocked_hosts[host] = time.monotonic() + ttl


def register_host_failure(host: str) -> bool:
    """Count a download failure against `host`; block it once the threshold
    is hit. Returns True when this call newly blocked the host (so the caller
    can log it once)."""
    if not host:
        return False
    with _blocked_hosts_lock:
        count = _host_failure_counts.get(host, 0) + 1
        _host_failure_counts[host] = count
        if count >= _HOST_FAILURE_BLOCK_THRESHOLD:
            _per_run_blocked_hosts[host] = time.monotonic() + _BLOCKED_HOST_TTL_SECONDS
            _host_failure_counts[host] = 0
            return True
    return False


def register_host_success(host: str) -> None:
    """A successful download resets the host's failure streak."""
    if not host:
        return
    with _blocked_hosts_lock:
        _host_failure_counts.pop(host, None)


def mark_failed_url(url: str) -> None:
    with _blocked_hosts_lock:
        _failed_image_urls.add(url)


def is_failed_url(url: str) -> bool:
    with _blocked_hosts_lock:
        return url in _failed_image_urls


def provider_on_cooldown(provider: str) -> bool:
    with _blocked_hosts_lock:
        until = _provider_cooldowns.get(provider)
        if until is None:
            return False
        if time.monotonic() < until:
            return True
        del _provider_cooldowns[provider]
        return False


def set_provider_cooldown(provider: str, seconds: float) -> None:
    with _blocked_hosts_lock:
        _provider_cooldowns[provider] = time.monotonic() + seconds


# Run-length cooldown for quota exhaustion (won't self-heal within a single
# job, unlike a transient 429 rate-limit) — distinct from the short 429
# backoffs elsewhere in this module.
_QUOTA_EXHAUSTION_COOLDOWN_SECONDS = 6 * 3600

_QUOTA_EXHAUSTION_PATTERNS = re.compile(
    r"payment required|quota|usage limit|out of credit|insufficient credit|"
    r"exceeded.{0,20}(limit|quota)|monthly limit|api limit reached",
    re.IGNORECASE,
)


def is_quota_exhaustion(status_code: Optional[int], response_text: str = "") -> bool:
    """True if a response looks like hard quota exhaustion rather than a
    transient rate-limit — HTTP 402 (Payment Required) is an unambiguous
    signal on its own; other codes need a matching phrase in the body since
    429/403 are also used for ordinary short-lived rate-limiting."""
    if status_code == 402:
        return True
    return bool(response_text) and bool(_QUOTA_EXHAUSTION_PATTERNS.search(response_text))

# ---------------------------------------------------------------------------
# TLS verification
# ---------------------------------------------------------------------------

_TLS_VERIFY = None  # cached on first call; config never changes at runtime


def _get_tls_verify() -> bool:
    # 默认开启 TLS 证书校验，防止素材搜索和下载过程被中间人篡改。
    # 仅在企业代理、自签证书等明确需要的场景下，允许用户通过
    # `config.toml` 显式设置 `tls_verify = false` 临时关闭。
    global _TLS_VERIFY
    if _TLS_VERIFY is not None:
        return _TLS_VERIFY
    tls_verify = config.app.get("tls_verify", True)
    if isinstance(tls_verify, str):
        tls_verify = tls_verify.strip().lower() not in ("0", "false", "no", "off")

    if not tls_verify:
        logger.warning(
            "TLS certificate verification is disabled by config.app.tls_verify=false. "
            "Only use this in trusted proxy environments."
        )

    _TLS_VERIFY = bool(tls_verify)
    return _TLS_VERIFY


# ---------------------------------------------------------------------------
# API key rotation
# ---------------------------------------------------------------------------

def get_api_key(cfg_key: str):
    api_keys = config.app.get(cfg_key)
    if not api_keys:
        # NOTE: never include config contents in this message — it holds live
        # API keys, and this exception can end up in worker logs.
        raise ValueError(
            f"{cfg_key} is not set — please set it in {config.config_file}"
        )

    # if only one key is provided, return it
    if isinstance(api_keys, str):
        return api_keys

    global _api_key_counter
    with _api_key_lock:
        _api_key_counter += 1
        return api_keys[_api_key_counter % len(api_keys)]


# ---------------------------------------------------------------------------
# Shared HTTP helpers
# ---------------------------------------------------------------------------

def _api_get_json(url: str, headers: dict = None, timeout: tuple = _HTTP_TIMEOUT_API) -> dict:
    """GET `url` and return the parsed JSON body, raising on HTTP errors.

    Shared by the provider search functions below to avoid repeating the
    proxies/verify/timeout boilerplate. Retries up to 3 times on HTTP 429
    with exponential backoff before giving up.
    """
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(3):
        r = requests.get(
            url, headers=headers, proxies=config.proxy,
            verify=_get_tls_verify(), timeout=timeout,
        )
        if r.status_code == 429 and attempt < 2:
            wait = 2 ** attempt  # 1 s, 2 s base
            jitter = wait * random.uniform(0.0, 0.5)  # ±0–50% so workers don't retry in sync
            logger.warning(f"429 rate limit from API (attempt {attempt + 1}/3) — retrying in {wait + jitter:.1f}s")
            time.sleep(wait + jitter)
            continue
        r.raise_for_status()
        return r.json()
    raise last_exc


def _cached_api_get_json(
    provider: str, url: str, headers: dict = None, timeout: tuple = _HTTP_TIMEOUT_API
) -> dict:
    """_api_get_json(), wrapped with the provider search-response cache
    (see _search_cache.py). `provider` scopes the cache key so different
    providers hitting structurally similar URLs never collide."""
    return cached_get_json(
        provider, url,
        lambda: _api_get_json(url, headers=headers, timeout=timeout),
        header_names=tuple((headers or {}).keys()),
    )


def _cached_api_post_json(
    provider: str, url: str, json_body: dict, headers: dict = None, timeout: tuple = _HTTP_TIMEOUT_API
) -> dict:
    """_api_post_json(), wrapped with the provider search-response cache —
    POST analog of _cached_api_get_json(), see _search_cache.py."""
    return cached_post_json(
        provider, url, json_body,
        lambda: _api_post_json(url, json_body, headers=headers, timeout=timeout),
        header_names=tuple((headers or {}).keys()),
    )


def _api_post_json(url: str, json_body: dict, headers: dict = None, timeout: tuple = _HTTP_TIMEOUT_API) -> dict:
    """POST `json_body` to `url` and return the parsed JSON body, raising on HTTP errors.

    POST analog of `_api_get_json`, for providers (e.g. Serper) whose search
    endpoint takes the query in a JSON request body rather than the URL.
    """
    r = requests.post(
        url, json=json_body, headers=headers, proxies=config.proxy,
        verify=_get_tls_verify(), timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def _download_bytes(url: str) -> bytes:
    """Download a small resource (e.g. a thumbnail) and return its bytes, or
    b'' on failure."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    try:
        r = requests.get(
            url, headers=headers, proxies=config.proxy,
            verify=_get_tls_verify(), timeout=_HTTP_TIMEOUT_THUMBNAIL,
        )
        r.raise_for_status()
        return r.content
    except Exception as e:
        logger.debug(f"thumbnail download failed: {url} => {e}")
        return b""


# ---------------------------------------------------------------------------
# Download-cache housekeeping
# ---------------------------------------------------------------------------

_CACHE_SUBDIRS = ("cache_videos", "cache_images", "cache_bgm")


def _touch_cache_file(path: str) -> None:
    """Bump a cache file's mtime on reuse so LRU pruning sees it as fresh."""
    try:
        os.utime(path, None)
    except OSError:
        pass


def prune_cache_dirs() -> None:
    """LRU-prune the download caches (cache_videos / cache_images / cache_bgm).

    Two limits, both config-tunable:
      - cache_max_age_days (default 30): delete anything not touched since
        then. save_video/save_image bump mtime on cache hits, so "touched"
        means "used by some job", not just "downloaded".
      - cache_max_total_gb (default 10): after the age pass, delete
        oldest-first until the combined size fits.

    Called once at pipeline start — the single-worker portal setup means no
    other job can be holding these files at that point. Stale .part files
    (interrupted downloads) older than an hour are always removed.
    """
    max_age_days = float(config.app.get("cache_max_age_days", 30))
    max_total_gb = float(config.app.get("cache_max_total_gb", 10))
    now = time.time()

    entries = []  # (mtime, size, path)
    for sub in _CACHE_SUBDIRS:
        d = utils.storage_dir(sub)
        if not os.path.isdir(d):
            continue
        try:
            with os.scandir(d) as it:
                for entry in it:
                    if not entry.is_file():
                        continue
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    if entry.name.endswith(".part") and now - st.st_mtime > 3600:
                        try:
                            os.remove(entry.path)
                        except OSError:
                            pass
                        continue
                    entries.append((st.st_mtime, st.st_size, entry.path))
        except OSError as exc:
            logger.debug(f"cache prune: could not scan {d}: {exc}")

    removed_count = 0
    removed_bytes = 0

    def _remove(size: int, path: str) -> bool:
        nonlocal removed_count, removed_bytes
        try:
            os.remove(path)
            removed_count += 1
            removed_bytes += size
            return True
        except OSError:
            return False

    age_cutoff = now - max_age_days * 86400
    kept = []
    for mtime, size, path in entries:
        if mtime < age_cutoff:
            _remove(size, path)
        else:
            kept.append((mtime, size, path))

    max_total_bytes = int(max_total_gb * 1024**3)
    total = sum(size for _, size, _ in kept)
    if total > max_total_bytes:
        kept.sort()  # oldest mtime first
        for mtime, size, path in kept:
            if total <= max_total_bytes:
                break
            if _remove(size, path):
                total -= size

    if removed_count:
        logger.info(
            f"cache prune: removed {removed_count} file(s), "
            f"{removed_bytes / 1024**3:.2f} GB freed "
            f"(age>{max_age_days:g}d or size cap {max_total_gb:g} GB)"
        )


# ---------------------------------------------------------------------------
# Image validation
# ---------------------------------------------------------------------------

def _is_valid_raster_image(image_path: str) -> bool:
    """Return True if image_path is a raster image PIL can decode.

    Rejects SVGs and other non-raster files that some search providers
    (e.g. DuckDuckGo) occasionally return with a misleading .jpg/.png
    extension -- Image.open() would otherwise crash later in the Ken
    Burns renderer.
    """
    try:
        with Image.open(image_path) as img:
            img.load()  # force full pixel decode; img.verify() only checks headers and misses truncated scan data
        return True
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
        return False
