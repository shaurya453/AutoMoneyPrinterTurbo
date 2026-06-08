import os
import random
import threading
from typing import List
from urllib.parse import urlencode

import requests
from loguru import logger
from moviepy.video.io.VideoFileClip import VideoFileClip

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoConcatMode
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


def search_videos_pexels(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
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
    params = {"query": search_term, "per_page": 20, "orientation": video_orientation}
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
        # loop through each video in the result
        for v in videos:
            duration = v["duration"]
            # check if video has desired minimum duration
            if duration < minimum_duration:
                continue
            video_files = v["video_files"]
            # loop through each url to determine the best quality
            for video in video_files:
                w = int(video["width"])
                h = int(video["height"])
                if w == video_width and h == video_height:
                    item = MaterialInfo()
                    item.provider = "pexels"
                    item.url = video["link"]
                    item.duration = duration
                    video_items.append(item)
                    break
        return video_items
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def search_videos_pixabay(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)

    video_width, video_height = aspect.to_resolution()

    api_key = get_api_key("pixabay_api_keys")
    # Build URL
    params = {
        "q": search_term,
        "video_type": "all",  # Accepted values: "all", "film", "animation"
        "per_page": 50,
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
                    video_items.append(item)
                    break
        return video_items
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


def download_videos(
    task_id: str,
    search_terms: List[str],
    source: str = "pexels",
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_contact_mode: VideoConcatMode = VideoConcatMode.random,
    audio_duration: float = 0.0,
    max_clip_duration: int = 5,
) -> List[str]:
    valid_video_items = []
    valid_video_urls = []
    found_duration = 0.0
    search_videos = search_videos_pexels
    if source == "pixabay":
        search_videos = search_videos_pixabay

    for search_term in search_terms:
        video_items = search_videos(
            search_term=search_term,
            minimum_duration=max_clip_duration,
            video_aspect=video_aspect,
        )
        logger.info(f"found {len(video_items)} videos for '{search_term}'")

        for item in video_items:
            if item.url not in valid_video_urls:
                valid_video_items.append(item)
                valid_video_urls.append(item.url)
                found_duration += item.duration

    logger.info(
        f"found total videos: {len(valid_video_items)}, required duration: {audio_duration} seconds, found duration: {found_duration} seconds"
    )
    video_paths = []

    material_directory = config.app.get("material_directory", "").strip()
    if material_directory == "task":
        material_directory = utils.task_dir(task_id)
    elif material_directory and not os.path.isdir(material_directory):
        material_directory = ""

    concat_mode_value = getattr(video_contact_mode, "value", video_contact_mode)
    if concat_mode_value == VideoConcatMode.random.value:
        random.shuffle(valid_video_items)

    total_duration = 0.0
    for item in valid_video_items:
        try:
            logger.info(f"downloading video: {item.url}")
            saved_video_path = save_video(
                video_url=item.url, save_dir=material_directory
            )
            if saved_video_path:
                logger.info(f"video saved: {saved_video_path}")
                video_paths.append(saved_video_path)
                seconds = min(max_clip_duration, item.duration)
                total_duration += seconds
                if total_duration > audio_duration:
                    logger.info(
                        f"total duration of downloaded videos: {total_duration} seconds, skip downloading more"
                    )
                    break
        except Exception as e:
            logger.error(f"failed to download video: {utils.to_json(item)} => {str(e)}")
    logger.success(f"downloaded {len(video_paths)} videos")
    return video_paths


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
            info = page.get("imageinfo", [])
            if info:
                thumb = info[0].get("thumburl") or info[0].get("url")
                if thumb:
                    urls.append(thumb)
        return urls
    except Exception as e:
        logger.error(f"Wikimedia image search failed: {e}")
        return []


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
        logger.info(f"image already cached: {image_path}")
        return image_path

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        r = requests.get(
            image_url, headers=headers, proxies=config.proxy,
            verify=_get_tls_verify(), timeout=(30, 120),
        )
        r.raise_for_status()
        with open(image_path, "wb") as fh:
            fh.write(r.content)
        if os.path.exists(image_path) and os.path.getsize(image_path) > 0:
            return image_path
    except Exception as e:
        logger.error(f"image download failed: {image_url} => {e}")
    return ""


_IMAGE_PROVIDERS = {
    "pexels": search_images_pexels,
    "pixabay": search_images_pixabay,
    "unsplash": search_images_unsplash,
    "wikimedia": search_images_wikimedia,
}

_DEFAULT_IMAGE_SOURCE_ORDER = ["pexels", "pixabay", "unsplash", "wikimedia"]


def download_image(
    search_terms: List[str],
    source_order: List[str] = None,
    save_dir: str = "",
) -> str:
    """
    Search for a still image using multiple providers in priority order and
    download the first result found.  Returns a local file path or '' if all
    providers and terms are exhausted.

    source_order: provider names to try in order.  Defaults to
        ["pexels", "pixabay", "unsplash", "wikimedia"].
    """
    if source_order is None:
        source_order = _DEFAULT_IMAGE_SOURCE_ORDER

    for term in search_terms:
        for provider in source_order:
            fn = _IMAGE_PROVIDERS.get(provider)
            if fn is None:
                logger.warning(f"unknown image provider: {provider}")
                continue
            urls = fn(term, n=3)
            for url in urls:
                if not url:
                    continue
                local = save_image(url, save_dir)
                if local:
                    logger.info(f"image obtained via {provider} for '{term}': {local}")
                    return local

    logger.warning(f"no image found for terms {search_terms} from {source_order}")
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


if __name__ == "__main__":
    download_videos(
        "test123", ["Money Exchange Medium"], audio_duration=100, source="pixabay"
    )
