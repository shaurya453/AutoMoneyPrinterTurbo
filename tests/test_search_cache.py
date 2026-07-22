"""Unit tests for the provider search-response cache (_search_cache.py)."""
import pytest

from app.config import config
from app.services.media import _search_cache


@pytest.fixture(autouse=True)
def _isolated_cache_db(tmp_path, monkeypatch):
    """Point the cache at a per-test SQLite file so tests never touch the
    real storage/search_cache.sqlite3, and reset the cached connection."""
    db_path = str(tmp_path / "search_cache.sqlite3")
    monkeypatch.setattr(_search_cache, "_db_path", lambda: db_path)
    _search_cache._conn = None
    config.app["search_cache_enabled"] = True
    config.app["search_cache_ttl_seconds"] = 86400
    yield
    _search_cache._conn = None
    config.app.pop("search_cache_enabled", None)
    config.app.pop("search_cache_ttl_seconds", None)


def test_cache_hit_avoids_second_fetch():
    calls = []

    def fetch():
        calls.append(1)
        return {"result": "live"}

    first = _search_cache.cached_get_json("nasa", "https://example.com/search?q=moon", fetch)
    second = _search_cache.cached_get_json("nasa", "https://example.com/search?q=moon", fetch)
    assert first == second == {"result": "live"}
    assert len(calls) == 1


def test_different_urls_do_not_collide():
    fetch_a = lambda: {"v": "a"}
    fetch_b = lambda: {"v": "b"}
    a = _search_cache.cached_get_json("nasa", "https://example.com/search?q=moon", fetch_a)
    b = _search_cache.cached_get_json("nasa", "https://example.com/search?q=mars", fetch_b)
    assert a == {"v": "a"}
    assert b == {"v": "b"}


def test_different_providers_do_not_collide_on_same_url():
    fetch_a = lambda: {"v": "a"}
    fetch_b = lambda: {"v": "b"}
    a = _search_cache.cached_get_json("nasa", "https://example.com/search", fetch_a)
    b = _search_cache.cached_get_json("archive_org", "https://example.com/search", fetch_b)
    assert a == {"v": "a"}
    assert b == {"v": "b"}


def test_ttl_expiry_forces_refetch(monkeypatch):
    config.app["search_cache_ttl_seconds"] = 0.01
    calls = []

    def fetch():
        calls.append(1)
        return {"n": len(calls)}

    first = _search_cache.cached_get_json("nasa", "https://example.com/x", fetch)
    import time
    time.sleep(0.05)
    second = _search_cache.cached_get_json("nasa", "https://example.com/x", fetch)
    assert first == {"n": 1}
    assert second == {"n": 2}
    assert len(calls) == 2


def test_disabled_cache_always_calls_fetch():
    config.app["search_cache_enabled"] = False
    calls = []

    def fetch():
        calls.append(1)
        return {"n": len(calls)}

    _search_cache.cached_get_json("nasa", "https://example.com/x", fetch)
    _search_cache.cached_get_json("nasa", "https://example.com/x", fetch)
    assert len(calls) == 2


def test_secret_query_params_redacted_from_key():
    # Same logical query, different rotated API keys -> same cache entry.
    calls = []

    def fetch():
        calls.append(1)
        return {"n": len(calls)}

    _search_cache.cached_get_json("pixabay", "https://pixabay.com/api/?q=cat&key=AAA", fetch)
    _search_cache.cached_get_json("pixabay", "https://pixabay.com/api/?q=cat&key=BBB", fetch)
    assert len(calls) == 1


def test_post_body_is_part_of_the_key():
    fetch_a = lambda: {"v": "a"}
    fetch_b = lambda: {"v": "b"}
    a = _search_cache.cached_post_json(
        "serper", "https://google.serper.dev/images", {"q": "cats"}, fetch_a
    )
    b = _search_cache.cached_post_json(
        "serper", "https://google.serper.dev/images", {"q": "dogs"}, fetch_b
    )
    assert a == {"v": "a"}
    assert b == {"v": "b"}


def test_cache_read_failure_falls_through_to_live_fetch(monkeypatch):
    def broken_conn():
        raise RuntimeError("db is on fire")

    monkeypatch.setattr(_search_cache, "_get_conn", broken_conn)
    result = _search_cache.cached_get_json("nasa", "https://example.com/x", lambda: {"ok": True})
    assert result == {"ok": True}
