import os
import re
import threading
from typing import List, Tuple
from urllib.parse import urlencode, urlparse

import requests
from loguru import logger
from moviepy.video.io.VideoFileClip import VideoFileClip
from PIL import Image, UnidentifiedImageError

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect
from app.services import nsfw, relevance
from app.utils import utils

# Thread-safe counter for API key rotation
_api_key_counter = 0
_api_key_lock = threading.Lock()


def _get_tls_verify() -> bool:
    # 默认开启 TLS 证书校验，防止素材搜索和下载过程被中间人篡改。
    # 仅在企业代理、自签证书等明确需要的场景下，允许用户通过
    # `config.toml` 显式设置 `tls_verify = false` 临时关闭。
    tls_verify = config.app.get("tls_verify", True)
    if isinstance(tls_verify, str):
        tls_verify = tls_verify.strip().lower() not in ("0", "false", "no", "off")

    if not tls_verify:
        logger.warning(
            "TLS certificate verification is disabled by config.app.tls_verify=false. "
            "Only use this in trusted proxy environments."
        )

    return bool(tls_verify)


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
            verify=_get_tls_verify(), timeout=(15, 30),
        )
        r.raise_for_status()
        return r.content
    except Exception as e:
        logger.debug(f"thumbnail download failed: {url} => {e}")
        return b""


_MAX_RERANK_CANDIDATES = 12


def _rerank_by_thumbnail(
    items: List[MaterialInfo], prompt: str, kind: str = "video"
) -> List[MaterialInfo]:
    """Reorder video search results by CLIP relevance of their thumbnail
    against `prompt` (a visual_caption or legacy search_term+video_topic
    string).

    No-op (returns items unchanged, in original order) if prompt is empty,
    the relevance model is unavailable, or RELEVANCE_LOG_ONLY is set.
    """
    if not items or not prompt or not relevance.is_available():
        return items

    candidates = items[:_MAX_RERANK_CANDIDATES]
    rest = items[_MAX_RERANK_CANDIDATES:]
    pairs = [
        (item, _download_bytes(item.thumbnail) if item.thumbnail else b"")
        for item in candidates
    ]
    ranked = relevance.rank(prompt, pairs, kind=kind)
    return [item for item, _ in ranked] + rest


def search_videos_pexels(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
    prompt: str = "",
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)
    video_orientation = aspect.name
    video_width, video_height = aspect.to_resolution()
    api_key = get_api_key("pexels_api_keys")
    headers = {
        "Authorization": api_key,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    }
    # Build URL
    params = {"query": search_term, "per_page": 80, "orientation": video_orientation}
    query_url = f"https://api.pexels.com/videos/search?{urlencode(params)}"
    logger.info(f"searching videos: {query_url}, with proxies: {config.proxy}")

    try:
        r = requests.get(
            query_url,
            headers=headers,
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=(30, 60),
        )
        response = r.json()
        video_items = []
        if "videos" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items
        videos = response["videos"]
        target_pixels = video_width * video_height
        # loop through each video in the result
        for v in videos:
            duration = v["duration"]
            # check if video has desired minimum duration
            if duration < minimum_duration:
                continue
            video_files = v["video_files"]
            # Pick smallest resolution >= target; fall back to largest available.
            best_video = None
            best_diff = float("inf")
            fallback_video = None
            fallback_pixels = 0
            for video in video_files:
                w = int(video.get("width") or 0)
                h = int(video.get("height") or 0)
                if w <= 0 or h <= 0:
                    continue
                pixels = w * h
                if pixels >= target_pixels:
                    diff = pixels - target_pixels
                    if diff < best_diff:
                        best_diff = diff
                        best_video = video
                elif pixels > fallback_pixels:
                    fallback_pixels = pixels
                    fallback_video = video
            chosen = best_video or fallback_video
            if chosen:
                item = MaterialInfo()
                item.provider = "pexels"
                item.url = chosen["link"]
                item.duration = duration
                item.thumbnail = v.get("image", "")
                video_items.append(item)
        return _rerank_by_thumbnail(video_items, prompt, kind="video")
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def search_videos_pixabay(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
    prompt: str = "",
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)

    video_width, video_height = aspect.to_resolution()

    api_key = get_api_key("pixabay_api_keys")
    # Build URL
    params = {
        "q": search_term,
        "video_type": "all",  # Accepted values: "all", "film", "animation"
        "per_page": 200,
        "safesearch": "true",
        "key": api_key,
    }
    query_url = f"https://pixabay.com/api/videos/?{urlencode(params)}"
    logger.info(f"searching videos: {query_url}, with proxies: {config.proxy}")

    try:
        r = requests.get(
            query_url, proxies=config.proxy, verify=_get_tls_verify(), timeout=(30, 60)
        )
        response = r.json()
        video_items = []
        if "hits" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items
        videos = response["hits"]
        # loop through each video in the result
        for v in videos:
            duration = v["duration"]
            # check if video has desired minimum duration
            if duration < minimum_duration:
                continue
            video_files = v["videos"]
            picture_id = v.get("picture_id", "")
            thumbnail = (
                f"https://i.vimeocdn.com/video/{picture_id}_640x360.jpg"
                if picture_id
                else ""
            )
            # loop through each url to determine the best quality
            for video_type in video_files:
                video = video_files[video_type]
                w = int(video["width"])
                # h = int(video["height"])
                if w >= video_width:
                    item = MaterialInfo()
                    item.provider = "pixabay"
                    item.url = video["url"]
                    item.duration = duration
                    item.thumbnail = thumbnail
                    video_items.append(item)
                    break
        return _rerank_by_thumbnail(video_items, prompt, kind="video")
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def save_video(video_url: str, save_dir: str = "") -> str:
    if not save_dir:
        save_dir = utils.storage_dir("cache_videos")

    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    url_without_query = video_url.split("?")[0]
    url_hash = utils.md5(url_without_query)
    video_id = f"vid-{url_hash}"
    video_path = f"{save_dir}/{video_id}.mp4"

    # if video already exists, return the path
    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        logger.info(f"video already exists: {video_path}")
        return video_path

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }

    # if video does not exist, download it
    try:
        with open(video_path, "wb") as f:
            f.write(
                requests.get(
                    video_url,
                    headers=headers,
                    proxies=config.proxy,
                    verify=_get_tls_verify(),
                    timeout=(60, 240),
                ).content
            )
    except Exception as e:
        logger.warning(f"failed to download video {video_url}: {e}")
        try:
            os.remove(video_path)
        except Exception:
            pass
        return ""

    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        clip = None
        try:
            clip = VideoFileClip(video_path)
            duration = clip.duration
            fps = clip.fps
            if duration > 0 and fps > 0:
                return video_path
        except Exception as e:
            logger.warning(f"invalid video file: {video_path} => {str(e)}")
            try:
                os.remove(video_path)
            except Exception as remove_error:
                logger.warning(
                    f"failed to remove invalid video file: {video_path}, error: {str(remove_error)}"
                )
        finally:
            if clip is not None:
                try:
                    clip.close()
                except Exception as close_error:
                    logger.warning(
                        f"failed to close video clip: {video_path}, error: {str(close_error)}"
                    )
    return ""


# ---------------------------------------------------------------------------
# Image search + download
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
        r = requests.get(
            url, headers=headers, proxies=config.proxy,
            verify=_get_tls_verify(), timeout=(30, 60),
        )
        photos = r.json().get("photos", [])
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
        r = requests.get(
            url, proxies=config.proxy, verify=_get_tls_verify(), timeout=(30, 60)
        )
        hits = r.json().get("hits", [])
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
        r = requests.get(
            url, headers=headers, proxies=config.proxy,
            verify=_get_tls_verify(), timeout=(30, 60),
        )
        results = r.json().get("results", [])
        return [
            p["urls"].get("full") or p["urls"].get("regular")
            for p in results
            if p.get("urls")
        ]
    except Exception as e:
        logger.error(f"Unsplash image search failed: {e}")
        return []


# Stock-photo libraries whose public preview images are almost always
# heavily watermarked. DDG draws from the open web and can surface these.
_WATERMARKED_IMAGE_DOMAINS = {
    "shutterstock.com", "istockphoto.com", "gettyimages.com", "alamy.com",
    "depositphotos.com", "123rf.com", "dreamstime.com", "stock.adobe.com",
    "bigstockphoto.com", "canstockphoto.com", "vectorstock.com",
    "fotolia.com", "stocksy.com",
}


def _is_watermarked_source(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in _WATERMARKED_IMAGE_DOMAINS)


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
        if _is_watermarked_source(url):
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


# Wikimedia Commons indexes scanned documents (PDF/DJVU page renders), vector
# diagrams (SVG), and audio/video alongside photos. Their thumbnails are
# returned by the same API but are almost never useful documentary b-roll,
# and PDF/DJVU page-render thumbnails are aggressively rate-limited (frequent
# 429 Too Many Requests). Skip files whose original extension isn't a raster
# photo format.
_WIKIMEDIA_SKIP_EXTENSIONS = {
    ".pdf", ".djvu", ".svg", ".tif", ".tiff", ".ogv", ".ogg", ".webm", ".mp4", ".gif",
}


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
        r = requests.get(
            url, headers=headers, proxies=config.proxy,
            verify=_get_tls_verify(), timeout=(30, 60),
        )
        pages = r.json().get("query", {}).get("pages", {})
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


def _is_valid_raster_image(image_path: str) -> bool:
    """Return True if image_path is a raster image PIL can decode.

    Rejects SVGs and other non-raster files that some search providers
    (e.g. DuckDuckGo) occasionally return with a misleading .jpg/.png
    extension -- Image.open() would otherwise crash later in the Ken
    Burns renderer.
    """
    try:
        with Image.open(image_path) as img:
            img.verify()
        return True
    except (UnidentifiedImageError, OSError):
        return False


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
            verify=_get_tls_verify(), timeout=(30, 120),
        )
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


_IMAGE_PROVIDERS = {
    "pexels": search_images_pexels,
    "pixabay": search_images_pixabay,
    "unsplash": search_images_unsplash,
    "wikimedia": search_images_wikimedia,
    "duckduckgo": search_images_ddg,
}

_DEFAULT_IMAGE_SOURCE_ORDER = ["duckduckgo", "wikimedia", "pexels", "pixabay", "unsplash"]

_CANDIDATES_PER_TERM = 6


def download_image(
    search_terms: List[str],
    source_order: List[str] = None,
    save_dir: str = "",
    used_urls: set = None,
    video_topic: str = "",
    caption_prompt: str = "",
) -> str:
    """
    Search for a still image using multiple providers in priority order and
    download the first result found.  Returns a local file path or '' if all
    providers and terms are exhausted.

    source_order: provider names to try in order.  Defaults to
        ["pexels", "pixabay", "unsplash", "wikimedia"].

    used_urls: if provided, mutated in-place with the chosen image's source
        URL so subsequent calls across the run won't reuse the same image.
        Anything already in
        used_urls is skipped entirely -- if every candidate across all terms
        and providers has already been used, this returns '' rather than
        reusing one (the caller falls back to a different media type).

    Every downloaded candidate (regardless of relevance settings) is passed
    through the NSFW pixel gate (app.services.nsfw); a hard-rejected
    candidate is deleted and never claimed/used or considered for the
    relevance fallback.

    caption_prompt / video_topic: if either is set (and the CLIP relevance
        model is available, and RELEVANCE_LOG_ONLY is not set), candidates
        are downloaded and scored against `caption_prompt` (falling back to
        `search_term + video_topic` if caption_prompt is empty); the first
        one per term that beats the junk anchors by `relevance_margin` is
        used. If nothing clears the margin, the best-scoring candidate seen
        across all terms is used instead -- relevance filtering never
        reduces the candidate pool to zero.
    """
    if source_order is None:
        source_order = _DEFAULT_IMAGE_SOURCE_ORDER

    use_relevance = (
        bool(caption_prompt or video_topic)
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
            urls = [
                url for url in fn(term, n=n)
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

    def _claim(url: str, term: str, provider: str, local: str, note: str = "") -> str:
        if used_urls is not None:
            used_urls.add(url)
        logger.info(f"image obtained via {provider} for '{term}': {local}{note}")
        return local

    def _try() -> str:
        fallback_path = fallback_url = fallback_provider = fallback_term = ""
        fallback_score = float("-inf")

        for term in search_terms:
            candidates = _gather_urls(term)
            iter_candidates = candidates if not use_relevance else candidates[:_CANDIDATES_PER_TERM]
            prompt = caption_prompt or relevance.build_prompt(term, video_topic)

            for provider, url in iter_candidates:
                local = save_image(url, save_dir)
                if not local:
                    continue

                with open(local, "rb") as fh:
                    image_bytes = fh.read()

                if not nsfw.passes(nsfw.is_nsfw_image(image_bytes)):
                    logger.info(f"rejected NSFW image candidate: {url}")
                    try:
                        os.remove(local)
                    except Exception:
                        pass
                    continue

                if not use_relevance:
                    return _claim(url, term, provider, local)

                s = relevance.score(prompt, image_bytes)
                if config.app.get("relevance_debug_log", False):
                    logger.debug(f"relevance[image] prompt={prompt!r} score={s} url={url}")
                margin_ok = relevance.passes_margin(prompt, image_bytes, margin)
                if margin_ok is None or margin_ok:
                    note = f" (score={s:.3f})" if s is not None else ""
                    return _claim(url, term, provider, local, note)
                if s is not None and s > fallback_score:
                    fallback_score = s
                    fallback_path, fallback_url = local, url
                    fallback_provider, fallback_term = provider, term

        if fallback_path:
            logger.warning(
                f"no image cleared relevance margin {margin} for {search_terms}; "
                f"using best available for '{fallback_term}' (score={fallback_score:.3f}): {fallback_path}"
            )
            return _claim(fallback_url, fallback_term, fallback_provider, fallback_path)
        return ""

    result = _try()
    if result:
        return result

    logger.warning(f"no unused image found for terms {search_terms} from {source_order}")
    return ""


# ---------------------------------------------------------------------------
# BGM search + download  (online sources — no local folder required)
# ---------------------------------------------------------------------------

def search_bgm_pixabay(search_term: str, n: int = 3) -> List[str]:
    """
    Search Pixabay for free background music.
    Uses the same pixabay_api_keys already configured in config.toml.
    Returns a list of MP3 download URLs.
    """
    api_key = get_api_key("pixabay_api_keys")
    if not api_key:
        logger.debug("pixabay_api_keys not configured, skipping Pixabay BGM search")
        return []
    params = {
        "key": api_key,
        "q": search_term,
        "per_page": n,
    }
    url = f"https://pixabay.com/api/music/?{urlencode(params)}"
    try:
        r = requests.get(
            url, proxies=config.proxy, verify=_get_tls_verify(), timeout=(30, 60)
        )
        hits = r.json().get("hits", [])
        return [h["audio"] for h in hits if h.get("audio")]
    except Exception as e:
        logger.error(f"Pixabay BGM search failed: {e}")
        return []


def save_bgm(bgm_url: str, save_dir: str = "") -> str:
    """Download a BGM audio file and cache it locally. Returns local path or '' on failure."""
    if not save_dir:
        save_dir = utils.storage_dir("cache_bgm")
    os.makedirs(save_dir, exist_ok=True)

    url_hash = utils.md5(bgm_url.split("?")[0])
    bgm_path = os.path.join(save_dir, f"bgm-{url_hash}.mp3")

    if os.path.exists(bgm_path) and os.path.getsize(bgm_path) > 0:
        logger.info(f"BGM already cached: {bgm_path}")
        return bgm_path

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        r = requests.get(
            bgm_url, headers=headers, proxies=config.proxy,
            verify=_get_tls_verify(), timeout=(60, 240),
        )
        r.raise_for_status()
        with open(bgm_path, "wb") as fh:
            fh.write(r.content)
        if os.path.exists(bgm_path) and os.path.getsize(bgm_path) > 0:
            return bgm_path
    except Exception as e:
        logger.error(f"BGM download failed: {bgm_url} => {e}")
    return ""


def download_bgm(search_term: str, save_dir: str = "") -> str:
    """
    Search online music providers for a background music track and download it.
    Currently supported: Pixabay (uses existing pixabay_api_keys from config).
    Returns local MP3 path on success, '' if nothing found.
    """
    for search_fn in [search_bgm_pixabay]:
        urls = search_fn(search_term, n=3)
        for url in urls:
            if not url:
                continue
            local = save_bgm(url, save_dir)
            if local:
                logger.info(f"BGM obtained online for '{search_term}': {local}")
                return local
    logger.warning(f"no BGM found online for '{search_term}', will use local fallback")
    return ""
