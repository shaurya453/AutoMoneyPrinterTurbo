"""Per-rung provider routing + criticality-aware video VLM gate — no network."""
from app.services.media import images
from app.services.pipeline import _fetch
from app.services.pipeline._planning import build_query_plan
from app.services.scoring import vlm


# ── build_query_plan pool tagging ────────────────────────────────────────────

def test_named_entity_brand_rungs_tagged_web():
    plan = build_query_plan(
        "designer leather handbags",
        ["Michael Kors Jet Set tote", "leather tote bag", "woman shopping purses"],
        "named_entity",
        entity_name="Michael Kors Jet Set tote",
    )
    pools = dict(plan)
    assert pools["Michael Kors Jet Set tote"] == "web"
    assert pools["Michael Kors Jet Set tote designer leather handbags"] == "web"
    assert pools["leather tote bag"] == "stock"
    assert pools["woman shopping purses"] == "stock"


def test_concept0_web_even_without_entity_overlap():
    # Rotation rule: [0] may be a founder/accessory whose tokens don't
    # overlap entity_name — still a proper noun only web search can find.
    plan = build_query_plan(
        "hifi speakers", ["Amar Bose portrait", "vintage speakers"], "named_entity",
        entity_name="Bose 901 Series VI",
    )
    pools = dict(plan)
    assert pools["Amar Bose portrait"] == "web"
    assert pools["vintage speakers"] == "stock"


def test_thematic_all_any():
    plan = build_query_plan("city life", ["street crowd", "traffic"], "thematic")
    assert all(pool == "any" for _, pool in plan)


def test_named_entity_without_entity_name_defaults_open():
    plan = build_query_plan("some topic", ["brand thing", "broad thing"], "named_entity")
    pools = dict(plan)
    assert pools["brand thing"] == "web"    # concept[0] contract holds regardless
    assert pools["broad thing"] == "any"    # no entity signal → don't restrict


def test_ladder_shim_matches_plan_terms():
    from app.services.pipeline._planning import _build_query_ladder
    args = ("topic words", ["c one", "c two"], "named_entity")
    assert _build_query_ladder(*args) == [t for t, _ in build_query_plan(*args)]


# ── download_image provider restriction ──────────────────────────────────────

def _run_with_stubs(monkeypatch, tmp_path, term_routing, source_order):
    calls = []

    def _mk(name):
        def _p(query, n=6, rank_tokens=None, avoid_tokens=None):
            calls.append(name)
            return []
        return _p

    monkeypatch.setattr(images, "_IMAGE_PROVIDERS", {"serper": _mk("serper"), "pexels": _mk("pexels")})
    images.download_image(
        search_terms=["brand term"],
        source_order=source_order,
        term_routing=term_routing,
    )
    return calls


def test_web_term_never_queries_stock(monkeypatch, tmp_path):
    calls = _run_with_stubs(monkeypatch, tmp_path, {"brand term": "web"}, ["serper", "pexels"])
    assert "serper" in calls and "pexels" not in calls


def test_stock_term_never_queries_web(monkeypatch, tmp_path):
    calls = _run_with_stubs(monkeypatch, tmp_path, {"brand term": "stock"}, ["serper", "pexels"])
    assert "pexels" in calls and "serper" not in calls


def test_empty_intersection_falls_back_to_full_order(monkeypatch, tmp_path):
    # "web" restriction with a stock-only source_order must not starve the term.
    calls = _run_with_stubs(monkeypatch, tmp_path, {"brand term": "web"}, ["pexels"])
    assert calls == ["pexels"]


def test_unrouted_term_uses_full_order(monkeypatch, tmp_path):
    calls = _run_with_stubs(monkeypatch, tmp_path, {}, ["serper", "pexels"])
    assert set(calls) == {"serper", "pexels"}


# ── video VLM threshold selection ────────────────────────────────────────────

def test_generic_broll_gets_loose_gate():
    assert _fetch._video_vlm_threshold({"content_track": "broll"}) == 0.35


def test_named_track_keeps_strict_gate():
    t = _fetch._video_vlm_threshold({"content_track": "named"})
    assert t >= 0.5


def test_must_show_keeps_strict_gate():
    t = _fetch._video_vlm_threshold({"content_track": "broll", "must_show": ["thing"]})
    assert t >= 0.5


def test_high_criticality_keeps_strict_gate():
    t = _fetch._video_vlm_threshold({"content_track": "broll", "visual_criticality": "high"})
    assert t >= 0.5


def test_passes_explicit_threshold_and_fail_open():
    assert vlm.passes(0.4, threshold=0.35) is True
    assert vlm.passes(0.3, threshold=0.35) is False
    assert vlm.passes(None, threshold=0.35) is True
