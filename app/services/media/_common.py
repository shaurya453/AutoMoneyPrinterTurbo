"""Shared state and low-level utilities for the media package.

This module is the dependency base layer: it must NOT import from
images.py or videos.py.
"""
import random
import threading
import time

import requests
from loguru import logger
from PIL import Image, UnidentifiedImageError

from app.config import config
from app.utils import utils  # noqa: F401 — re-exported for submodule convenience

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
}

# Hosts that returned HTTP 429 during this process run; populated dynamically.
# Maps host → monotonic unblock timestamp. Entries expire after _BLOCKED_HOST_TTL_SECONDS.
_BLOCKED_HOST_TTL_SECONDS = 300  # 5 minutes
_per_run_blocked_hosts: dict = {}
_blocked_hosts_lock = threading.Lock()

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
        raise ValueError(
            f"\n\n##### {cfg_key} is not set #####\n\nPlease set it in the config.toml file: {config.config_file}\n\n"
            f"{utils.to_json(config.app)}"
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
