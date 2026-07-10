"""Video and BGM search providers, download, and save utilities."""
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import List
from urllib.parse import urlencode, urlparse

import requests
from loguru import logger
from moviepy.video.io.VideoFileClip import VideoFileClip

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect
from app.services.media._common import (
    _HTTP_TIMEOUT_MEDIA,
    _api_get_json,
    _download_bytes,
    _get_tls_verify,
    _touch_cache_file,
    get_api_key,
)
from app.services.scoring import relevance
from app.utils import utils

# ---------------------------------------------------------------------------
# Thumbnail reranking
# ---------------------------------------------------------------------------

_MAX_RERANK_CANDIDATES = 25


def _pexels_url_to_tags(url: str) -> str:
    """Extract descriptive words from a Pexels video/photo page URL.

    e.g. 'https://www.pexels.com/video/bird-flying-in-sky-4048183/' → 'bird flying in sky'
    """
    if not url:
        return ""
    try:
        path = urlparse(url).path.strip("/")
        segment = path.split("/")[-1]
        parts = [p for p in segment.split("-") if p and not p.isdigit()]
        return " ".join(parts)
    except Exception:
        return ""


def sort_by_metadata(
    candidates: list,
    caption_prompt: str,
    must_show: List[str] = None,
    avoid: List[str] = None,
) -> list:
    """Re-order candidates by keyword match score against tags/metadata.

    A lightweight pre-sort before download: candidates whose tags contain
    more caption_prompt words are tried first; must_show hits get a bonus,
    avoid hits get a penalty. Never discards — only reorders.
    """
    if not candidates:
        return candidates
    must_show = must_show or []
    avoid = avoid or []

    stop = {"a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or", "with"}
    prompt_words = [
        w.lower()
        for w in re.findall(r"\w+", caption_prompt)
        if len(w) > 2 and w.lower() not in stop
    ]
    must_words = [w.lower() for w in " ".join(must_show).split() if len(w) > 2]
    avoid_words = [w.lower() for w in " ".join(avoid).split() if len(w) > 2]

    if not prompt_words and not must_words and not avoid_words:
        return candidates

    def _score(item) -> int:
        haystack = (item.tags or "").lower()
        score = sum(1 for w in prompt_words if w in haystack)
        score += sum(2 for w in must_words if w in haystack)
        score -= sum(3 for w in avoid_words if w in haystack)
        return score

    scored = sorted(candidates, key=_score, reverse=True)
    if config.app.get("relevance_debug_log", False):
        for item in scored[:5]:
            logger.debug(
                f"metadata sort: score={_score(item):+d} tags={item.tags!r:.60} url={item.url}"
            )
    return scored


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
    # Thumbnails are small; fetch them concurrently instead of one blocking
    # HTTP GET at a time (up to 25 per search). Bounded pool — this already
    # runs inside a clip-fetch worker thread.
    with ThreadPoolExecutor(max_workers=6) as pool:
        thumb_bytes = list(
            pool.map(
                lambda item: _download_bytes(item.thumbnail) if item.thumbnail else b"",
                candidates,
            )
        )
    pairs = list(zip(candidates, thumb_bytes))
    ranked = relevance.rank(prompt, pairs, kind=kind)
    return [item for item, _ in ranked] + rest


# ---------------------------------------------------------------------------
# Video search providers
# ---------------------------------------------------------------------------

def search_videos_pexels(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
    prompt: str = "",
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)
    video_orientation = aspect.name
    video_width, video_height = aspect.to_resolution()
    try:
        api_key = get_api_key("pexels_api_keys")
    except ValueError:
        logger.warning("pexels_api_keys not configured, skipping Pexels video search")
        return []
    headers = {
        "Authorization": api_key,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    }
    # Build URL
    params = {"query": search_term, "per_page": 80, "orientation": video_orientation}
    query_url = f"https://api.pexels.com/videos/search?{urlencode(params)}"
    logger.info(f"searching videos: {query_url}, with proxies: {config.proxy}")

    try:
        response = _api_get_json(query_url, headers=headers)
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
                item.tags = _pexels_url_to_tags(v.get("url", ""))
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

    try:
        api_key = get_api_key("pixabay_api_keys")
    except ValueError:
        logger.warning("pixabay_api_keys not configured, skipping Pixabay video search")
        return []
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
        response = _api_get_json(query_url)
        video_items = []
        if "hits" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items
        videos = response["hits"]
        target_pixels = video_width * video_height
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
            # Pick smallest resolution >= target; fall back to largest available.
            best_video = None
            best_diff = float("inf")
            fallback_video = None
            fallback_pixels = 0
            for video in video_files.values():
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
                item.provider = "pixabay"
                item.url = chosen["url"]
                item.duration = duration
                item.thumbnail = thumbnail
                item.tags = v.get("tags", "")
                video_items.append(item)
        return _rerank_by_thumbnail(video_items, prompt, kind="video")
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def search_videos_coverr(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
    prompt: str = "",
) -> List[MaterialInfo]:
    """Search Coverr for free cinematic B-roll (no API key required for the free tier).

    Coverr's catalogue is especially strong for nature, lifestyle, architecture,
    and abstract/thematic footage — useful as a supplemental source when
    Pexels/Pixabay return thin results for unusual concepts.
    """
    if not config.app.get("coverr_enabled", True):
        return []

    # Coverr requires an API key even on the free tier (register at coverr.co).
    # Without one the request returns 401, so skip silently rather than logging
    # an error every clip.
    coverr_key = config.app.get("coverr_api_key", "").strip()
    if not coverr_key:
        return []

    aspect = VideoAspect(video_aspect)
    video_width, video_height = aspect.to_resolution()

    params = {"keywords": search_term, "page": 1, "size": 20}
    api_url = f"https://api.coverr.co/videos?{urlencode(params)}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Authorization": f"Bearer {coverr_key}",
    }

    logger.info(f"searching Coverr: {api_url}")
    try:
        response = _api_get_json(api_url, headers=headers)
        video_items = []
        hits = response.get("hits", [])
        target_pixels = video_width * video_height

        for hit in hits:
            duration = int(hit.get("duration") or 0)
            if duration < minimum_duration:
                continue

            # Try known field names for the direct MP4 URL.
            # If none contain ".mp4", attempt to construct from slug.
            video_url = ""
            for field in ("mp4_url", "download_url", "url"):
                candidate_url = hit.get(field, "")
                if candidate_url and ".mp4" in candidate_url:
                    video_url = candidate_url
                    break
            if not video_url:
                slug = hit.get("slug") or hit.get("id", "")
                if slug:
                    video_url = f"https://media.coverr.co/videos/{slug}.mp4"

            if not video_url:
                continue

            w = int(hit.get("width") or 0)
            h = int(hit.get("height") or 0)
            if w > 0 and h > 0:
                pixels = w * h
                # Skip clips that are much smaller than the target resolution.
                if pixels < target_pixels // 4:
                    continue

            item = MaterialInfo()
            item.provider = "coverr"
            item.url = video_url
            item.duration = duration
            item.thumbnail = hit.get("thumbnail") or hit.get("preview") or ""
            item.tags = (hit.get("title") or hit.get("slug", "").replace("-", " ")).strip()
            video_items.append(item)

        return _rerank_by_thumbnail(video_items, prompt, kind="video")
    except Exception as e:
        logger.error(f"Coverr video search failed: {e}")

    return []


# ---------------------------------------------------------------------------
# Video save
# ---------------------------------------------------------------------------

def save_video(video_url: str, save_dir: str = "") -> str:
    if not save_dir:
        save_dir = utils.storage_dir("cache_videos")

    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    url_without_query = video_url.split("?")[0]
    url_hash = utils.md5(url_without_query)
    video_id = f"vid-{url_hash}"
    video_path = f"{save_dir}/{video_id}.mp4"

    # if video already exists and is a plausible size, return the path
    if os.path.exists(video_path) and os.path.getsize(video_path) > 4096:
        logger.info(f"video already exists: {video_path}")
        _touch_cache_file(video_path)
        return video_path

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }

    # Stream to a .part file rather than buffering the whole video in memory —
    # stock clips run 100MB+, and several fetch workers download concurrently.
    part_path = video_path + ".part"
    try:
        with requests.get(
            video_url,
            headers=headers,
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=_HTTP_TIMEOUT_MEDIA,
            stream=True,
        ) as r:
            r.raise_for_status()
            with open(part_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        os.replace(part_path, video_path)
    except Exception as e:
        logger.warning(f"failed to download video {video_url}: {e}")
        for p in (part_path, video_path):
            try:
                os.remove(p)
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
# BGM search + download  (online sources — no local folder required)
# ---------------------------------------------------------------------------

def search_bgm_pixabay(search_term: str, n: int = 3) -> List[str]:
    """
    Search Pixabay for free background music.
    Uses the same pixabay_api_keys already configured in config.toml.
    Returns a list of MP3 download URLs.
    """
    try:
        api_key = get_api_key("pixabay_api_keys")
    except ValueError:
        logger.debug("pixabay_api_keys not configured, skipping Pixabay BGM search")
        return []
    params = {
        "key": api_key,
        "q": search_term,
        "per_page": n,
    }
    url = f"https://pixabay.com/api/music/?{urlencode(params)}"
    try:
        hits = _api_get_json(url).get("hits", [])
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
            verify=_get_tls_verify(), timeout=_HTTP_TIMEOUT_MEDIA,
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
