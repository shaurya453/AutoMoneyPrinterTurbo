"""Referent-swap (standalone_subject) tests — no network.

A standalone_subject sentence is about a different googleable entity than the
video's topic (e.g. an ingredient). Its primary fetch must drop the
video_topic anchor from the query ladder, the CLIP caption prompt, and the
VLM topic — while the topic-wide rescue fallback keeps the full anchor.
"""
from unittest import mock

from app.models.schema import VideoAspect
from app.services.pipeline import _fetch


_TOPIC = "tinted sunscreen for mature skin"


def _sentence(standalone: bool) -> dict:
    return {
        "text": "The formula pairs SPF 40 with niacinamide.",
        "content_track": "named",
        "entity_name": "Saie Slip Tint SPF 35",
        "standalone_subject": standalone,
        "media_type": "image",
        "visual_concepts": ["niacinamide", "skincare ingredient chart"],
        "visual_caption": "molecular structure diagram of niacinamide",
        "must_show": ["niacinamide"],
    }


def _run_fetch_clip(sentence, fallback_terms=None):
    """Run _fetch_clip with both fetchers stubbed out, capturing image-fetch args."""
    calls = []

    def fake_image_clip(*args, **kwargs):
        calls.append({
            "caption_prompt": args[7],
            "query_ladder": args[8],
            "video_topic": kwargs.get("video_topic"),
        })
        return None  # force fallthrough so every stage is exercised

    with mock.patch.object(_fetch, "_fetch_image_clip", side_effect=fake_image_clip), \
         mock.patch.object(_fetch, "_fetch_video_clip", return_value=None):
        result = _fetch._fetch_clip(
            sentence, 4.0, 0.5, "pexels", VideoAspect.landscape,
            clip_idx=0, clips_dir="/nonexistent", used_urls=set(),
            fallback_terms=fallback_terms, video_topic=_TOPIC,
        )
    assert result is None
    return calls


def test_standalone_subject_drops_topic_anchor():
    calls = _run_fetch_clip(_sentence(standalone=True))
    primary = calls[0]
    assert _TOPIC not in primary["caption_prompt"]
    assert primary["video_topic"] == ""
    assert all(_TOPIC not in rung for rung in primary["query_ladder"])
    assert any("niacinamide" in rung for rung in primary["query_ladder"])


def test_normal_sentence_keeps_topic_anchor():
    calls = _run_fetch_clip(_sentence(standalone=False))
    primary = calls[0]
    assert _TOPIC in primary["caption_prompt"]
    assert primary["video_topic"] == _TOPIC
    # thematic ladder ends with the bare-topic safety rung
    assert any(_TOPIC in rung for rung in primary["query_ladder"])


def test_rescue_fallback_restores_topic_anchor():
    calls = _run_fetch_clip(
        _sentence(standalone=True),
        fallback_terms=["skincare products shelf", "bathroom vanity"],
    )
    rescue = calls[-1]  # last image-fetch call is the topic-wide rescue
    assert rescue["video_topic"] == _TOPIC
    assert _TOPIC in rescue["caption_prompt"]
    # thematic ladders keep concepts bare; the anchor returns as the safety rung
    assert _TOPIC in rescue["query_ladder"]
