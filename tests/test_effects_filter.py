"""Overlay blend filter: fades and pad must use the blend-identity color."""
from app.services.render.effects import _build_overlay_filter, _grade_chain_str


def _fades(filter_complex):
    return [p for p in filter_complex.replace(";", ",").split(",") if p.startswith("fade=")]


def test_multiply_fades_to_white():
    fc = _build_overlay_filter("multiply", 1.0, 1920, 1080, accent=4.0, fade=0.5, clip_dur=6.0)
    fades = _fades(fc)
    assert len(fades) == 2
    assert all("color=white" in f for f in fades)
    # Identity pad stays white too.
    assert "color=c=white" in fc


def test_screen_fades_to_black():
    fc = _build_overlay_filter("screen", 1.0, 1920, 1080, accent=4.0, fade=0.5, clip_dur=6.0)
    fades = _fades(fc)
    assert len(fades) == 2
    assert all("color=black" in f for f in fades)
    assert "color=c=black" in fc


def test_no_pad_when_overlay_covers_clip():
    fc = _build_overlay_filter("screen", 1.0, 1920, 1080, accent=6.0, fade=0.5, clip_dur=6.0)
    assert "concat" not in fc
    assert "[_ov_trimmed]" in fc


def test_blend_mode_and_opacity_in_graph():
    fc = _build_overlay_filter("multiply", 0.8, 1280, 720, accent=3.0, fade=0.4, clip_dur=5.0)
    assert "blend=all_mode=multiply:all_opacity=0.8" in fc
    assert "scale=1280:720" in fc


def test_grade_chain_empty_when_nothing_requested():
    assert _grade_chain_str("", 0.0, 0.0) == ""
    assert _grade_chain_str("not-a-real-grade", 0.0, 0.0) == ""


def test_grade_chain_includes_requested_pieces():
    chain = _grade_chain_str("sepia", 0.5, 0.8)
    assert "colorchannelmixer" in chain
    assert "noise=alls=" in chain
    assert "vignette=angle=" in chain


def test_grade_chain_grain_only():
    chain = _grade_chain_str("", 1.0, 0.0)
    assert chain.startswith("noise=alls=")
    assert "vignette" not in chain


def test_build_overlay_filter_fuses_post_chain():
    fc = _build_overlay_filter(
        "screen", 1.0, 1920, 1080, accent=4.0, fade=0.5, clip_dur=6.0,
        post_chain="hue=s=0",
    )
    assert "[_blended]" in fc
    assert fc.strip().endswith("[_blended]hue=s=0[out]")
