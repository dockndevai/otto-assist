"""Tests for the local speech-to-speech-grade voice loop (#109):
- semantic endpointing decision table (otto.voice.endpointer)
- ack-cache phrase enumeration + instant-ack fallback (otto.voice.acks / tts.Mouth).
"""
from otto.config import load_settings
from otto.voice.acks import all_phrases
from otto.voice.endpointer import Endpointer
from otto.voice.tts import Mouth


# ---- semantic endpointing -------------------------------------------------
def _ep():
    return Endpointer(base_ms=500, min_ms=250, max_ms=900)


def test_complete_thought_commits_fast():
    ep = _ep()
    # Terminal punctuation → snappy window, commits as soon as the short pause is met.
    d = ep.decide("what's the weather?", trailing_silence_ms=260)
    assert d.reason == "complete"
    assert d.target_silence_ms == 250
    assert d.commit is True


def test_short_imperative_without_punctuation_is_complete():
    ep = _ep()
    d = ep.decide("open chrome", trailing_silence_ms=300)
    assert d.reason == "complete"
    assert d.target_silence_ms == 250
    assert d.commit is True


def test_trailing_conjunction_stays_patient():
    ep = _ep()
    d = ep.decide("open chrome and", trailing_silence_ms=600)
    assert d.reason == "unfinished"
    assert d.target_silence_ms == 900
    assert d.commit is False                      # 600ms < 900ms → keep listening


def test_hold_phrase_waits():
    ep = _ep()
    d = ep.decide("um", trailing_silence_ms=800)
    assert d.reason == "unfinished"
    assert d.commit is False


def test_mid_quantity_uses_base_window():
    ep = _ep()
    d = ep.decide("set a timer for twenty", trailing_silence_ms=300)
    assert d.reason == "quantity"
    assert d.target_silence_ms == 500
    assert d.commit is False                      # 300ms < 500ms → wait for "twenty-five…"
    assert ep.decide("set a timer for twenty", 520).commit is True


def test_neutral_declarative_uses_base_window():
    ep = _ep()
    d = ep.decide("the build is failing on main", trailing_silence_ms=510)
    assert d.reason == "neutral"
    assert d.target_silence_ms == 500
    assert d.commit is True


def test_empty_partial_stays_patient():
    ep = _ep()
    d = ep.decide("", trailing_silence_ms=400)
    assert d.target_silence_ms == 900
    assert d.commit is False


def test_window_bounds_are_clamped():
    # min <= base <= max regardless of a nonsensical config.
    ep = Endpointer(base_ms=50, min_ms=400, max_ms=300)
    assert ep.min_ms == 400
    assert ep.max_ms == 400                        # max raised to >= min
    assert ep.base_ms == 400                        # base clamped into [min, max]


def test_should_commit_shortcut_matches_decide():
    ep = _ep()
    assert ep.should_commit("open chrome", 300) is True
    assert ep.should_commit("open chrome and", 300) is False


# ---- instant acks ---------------------------------------------------------
def test_all_phrases_nonempty_and_deduped():
    phrases = all_phrases()
    assert phrases, "ack bank must not be empty"
    assert len(phrases) == len(set(phrases))        # no duplicates
    assert "On it." in phrases


def test_ack_cache_path_is_stable_and_voice_scoped():
    s = load_settings()
    m = Mouth(s)
    p1 = m._ack_cache_path("On it.", "af_heart")
    p2 = m._ack_cache_path("On it.", "af_heart")
    p3 = m._ack_cache_path("On it.", "am_michael")
    assert p1 == p2                                 # deterministic
    assert p1 != p3                                 # per-voice key
    assert str(p1).endswith(".wav")


def test_speak_cached_falls_back_to_speak_without_cache(monkeypatch):
    s = load_settings()
    m = Mouth(s)
    m._engine = "system"                            # `say` has no cache → must fall back to speak()
    m.speak_cached("On it.")
    assert m._q.get_nowait() == "On it."            # enqueued as plain text, not a ("wav", path)
