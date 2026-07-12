"""Portrait fade must use the sub-pixel zoompan recipe, not integer scale-grow."""
import shutil
import subprocess
import types

import pytest
from PIL import Image

from app.services.render import ken_burns


def _portrait_img(tmp_path, w=120, h=160):
    p = str(tmp_path / "portrait.png")
    img = Image.new("RGB", (w, h))
    for y in range(h):  # gradient so encoded frames aren't degenerate
        for x in range(0, w, 4):
            img.putpixel((x, y), (x % 256, y % 256, 128))
    img.save(p)
    return p


def test_fade_filtergraph_uses_zoompan_not_integer_grow(tmp_path, monkeypatch):
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        # Satisfy the "output exists" check without encoding anything.
        out = cmd[-1]
        with open(out, "wb") as f:
            f.write(b"x")
        return types.SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(ken_burns.subprocess, "run", fake_run)
    out = str(tmp_path / "fade.mp4")
    result = ken_burns._render_ken_burns_ffmpeg(
        _portrait_img(tmp_path), 1.0, 192, 108, out,
        frame_scale=0.95, animation="fade",
    )
    assert result == out
    vf = " ".join(captured["cmd"])
    assert "zoompan" in vf
    assert "fade=t=in" in vf and "fade=t=out" in vf
    # The blocky per-frame integer grow must be gone.
    assert "eval=frame" not in vf
    assert "trunc(" not in vf


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_fade_smoke_render(tmp_path):
    out = str(tmp_path / "fade.mp4")
    result = ken_burns._render_ken_burns_ffmpeg(
        _portrait_img(tmp_path), 0.5, 192, 108, out,
        frame_scale=0.95, animation="fade",
    )
    assert result == out
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", out],
        capture_output=True, text=True, timeout=30,
    )
    assert abs(float(probe.stdout.strip()) - 0.5) < 0.1
