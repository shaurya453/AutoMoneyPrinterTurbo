"""app.services.media — re-exports everything that was public in material.py.

Importing `from app.services import media as material` gives callers the
same attribute namespace as the old `material` module.
"""

from app.services.media._common import get_api_key  # noqa: F401
from app.services.media.images import (  # noqa: F401
    download_image,
    save_image,
    search_images_ddg,
    search_images_pexels,
    search_images_pixabay,
    search_images_serper,
    search_images_unsplash,
    search_images_wikimedia,
)
from app.services.media.videos import (  # noqa: F401
    download_bgm,
    save_bgm,
    save_video,
    search_bgm_pixabay,
    search_videos_coverr,
    search_videos_pexels,
    search_videos_pixabay,
    sort_by_metadata,
)
