"""validate_job.py gate tests — run the script exactly as portal-worker does."""
import json
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_ROOT, "scripts", "validate_job.py")


def _run(tmp_path, job: dict):
    path = tmp_path / "job.json"
    path.write_text(json.dumps(job), encoding="utf-8")
    return subprocess.run(
        [sys.executable, _SCRIPT, str(path)],
        capture_output=True, text=True, timeout=30,
    )


def _valid_job():
    return {
        "video_script": "First sentence. Second sentence.",
        "video_topic": "test topic",
        "video_type": "thematic",
        "gapfill_terms": ["a", "b", "c", "d", "e"],
        "sentences": [
            {
                "text": "First sentence.",
                "content_track": "broll",
                "media_type": "video",
                "visual_concepts": ["one", "two", "three"],
                "visual_caption": "first caption",
                "visual_effect": "warmth",
            },
            {
                "text": "Second sentence.",
                "content_track": "broll",
                "media_type": "video",
                "visual_concepts": ["four", "five", "six"],
                "visual_caption": "second caption",
            },
        ],
    }


def test_valid_job_passes(tmp_path):
    res = _run(tmp_path, _valid_job())
    assert res.returncode == 0, res.stdout
    assert res.stdout.startswith(("VALIDATION_OK", "VALIDATION_WARNINGS"))


def test_empty_text_is_hard_error(tmp_path):
    job = _valid_job()
    job["sentences"][1]["text"] = ""
    res = _run(tmp_path, job)
    assert res.returncode == 1
    assert "VALIDATION_ERROR" in res.stdout
    assert "text field is empty" in res.stdout


def test_legacy_graphic_track_is_hard_error(tmp_path):
    job = _valid_job()
    job["sentences"][1]["content_track"] = "graphic"
    res = _run(tmp_path, job)
    assert res.returncode == 1
    assert "no longer supported" in res.stdout


def test_missing_video_script_is_hard_error(tmp_path):
    job = _valid_job()
    job["video_script"] = ""
    res = _run(tmp_path, job)
    assert res.returncode == 1
    assert "video_script" in res.stdout


def test_invalid_json_is_hard_error(tmp_path):
    path = tmp_path / "job.json"
    path.write_text("{not json", encoding="utf-8")
    res = subprocess.run(
        [sys.executable, _SCRIPT, str(path)],
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 1
    assert "JSON parse failed" in res.stdout


def test_unknown_visual_effect_warns_but_passes(tmp_path):
    job = _valid_job()
    job["sentences"][0]["visual_effect"] = "explosions"
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "unknown visual_effect" in res.stdout


def test_named_without_entity_name_warns(tmp_path):
    job = _valid_job()
    job["sentences"][0]["content_track"] = "named"
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "entity_name is missing" in res.stdout


def test_topic_sharing_no_title_words_warns(tmp_path):
    job = _valid_job()
    job["video_title"] = "We Tested 10 Tinted Sunscreens on Mature Skin"
    job["video_topic"] = "midday grease test"
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "shares no words with the title" in res.stdout


def test_topic_matching_title_does_not_warn(tmp_path):
    job = _valid_job()
    job["video_title"] = "We Tested 10 Tinted Sunscreens on Mature Skin"
    # singular "sunscreen" must match plural title token
    job["video_topic"] = "tinted sunscreen for mature skin"
    res = _run(tmp_path, job)
    assert "shares no words with the title" not in res.stdout


def test_thematic_with_many_entities_warns(tmp_path):
    job = _valid_job()
    job["video_type"] = "thematic"
    for name in ("Product A", "Product B", "Product C"):
        job["sentences"].append({
            "text": f"About {name}.",
            "content_track": "named",
            "entity_name": name,
            "media_type": "image",
            "visual_concepts": [f"{name} box"],
            "visual_caption": f"studio shot of {name}",
        })
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "should use video_type='named_entity'" in res.stdout


def test_thin_gapfill_pool_warns(tmp_path):
    job = _valid_job()  # fixture has only 5 gapfill terms
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "gapfill_terms has only 5" in res.stdout


def test_standalone_subject_on_broll_warns(tmp_path):
    job = _valid_job()
    job["sentences"][0]["standalone_subject"] = True
    job["sentences"][0]["must_show"] = ["niacinamide"]
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "standalone_subject=true but content_track" in res.stdout


def test_standalone_subject_without_must_show_warns(tmp_path):
    job = _valid_job()
    job["sentences"][0]["content_track"] = "named"
    job["sentences"][0]["entity_name"] = "Some Product"
    job["sentences"][0]["standalone_subject"] = True
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "must_show is empty" in res.stdout


def test_standalone_subject_correct_usage_does_not_warn(tmp_path):
    job = _valid_job()
    job["sentences"][0]["content_track"] = "named"
    job["sentences"][0]["entity_name"] = "Some Product"
    job["sentences"][0]["standalone_subject"] = True
    job["sentences"][0]["must_show"] = ["niacinamide"]
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "standalone_subject" not in res.stdout


def test_unknown_visual_criticality_warns(tmp_path):
    job = _valid_job()
    job["sentences"][0]["visual_criticality"] = "extreme"
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "unknown visual_criticality" in res.stdout


def test_criticality_inflation_warns(tmp_path):
    job = _valid_job()  # 2 sentences — mark both high (100% > 30%)
    for sent in job["sentences"]:
        sent["visual_criticality"] = "high"
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "high/critical" in res.stdout


def test_sparse_criticality_does_not_warn(tmp_path):
    job = _valid_job()
    job["sentences"][0]["visual_criticality"] = "low"
    res = _run(tmp_path, job)
    assert "visual_criticality" not in res.stdout.replace(
        "unknown visual_criticality", ""
    )


# ── phantom topic anchor ─────────────────────────────────────────────────────

def _named_sentence(text, concept0, entity):
    return {
        "text": text,
        "content_track": "named",
        "entity_name": entity,
        "media_type": "image",
        "visual_concepts": [concept0, "broad thing", "broader thing"],
        "visual_caption": f"caption for {concept0}",
    }


def test_phantom_anchor_warns_on_retailer_topic(tmp_path):
    job = _valid_job()
    job["video_type"] = "named_entity"
    job["video_topic"] = "Marshall handbags"
    job["video_title"] = "8 Marshall Handbags That Outlast $500 Brands"
    job["sentences"] += [
        _named_sentence("Kors line.", "Michael Kors tote", "Michael Kors signature bags"),
        _named_sentence("Nash line.", "Patricia Nash satchel", "Patricia Nash tooled leather bags"),
    ]
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "shares no words with any entity_name" in res.stdout


def test_matching_topic_does_not_warn_phantom_anchor(tmp_path):
    job = _valid_job()
    job["video_type"] = "named_entity"
    job["video_topic"] = "Bose 901 speakers"
    job["sentences"].append(
        _named_sentence("The 901.", "Bose 901 front view", "Bose 901 Series VI speakers")
    )
    res = _run(tmp_path, job)
    assert "shares no words with any entity_name" not in res.stdout


# ── slot-1/2 collapse and within-sentence duplicates ─────────────────────────

def test_slot_collapse_warns(tmp_path):
    job = _valid_job()
    # 8 sentences sharing the same visual_concepts[1] (> max(6, ceil(10/6))=6)
    for k in range(8):
        job["sentences"].append({
            "text": f"Sentence {k}.",
            "content_track": "broll",
            "media_type": "video",
            "visual_concepts": [f"unique concept {k}", "leather tote bag", f"broad {k}"],
            "visual_caption": f"caption {k}",
        })
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "visual_concepts[1] collapse" in res.stdout
    assert "leather tote bag" in res.stdout


def test_varied_slots_do_not_warn_collapse(tmp_path):
    job = _valid_job()
    res = _run(tmp_path, job)
    assert "collapse" not in res.stdout


def test_within_sentence_duplicate_concept_warns(tmp_path):
    job = _valid_job()
    job["sentences"][0]["visual_concepts"] = ["a thing", "b thing", "a thing"]
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "duplicate concept within visual_concepts" in res.stdout


# ── Avatar checks ──────────────────────────────────────────────────────────

def _avatar_job(tmp_path):
    """Valid job with a real avatar image on disk and sentences 0–1 marked."""
    job = _valid_job()
    img = tmp_path / "avatar.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")  # existence is all the gate checks
    job["avatar_image"] = str(img)
    for s in job["sentences"]:
        s["avatar"] = True
        s.pop("visual_effect", None)
    return job


def test_avatar_without_image_is_hard_error(tmp_path):
    job = _valid_job()
    job["sentences"][0]["avatar"] = True
    res = _run(tmp_path, job)
    assert res.returncode == 1
    assert "avatar_image is missing" in res.stdout


def test_avatar_happy_path_passes(tmp_path):
    res = _run(tmp_path, _avatar_job(tmp_path))
    assert res.returncode == 0, res.stdout
    assert "avatar" not in res.stdout.lower() or "VALIDATION_OK" in res.stdout


def test_avatar_image_missing_on_disk_warns(tmp_path):
    job = _avatar_job(tmp_path)
    job["avatar_image"] = str(tmp_path / "nope.png")
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "does not exist on disk" in res.stdout


def test_avatar_unmarked_sentence_zero_warns(tmp_path):
    job = _avatar_job(tmp_path)
    del job["sentences"][0]["avatar"]
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "sentence 0 lacks avatar=true" in res.stdout


def test_avatar_graphic_conflict_warns(tmp_path):
    job = _avatar_job(tmp_path)
    job["sentences"][1]["graphic_type"] = "lower_third"
    job["sentences"][1]["variables"] = {"label": "X"}
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "mutually exclusive" in res.stdout


def test_avatar_effect_conflict_warns(tmp_path):
    job = _avatar_job(tmp_path)
    job["sentences"][1]["visual_effect"] = "warmth"
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "visual_effect on an avatar sentence" in res.stdout


def test_avatar_three_blocks_warn(tmp_path):
    job = _avatar_job(tmp_path)
    extra = [dict(job["sentences"][1], visual_caption=f"c{i}") for i in range(4)]
    for i, s in enumerate(extra):
        s.pop("avatar", None)
        s["text"] = f"Extra sentence number {i}."
    # blocks: [0,1] then [3] then [5]
    extra[1]["avatar"] = True
    extra[3]["avatar"] = True
    job["sentences"].extend(extra)
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "avatar blocks found" in res.stdout


def test_avatar_long_intro_does_not_warn(tmp_path):
    # No length cap — a long intro sentence should pass cleanly, not warn.
    job = _avatar_job(tmp_path)
    job["sentences"][0]["text"] = " ".join(["word"] * 50)  # 20s at 2.5wps
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "hard-caps" not in res.stdout


def test_leaked_plug_marker_in_sentence_is_hard_error(tmp_path):
    job = _valid_job()
    job["sentences"][0]["text"] = "<<PLUG>>First sentence.<</PLUG>>"
    res = _run(tmp_path, job)
    assert res.returncode == 1
    assert "VALIDATION_ERROR" in res.stdout
    assert "marker text found" in res.stdout


def test_leaked_plug_marker_in_video_script_is_hard_error(tmp_path):
    job = _valid_job()
    job["video_script"] = "<<PLUG>>First sentence.<</PLUG>> Second sentence."
    res = _run(tmp_path, job)
    assert res.returncode == 1
    assert "VALIDATION_ERROR" in res.stdout
    assert "marker text found" in res.stdout


def test_info_callout_wrong_label_count_warns(tmp_path):
    job = _valid_job()
    job["sentences"][0]["content_track"] = "named"
    job["sentences"][0]["entity_name"] = "Widget Pro"
    job["sentences"][0].pop("visual_effect", None)
    job["sentences"][0]["graphic_type"] = "info_callout"
    job["sentences"][0]["variables"] = {"labels": ["Only two", "labels here"]}
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "variables.labels must be a list of exactly 3" in res.stdout


def test_info_callout_on_broll_warns(tmp_path):
    job = _valid_job()
    job["sentences"][0].pop("visual_effect", None)
    job["sentences"][0]["graphic_type"] = "info_callout"
    job["sentences"][0]["variables"] = {"labels": ["A", "B", "C"]}
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "info_callout used on a sentence that is not content_track='named'" in res.stdout


def test_quote_card_empty_quote_warns(tmp_path):
    job = _valid_job()
    job["sentences"][0].pop("visual_effect", None)
    job["sentences"][0]["graphic_type"] = "quote_card"
    job["sentences"][0]["variables"] = {"quote": "  ", "attribution": "Someone"}
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "quote_card variables.quote must be a non-empty string" in res.stdout


def test_timeline_card_empty_label_warns(tmp_path):
    job = _valid_job()
    job["sentences"][0].pop("visual_effect", None)
    job["sentences"][0]["graphic_type"] = "timeline_card"
    job["sentences"][0]["variables"] = {"label": ""}
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "timeline_card variables.label must be a non-empty string" in res.stdout


def test_quote_card_valid_does_not_warn_on_its_own_check(tmp_path):
    job = _valid_job()
    job["sentences"][0].pop("visual_effect", None)
    job["sentences"][0]["graphic_type"] = "quote_card"
    job["sentences"][0]["variables"] = {"quote": "A real quote.", "attribution": "Someone"}
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "quote_card variables.quote must be a non-empty string" not in res.stdout


def test_quote_card_and_timeline_card_share_non_lt_cap(tmp_path):
    job = _valid_job()
    filler = job["sentences"][1]
    sentences = [
        dict(filler, text=f"Filler sentence {i}.", visual_caption=f"filler caption {i}")
        for i in range(5)  # keep the graphic run out of the hook zone (0-4)
    ]
    for i in range(10):
        s = dict(filler)
        s["text"] = f"Sentence number {i} about the timeline."
        s["visual_caption"] = f"timeline caption {i}"
        s["graphic_type"] = "quote_card" if i % 2 == 0 else "timeline_card"
        s["variables"] = {"quote": "Q", "attribution": "A"} if i % 2 == 0 else {"label": "1999"}
        sentences.append(s)
    job["sentences"] = sentences
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "too many infographic/list/quote_card/timeline_card graphics" in res.stdout


def test_info_callout_cap_and_spacing_independent_of_other_pool(tmp_path):
    job = _valid_job()
    filler = job["sentences"][1]  # broll, no visual_effect
    sentences = [
        dict(filler, text=f"Filler sentence {i}.", visual_caption=f"filler caption {i}")
        for i in range(5)  # keep the named/info_callout run out of the hook zone (0-4)
    ]
    for i in range(10):
        s = dict(filler)
        s["text"] = f"Sentence number {i} about the product."
        s["visual_caption"] = f"product caption {i}"
        s["content_track"] = "named"
        s["entity_name"] = f"Widget {i}"
        s["graphic_type"] = "info_callout"
        s["variables"] = {"labels": ["A", "B", "C"]}
        sentences.append(s)
    job["sentences"] = sentences
    res = _run(tmp_path, job)
    assert res.returncode == 0
    assert "too many info_callout graphics" in res.stdout
    assert "info_callout graphics too close" in res.stdout
    assert "too many infographic/list graphics" not in res.stdout
