"""Image search providers, download, and save utilities."""
import os
import re
import time
from typing import List, Optional, Tuple
from urllib.parse import urlencode, urlparse

import requests
from loguru import logger

from app.config import config
from app.services.media._common import (
    _BLOCKED_HOST_TTL_SECONDS,
    _HTTP_TIMEOUT_IMAGE,
    _SLOW_IMAGE_DOMAINS,
    _api_get_json,
    _api_post_json,
    _blocked_hosts_lock,
    _get_tls_verify,
    _is_valid_raster_image,
    _per_run_blocked_hosts,
    get_api_key,
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

def search_images_pexels(search_term: str, n: int = 5) -> List[str]:
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
    params = {"query": search_term, "per_page": n, "orientation": "landscape"}
    url = f"https://api.pexels.com/v1/search?{urlencode(params)}"
    try:
        photos = _api_get_json(url, headers=headers).get("photos", [])
        return [
            p["src"].get("large2x") or p["src"]["original"]
            for p in photos
            if p.get("src")
        ]
    except Exception as e:
        logger.error(f"Pexels image search failed: {e}")
        return []


def search_images_pixabay(search_term: str, n: int = 5) -> List[str]:
    """Return up to n image URLs from Pixabay Images API."""
    try:
        api_key = get_api_key("pixabay_api_keys")
    except ValueError:
        logger.warning("pixabay_api_keys not configured, skipping Pixabay image search")
        return []
    params = {
        "q": search_term,
        "image_type": "photo",
        "per_page": n,
        "safesearch": "true",
        "key": api_key,
    }
    url = f"https://pixabay.com/api/?{urlencode(params)}"
    try:
        hits = _api_get_json(url).get("hits", [])
        return [
            h.get("largeImageURL") or h.get("webformatURL")
            for h in hits
            if h.get("largeImageURL") or h.get("webformatURL")
        ]
    except Exception as e:
        logger.error(f"Pixabay image search failed: {e}")
        return []


def search_images_unsplash(search_term: str, n: int = 5) -> List[str]:
    """Return up to n image URLs from Unsplash API."""
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
        logger.error(f"Unsplash image search failed: {e}")
        return []


def search_images_wikimedia(search_term: str, n: int = 5) -> List[str]:
    """Return up to n image URLs from Wikimedia Commons (no API key required)."""
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
        logger.error(f"Wikimedia image search failed: {e}")
        return []


def search_images_ddg(search_term: str, n: int = 5) -> List[str]:
    """Return up to n image URLs from DuckDuckGo image search (free, no API key).

    Filters out images that are too small or have an extreme aspect ratio --
    those crop poorly into the target video frame's Ken Burns window. Also
    filters out known watermarked stock-photo domains.
    """
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
        results = DDGS(proxy=proxy, timeout=30).images(
            query=search_term,
            max_results=n * 3,
            safesearch="on",
        )
    except Exception as e:
        logger.error(f"DuckDuckGo image search failed: {e}")
        return []

    urls = []
    for r in results:
        url = r.get("image")
        if not url:
            continue
        if _is_watermarked_source(url) or _is_watermarked_domain(r.get("source", "")):
            continue
        if _is_nsfw_result(r):
            logger.info(f"skipping likely NSFW image result: {url}")
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
        urls.append(url)
        if len(urls) >= n:
            break
    return urls


def search_images_serper(search_term: str, n: int = 5) -> List[str]:
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

    urls = []
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
        try:
            width, height = int(item.get("imageWidth") or 0), int(item.get("imageHeight") or 0)
        except (TypeError, ValueError):
            width, height = 0, 0
        if width and height:
            if min(width, height) < 480:
                continue
            if max(width, height) / min(width, height) > 3:
                continue
        urls.append(url)
        if len(urls) >= n:
            break
    return urls


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
            return image_path
        logger.warning(f"cached image is not a valid raster image, re-downloading: {image_path}")
        os.remove(image_path)

    # Fast-skip known-slow / currently 429-blocked hosts without connecting.
    try:
        _host = urlparse(image_url).netloc.lower().split(":")[0]
    except Exception:
        _host = ""
    if _host:
        if any(_host == d or _host.endswith("." + d) for d in _SLOW_IMAGE_DOMAINS):
            logger.debug(f"skipping slow-domain image: {image_url}")
            return ""
        with _blocked_hosts_lock:
            _unblock_at = _per_run_blocked_hosts.get(_host)
            if _unblock_at is not None:
                if time.monotonic() < _unblock_at:
                    logger.debug(f"skipping 429-blocked host {_host}")
                    return ""
                else:
                    del _per_run_blocked_hosts[_host]

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
            with _blocked_hosts_lock:
                _per_run_blocked_hosts[_host] = time.monotonic() + _BLOCKED_HOST_TTL_SECONDS
            logger.warning(f"429 from {_host} — blocked for 5 min: {image_url}")
            return ""
        r.raise_for_status()
        with open(image_path, "wb") as fh:
            fh.write(r.content)
        if os.path.exists(image_path) and os.path.getsize(image_path) > 0:
            if _is_valid_raster_image(image_path):
                return image_path
            logger.warning(f"downloaded file is not a valid raster image, discarding: {image_url}")
            os.remove(image_path)
    except Exception as e:
        logger.error(f"image download failed: {image_url} => {e}")
    return ""


# ---------------------------------------------------------------------------
# Multi-provider image download with relevance / dedup
# ---------------------------------------------------------------------------

_IMAGE_PROVIDERS = {
    "pexels": search_images_pexels,
    "pixabay": search_images_pixabay,
    "unsplash": search_images_unsplash,
    "wikimedia": search_images_wikimedia,
    "duckduckgo": search_images_ddg,
    "serper": search_images_serper,
}

_DEFAULT_IMAGE_SOURCE_ORDER = ["duckduckgo", "pexels", "pixabay", "unsplash", "wikimedia"]

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
    report: Optional[dict] = None,
) -> str:
    """
    Search for a still image using multiple providers in priority order and
    download the first result found.  Returns a local file path or '' if all
    providers and terms are exhausted.

    source_order: provider names to try in order.  Defaults to
        ["duckduckgo", "wikimedia", "pexels", "pixabay", "unsplash"].

    content_track: "named" sentences (a specific product/person/place) get a
        larger per-term candidate slice (image_candidates_per_term_named,
        default 10) than generic "broll" (image_candidates_per_term, default
        6) when relevance filtering is active -- named entities benefit more
        from trying harder to find the one real match, while broll's search
        pool is broad enough that extra candidates rarely change the outcome.

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
        the best-scoring candidate seen across all terms is used instead --
        relevance filtering never reduces the candidate pool to zero.
    """
    if source_order is None:
        source_order = _DEFAULT_IMAGE_SOURCE_ORDER

    use_relevance = (
        bool(caption_prompt)
        and relevance.is_available()
        and not relevance.is_log_only()
    )
    margin = float(config.app.get("relevance_margin", 0.02))

    def _gather_urls(term: str) -> List[Tuple[str, str]]:
        per_provider: List[Tuple[str, List[str]]] = []
        for provider in source_order:
            fn = _IMAGE_PROVIDERS.get(provider)
            if fn is None:
                logger.warning(f"unknown image provider: {provider}")
                continue
            # DDG draws from the broad open web, so individual hosts are more
            # likely to block hotlinking (e.g. Akamai-protected CDNs) -- request
            # more candidates so a working one is likely among them.
            n = 10 if provider == "duckduckgo" else 6
            # Serper (Google Images) works best with short, canonical entity
            # names — verbose concept strings like "JBL L100 Century alnico
            # drivers front view" return nothing. Use the short override when set.
            query = serper_term if (provider == "serper" and serper_term) else term
            urls = [
                url for url in fn(query, n=n)
                if url and (used_urls is None or url not in used_urls)
            ]
            per_provider.append((provider, urls))

        # Interleave round-robin across providers so a relevance-limited
        # slice (_CANDIDATES_PER_TERM) still draws from multiple sources
        # instead of being dominated by whichever provider is first in
        # source_order (e.g. DuckDuckGo, which usually returns the most
        # results but is also the least curated/highest-risk source).
        candidates: List[Tuple[str, str]] = []
        i = 0
        while True:
            added = False
            for provider, urls in per_provider:
                if i < len(urls):
                    candidates.append((provider, urls[i]))
                    added = True
            if not added:
                break
            i += 1
        return candidates

    def _claim(url: str, term: str, provider: str, local: str, dedup_emb=None, note: str = "") -> str:
        """The single authoritative commit point for a candidate -- owns both
        URL-uniqueness and visual-uniqueness atomically. Acceptance isn't
        decided until here (relevance-margin scoring and VLM comparative
        pooling both run before this), so this is the correct place for the
        dedup embedding to actually claim its window slot, not the earlier
        soft contains_similar() pre-filter."""
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

        # Comparative VLM pooling: instead of claiming the first candidate that
        # individually passes every gate, gather up to vlm_compare_pool_size
        # accepted candidates, then show them to the VLM together in one call
        # and take its pick -- more reliable than independent per-candidate
        # threshold scoring for "which of these is the best match." Scoped by
        # vlm_compare_scope ("off" | "named" | "all", default "named") so the
        # extra VLM cost is spent where a wrong pick is most noticeable.
        compare_scope = str(config.app.get("vlm_compare_scope", "named")).strip().lower()
        compare_mode = vlm.is_enabled() and (
            compare_scope == "all" or (compare_scope == "named" and content_track == "named")
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
            candidates = _gather_urls(term)
            iter_candidates = candidates if not use_relevance else candidates[:candidates_per_term]
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

                if not nsfw.passes(nsfw.is_nsfw_image(image_bytes)):
                    logger.info(f"rejected NSFW image candidate: {url}")
                    if report is not None:
                        report.setdefault("rejections", []).append("nsfw")
                    continue

                if vlm.is_enabled():
                    vlm_score = vlm.verify_image(
                        image_bytes, narration, visual_caption, video_topic,
                        must_show or [], avoid or [],
                    )
                    if vlm_score is not None and vlm_score < vlm_img_threshold:
                        logger.info(f"VLM rejected image candidate (score={vlm_score:.2f}): {url}")
                        # Mark as used so subsequent clips in this sentence skip re-download.
                        if used_urls is not None:
                            used_urls.add(url)
                        if report is not None:
                            report.setdefault("rejections", []).append("vlm")
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

                if not use_relevance:
                    if compare_mode:
                        pool.append((provider, term, url, local, image_bytes, dedup_emb))
                        if len(pool) >= pool_size:
                            break
                        continue
                    _r = _claim(url, term, provider, local, dedup_emb=dedup_emb)
                    if _r:
                        return _r
                    continue  # another thread claimed this URL/embedding; try next candidate

                s = relevance.score(prompt, image_bytes)
                if config.app.get("relevance_debug_log", False):
                    logger.debug(f"relevance[image] prompt={prompt!r} score={s} url={url}")
                margin_ok = relevance.passes_margin(prompt, image_bytes, margin)
                if margin_ok is None or margin_ok:
                    if compare_mode:
                        pool.append((provider, term, url, local, image_bytes, dedup_emb))
                        if len(pool) >= pool_size:
                            break
                        continue
                    note = f" (score={s:.3f})" if s is not None else ""
                    _r = _claim(url, term, provider, local, dedup_emb=dedup_emb, note=note)
                    if _r:
                        return _r
                    continue  # another thread claimed this URL/embedding; try next candidate
                if s is not None and s > fallback_score:
                    fallback_score = s
                    fallback_path, fallback_url = local, url
                    fallback_provider, fallback_term = provider, term
                    fallback_emb = dedup_emb

            if compare_mode and len(pool) >= pool_size:
                break  # enough pooled candidates -- stop trying further terms too

        if compare_mode and pool:
            if len(pool) == 1:
                provider, term, url, local, image_bytes, dedup_emb = pool[0]
                _r = _claim(url, term, provider, local, dedup_emb=dedup_emb)
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
                provider, term, url, local, image_bytes, dedup_emb = pool[winner_idx]
                for i, entry in enumerate(pool):
                    if i != winner_idx:
                        try:
                            os.remove(entry[3])
                        except Exception:
                            pass
                _r = _claim(url, term, provider, local, dedup_emb=dedup_emb)
                if _r:
                    if report is not None:
                        report["vlm_compare_used"] = True
                        report["vlm_compare_pool_size"] = len(pool)
                    return _r
                # Raced by a concurrent thread -- fall through, same as above.

        if fallback_path:
            logger.warning(
                f"no image cleared relevance margin {margin} for {search_terms}; "
                f"using best available for '{fallback_term}' (score={fallback_score:.3f}): {fallback_path}"
            )
            _r = _claim(fallback_url, fallback_term, fallback_provider, fallback_path, dedup_emb=fallback_emb)
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
