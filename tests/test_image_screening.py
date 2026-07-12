"""Web-image junk screening + lexical pre-rank tests — pure functions, no network."""
from app.services.media import images


def _r(image="https://cdn.example.com/x.jpg", title="", source="", url=""):
    return {"image": image, "title": title, "source": source, "url": url}


# ── _junk_screen ─────────────────────────────────────────────────────────────

def test_instagram_lookaside_blocked():
    r = _r(image="https://lookaside.instagram.com/seo/google_widget/crawler/?media_id=1")
    assert images._junk_screen(r, "leather purse") == "blocked"


def test_tiktok_and_ytimg_blocked():
    assert images._junk_screen(_r(image="https://www.tiktok.com/api/img/?itemId=1"), "q") == "blocked"
    assert images._junk_screen(_r(image="https://i.ytimg.com/vi/abc/maxresdefault.jpg"), "q") == "blocked"


def test_cartoon_vector_title_junked():
    r = _r(title="Michael Kors tote cartoon vector illustration")
    assert images._junk_screen(r, "Michael Kors tote") == "junk_title"


def test_logo_keyword_exempt_when_query_asks_for_logo():
    r = _r(title="Dooney & Bourke logo on leather")
    assert images._junk_screen(r, "Dooney Bourke logo") == ""
    assert images._junk_screen(r, "Dooney Bourke tote") == "junk_title"


def test_clean_result_passes():
    r = _r(title="brown leather tote bag on table", source="example.com")
    assert images._junk_screen(r, "leather tote bag") == ""


def test_amazon_penalized_not_blocked():
    r = _r(image="https://m.media-amazon.com/images/I/81QqWFxF5LL.jpg")
    assert images._junk_screen(r, "q") == ""  # not blocked...
    assert images._is_penalized_domain("https://m.media-amazon.com/images/I/81QqWFxF5LL.jpg")


# ── _rank_results ────────────────────────────────────────────────────────────

def test_rank_token_hit_ranks_first():
    results = [
        _r(image="u1", title="random scenery"),
        _r(image="u2", title="Fossil leather tote product shot"),
    ]
    ranked = images._rank_results(results, ["fossil", "tote"], [], 2)
    assert ranked[0]["image"] == "u2"


def test_avoid_token_in_title_dropped():
    results = [
        _r(image="u1", title="peeling faux leather bag"),
        _r(image="u2", title="pristine leather bag"),
    ]
    ranked = images._rank_results(results, [], ["peeling"], 2)
    assert [r["image"] for r in ranked] == ["u2"]


def test_penalized_domain_sorts_behind_clean_domain():
    results = [
        _r(image="https://m.media-amazon.com/x.jpg", title="leather tote"),
        _r(image="https://brandsite.com/x.jpg", title="leather tote"),
    ]
    ranked = images._rank_results(results, ["tote"], [], 2)
    assert ranked[0]["image"] == "https://brandsite.com/x.jpg"


def test_ties_keep_search_order_and_truncate():
    results = [_r(image=f"u{i}", title="same title") for i in range(5)]
    ranked = images._rank_results(results, [], [], 3)
    assert [r["image"] for r in ranked] == ["u0", "u1", "u2"]


def test_no_tokens_is_orderpreserving_passthrough():
    results = [_r(image="u1"), _r(image="u2")]
    ranked = images._rank_results(results, [], [], 5)
    assert [r["image"] for r in ranked] == ["u1", "u2"]
