"""Circuit-breaker tests for the media fetch layer — no network involved.

Covers the per-host download-failure breaker, the dead-URL memo, and the
provider cooldowns added after the 2026-07-10 job post-mortem (143 Unsplash
403s, 128 DDG 30s timeouts, 287 hotlink-403 downloads in one run).
"""
import time

import pytest

from app.services.media import _common
from app.services.media import images


@pytest.fixture(autouse=True)
def _clean_breaker_state():
    with _common._blocked_hosts_lock:
        _common._per_run_blocked_hosts.clear()
        _common._host_failure_counts.clear()
        _common._failed_image_urls.clear()
        _common._provider_cooldowns.clear()
    yield
    with _common._blocked_hosts_lock:
        _common._per_run_blocked_hosts.clear()
        _common._host_failure_counts.clear()
        _common._failed_image_urls.clear()
        _common._provider_cooldowns.clear()


def test_host_blocks_after_three_failures():
    assert not _common.register_host_failure("bad.example.com")
    assert not _common.register_host_failure("bad.example.com")
    assert not _common.is_host_blocked("bad.example.com")
    assert _common.register_host_failure("bad.example.com")  # newly blocked
    assert _common.is_host_blocked("bad.example.com")


def test_success_resets_failure_streak():
    _common.register_host_failure("flaky.example.com")
    _common.register_host_failure("flaky.example.com")
    _common.register_host_success("flaky.example.com")
    assert not _common.register_host_failure("flaky.example.com")
    assert not _common.is_host_blocked("flaky.example.com")


def test_host_block_expires():
    _common.block_host("brief.example.com", ttl=0.01)
    assert _common.is_host_blocked("brief.example.com")
    time.sleep(0.02)
    assert not _common.is_host_blocked("brief.example.com")


def test_failed_url_memo():
    url = "https://cdn.example.com/img.jpg"
    assert not _common.is_failed_url(url)
    _common.mark_failed_url(url)
    assert _common.is_failed_url(url)


def test_provider_cooldown_set_and_expire():
    assert not _common.provider_on_cooldown("unsplash")
    _common.set_provider_cooldown("unsplash", 0.01)
    assert _common.provider_on_cooldown("unsplash")
    time.sleep(0.02)
    assert not _common.provider_on_cooldown("unsplash")


def test_save_image_skips_failed_url_without_network():
    url = "https://dead.example.com/photo.jpg"
    _common.mark_failed_url(url)
    assert images.save_image(url) == ""


def test_save_image_skips_lookaside_domains_without_network():
    # lookaside.* serve HTML crawler pages, never the image bytes
    assert images.save_image(
        "https://lookaside.instagram.com/seo/google_widget/crawler/?media_id=1"
    ) == ""
    assert images.save_image(
        "https://lookaside.fbsbx.com/lookaside/crawler/media/?media_id=2"
    ) == ""


def test_save_image_skips_blocked_host_without_network():
    _common.block_host("hotlink.example.com")
    assert images.save_image("https://hotlink.example.com/a.jpg") == ""


def test_unsplash_search_short_circuits_on_cooldown():
    _common.set_provider_cooldown("unsplash", 60)
    assert images.search_images_unsplash("anything") == []


def test_ddg_search_short_circuits_on_cooldown():
    _common.set_provider_cooldown("duckduckgo", 60)
    assert images.search_images_ddg("anything") == []


def test_pngtree_and_lovepik_are_watermarked_sources():
    assert images._is_watermarked_source("https://png.pngtree.com/thumb/foo.jpg")
    assert images._is_watermarked_source("https://img.lovepik.com/photo/1.jpg")
    assert images._is_watermarked_domain("pikbest.com")
