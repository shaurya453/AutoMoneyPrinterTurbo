"""Image search providers, download, and save utilities."""
import itertools
import os
import re
import threading
import time
from typing import Iterator, List, Optional, Tuple
from urllib.parse import urlencode, urlparse

import requests
from loguru import logger

from app.config import config
from app.services.media._common import (
    _HTTP_TIMEOUT_IMAGE,
    _SLOW_IMAGE_DOMAINS,
    _api_get_json,
    _api_post_json,
    _get_tls_verify,
    _is_valid_raster_image,
    _touch_cache_file,
    block_host,
    get_api_key,
    is_failed_url,
    is_host_blocked,
    mark_failed_url,
    provider_on_cooldown,
    register_host_failure,
    register_host_success,
    set_provider_cooldown,
)
from app.services.scoring import nsfw, relevance, vlm
from app.utils import utils

# ---------------------------------------------------------------------------
# Watermark detection
# ---------------------------------------------------------------------------

# Stock-photo libraries whose public preview images are almost always
# heavily watermarked. DDG draws from the open web and can surface these.
_WATERMARKED_IMAGE_DOMAINS = {
    # Major stock photo agencies
    "shutterstock.com", "istockphoto.com", "gettyimages.com", "alamy.com",
    "depositphotos.com", "123rf.com", "dreamstime.com", "stock.adobe.com",
    "bigstockphoto.com", "canstockphoto.com", "vectorstock.com",
    "fotolia.com", "stocksy.com",
    # Getty family brands
    "wireimage.com", "filmmagic.com", "hultonarchive.com",
    # Other stock agencies
    "pond5.com", "dissolve.com", "offset.com", "picfair.com",
    "agefotostock.com", "superstock.com", "masterfile.com",
    "bridgemanimages.com", "imagebroker.com", "robertharding.com",
    "mauritiusimages.com", "eyeem.com", "pixta.net", "yayimages.com",
    "vecteezy.com", "freepik.com",
    # Chinese stock-preview sites — every public image carries a heavy
    # watermark (and most direct downloads 403 anyway).
    "pngtree.com", "lovepik.com", "pikbest.com", "699pic.com",
}

# URL path fragments that indicate a watermarked comp/preview image.
# Getty and Alamy use /comp/, many agencies use /preview/ or /wm/.
_WATERMARKED_PATH_FRAGMENTS = {"/comp/", "/preview/", "/watermark/", "/wm/"}


def _is_watermarked_source(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().split(":")[0]
        path = parsed.path.lower()
    except Exception:
        return False
    if any(host == d or host.endswith("." + d) for d in _WATERMARKED_IMAGE_DOMAINS):
        return True
    return any(frag in path for frag in _WATERMARKED_PATH_FRAGMENTS)


def _is_watermarked_domain(domain: str) -> bool:
    """Check a bare domain string (e.g. Serper's 'domain' field or DDG's 'source')."""
    if not domain:
        return False
    host = domain.lower().split(":")[0].strip("/")
    return any(host == d or host.endswith("." + d) for d in _WATERMARKED_IMAGE_DOMAINS)


# ---------------------------------------------------------------------------
# NSFW detection for image search results
# ---------------------------------------------------------------------------

# Known adult-content domains and explicit keywords. DuckDuckGo's image
# search ("auto" backend) frequently falls back to a Bing-scraping engine
# whose safesearch parameter is silently ignored by the underlying library,
# so this keyword/domain check is the real safety net against explicit
# results (e.g. nudity) ending up in a generated video.
_NSFW_IMAGE_DOMAINS = {
    "pornhub.com", "xvideos.com", "xnxx.com", "xhamster.com", "redtube.com",
    "youporn.com", "tube8.com", "spankbang.com", "onlyfans.com", "chaturbate.com",
    "xxx.com", "porn.com", "rule34.xxx", "e-hentai.org", "nhentai.net",
    "motherless.com", "thothub.tv", "fapello.com", "erome.com",
}

_NSFW_KEYWORDS = {
    "porn", "pornstar", "xxx", "nsfw", "nude", "naked", "nudity", "topless",
    "sex", "sexy", "fetish", "hentai", "erotic", "erotica", "xvideos", "xnxx",
    "onlyfans", "escort", "boobs", "tits", "vagina", "pussy", "penis", "cock",
    "dick", "anal", "blowjob", "masturbat", "orgasm", "creampie", "cumshot",
    "stripper", "camgirl", "milf", "bdsm", "bondage",
}


def _is_nsfw_result(result: dict) -> bool:
    """Return True if a DDG image result looks like adult/explicit content.

    Checks the result's source domain and its title/url text against a
    blocklist of adult-content domains and explicit keywords.
    """
    for field in ("image", "url", "thumbnail"):
        link = result.get(field) or ""
        try:
            host = urlparse(link).netloc.lower().split(":")[0]
        except Exception:
            host = ""
        if host and any(host == d or host.endswith("." + d) for d in _NSFW_IMAGE_DOMAINS):
            return True

    text = " ".join(
        str(result.get(field) or "") for field in ("title", "url", "image", "source")
    ).lower()
    return any(re.search(rf"\b{kw}\w*", text) for kw in _NSFW_KEYWORDS)


# Rehost domains that never serve a usable direct image from web-image search:
# social crawler proxies and video-thumbnail CDNs. Hard-skipped (unlike the
# penalized set below). Config-extendable via image_blocked_domains_extra.
_BLOCKED_IMAGE_DOMAINS = {
    "lookaside.instagram.com", "lookaside.fbsbx.com", "instagram.com",
    "facebook.com", "fbcdn.net", "tiktok.com", "tiktokcdn.com",
    "ytimg.com", "i.ytimg.com",
}

# Retail/rehost domains: for named products these are often the ONLY real
# photos, but collages, badge overlays, and catalog junk are common — so they
# rank behind curated sources instead of being skipped. Config-extendable via
# image_penalized_domains_extra.
_PENALIZED_IMAGE_DOMAINS = {
    "amazon.com", "media-amazon.com", "pinterest.com", "pinimg.com",
    "etsy.com", "etsystatic.com", "ebay.com", "ebayimg.com",
    "aliexpress.com", "alicdn.com", "walmart.com", "walmartimages.com",
}

# Result-title keywords that mark non-photographic junk. "logo" is exempted
# when the query itself asks for a logo (e.g. a "Dooney Bourke logo" concept)
# — the query carries the sentence's must_show intent down to this layer.
# Config-extendable via image_junk_keywords_extra.
_JUNK_TITLE_KEYWORDS = {
    "cartoon", "clipart", "clip art", "vector", "coloring page",
    "meme", "svg", "icon set", "logo",
}


def _domain_in(url_or_domain: str, domains: set) -> bool:
    """True when the host of a URL (or a bare domain string) matches any
    entry in `domains` exactly or as a subdomain."""
    raw = (url_or_domain or "").strip().lower()
    if not raw:
        return False
    host = raw
    if "//" in raw or "/" in raw:
        try:
            host = urlparse(raw if "//" in raw else "//" + raw).netloc.lower()
        except Exception:
            host = raw
    host = host.split(":")[0]
    return any(host == d or host.endswith("." + d) for d in domains)


def _config_set_extra(key: str) -> set:
    return {str(v).strip().lower() for v in (config.app.get(key) or []) if str(v).strip()}


def _is_penalized_domain(url_or_domain: str) -> bool:
    return _domain_in(
        url_or_domain,
        _PENALIZED_IMAGE_DOMAINS | _config_set_extra("image_penalized_domains_extra"),
    )


def _junk_screen(result: dict, query: str) -> str:
    """Screen one web-image result dict ({image, url, title, source} keys).

    Returns "" (usable), "blocked" (rehost domain that never serves a direct
    image), or "junk_title" (non-photographic content by title keyword).
    """
    blocked = _BLOCKED_IMAGE_DOMAINS | _config_set_extra("image_blocked_domains_extra")
    for field in ("image", "url", "source"):
        if _domain_in(result.get(field) or "", blocked):
            return "blocked"

    title = str(result.get("title") or "").lower()
    if title:
        keywords = _JUNK_TITLE_KEYWORDS | _config_set_extra("image_junk_keywords_extra")
        query_l = (query or "").lower()
        for kw in keywords:
            if kw in query_l:
                continue  # the query explicitly asks for this (e.g. a logo shot)
            if kw in title:
                return "junk_title"
    return ""


def _rank_results(results: list, rank_tokens: List[str], avoid_tokens: List[str], n: int) -> list:
    """Order web-image result dicts by cheap lexical signals, best first.

    +2 per rank token found in the title, -3 per avoid token, -2 for a
    penalized (retail rehost) domain. Results whose TITLE contains an avoid
    token are dropped outright — the sentence explicitly said not to show
    that. Stable sort, truncated to n. Mirrors videos.sort_by_metadata.
    """
    rank_tokens = [t.lower() for t in (rank_tokens or []) if len(t) > 2]
    avoid_tokens = [t.lower() for t in (avoid_tokens or []) if len(t) > 2]

    def _score(r: dict) -> Optional[int]:
        title = str(r.get("title") or "").lower()
        domain = str(r.get("source") or "").lower()
        haystack = f"{title} {domain}"
        if title and any(t in title for t in avoid_tokens):
            return None  # hard skip
        score = sum(2 for t in rank_tokens if t in haystack)
        score -= sum(3 for t in avoid_tokens if t in haystack)
        if _is_penalized_domain(r.get("image") or r.get("source") or ""):
            score -= 2
        return score

    scored = [(r, _score(r)) for r in results]
    kept = [(r, sc) for r, sc in scored if sc is not None]
    kept.sort(key=lambda p: -p[1])  # sorted() is stable: ties keep search order
    return [r for r, _ in kept[:n]]


# ---------------------------------------------------------------------------
# Wikimedia helper
# ---------------------------------------------------------------------------

# Wikimedia Commons indexes scanned documents (PDF/DJVU page renders), vector
# diagrams (SVG), and audio/video alongside photos. Their thumbnails are
# returned by the same API but are almost never useful documentary b-roll,
# and PDF/DJVU page-render thumbnails are aggressively rate-limited (frequent
# 429 Too Many Requests). Skip files whose original extension isn't a raster
# photo format.
_WIKIMEDIA_SKIP_EXTENSIONS = {
    ".pdf", ".djvu", ".svg", ".tif", ".tiff", ".ogv", ".ogg", ".webm", ".mp4", ".gif",
}

# ---------------------------------------------------------------------------
# Image search providers
# ---------------------------------------------------------------------------

def search_images_pexels(
    search_term: str, n: int = 5,
    rank_tokens: List[str] = None, avoid_tokens: List[str] = None,
) -> List[str]:
    """Return up to n image URLs from Pexels Photos API."""
    try:
        api_key = get_api_key("pexels_api_keys")
    except ValueError:
        logger.warning("pexels_api_keys not configured, skipping Pexels image search")
        return []
    headers = {
        "Authorization": api_key,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }
    # Over-fetch when ranking so the lexical sort has something to choose from.
    per_page = n * 2 if (rank_tokens or avoid_tokens) else n
    params = {"query": search_term, "per_page": per_page, "orientation": "landscape"}
    url = f"https://api.pexels.com/v1/search?{urlencode(params)}"
    try:
        photos = _api_get_json(url, headers=headers).get("photos", [])
        results = [
            {"image": p["src"].get("large2x") or p["src"]["original"],
             "title": p.get("alt") or "", "url": p.get("url") or "", "source": ""}
            for p in photos
            if p.get("src")
        ]
        return [r["image"] for r in _rank_results(results, rank_tokens or [], avoid_tokens or [], n)]
    except Exception as e:
        logger.error(f"Pexels image search failed: {e}")
        return []


def search_images_pixabay(
    search_term: str, n: int = 5,
    rank_tokens: List[str] = None, avoid_tokens: List[str] = None,
) -> List[str]:
    """Return up to n image URLs from Pixabay Images API."""
    try:
        api_key = get_api_key("pixabay_api_keys")
    except ValueError:
        logger.warning("pixabay_api_keys not configured, skipping Pixabay image search")
        return []
    params = {
        "q": search_term,
        "image_type": "photo",
        "per_page": n * 2 if (rank_tokens or avoid_tokens) else n,
        "safesearch": "true",
        "key": api_key,
    }
    url = f"https://pixabay.com/api/?{urlencode(params)}"
    try:
        hits = _api_get_json(url).get("hits", [])
        results = [
            {"image": h.get("largeImageURL") or h.get("webformatURL"),
             "title": h.get("tags") or "", "url": h.get("pageURL") or "", "source": ""}
            for h in hits
            if h.get("largeImageURL") or h.get("webformatURL")
        ]
        return [r["image"] for r in _rank_results(results, rank_tokens or [], avoid_tokens or [], n)]
    except Exception as e:
        logger.error(f"Pixabay image search failed: {e}")
        return []


def search_images_unsplash(
    search_term: str, n: int = 5,
    rank_tokens: List[str] = None, avoid_tokens: List[str] = None,
) -> List[str]:
    """Return up to n image URLs from Unsplash API."""
    if provider_on_cooldown("unsplash"):
        return []
    try:
        api_key = get_api_key("unsplash_api_keys")
    except ValueError:
        logger.warning("unsplash_api_keys not configured, skipping Unsplash image search")
        return []
    headers = {"Authorization": f"Client-ID {api_key}"}
    params = {"query": search_term, "per_page": n, "orientation": "landscape"}
    url = f"https://api.unsplash.com/search/photos?{urlencode(params)}"
    try:
        results = _api_get_json(url, headers=headers).get("results", [])
        return [
            p["urls"].get("full") or p["urls"].get("regular")
            for p in results
            if p.get("urls")
        ]
    except Exception as e:
        if isinstance(e, requests.HTTPError) and e.response is not None \
                and e.response.status_code == 403:
            # Unsplash signals quota exhaustion with 403 (demo tier: 50
            # requests/hour). Hammering it just burns worker time.
            set_provider_cooldown("unsplash", 15 * 60)
            logger.warning(
                "Unsplash returned 403 (hourly request quota exhausted) — "
                "pausing Unsplash searches for 15 min"
            )
        else:
            logger.error(f"Unsplash image search failed: {e}")
        return []


def search_images_wikimedia(
    search_term: str, n: int = 5,
    rank_tokens: List[str] = None, avoid_tokens: List[str] = None,  # metadata too thin to rank
) -> List[str]:
    """Return up to n image URLs from Wikimedia Commons (no API key required)."""
    if provider_on_cooldown("wikimedia"):
        return []
    params = {
        "action": "query",
        "generator": "search",
        "gsrnamespace": "6",    # File: namespace
        "gsrsearch": search_term,
        "gsrlimit": str(n),
        "prop": "imageinfo",
        "iiprop": "url",
        "iiurlwidth": "1920",
        "format": "json",
    }
    url = f"https://commons.wikimedia.org/w/api.php?{urlencode(params)}"
    headers = {"User-Agent": "MoneyPrinterTurbo/1.0 (documentary-pipeline)"}
    try:
        pages = _api_get_json(url, headers=headers).get("query", {}).get("pages", {})
        urls = []
        for page in pages.values():
            title_ext = os.path.splitext(page.get("title", ""))[-1].lower()
            if title_ext in _WIKIMEDIA_SKIP_EXTENSIONS:
                continue
            info = page.get("imageinfo", [])
            if info:
                thumb = info[0].get("thumburl") or info[0].get("url")
                if thumb:
                    urls.append(thumb)
        return urls
    except Exception as e:
        if isinstance(e, requests.HTTPError) and e.response is not None \
                and e.response.status_code == 429:
            # Commons rate-limits aggressively under parallel fetch — the
            # in-request retries already slept through ~5s of backoff, so
            # rest the whole provider instead of re-hitting it per term.
            set_provider_cooldown("wikimedia", 5 * 60)
            logger.warning(
                "Wikimedia Commons keeps rate-limiting (429) — pausing "
                "Wikimedia searches for 5 min"
            )
        else:
            logger.error(f"Wikimedia image search failed: {e}")
        return []


def search_images_ddg(
    search_term: str, n: int = 5,
    rank_tokens: List[str] = None, avoid_tokens: List[str] = None,
) -> List[str]:
    """Return up to n image URLs from DuckDuckGo image search (free, no API key).

    Filters out images that are too small or have an extreme aspect ratio --
    those crop poorly into the target video frame's Ken Burns window. Also
    filters out known watermarked stock-photo domains.
    """
    if provider_on_cooldown("duckduckgo"):
        return []
    try:
        from ddgs import DDGS
    except ImportError:
        logger.warning("ddgs package not installed, skipping DuckDuckGo image search")
        return []

    proxy = (config.proxy or {}).get("https") or (config.proxy or {}).get("http")
    try:
        # Over-fetch since some results get filtered out below.
        # safesearch="on" is honored by the "duckduckgo" backend (forces
        # DuckDuckGo's strict p=1 filter), but the "bing" backend that ddgs
        # falls back to ignores it entirely -- so this alone is not
        # sufficient. The _is_nsfw_result() check below is the real
        # defense-in-depth filter for results that slip through.
        # timeout=10: when DDG rate-limits it stalls rather than erroring,
        # so a long timeout just holds a fetch worker hostage.
        results = DDGS(proxy=proxy, timeout=10).images(
            query=search_term,
            max_results=n * 3,
            safesearch="on",
        )
    except Exception as e:
        # DDG failures are almost always rate-limit stalls; one predicts
        # more, so rest the provider briefly instead of timing out on every
        # term. The other providers keep the round-robin supplied meanwhile.
        set_provider_cooldown("duckduckgo", 60)
        logger.warning(f"DuckDuckGo image search failed ({e}) — pausing DDG searches for 60s")
        return []

    kept = []
    for r in results:
        url = r.get("image")
        if not url:
            continue
        if _is_watermarked_source(url) or _is_watermarked_domain(r.get("source", "")):
            continue
        if _is_nsfw_result(r):
            logger.info(f"skipping likely NSFW image result: {url}")
            continue
        _screen = _junk_screen(r, search_term)
        if _screen:
            logger.debug(f"skipping {_screen} image result: {url}")
            continue
        try:
            width, height = int(r.get("width") or 0), int(r.get("height") or 0)
        except (TypeError, ValueError):
            width, height = 0, 0
        if width and height:
            if min(width, height) < 480:
                continue
            if max(width, height) / min(width, height) > 3:
                continue
        kept.append(r)
    ranked = _rank_results(kept, rank_tokens or [], avoid_tokens or [], n)
    return [r["image"] for r in ranked]


# Openverse OAuth2 token cache (client-credentials flow). Anonymous access is
# Cloudflare-challenged from datacenter IPs, so registered credentials
# (openverse_client_id/_secret in config.toml) are effectively required on
# servers; without them the provider cools down on the first 403 and stays
# quiet.
_openverse_token_lock = threading.Lock()
_openverse_token: dict = {"token": "", "expires_at": 0.0}


def _openverse_auth_headers() -> dict:
    """Return {'Authorization': 'Bearer ...'} when credentials are configured
    and a token could be obtained; {} otherwise (anonymous attempt)."""
    client_id = str(config.app.get("openverse_client_id", "") or "").strip()
    client_secret = str(config.app.get("openverse_client_secret", "") or "").strip()
    if not client_id or not client_secret:
        return {}
    with _openverse_token_lock:
        if _openverse_token["token"] and time.monotonic() < _openverse_token["expires_at"] - 60:
            return {"Authorization": f"Bearer {_openverse_token['token']}"}
        try:
            r = requests.post(
                "https://api.openverse.org/v1/auth_tokens/token/",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "client_credentials",
                },
                proxies=config.proxy, verify=_get_tls_verify(), timeout=(15, 30),
            )
            r.raise_for_status()
            payload = r.json()
            _openverse_token["token"] = payload.get("access_token", "")
            _openverse_token["expires_at"] = time.monotonic() + float(payload.get("expires_in", 600))
        except Exception as e:
            logger.warning(f"Openverse token fetch failed: {e}")
            return {}
    if _openverse_token["token"]:
        return {"Authorization": f"Bearer {_openverse_token['token']}"}
    return {}


def search_images_openverse(
    search_term: str, n: int = 5,
    rank_tokens: List[str] = None, avoid_tokens: List[str] = None,  # metadata too thin to rank
) -> List[str]:
    """Return up to n image URLs from Openverse (openly licensed).

    Openverse aggregates CC-licensed images (Flickr Commons, Wikimedia,
    museums, science repositories) — the best free source for diagrams,
    schematics, and evidence-style imagery that stock libraries don't carry
    (e.g. a chemical structure for a referent-swap sentence).
    """
    if provider_on_cooldown("openverse"):
        return []
    params = {
        "q": search_term,
        "page_size": min(n * 2, 20),
        "mature": "false",
    }
    url = f"https://api.openverse.org/v1/images/?{urlencode(params)}"
    headers = {"User-Agent": "MoneyPrinterTurbo/1.0 (documentary-pipeline)"}
    headers.update(_openverse_auth_headers())
    try:
        results = _api_get_json(url, headers=headers).get("results", [])
    except Exception as e:
        if isinstance(e, requests.HTTPError) and e.response is not None \
                and e.response.status_code in (401, 403):
            # Cloudflare challenge / missing credentials — retrying every term
            # is pointless. Register free API credentials at
            # https://api.openverse.org/v1/#tag/auth and set
            # openverse_client_id/_secret in config.toml to enable.
            set_provider_cooldown("openverse", 15 * 60)
            logger.warning(
                "Openverse rejected the request (Cloudflare/auth) — pausing for "
                "15 min; set openverse_client_id/_secret in config.toml to enable this provider"
            )
        elif isinstance(e, requests.HTTPError) and e.response is not None \
                and e.response.status_code == 429:
            set_provider_cooldown("openverse", 5 * 60)
            logger.warning(
                "Openverse keeps rate-limiting (429) — pausing Openverse "
                "searches for 5 min"
            )
        else:
            logger.error(f"Openverse image search failed: {e}")
        return []

    urls = []
    for r in results:
        u = r.get("url")
        if not u:
            continue
        if _is_watermarked_source(u) or _is_watermarked_domain(r.get("source", "")):
            continue
        try:
            width, height = int(r.get("width") or 0), int(r.get("height") or 0)
        except (TypeError, ValueError):
            width, height = 0, 0
        if width and height:
            if min(width, height) < 480:
                continue
            if max(width, height) / min(width, height) > 3:
                continue
        urls.append(u)
        if len(urls) >= n:
            break
    return urls


def search_images_serper(
    search_term: str, n: int = 5,
    rank_tokens: List[str] = None, avoid_tokens: List[str] = None,
) -> List[str]:
    """Return up to n image URLs from Google Images via the Serper API.

    Used for `content_track="named"` sentences (specific products, people,
    places, events) that generic stock-photo libraries are unlikely to
    carry. Applies the same watermark/NSFW/dimension filters as
    `search_images_ddg` so Serper results pass through the same safety gate
    as every other provider.
    """
    try:
        api_key = get_api_key("serper_api_keys")
    except ValueError:
        logger.warning("serper_api_keys not configured, skipping Serper image search")
        return []

    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    body = {"q": search_term, "num": n * 3}
    try:
        data = _api_post_json("https://google.serper.dev/images", body, headers=headers)
    except Exception as e:
        logger.error(f"Serper image search failed: {e}")
        return []

    kept = []
    for item in data.get("images", []):
        url = item.get("imageUrl")
        if not url:
            continue
        result = {
            "image": url,
            "url": item.get("link", ""),
            "thumbnail": item.get("thumbnailUrl", ""),
            "title": item.get("title", ""),
            "source": item.get("domain", ""),
        }
        if _is_watermarked_source(url) or _is_watermarked_domain(item.get("domain", "")):
            continue
        if _is_nsfw_result(result):
            logger.info(f"skipping likely NSFW image result: {url}")
            continue
        _screen = _junk_screen(result, search_term)
        if _screen:
            logger.debug(f"skipping {_screen} image result: {url}")
            continue
        try:
            width, height = int(item.get("imageWidth") or 0), int(item.get("imageHeight") or 0)
        except (TypeError, ValueError):
            width, height = 0, 0
        if width and height:
            if min(width, height) < 480:
                continue
            if max(width, height) / min(width, height) > 3:
                continue
        kept.append(result)
    ranked = _rank_results(kept, rank_tokens or [], avoid_tokens or [], n)
    return [r["image"] for r in ranked]


# ---------------------------------------------------------------------------
# Image save / download
# ---------------------------------------------------------------------------

def save_image(image_url: str, save_dir: str = "") -> str:
    """Download an image URL and return its local path. Returns '' on failure."""
    if not save_dir:
        save_dir = utils.storage_dir("cache_images")
    os.makedirs(save_dir, exist_ok=True)

    url_clean = image_url.split("?")[0]
    url_hash = utils.md5(url_clean)
    ext = os.path.splitext(url_clean)[-1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    image_path = os.path.join(save_dir, f"img-{url_hash}{ext}")

    if os.path.exists(image_path) and os.path.getsize(image_path) > 0:
        if _is_valid_raster_image(image_path):
            logger.info(f"image already cached: {image_path}")
            _touch_cache_file(image_path)
            return image_path
        logger.warning(f"cached image is not a valid raster image, re-downloading: {image_path}")
        os.remove(image_path)

    # Fast-skip known-dead URLs and known-slow / currently blocked hosts
    # without connecting.
    if is_failed_url(image_url):
        logger.debug(f"skipping previously failed image URL: {image_url}")
        return ""
    try:
        _host = urlparse(image_url).netloc.lower().split(":")[0]
    except Exception:
        _host = ""
    if _host:
        if any(_host == d or _host.endswith("." + d) for d in _SLOW_IMAGE_DOMAINS):
            logger.debug(f"skipping slow-domain image: {image_url}")
            return ""
        if is_host_blocked(_host):
            logger.debug(f"skipping blocked host {_host}")
            return ""

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    try:
        r = requests.get(
            image_url, headers=headers, proxies=config.proxy,
            verify=_get_tls_verify(), timeout=_HTTP_TIMEOUT_IMAGE,
        )
        if r.status_code == 429:
            block_host(_host)
            logger.warning(f"429 from {_host} — blocked for 5 min: {image_url}")
            return ""
        r.raise_for_status()
        with open(image_path, "wb") as fh:
            fh.write(r.content)
        if os.path.exists(image_path) and os.path.getsize(image_path) > 0:
            if _is_valid_raster_image(image_path):
                register_host_success(_host)
                return image_path
            logger.warning(f"downloaded file is not a valid raster image, discarding: {image_url}")
            os.remove(image_path)
            mark_failed_url(image_url)
            if register_host_failure(_host):
                logger.warning(f"{_host} keeps serving non-image responses — blocked for 5 min")
    except Exception as e:
        logger.error(f"image download failed: {image_url} => {e}")
        mark_failed_url(image_url)
        if register_host_failure(_host):
            logger.warning(f"repeated download failures from {_host} — blocked for 5 min")
    return ""


# ---------------------------------------------------------------------------
# Multi-provider image download with relevance / dedup
# ---------------------------------------------------------------------------

_IMAGE_PROVIDERS = {
    "pexels": search_images_pexels,
    "pixabay": search_images_pixabay,
    "unsplash": search_images_unsplash,
    "wikimedia": search_images_wikimedia,
    "openverse": search_images_openverse,
    "duckduckgo": search_images_ddg,
    "serper": search_images_serper,
}

# "openverse" deliberately excluded: Cloudflare blocks this server's IP on
# every api.openverse.org endpoint even with valid registered credentials
# (confirmed 2026-07-13) -- see the openverse_client_id/_secret comment in
# config.toml. The provider function/mapping below is kept dormant in case a
# [proxy] is ever added to route around the block.
_DEFAULT_IMAGE_SOURCE_ORDER = ["duckduckgo", "pexels", "pixabay", "unsplash", "wikimedia"]

# Provider pools for per-term routing (see download_image's term_routing).
# Wikimedia/Openverse sit in both: they index real-world entities like a web
# search AND are curated/licensed like stock.
_WEB_IMAGE_PROVIDERS = {"serper", "duckduckgo", "wikimedia", "openverse"}
_STOCK_IMAGE_PROVIDERS = {"pexels", "pixabay", "unsplash", "wikimedia", "openverse"}

_CANDIDATES_PER_TERM = 6


def download_image(
    search_terms: List[str],
    source_order: List[str] = None,
    save_dir: str = "",
    used_urls: set = None,
    caption_prompt: str = "",
    recent_embeddings=None,
    dedup_threshold: float = 0.92,
    narration: str = "",
    visual_caption: str = "",
    video_topic: str = "",
    must_show: List[str] = None,
    avoid: List[str] = None,
    serper_term: str = "",
    deadline: Optional[float] = None,
    content_track: str = "broll",
    criticality: str = "medium",
    report: Optional[dict] = None,
    term_routing: Optional[dict] = None,
) -> str:
    """
    Search for a still image using multiple providers in priority order and
    download the first result found.  Returns a local file path or '' if all
    providers and terms are exhausted.

    source_order: provider names to try in order.  Defaults to
        _DEFAULT_IMAGE_SOURCE_ORDER (duckduckgo, pexels, pixabay, unsplash,
        wikimedia).  ("openverse" exists as a provider but is dormant --
        Cloudflare-blocked from this server; see _DEFAULT_IMAGE_SOURCE_ORDER.)

    content_track: "named" sentences (a specific product/person/place) get a
        larger per-term candidate slice (image_candidates_per_term_named,
        default 10) than generic "broll" (image_candidates_per_term, default
        6) when relevance filtering is active -- named entities benefit more
        from trying harder to find the one real match, while broll's search
        pool is broad enough that extra candidates rarely change the outcome.

    criticality: the sentence's visual_criticality level ("low" | "medium" |
        "high" | "critical", default "medium"). Adjusts the candidate slice
        (low -2, high +4, critical +8) and enables VLM comparative pooling
        for high/critical sentences regardless of content_track.

    term_routing: optional {term: "web" | "stock"} map (see
        _planning.build_query_plan). A "web" term only queries web-image
        providers (Serper/DDG/Wikimedia/Openverse) — stock libraries never
        carry branded products; a "stock" term only queries curated stock.
        Terms absent from the map use the full source_order. If the
        restriction intersects to nothing (e.g. Serper unconfigured), the
        full source_order is used — degrade, don't starve.

    used_urls: if provided, mutated in-place with the chosen image's source
        URL so subsequent calls across the run won't reuse the same image.
        Anything already in used_urls is skipped entirely -- if every
        candidate across all terms and providers has already been used, this
        returns '' rather than reusing one (the caller falls back to a
        different media type).

    Every downloaded candidate (regardless of relevance settings) is passed
    through the NSFW pixel gate (app.services.nsfw); a hard-rejected
    candidate is deleted and never claimed/used or considered for the
    relevance fallback.

    caption_prompt: if set (and the CLIP relevance model is available, and
        RELEVANCE_LOG_ONLY is not set), candidates are downloaded and scored
        against this prompt; the first one per term that beats the junk
        anchors by `relevance_margin` is used. If nothing clears the margin,
        the best-scoring candidate MAY be used as a last resort -- but only
        for broll sentences at low/medium criticality, only at or above
        `image_fallback_min_score`, and only if it passes a VLM verify.
        Named-track and high/critical sentences never below-margin-accept:
        they return '' so the caller's rescue ladder fills the slot with
        something generic-but-on-topic instead of a known-weak match.
    """
    if source_order is None:
        source_order = _DEFAULT_IMAGE_SOURCE_ORDER

    use_relevance = (
        bool(caption_prompt)
        and relevance.is_available()
        and not relevance.is_log_only()
    )
    margin = float(config.app.get("relevance_margin", 0.02))

    # Cheap lexical signals for the providers' metadata pre-sort (see
    # _rank_results): the sentence's must_show terms + visual_caption words
    # rank candidates, avoid terms demote/drop them — before any download.
    _stop = {"a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or", "with"}
    _rank_tokens = [
        w.lower()
        for w in re.findall(r"\w+", " ".join((must_show or []) + [visual_caption or ""]))
        if len(w) > 2 and w.lower() not in _stop
    ]
    _avoid_tokens = [
        w.lower()
        for w in re.findall(r"\w+", " ".join(avoid or []))
        if len(w) > 2 and w.lower() not in _stop
    ]

    def _gather_urls(term: str) -> Iterator[Tuple[str, str]]:
        """Yield (provider, url) candidates, round-robin across providers.

        Round-robin so a relevance-limited slice (_CANDIDATES_PER_TERM) still
        draws from multiple sources instead of being dominated by whichever
        provider is first in source_order (e.g. DuckDuckGo, which usually
        returns the most results but is also the least curated source).

        Lazy: each provider's search API is only queried when the rotation
        first reaches it. If the consumer accepts an early candidate (the
        common case), the remaining providers are never queried at all —
        the old eager version burned quota on every provider per term even
        when candidate #1 was accepted.
        """
        fetched: dict = {}  # provider -> filtered url list

        def _fetch(provider: str) -> List[str]:
            fn = _IMAGE_PROVIDERS[provider]
            # DDG draws from the broad open web, so individual hosts are more
            # likely to block hotlinking (e.g. Akamai-protected CDNs) -- request
            # more candidates so a working one is likely among them.
            n = 10 if provider == "duckduckgo" else 6
            # Serper (Google Images) works best with short, canonical entity
            # names — verbose concept strings like "JBL L100 Century alnico
            # drivers front view" return nothing. Use the short override when set.
            query = serper_term if (provider == "serper" and serper_term) else term
            return [
                url for url in fn(query, n=n, rank_tokens=_rank_tokens, avoid_tokens=_avoid_tokens)
                if url
                and (used_urls is None or url not in used_urls)
                and not is_failed_url(url)  # dead URLs must not occupy candidate slots
            ]

        providers = []
        for provider in source_order:
            if provider in _IMAGE_PROVIDERS:
                providers.append(provider)
            else:
                logger.warning(f"unknown image provider: {provider}")

        _pool = (term_routing or {}).get(term, "")
        if _pool in ("web", "stock"):
            _allowed = _WEB_IMAGE_PROVIDERS if _pool == "web" else _STOCK_IMAGE_PROVIDERS
            _restricted = [p for p in providers if p in _allowed]
            if _restricted:
                providers = _restricted
            # else: restriction intersects to nothing (e.g. Serper not
            # configured) — keep the full order rather than starve the term.

        i = 0
        while True:
            yielded = False
            for provider in providers:
                if provider not in fetched:
                    fetched[provider] = _fetch(provider)
                urls = fetched[provider]
                if i < len(urls):
                    yield provider, urls[i]
                    yielded = True
            if not yielded:
                break
            i += 1

    def _claim(url: str, term: str, provider: str, local: str, dedup_emb=None, note: str = "",
               extra: Optional[dict] = None) -> str:
        """The single authoritative commit point for a candidate -- owns both
        URL-uniqueness and visual-uniqueness atomically. Acceptance isn't
        decided until here (relevance-margin scoring and VLM comparative
        pooling both run before this), so this is the correct place for the
        dedup embedding to actually claim its window slot, not the earlier
        soft contains_similar() pre-filter.

        extra: decision metadata (chosen term, source URL, scores) merged into
        the report dict only on a successful claim, so the quality/decisions
        ledger reflects the candidate that actually shipped, not a raced one."""
        if used_urls is not None:
            if hasattr(used_urls, 'try_claim'):
                # Atomically claim both the source URL and the local cache
                # path.  If another thread beat us to this candidate, return
                # '' so the caller can try the next one instead.
                if not used_urls.try_claim(url, local):
                    logger.debug(f"image URL already claimed by concurrent thread: {url}")
                    return ''
            else:
                used_urls.add(url)
                used_urls.add(local)  # prevent same cached file reused via a different URL
        if recent_embeddings is not None and dedup_emb is not None:
            if not recent_embeddings.try_claim(dedup_emb, dedup_threshold):
                logger.info(f"image candidate raced out by a concurrent near-duplicate claim: {url}")
                return ''
        logger.info(f"image obtained via {provider} for '{term}': {local}{note}")
        if report is not None:
            report["provider"] = provider
            report["chosen_term"] = term
            report["url"] = url
            if extra:
                report.update(extra)
        return local

    def _try() -> str:
        fallback_path = fallback_url = fallback_provider = fallback_term = ""
        fallback_score = float("-inf")
        fallback_emb = None
        vlm_img_threshold = float(config.app.get("vlm_image_threshold", 0.30))
        candidates_per_term = int(config.app.get(
            "image_candidates_per_term_named" if content_track == "named"
            else "image_candidates_per_term",
            10 if content_track == "named" else _CANDIDATES_PER_TERM,
        ))
        # visual_criticality effort dial (see AGENT_GUIDE): connective filler
        # doesn't deserve the full candidate budget; a sentence whose exact
        # subject must appear deserves more looks before falling back.
        if criticality == "low":
            candidates_per_term = max(3, candidates_per_term - 2)
        elif criticality == "high":
            candidates_per_term += 4
        elif criticality == "critical":
            candidates_per_term += 8

        # Comparative VLM pooling: instead of claiming the first candidate that
        # individually passes every gate, gather up to vlm_compare_pool_size
        # accepted candidates, then show them to the VLM together in one call
        # and take its pick -- more reliable than independent per-candidate
        # threshold scoring for "which of these is the best match." Scoped by
        # vlm_compare_scope ("off" | "named" | "all", default "named") so the
        # extra VLM cost is spent where a wrong pick is most noticeable.
        compare_scope = str(config.app.get("vlm_compare_scope", "named")).strip().lower()
        compare_mode = vlm.is_enabled() and compare_scope != "off" and (
            compare_scope == "all"
            or content_track == "named"
            # high/critical broll gets the comparative pick too — a wrong
            # choice is as visible there as on the named track.
            or criticality in ("high", "critical")
        )
        pool_size = int(config.app.get("vlm_compare_pool_size", 3)) if compare_mode else 1
        # Each entry: (provider, term, url, local, image_bytes, dedup_emb).
        pool: list = []

        # Rejected candidates are intentionally left on disk, not deleted.
        # The local cache (save_image) is shared and keyed by URL hash
        # across every ThreadPoolExecutor worker in the run, so a sibling
        # thread evaluating a *different* sentence can independently
        # discover and accept the exact same URL (e.g. a named product
        # mentioned in two sentences) while this thread is rejecting it on
        # its own narration/VLM prompt. Deleting here raced that sibling
        # thread's read of the same file, crashing its fetch with a
        # FileNotFoundError. The cache is content-addressed, so leaving
        # rejected candidates in place only costs disk space, not
        # correctness.

        for term in search_terms:
            if deadline is not None and time.monotonic() > deadline:
                logger.warning(f"image search deadline reached — stopping at term '{term}'")
                break
            candidates = _gather_urls(term)  # lazy generator — providers queried on demand
            iter_candidates = (
                candidates
                if not use_relevance
                else itertools.islice(candidates, candidates_per_term)
            )
            prompt = caption_prompt or term

            for provider, url in iter_candidates:
                if deadline is not None and time.monotonic() > deadline:
                    break
                local = save_image(url, save_dir)
                if not local:
                    continue

                # Reject if this physical file was already used (same image
                # reached via a different URL or a repeated cache hit).
                if used_urls is not None and local in used_urls:
                    logger.debug(f"skipping already-used cached image: {local}")
                    continue

                with open(local, "rb") as fh:
                    image_bytes = fh.read()

                _cand_scores: dict = {}  # decision metadata for the ledger, filled as gates run

                if not nsfw.passes(nsfw.is_nsfw_image(image_bytes)):
                    logger.info(f"rejected NSFW image candidate: {url}")
                    # Mark as used so no later sentence re-downloads and
                    # re-scans the same rejected image (same convention as the
                    # VLM rejection below and the video path's try_claim).
                    if used_urls is not None:
                        used_urls.add(url)
                    if report is not None:
                        report.setdefault("rejections", []).append("nsfw")
                    continue

                dedup_emb = None
                if recent_embeddings is not None:
                    dedup_emb = relevance.embed_image(image_bytes)
                    # Soft pre-filter only (read-only peek) -- avoids wasting a
                    # relevance score / VLM call / pool slot on an already-known
                    # duplicate. The authoritative claim happens in _claim(),
                    # since acceptance isn't decided until after relevance-margin
                    # scoring and (in compare_mode) VLM comparative pooling.
                    if dedup_emb is not None and recent_embeddings.contains_similar(
                        dedup_emb, dedup_threshold
                    ):
                        logger.info(f"skipping near-duplicate image candidate: {url}")
                        if report is not None:
                            report.setdefault("rejections", []).append("dedup")
                        continue

                # CLIP relevance margin runs BEFORE the VLM: it's a free local
                # model, the VLM is a paid API call. A candidate that can't
                # clear the margin is either the fallback capture below or
                # nothing — no reason to spend a VLM verdict on it. (The old
                # order VLM-scanned every candidate; on one anchored-wrong job
                # that was 1,381 paid rejections CLIP would have caught.)
                s = None
                if use_relevance:
                    s = relevance.score(prompt, image_bytes)
                    if s is not None:
                        _cand_scores["clip_score"] = round(s, 3)
                    if config.app.get("relevance_debug_log", False):
                        logger.debug(f"relevance[image] prompt={prompt!r} score={s} url={url}")
                    margin_ok = relevance.passes_margin(prompt, image_bytes, margin)
                    if margin_ok is False:
                        if s is not None and s > fallback_score:
                            fallback_score = s
                            fallback_path, fallback_url = local, url
                            fallback_provider, fallback_term = provider, term
                            fallback_emb = dedup_emb
                        continue

                if vlm.is_enabled():
                    vlm_score = vlm.verify_image(
                        image_bytes, narration, visual_caption, video_topic,
                        must_show or [], avoid or [],
                    )
                    if vlm_score is not None:
                        _cand_scores["vlm_score"] = round(vlm_score, 3)
                    if vlm_score is not None and vlm_score < vlm_img_threshold:
                        logger.info(f"VLM rejected image candidate (score={vlm_score:.2f}): {url}")
                        # Mark as used so subsequent clips in this sentence skip re-download.
                        if used_urls is not None:
                            used_urls.add(url)
                        if report is not None:
                            report.setdefault("rejections", []).append("vlm")
                        continue

                if compare_mode:
                    pool.append((provider, term, url, local, image_bytes, dedup_emb, _cand_scores))
                    if len(pool) >= pool_size:
                        break
                    continue
                note = f" (score={s:.3f})" if s is not None else ""
                _r = _claim(url, term, provider, local, dedup_emb=dedup_emb, note=note, extra=_cand_scores)
                if _r:
                    return _r
                continue  # another thread claimed this URL/embedding; try next candidate

            if compare_mode and len(pool) >= pool_size:
                break  # enough pooled candidates -- stop trying further terms too

        if compare_mode and pool:
            if len(pool) == 1:
                provider, term, url, local, image_bytes, dedup_emb, _scores = pool[0]
                _r = _claim(url, term, provider, local, dedup_emb=dedup_emb, extra=_scores)
                if _r:
                    return _r
                # Raced by a concurrent thread -- fall through to the
                # relevance-fallback logic below, same as any other dead end.
            else:
                winner_idx = vlm.compare_candidates(
                    [p[4] for p in pool], narration, visual_caption, video_topic,
                    must_show or [], avoid or [],
                )
                if winner_idx is None or not (0 <= winner_idx < len(pool)):
                    winner_idx = 0  # fail-open: default to the first pooled candidate
                provider, term, url, local, image_bytes, dedup_emb, _scores = pool[winner_idx]
                # Pool losers are intentionally NOT deleted from disk — the
                # cache is content-addressed and shared across worker threads,
                # so a sibling thread may hold the same local path for its own
                # candidate (see the "left on disk, not deleted" note above);
                # deleting here recreates exactly that FileNotFoundError race.
                _r = _claim(url, term, provider, local, dedup_emb=dedup_emb, extra=_scores)
                if _r:
                    if report is not None:
                        report["vlm_compare_used"] = True
                        report["vlm_compare_pool_size"] = len(pool)
                    return _r
                # Raced by a concurrent thread -- fall through, same as above.

        if fallback_path:
            # A below-margin image is a KNOWN-weak match. Under narration that
            # names a specific entity (named track) or demands an exact visual
            # (high/critical), shipping it is worse than an empty slot — the
            # caller's rescue ladder (entity cousins → gapfill → placeholder)
            # produces something generic-but-on-topic instead. This is what
            # put a wolf-embroidered bag under Michael Kors narration.
            def _block(reason: str) -> str:
                logger.warning(
                    f"below-margin fallback blocked ({reason}) for {search_terms} "
                    f"(best score={fallback_score:.3f}) — leaving slot to the rescue ladder"
                )
                if report is not None:
                    report["below_margin_blocked"] = True
                return ""

            if content_track == "named" or criticality in ("high", "critical"):
                return _block(f"track={content_track}, criticality={criticality}")
            floor = float(config.app.get("image_fallback_min_score", 0.20))
            if fallback_score < floor:
                return _block(f"score below image_fallback_min_score={floor}")
            # The margin gate rejected this candidate before the VLM ever saw
            # it (CLIP-first ordering), so give it the one VLM verdict it
            # skipped before letting it on screen.
            if vlm.is_enabled():
                try:
                    with open(fallback_path, "rb") as fh:
                        _fb_bytes = fh.read()
                    _fb_vlm = vlm.verify_image(
                        _fb_bytes, narration, visual_caption, video_topic,
                        must_show or [], avoid or [],
                    )
                    if _fb_vlm is not None and _fb_vlm < vlm_img_threshold:
                        return _block(f"VLM score {_fb_vlm:.2f} < {vlm_img_threshold}")
                except OSError:
                    return _block("fallback file unreadable")
            logger.warning(
                f"no image cleared relevance margin {margin} for {search_terms}; "
                f"using best available for '{fallback_term}' (score={fallback_score:.3f}): {fallback_path}"
            )
            _extra = {"below_margin": True, "below_margin_score": round(fallback_score, 3)}
            _r = _claim(fallback_url, fallback_term, fallback_provider, fallback_path,
                        dedup_emb=fallback_emb, extra=_extra)
            if _r:
                return _r
            # Another thread claimed the fallback while we were scoring; no
            # image found this call — let the higher-level caller fall back.
        return ""

    result = _try()
    if result:
        return result

    logger.warning(f"no unused image found for terms {search_terms} from {source_order}")
    return ""
