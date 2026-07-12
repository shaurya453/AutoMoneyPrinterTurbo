"""visual_criticality effort-dial + Openverse provider tests — no network."""
from unittest import mock

from app.services.media import _common
from app.services.media import images
from app.services.pipeline import _fetch


# ── criticality helpers ─────────────────────────────────────────────────────

def test_get_criticality_normalizes():
    assert _fetch.get_criticality({}) == "medium"
    assert _fetch.get_criticality({"visual_criticality": "CRITICAL"}) == "critical"
    assert _fetch.get_criticality({"visual_criticality": " high "}) == "high"
    assert _fetch.get_criticality({"visual_criticality": "extreme"}) == "medium"
    assert _fetch.get_criticality({"visual_criticality": None}) == "medium"


def test_budget_multiplier_mapping():
    assert _fetch.criticality_budget_multiplier({}) == 1.0
    assert _fetch.criticality_budget_multiplier({"visual_criticality": "low"}) == 0.6
    assert _fetch.criticality_budget_multiplier({"visual_criticality": "critical"}) == 1.5


def test_video_attempts_scale_with_criticality():
    # low can never drop below 1 attempt
    assert _fetch._CRITICALITY_VIDEO_ATTEMPTS["low"] == -1
    assert _fetch._CRITICALITY_VIDEO_ATTEMPTS["critical"] == 4
    assert set(_fetch._CRITICALITY_VIDEO_ATTEMPTS) == set(_fetch._CRITICALITY_LEVELS)
    assert set(_fetch._CRITICALITY_BUDGET_MULT) == set(_fetch._CRITICALITY_LEVELS)


# ── Openverse provider ──────────────────────────────────────────────────────

def _openverse_payload():
    return {
        "results": [
            {"url": "https://cdn.example.org/niacinamide-structure.png",
             "width": 1200, "height": 900, "source": "wikimedia"},
            {"url": "https://cdn.example.org/too-small.png",
             "width": 200, "height": 200, "source": "flickr"},
            {"url": "https://www.shutterstock.com/watermarked.jpg",
             "width": 1200, "height": 900, "source": "shutterstock.com"},
            {"url": "https://cdn.example.org/banner-strip.png",
             "width": 3000, "height": 400, "source": "flickr"},
            {"url": "https://cdn.example.org/ok-2.jpg",
             "width": 1024, "height": 768, "source": "flickr"},
        ]
    }


def test_openverse_filters_and_returns_urls():
    with mock.patch.object(images, "_api_get_json", return_value=_openverse_payload()):
        urls = images.search_images_openverse("niacinamide structure", n=5)
    assert urls == [
        "https://cdn.example.org/niacinamide-structure.png",
        "https://cdn.example.org/ok-2.jpg",
    ]


def test_openverse_short_circuits_on_cooldown():
    _common.set_provider_cooldown("openverse", 60)
    try:
        with mock.patch.object(images, "_api_get_json") as api:
            assert images.search_images_openverse("anything") == []
            api.assert_not_called()
    finally:
        with _common._blocked_hosts_lock:
            _common._provider_cooldowns.clear()


def test_openverse_registered_in_provider_orders():
    assert "openverse" in images._IMAGE_PROVIDERS
    assert "openverse" in images._DEFAULT_IMAGE_SOURCE_ORDER
