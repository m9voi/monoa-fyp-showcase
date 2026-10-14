"""Product Completion Batch 03 — Mo voice input: POST /api/mo/transcribe.

Voice is ONLY an input method. This file tests the transcription endpoint in
isolation (auth, validation, bounds, provider success/failure, no leakage, no
persistence) — never the downstream /api/process-input pipeline, which is
unchanged and already covered by tests/test_mo_hitl.py etc. No real paid
transcription call is made anywhere here; the OpenAI boundary (`main.ai_client`)
is always faked or set to None.
"""
import base64
import os

import main
import db as db_layer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), "r", encoding="utf-8") as fh:
        return fh.read()


class _FakeTranscript:
    def __init__(self, text):
        self.text = text


class _FakeAudioTranscriptions:
    def __init__(self, text="Add a task to buy milk", error=None):
        self._text = text
        self._error = error
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self._error is not None:
            raise self._error
        return _FakeTranscript(self._text)


class _FakeAudio:
    def __init__(self, **kw):
        self.transcriptions = _FakeAudioTranscriptions(**kw)


class _FakeVoiceClient:
    def __init__(self, **kw):
        self.audio = _FakeAudio(**kw)


def _b64(raw: bytes = b"FAKEAUDIOBYTES-not-real-audio-content") -> str:
    return base64.b64encode(raw).decode("ascii")


def _table_row_counts(conn):
    tables = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence'"
        ).fetchall()
    ]
    return {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def test_unauthenticated_transcription_rejected(client):
    r = client.post("/api/mo/transcribe", json={"audio_base64": _b64(), "mime_type": "audio/webm"})
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def test_malformed_base64_rejected(alice):
    tc, _ = alice
    r = tc.post("/api/mo/transcribe", json={"audio_base64": "not-valid-base64!!!", "mime_type": "audio/webm"})
    assert r.status_code == 422


def test_empty_audio_string_rejected(alice):
    tc, _ = alice
    r = tc.post("/api/mo/transcribe", json={"audio_base64": "", "mime_type": "audio/webm"})
    assert r.status_code == 422


def test_whitespace_only_audio_rejected(alice):
    tc, _ = alice
    r = tc.post("/api/mo/transcribe", json={"audio_base64": "   ", "mime_type": "audio/webm"})
    assert r.status_code == 422


def test_valid_base64_of_zero_bytes_rejected(alice):
    tc, _ = alice
    r = tc.post("/api/mo/transcribe", json={"audio_base64": _b64(b""), "mime_type": "audio/webm"})
    assert r.status_code == 422


def test_unsupported_mime_type_rejected(alice):
    tc, _ = alice
    r = tc.post("/api/mo/transcribe", json={"audio_base64": _b64(), "mime_type": "video/mp4"})
    assert r.status_code == 422


def test_missing_mime_type_rejected(alice):
    tc, _ = alice
    r = tc.post("/api/mo/transcribe", json={"audio_base64": _b64(), "mime_type": ""})
    assert r.status_code == 422


def test_every_allowed_mime_type_passes_validation(alice, monkeypatch):
    tc, _ = alice
    fake = _FakeVoiceClient()
    monkeypatch.setattr(main, "ai_client", fake)
    for mime in main.MO_TRANSCRIBE_ALLOWED_MIME_TYPES:
        r = tc.post("/api/mo/transcribe", json={"audio_base64": _b64(), "mime_type": mime})
        assert r.status_code == 200, (mime, r.text)


def test_oversized_base64_string_rejected(alice):
    tc, _ = alice
    huge = "A" * (main._MO_TRANSCRIBE_MAX_B64_CHARS + 1000)
    r = tc.post("/api/mo/transcribe", json={"audio_base64": huge, "mime_type": "audio/webm"})
    assert r.status_code == 413


def test_oversized_decoded_audio_rejected(alice, monkeypatch):
    tc, _ = alice
    # Shrink the ceiling so the test doesn't need to build a real 8MB body.
    monkeypatch.setattr(main.sec, "MO_TRANSCRIBE_MAX_AUDIO_BYTES", 100)
    r = tc.post("/api/mo/transcribe", json={"audio_base64": _b64(b"x" * 500), "mime_type": "audio/webm"})
    assert r.status_code == 413


# --------------------------------------------------------------------------- #
# Success path (mocked provider — no real transcription call)
# --------------------------------------------------------------------------- #
def test_successful_transcription_returns_the_transcript(alice, monkeypatch):
    tc, _ = alice
    fake = _FakeVoiceClient(text="Spent RM12 on lunch")
    monkeypatch.setattr(main, "ai_client", fake)
    r = tc.post("/api/mo/transcribe", json={"audio_base64": _b64(), "mime_type": "audio/webm;codecs=opus"})
    assert r.status_code == 200, r.text
    assert r.json() == {"transcript": "Spent RM12 on lunch"}


def test_ios_webkit_style_codec_param_passes_validation(alice, monkeypatch):
    # WebKit/iOS Safari reports MediaRecorder.mimeType / Blob.type with a
    # space after ";" and quoted codec values (e.g. `audio/mp4;
    # codecs="mp4a.40.2"`), unlike the compact `audio/mp4;codecs=mp4a.40.2`
    # form. An exact-string allowlist rejected this with a 422 (surfaced to
    # the client as the same generic "couldn't transcribe" message as every
    # other failure) even though the base type is fully supported. The
    # validation must match on the base type only, ignoring any codecs
    # parameter's exact formatting.
    tc, _ = alice
    fake = _FakeVoiceClient(text="What's my spending summary?")
    monkeypatch.setattr(main, "ai_client", fake)
    r = tc.post(
        "/api/mo/transcribe",
        json={"audio_base64": _b64(), "mime_type": 'audio/mp4; codecs="mp4a.40.2"'},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"transcript": "What's my spending summary?"}
    # the OpenAI SDK call must receive the clean base type, not the raw
    # WebKit-formatted string, for both the filename extension and the
    # declared content type.
    filename, _content, content_type = fake.audio.transcriptions.last_kwargs["file"]
    assert filename == "voice.mp4"
    assert content_type == "audio/mp4"


def test_successful_transcription_trims_whitespace(alice, monkeypatch):
    tc, _ = alice
    fake = _FakeVoiceClient(text="  hello there  \n")
    monkeypatch.setattr(main, "ai_client", fake)
    r = tc.post("/api/mo/transcribe", json={"audio_base64": _b64(), "mime_type": "audio/wav"})
    assert r.json()["transcript"] == "hello there"


def test_empty_speech_result_returns_empty_transcript_not_an_error(alice, monkeypatch):
    tc, _ = alice
    fake = _FakeVoiceClient(text="")
    monkeypatch.setattr(main, "ai_client", fake)
    r = tc.post("/api/mo/transcribe", json={"audio_base64": _b64(), "mime_type": "audio/mp4"})
    assert r.status_code == 200
    assert r.json()["transcript"] == ""


def test_transcription_call_uses_the_real_audio_bytes_and_configured_model(alice, monkeypatch):
    tc, _ = alice
    fake = _FakeVoiceClient()
    monkeypatch.setattr(main, "ai_client", fake)
    raw = b"distinct-marker-bytes-12345"
    tc.post("/api/mo/transcribe", json={"audio_base64": _b64(raw), "mime_type": "audio/mp4"})
    sent = fake.audio.transcriptions.last_kwargs
    assert sent["model"] == main.MO_TRANSCRIBE_MODEL
    filename, sent_bytes, sent_mime = sent["file"]
    assert sent_bytes == raw
    assert sent_mime == "audio/mp4"


# --------------------------------------------------------------------------- #
# Upstream failure — safe error, no leakage
# --------------------------------------------------------------------------- #
def test_upstream_provider_failure_returns_safe_502(alice, monkeypatch):
    tc, _ = alice
    fake = _FakeVoiceClient(error=RuntimeError("upstream boom: sk-fakeSecretKeyLeakTest12345"))
    monkeypatch.setattr(main, "ai_client", fake)
    r = tc.post("/api/mo/transcribe", json={"audio_base64": _b64(), "mime_type": "audio/webm"})
    assert r.status_code == 502
    body_text = r.text.lower()
    assert "sk-fakesecretkeyleaktest12345" not in body_text
    assert "upstream boom" not in body_text
    assert "runtimeerror" not in body_text


def test_no_ai_client_configured_returns_safe_503(alice, monkeypatch):
    tc, _ = alice
    monkeypatch.setattr(main, "ai_client", None)
    r = tc.post("/api/mo/transcribe", json={"audio_base64": _b64(), "mime_type": "audio/webm"})
    assert r.status_code == 503


# --------------------------------------------------------------------------- #
# Privacy: no persistence, nothing written anywhere in the DB
# --------------------------------------------------------------------------- #
def test_transcription_writes_nothing_to_the_database(alice, monkeypatch):
    tc, _ = alice
    fake = _FakeVoiceClient(text="temporary check")
    monkeypatch.setattr(main, "ai_client", fake)

    conn = db_layer.get_connection()
    before = _table_row_counts(conn)
    conn.close()

    tc.post("/api/mo/transcribe", json={"audio_base64": _b64(), "mime_type": "audio/webm"})

    conn = db_layer.get_connection()
    after = _table_row_counts(conn)
    conn.close()
    assert before == after


def test_a_failed_transcription_also_writes_nothing(alice, monkeypatch):
    tc, _ = alice
    fake = _FakeVoiceClient(error=RuntimeError("boom"))
    monkeypatch.setattr(main, "ai_client", fake)

    conn = db_layer.get_connection()
    before = _table_row_counts(conn)
    conn.close()

    tc.post("/api/mo/transcribe", json={"audio_base64": _b64(), "mime_type": "audio/webm"})

    conn = db_layer.get_connection()
    after = _table_row_counts(conn)
    conn.close()
    assert before == after


# --------------------------------------------------------------------------- #
# Voice does not bypass the existing pipeline — the transcript is just text
# --------------------------------------------------------------------------- #
def test_transcript_is_not_auto_sent_to_process_input(alice, monkeypatch):
    """The transcribe endpoint must never itself call /api/process-input or
    create a proposal — it only returns text. Confirmed by checking no
    mo_proposals row exists after a transcription."""
    tc, _ = alice
    fake = _FakeVoiceClient(text="Spent RM12 on lunch")
    monkeypatch.setattr(main, "ai_client", fake)
    tc.post("/api/mo/transcribe", json={"audio_base64": _b64(), "mime_type": "audio/webm"})

    conn = db_layer.get_connection()
    n = conn.execute("SELECT count(*) FROM mo_proposals").fetchone()[0]
    conn.close()
    assert n == 0


def test_returned_transcript_can_be_sent_through_the_real_existing_pipeline(alice, monkeypatch):
    """End-to-end proof of the architecture rule: transcribe -> take the
    returned text -> POST it to the UNCHANGED /api/process-input exactly like
    typed input -> the existing proposal flow runs normally."""
    tc, _ = alice

    transcribe_client = _FakeVoiceClient(text="spent RM20 on food")
    monkeypatch.setattr(main, "ai_client", transcribe_client)
    r = tc.post("/api/mo/transcribe", json={"audio_base64": _b64(), "mime_type": "audio/webm"})
    transcript = r.json()["transcript"]
    assert transcript == "spent RM20 on food"

    def fake_interpret(_text, context_date=None, context_entity=None, user_interests=None):
        return main.MoInterpretation(
            mode="single_operation",
            operations=[main.MoOperation(
                intent=main.IntentEnum("CREATE_TRANSACTION"),
                parameters={"amount": 20, "type": "expense", "description": "food",
                            "category": "Food", "date": None},
            )],
        )
    monkeypatch.setattr(main.AIService, "interpret", staticmethod(fake_interpret))

    r2 = tc.post("/api/process-input", json={"user_input": transcript})
    assert r2.status_code == 200, r2.text
    assert r2.json()["kind"] == "proposal_required"
    assert r2.json()["proposal"]["action_type"] == "create_transaction"


# --------------------------------------------------------------------------- #
# Frontend wiring (source-string checks — see tests/test_profile.py's module #
# docstring for why: this stack has no DOM/React renderer for `node --test`, #
# so page/component WIRING is verified by reading the real .tsx/.ts source.  #
# --------------------------------------------------------------------------- #
_OMNIBAR = _read("app", "components", "OmniBar.tsx")
_USE_VOICE = _read("app", "components", "useVoiceInput.ts")
_VOICE_LIB = _read("app", "lib", "voiceRecording.ts")


def test_omnibar_wires_the_mic_button_to_the_real_voice_hook():
    assert 'from "./useVoiceInput"' in _OMNIBAR
    assert "useVoiceInput(onTranscribed)" in _OMNIBAR


def test_transcribed_text_lands_in_the_same_input_state_typed_text_uses():
    # The ONE architecture rule: no second Mo pipeline. A transcription
    # result must become plain `input` state, not a direct send.
    on_transcribed_fn = _OMNIBAR[_OMNIBAR.index("const onTranscribed ="):]
    body = on_transcribed_fn[:on_transcribed_fn.index("}, [])")]
    assert "setInput(text)" in body
    assert "apiFetch" not in body  # never sends on its own


def test_handle_submit_still_posts_to_the_unchanged_process_input_path():
    # Regression proof: adding voice did not touch the existing typed-send
    # flow at all — same endpoint, same body shape, same client_token reuse.
    submit_fn = _OMNIBAR[_OMNIBAR.index("const handleSubmit ="):_OMNIBAR.index("const prepareSuggestion")]
    assert '"/api/process-input"' in submit_fn
    assert "user_input: text, client_token: token" in submit_fn


def test_stop_and_cancel_buttons_are_wired_to_the_real_hook_methods():
    assert "onClick={voice.stopRecording}" in _OMNIBAR
    assert "onClick={voice.cancelRecording}" in _OMNIBAR


def test_composer_disables_send_during_recording_and_transcribing_not_just_typed_busy():
    # The text-input form is only ever rendered for idle/transcribed/error —
    # recording and transcribing each replace it with their own block, so
    # Send physically cannot be pressed while a recording is in flight.
    assert 'voice.state === "recording" ?' in _OMNIBAR
    assert 'voice.state === "transcribing" ?' in _OMNIBAR


def test_voice_error_states_are_rendered_distinctly():
    assert "VOICE_ERROR_MESSAGES" in _VOICE_LIB
    for reason in ("permission_denied", "mic_unavailable", "unsupported", "no_speech",
                   "transcription_failed", "network"):
        assert f'{reason}:' in _VOICE_LIB


def test_hook_never_calls_process_input_directly_voice_is_input_only():
    # A comment may legitimately MENTION process-input while explaining that
    # this hook doesn't call it — check for an actual apiFetch call target,
    # not a bare substring.
    assert 'apiFetch<{ transcript: string }>("/api/mo/transcribe"' in _USE_VOICE
    assert 'apiFetch(' not in _USE_VOICE.replace(
        'apiFetch<{ transcript: string }>("/api/mo/transcribe"', ""
    )


def test_cancel_detaches_onstop_before_stopping_so_no_transcription_is_sent():
    cancel_fn = _USE_VOICE[_USE_VOICE.index("const cancelRecording ="):_USE_VOICE.index("const dismissError")]
    assert "mediaRecorderRef.current.onstop = null" in cancel_fn
    assert cancel_fn.index("onstop = null") < cancel_fn.index(".stop()")


def test_max_duration_matches_the_documented_backend_limit():
    # Frontend VOICE_MAX_SECONDS and backend COREOS_MO_TRANSCRIBE_MAX_SECONDS
    # default must stay in sync so a full-length recording is never rejected
    # by the server purely because the two limits drifted apart.
    assert "VOICE_MAX_SECONDS = 30" in _VOICE_LIB
    assert main.sec.MO_TRANSCRIBE_MAX_SECONDS == 30


def test_mime_negotiation_never_assumes_webm_only():
    assert "pickSupportedMimeType" in _USE_VOICE
    assert "isTypeSupported" in _USE_VOICE
    assert "audio/mp4" in _VOICE_LIB  # Safari/iOS path present, not just webm
