import warnings
from enum import Enum
from typing import Optional, Union

import pydantic
from pydantic import BaseModel

from app.config import config

warnings.filterwarnings(
    "ignore",
    category=UserWarning,
    message="Field name.*shadows an attribute in parent.*",
)


class VideoConcatMode(str, Enum):
    random = "random"
    sequential = "sequential"


class VideoTransitionMode(str, Enum):
    none = None
    shuffle = "Shuffle"
    fade_in = "FadeIn"
    fade_out = "FadeOut"
    slide_in = "SlideIn"
    slide_out = "SlideOut"
    crossfade = "Crossfade"


class VideoAspect(str, Enum):
    landscape = "16:9"
    portrait = "9:16"
    square = "1:1"

    def to_resolution(self):
        if self.value == "16:9":
            return 1920, 1080
        elif self.value == "9:16":
            return 1080, 1920
        elif self.value == "1:1":
            return 1080, 1080
        return 1080, 1920


class _Config:
    arbitrary_types_allowed = True


@pydantic.dataclasses.dataclass(config=_Config)
class MaterialInfo:
    provider: str = "pexels"
    url: str = ""
    duration: int = 0
    thumbnail: str = ""


class VideoParams(BaseModel):
    video_subject: str
    video_script: str = ""
    video_aspect: Optional[VideoAspect] = VideoAspect.portrait.value
    video_concat_mode: Optional[VideoConcatMode] = VideoConcatMode.random.value
    video_transition_mode: Optional[VideoTransitionMode] = None
    video_clip_duration: Optional[int] = 5

    video_source: Optional[str] = "pexels"

    voice_name: Optional[str] = ""
    voice_volume: Optional[float] = 1.0
    voice_rate: Optional[float] = 1.0
    bgm_type: Optional[str] = "random"
    bgm_file: Optional[str] = ""
    bgm_volume: Optional[float] = 0.2

    subtitle_enabled: Optional[bool] = True
    subtitle_position: Optional[str] = config.ui.get("subtitle_position", "bottom")
    custom_position: float = config.ui.get("custom_position", 70.0)
    font_name: Optional[str] = "Inter_18pt-SemiBold.ttf"
    text_fore_color: Optional[str] = "#FFFFFF"
    text_background_color: Union[bool, str] = False

    font_size: int = 30
    stroke_color: Optional[str] = "#000000"
    stroke_width: float = 1.5
    subtitle_highlight: bool = False
    n_threads: Optional[int] = 2
