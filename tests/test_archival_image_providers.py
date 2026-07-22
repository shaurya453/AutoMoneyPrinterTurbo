"""Unit tests for the NASA / Internet Archive / Smithsonian image providers —
no network involved, `_cached_api_get_json` is monkeypatched with canned
responses (patched at that level, not the inner `_api_get_json`, since the
providers now call through the search-response cache wrapper).
"""
import pytest

from app.config import config
from app.services.media import _common, images


@pytest.fixture(autouse=True)
def _clean_breaker_state():
    with _common._blocked_hosts_lock:
        _common._provider_cooldowns.clear()
    yield
    with _common._blocked_hosts_lock:
        _common._provider_cooldowns.clear()


def test_nasa_derives_large_url_from_nasa_id(monkeypatch):
    canned = {
        "collection": {
            "items": [
                {
                    "href": "https://images-assets.nasa.gov/image/ABC123/collection.json",
                    "data": [{"nasa_id": "ABC123"}],
                },
                {
                    "href": "https://images-assets.nasa.gov/image/XYZ789/collection.json",
                    "data": [{"nasa_id": "XYZ789"}],
                },
            ]
        }
    }
    monkeypatch.setattr(images, "_cached_api_get_json", lambda *a, **k: canned)
    urls = images.search_images_nasa("apollo", n=2)
    assert urls == [
        "https://images-assets.nasa.gov/image/ABC123/ABC123~large.jpg",
        "https://images-assets.nasa.gov/image/XYZ789/XYZ789~large.jpg",
    ]


def test_nasa_skips_items_missing_id_or_href(monkeypatch):
    canned = {"collection": {"items": [{"href": "", "data": [{"nasa_id": "X"}]}, {"data": [{}]}]}}
    monkeypatch.setattr(images, "_cached_api_get_json", lambda *a, **k: canned)
    assert images.search_images_nasa("apollo", n=5) == []


def test_nasa_respects_cooldown(monkeypatch):
    called = []
    monkeypatch.setattr(images, "_cached_api_get_json", lambda *a, **k: called.append(1) or {})
    _common.set_provider_cooldown("nasa", 60)
    assert images.search_images_nasa("apollo", n=2) == []
    assert not called


def test_archive_org_picks_largest_non_thumb_file(monkeypatch):
    def fake_get_json(provider, url, *a, **k):
        if "advancedsearch" in url:
            return {"response": {"docs": [{"identifier": "item1"}]}}
        return {
            "files": [
                {"name": "item1_thumb.jpg", "size": "500"},
                {"name": "item1.jpg", "size": "50000"},
                {"name": "item1_small.jpg", "size": "8000"},
                {"name": "item1.pdf", "size": "999999"},  # not an image, must be ignored
            ]
        }
    monkeypatch.setattr(images, "_cached_api_get_json", fake_get_json)
    urls = images.search_images_archive_org("propaganda poster", n=1)
    assert urls == ["https://archive.org/download/item1/item1.jpg"]


def test_archive_org_skips_docs_with_no_image_files(monkeypatch):
    def fake_get_json(provider, url, *a, **k):
        if "advancedsearch" in url:
            return {"response": {"docs": [{"identifier": "item1"}]}}
        return {"files": [{"name": "item1.pdf", "size": "100"}]}
    monkeypatch.setattr(images, "_cached_api_get_json", fake_get_json)
    assert images.search_images_archive_org("propaganda poster", n=1) == []


def test_smithsonian_skips_without_api_key():
    config.app.pop("smithsonian_api_keys", None)
    assert images.search_images_smithsonian("castle", n=3) == []


def test_smithsonian_extracts_first_image_media(monkeypatch):
    config.app["smithsonian_api_keys"] = "TEST_KEY"
    try:
        canned = {
            "response": {
                "rows": [
                    {"content": {"descriptiveNonRepeating": {}}},  # no online_media at all
                    {
                        "content": {
                            "descriptiveNonRepeating": {
                                "online_media": {
                                    "media": [
                                        {"type": "Images", "content": "https://ids.si.edu/x/1"},
                                    ]
                                }
                            }
                        }
                    },
                ]
            }
        }
        monkeypatch.setattr(images, "_cached_api_get_json", lambda *a, **k: canned)
        urls = images.search_images_smithsonian("castle", n=5)
        assert urls == ["https://ids.si.edu/x/1"]
    finally:
        config.app.pop("smithsonian_api_keys", None)


def test_all_three_registered_in_image_providers():
    assert images._IMAGE_PROVIDERS["nasa"] is images.search_images_nasa
    assert images._IMAGE_PROVIDERS["archive_org"] is images.search_images_archive_org
    assert images._IMAGE_PROVIDERS["smithsonian"] is images.search_images_smithsonian
