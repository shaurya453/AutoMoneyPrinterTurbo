"""Overlay blend filter: fades and pad must use the blend-identity color."""
from app.services.render.effects import _build_overlay_filter


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
