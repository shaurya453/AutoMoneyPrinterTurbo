"""Configurable xfade transition name (crossfade_transition config key)."""
from app.config import config
from app.services.render.combine import _configured_xfade_transition


def test_default_transition_is_fade():
    config.app.pop("crossfade_transition", None)
    assert _configured_xfade_transition() == "fade"


def test_whip_maps_to_hblur():
    config.app["crossfade_transition"] = "hblur"
    try:
        assert _configured_xfade_transition() == "hblur"
    finally:
        config.app.pop("crossfade_transition", None)


def test_unknown_transition_falls_back_to_fade():
    config.app["crossfade_transition"] = "not-a-real-transition"
    try:
        assert _configured_xfade_transition() == "fade"
    finally:
        config.app.pop("crossfade_transition", None)
