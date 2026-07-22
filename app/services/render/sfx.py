"""One-shot mood-triggered sound effects.

Ported from vidspeed's `src/effectsLibrary.ts` (`SFX_LIBRARY`), which already
mirrors AMPT's own mood vocabulary (`_EFFECT_OVERLAYS` in `effects.py`) by
design — the same mood id names the same thing in both pipelines. Files
themselves are copied verbatim into `resource/sfx/` (loudness-normalized to
~-18 LUFS at the vidspeed source, per that project's `scripts/normalize_sfx.sh`).

vidspeed dropped `royalty`, `confusion`, and `euphoria` from its SFX grouping
(no dedicated one-shots for those moods) — those three moods draw from
`_UNIVERSAL_SFX` here instead.
"""

import os
import random
import threading
from typing import Optional

from app.utils import utils

# id -> {"file": <basename under resource/sfx/>, "volume": <relative mix level, 0..1>}
_SFX_LIBRARY: dict[str, dict] = {
    # Universal — usable under any mood
    "whoosh": {"file": "whoosh.mp3", "volume": 0.7},
    "riser": {"file": "riser.mp3", "volume": 0.7},
    "impact-boom": {"file": "impact-boom.mp3", "volume": 0.8},
    "sub-drop": {"file": "sub-drop.mp3", "volume": 0.7},
    "pop": {"file": "pop.mp3", "volume": 0.6},
    "click": {"file": "click.mp3", "volume": 0.6},
    "ding": {"file": "ding.mp3", "volume": 0.6},
    "swipe": {"file": "swipe.mp3", "volume": 0.6},
    "page-turn": {"file": "page-turn.mp3", "volume": 0.6},
    "camera-shutter": {"file": "camera-shutter.mp3", "volume": 0.7},
    # threat
    "heartbeat-thud": {"file": "heartbeat-thud.mp3", "volume": 0.8},
    "knife-schwing": {"file": "knife-schwing.mp3", "volume": 0.7},
    "alarm-blip": {"file": "alarm-blip.mp3", "volume": 0.7},
    # cold
    "ice-crack": {"file": "ice-crack.mp3", "volume": 0.8},
    "wind-gust": {"file": "wind-gust.mp3", "volume": 0.6},
    "frost-shimmer": {"file": "frost-shimmer.mp3", "volume": 0.6},
    # mystery
    "suspense-riser": {"file": "suspense-riser.mp3", "volume": 0.7},
    "low-drone-hit": {"file": "low-drone-hit.mp3", "volume": 0.7},
    "mysterious-chime": {"file": "mysterious-chime.mp3", "volume": 0.6},
    # dream
    "dreamy-shimmer": {"file": "dreamy-shimmer.mp3", "volume": 0.6},
    "harp-gliss": {"file": "harp-gliss.mp3", "volume": 0.6},
    "reverse-swell": {"file": "reverse-swell.mp3", "volume": 0.6},
    # warmth
    "warm-swell": {"file": "warm-swell.mp3", "volume": 0.6},
    "sparkle-up": {"file": "sparkle-up.mp3", "volume": 0.6},
    # revelation
    "revelation-boom": {"file": "revelation-boom.mp3", "volume": 0.8},
    "light-whoosh": {"file": "light-whoosh.mp3", "volume": 0.7},
    "epiphany-chime": {"file": "epiphany-chime.mp3", "volume": 0.6},
    # noir
    "noir-crackle": {"file": "noir-crackle.mp3", "volume": 0.6},
    "noir-rain": {"file": "noir-rain.mp3", "volume": 0.5},
    "thunder-crack": {"file": "thunder-crack.mp3", "volume": 0.8},
    # sepia
    "projector-click": {"file": "projector-click.mp3", "volume": 0.6},
    "vinyl-crackle-hit": {"file": "vinyl-crackle-hit.mp3", "volume": 0.6},
    "old-shutter": {"file": "old-shutter.mp3", "volume": 0.7},
    # nature
    "birdsong-chirp": {"file": "birdsong-chirp.mp3", "volume": 0.6},
    "leaf-rustle": {"file": "leaf-rustle.mp3", "volume": 0.5},
    "water-drop": {"file": "water-drop.mp3", "volume": 0.6},
    # tech
    "tech-blip": {"file": "tech-blip.mp3", "volume": 0.6},
    "ui-beep": {"file": "ui-beep.mp3", "volume": 0.6},
    "data-swipe": {"file": "data-swipe.mp3", "volume": 0.6},
    "hologram-hum": {"file": "hologram-hum.mp3", "volume": 0.5},
    # hacker_tech
    "keyboard-clack": {"file": "keyboard-clack.mp3", "volume": 0.6},
    "terminal-beep": {"file": "terminal-beep.mp3", "volume": 0.6},
    "access-granted": {"file": "access-granted.mp3", "volume": 0.7},
    "modem-screech": {"file": "modem-screech.mp3", "volume": 0.6},
    # urgency
    "police-siren": {"file": "police-siren.mp3", "volume": 0.7},
    "ticking-clock": {"file": "ticking-clock.mp3", "volume": 0.6},
    "alert-buzz": {"file": "alert-buzz.mp3", "volume": 0.7},
    # corporate
    "cash-register": {"file": "cash-register.mp3", "volume": 0.7},
    "coin-drop": {"file": "coin-drop.mp3", "volume": 0.7},
    "ka-ching": {"file": "ka-ching.mp3", "volume": 0.7},
    "success-chime": {"file": "success-chime.mp3", "volume": 0.6},
    "stamp-thud": {"file": "stamp-thud.mp3", "volume": 0.7},
    "notification-ping": {"file": "notification-ping.mp3", "volume": 0.6},
    # glitch_soft
    "glitch-zap": {"file": "glitch-zap.mp3", "volume": 0.7},
    "static-burst": {"file": "static-burst.mp3", "volume": 0.7},
    "signal-distort": {"file": "signal-distort.mp3", "volume": 0.6},
    # network
    "data-transfer": {"file": "data-transfer.mp3", "volume": 0.6},
    "ping-pulse": {"file": "ping-pulse.mp3", "volume": 0.6},
    "node-connect": {"file": "node-connect.mp3", "volume": 0.6},
    # toxic
    "bubbling-ooze": {"file": "bubbling-ooze.mp3", "volume": 0.6},
    "geiger-click": {"file": "geiger-click.mp3", "volume": 0.6},
    "acid-sizzle": {"file": "acid-sizzle.mp3", "volume": 0.6},
    # static_dread
    "tv-static": {"file": "tv-static.mp3", "volume": 0.7},
    "unsettling-drone": {"file": "unsettling-drone.mp3", "volume": 0.7},
    "distorsion-hit": {"file": "distorsion-hit.mp3", "volume": 0.7},
}

_UNIVERSAL_SFX: list[str] = [
    "whoosh", "riser", "impact-boom", "ding", "pop", "click",
    "swipe", "page-turn", "camera-shutter",
]

# Mood -> candidate SFX ids. Moods absent here (royalty, confusion, euphoria —
# vidspeed carries no dedicated one-shots for them either) fall back to
# _UNIVERSAL_SFX in pick_sfx_for_mood().
_MOOD_SFX: dict[str, list[str]] = {
    "threat": ["heartbeat-thud", "knife-schwing", "alarm-blip"],
    "cold": ["ice-crack", "wind-gust", "frost-shimmer"],
    "mystery": ["suspense-riser", "low-drone-hit", "mysterious-chime"],
    "dream": ["dreamy-shimmer", "harp-gliss", "reverse-swell"],
    "warmth": ["warm-swell", "sparkle-up"],
    "revelation": ["revelation-boom", "light-whoosh", "epiphany-chime"],
    "noir": ["noir-crackle", "noir-rain", "thunder-crack"],
    "sepia": ["projector-click", "vinyl-crackle-hit", "old-shutter"],
    "nature": ["birdsong-chirp", "leaf-rustle", "water-drop"],
    "tech": ["tech-blip", "ui-beep", "data-swipe", "hologram-hum"],
    "hacker_tech": ["keyboard-clack", "terminal-beep", "access-granted", "modem-screech"],
    "urgency": ["police-siren", "ticking-clock", "alert-buzz"],
    "corporate": ["cash-register", "coin-drop", "ka-ching", "success-chime", "stamp-thud", "notification-ping"],
    "glitch_soft": ["glitch-zap", "static-burst", "signal-distort"],
    "network": ["data-transfer", "ping-pulse", "node-connect"],
    "toxic": ["bubbling-ooze", "geiger-click", "acid-sizzle"],
    "static_dread": ["tv-static", "unsettling-drone", "distorsion-hit"],
}

_last_sfx_id: str = ""
_sfx_pick_lock = threading.Lock()


def sfx_dir() -> str:
    return os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "resource", "sfx")
    )


def pick_sfx_for_mood(mood: str, rng: random.Random = random) -> Optional[dict]:
    """Pick a one-shot SFX id for a mood, avoiding an immediate repeat.

    Returns {"id", "path", "volume"} or None if the mood has no candidates
    and the universal pool is somehow empty too (never happens in practice —
    kept defensive to mirror _pick_animation()'s single-entry-pool fallback).
    """
    global _last_sfx_id
    pool = _MOOD_SFX.get(mood) or _UNIVERSAL_SFX
    with _sfx_pick_lock:
        candidates = [c for c in pool if c != _last_sfx_id]
        if not candidates:
            candidates = pool  # single-entry pool: allow repeat rather than skip
        if not candidates:
            return None
        choice = rng.choice(candidates)
        _last_sfx_id = choice

    entry = _SFX_LIBRARY[choice]
    path = os.path.join(sfx_dir(), entry["file"])
    if not os.path.exists(path):
        return None
    return {"id": choice, "path": path, "volume": entry["volume"]}
