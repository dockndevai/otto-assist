"""Tests for the local speech-to-speech-grade voice loop (#109):
- semantic endpointing decision table (otto.voice.endpointer)
- ack-cache phrase enumeration + instant-ack fallback (otto.voice.acks / tts.Mouth).
"""
from otto.config import load_settings
from otto.voice.acks import all_phrases, backchannels
from otto.voice.endpointer import Endpointer
from otto.voice.tts import Mouth
from otto.voice.turn_model import (
    SmartTurnModel,
    build_turn_model,
    prob_to_end_silence,
    resolve_model_path,
)


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


# ---- learned turn detection (Smart Turn v3) -------------------------------
def test_prob_to_end_silence_thresholds():
    kw = dict(min_ms=250, base_ms=500, max_ms=900, high=0.65, low=0.35)
    assert prob_to_end_silence(0.9, **kw) == (250, "complete")     # confident finish → snappy
    assert prob_to_end_silence(0.1, **kw) == (900, "unfinished")   # mid-thought → patient
    assert prob_to_end_silence(0.5, **kw) == (500, "neutral")      # ambiguous → base
    assert prob_to_end_silence(0.65, **kw)[1] == "complete"        # boundary is inclusive (>= high)
    assert prob_to_end_silence(0.35, **kw)[1] == "unfinished"      # boundary is inclusive (<= low)
    # Clamps out-of-range probabilities instead of misclassifying them.
    assert prob_to_end_silence(5.0, **kw) == (250, "complete")
    assert prob_to_end_silence(-1.0, **kw) == (900, "unfinished")


def test_resolve_model_path_off_and_missing_fall_back_to_regex():
    s = load_settings()
    s.voice.stt.turn_model = "off"
    assert resolve_model_path(s) is None                 # off → no model → regex endpointer
    s.voice.stt.turn_model = "/no/such/model/file.onnx"  # explicit but missing
    assert resolve_model_path(s) is None
    assert build_turn_model(s) is None


def test_resolve_model_path_explicit_existing(tmp_path):
    s = load_settings()
    f = tmp_path / "smart-turn-v3.onnx"
    f.write_bytes(b"\x00")                               # resolve only checks existence
    s.voice.stt.turn_model = str(f)
    assert resolve_model_path(s) == f
    m = build_turn_model(s)
    assert isinstance(m, SmartTurnModel)
    assert m.min_ms == s.voice.stt.endpoint_min_ms       # windows come from the endpoint_* config


def test_smart_turn_model_bad_file_is_safe(tmp_path):
    f = tmp_path / "bad.onnx"
    f.write_bytes(b"not a real onnx model")
    m = SmartTurnModel(f, min_ms=250, base_ms=500, max_ms=900)
    assert m.load() is False                             # unloadable → disabled, no raise
    assert m.ok is False
    assert m.predict_prob(b"\x00\x00" * 8000) is None    # never raises into the audio path
    assert m.target_for(b"\x00\x00" * 8000) is None


# ---- backchannels ---------------------------------------------------------
def test_backchannels_present_and_short():
    bc = backchannels()
    assert bc, "backchannel bank must not be empty"
    assert "Mm-hmm." in bc
    # Backchannels must be tiny so they don't step on the speaker (<= 3 words).
    assert all(len(p.split()) <= 3 for p in bc)


def test_backchannels_are_cached_with_the_ack_bank():
    # They ride the instant-ack WAV cache, so all_phrases() must include them (deduped).
    phrases = all_phrases()
    assert set(backchannels()).issubset(set(phrases))
    assert len(phrases) == len(set(phrases))
