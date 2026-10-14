"""Phase 10 — Candidate Defect 3: Mo multi-turn clarification context.

Reproduces, deterministically, the exact physical-device conversation:

  User: "Mo help me plan my day with outdoor activities"
  Mo:   [asks a clarification question]
  User: "Morning or night any kind of activity"
  Mo:   [asks a clarification question again]
  User: "Football in the evening"
  Mo:   "I didn't quite catch that — try rephrasing what you'd like to do."

These tests characterize CURRENT behavior (see the Phase 10 report's
"E. MO MULTI-TURN ROOT CAUSE" / "F. CURRENT ARCHITECTURE LIMITATION" sections):
a "clarification" (or "unsupported") turn writes NOTHING to `mo_context` —
_mo_remember_context() is only ever called after a turn that resolves a real
date or a real named entity (see main.py's own docstrings on 0018/0019).
There is no mechanism anywhere that remembers "I just asked a clarification
question" or the ORIGINAL ambiguous request, so the very next turn is sent to
AIService.interpret() with the same empty context_date/context_entity hint a
completely unrelated fresh message would get. This is not a bug in existing,
tested logic — every existing single-turn and date/entity-hint behavior is
unchanged and still fully covered by test_mo_hitl.py et al. It is a real,
confirmed absence of a feature this codebase never had.
"""
import main as m
from conftest import mo_propose


def _patch_interpret_by_input(monkeypatch, responses: dict, calls: list):
    """Like conftest's mock_ai / mock_ai_interp, but dispatches on the exact
    user_input text (needed here because the three physical-device turns must
    each get a DIFFERENT canned interpretation) and records the context_date/
    context_entity the endpoint actually passed on each call, so the test can
    assert on what real context reached the model — not just the final reply."""

    def fake_interpret(user_input, context_date=None, context_entity=None, user_interests=None):
        calls.append({"user_input": user_input, "context_date": context_date, "context_entity": context_entity})
        return responses[user_input]

    monkeypatch.setattr(m.AIService, "interpret", staticmethod(fake_interpret))


def test_reproduces_the_physical_device_conversation(alice, monkeypatch):
    tc, _user = alice
    calls: list = []

    turn1 = "Mo help me plan my day with outdoor activities"
    turn2 = "Morning or night any kind of activity"
    turn3 = "Football in the evening"

    responses = {
        turn1: m.MoInterpretation(
            mode="clarification", operations=[],
            clarification_question="What activity, and what time of day?",
        ),
        turn2: m.MoInterpretation(
            mode="clarification", operations=[],
            clarification_question="Which activity would you like, and morning or evening?",
        ),
        # Faithful reproduction: sent with NO knowledge of turns 1-2 (see the
        # context_date/context_entity assertions below), the bare, context-free
        # phrase "Football in the evening" resolves to no usable operation —
        # exactly the "read with nothing to read" fallback (main.py's
        # _mo_understand, mode == "read" with an empty operations[]) that
        # produces the verbatim message observed on the physical device.
        turn3: m.MoInterpretation(mode="read", operations=[]),
    }
    _patch_interpret_by_input(monkeypatch, responses, calls)

    r1 = mo_propose(tc, turn1)
    assert r1.status_code == 200, r1.text
    assert r1.json()["kind"] == "clarification_required"
    assert "activity" in r1.json()["message"].lower()

    r2 = mo_propose(tc, turn2)
    assert r2.status_code == 200, r2.text
    assert r2.json()["kind"] == "clarification_required"

    r3 = mo_propose(tc, turn3)
    assert r3.status_code == 200, r3.text
    # This is the exact observed physical-device symptom, reproduced deterministically:
    # kind "error" with the verbatim "I didn't quite catch that" copy.
    assert r3.json()["kind"] == "error"
    assert "catch" in r3.json()["message"].lower()

    # Root cause, proven directly: every call reached the model with an EMPTY
    # context hint — turns 1 and 2 never resolved a date or entity (they were
    # both "clarification"), so _mo_recent_context() has nothing to offer turn
    # 3. Nothing about the original "plan my day with outdoor activities"
    # request, or the fact that a clarification was pending, was carried
    # forward in any form.
    assert len(calls) == 3
    for i, call in enumerate(calls):
        assert call["context_date"] is None, f"turn {i + 1} unexpectedly had a date hint: {call}"
        assert call["context_entity"] == {"date": None, "entity_type": None, "entity_title": None}, (
            f"turn {i + 1} unexpectedly had an entity hint: {call}"
        )
    # And turn 3 was sent completely alone — no trace of turn 1 or 2's text.
    assert calls[2]["user_input"] == turn3


def test_clarification_turns_never_write_mo_context(alice, monkeypatch):
    """Directly proves the mechanism: a clarification (or unsupported) turn
    leaves the mo_context table completely untouched, for however many turns
    in a row — confirming this is a structural absence, not a flaky race."""
    tc, user = alice
    calls: list = []
    responses = {
        "vague request one": m.MoInterpretation(
            mode="clarification", operations=[], clarification_question="Which day?"),
        "still vague": m.MoInterpretation(
            mode="clarification", operations=[], clarification_question="Still not sure — which day?"),
        "totally unrelated gibberish": m.MoInterpretation(mode="unsupported", operations=[]),
    }
    _patch_interpret_by_input(monkeypatch, responses, calls)

    for text in responses:
        r = mo_propose(tc, text)
        assert r.status_code == 200, r.text

    import db as db_layer
    conn = db_layer.get_connection()
    row = conn.execute(
        "SELECT * FROM mo_context WHERE user_id = ?", (user["id"],)
    ).fetchone()
    conn.close()
    assert row is None, (
        "mo_context should have no row at all after only clarification/unsupported "
        "turns — none of them ever resolves a date or entity to remember."
    )


def test_a_resolved_operation_still_correctly_remembers_context(alice, mock_ai):
    """Regression guard: confirms the EXISTING, correctly-working date/entity
    hint mechanism (mo_context) is untouched by this investigation — a turn
    that DOES resolve something is still remembered exactly as before."""
    tc, user = alice
    mock_ai("GET_SCHEDULE_SUMMARY", parameters={"target_date": "2026-09-20"})
    r = mo_propose(tc, "What's on Sunday?")
    assert r.status_code == 200, r.text

    import db as db_layer
    conn = db_layer.get_connection()
    row = conn.execute(
        "SELECT context_date FROM mo_context WHERE user_id = ?", (user["id"],)
    ).fetchone()
    conn.close()
    assert row is not None and row["context_date"] == "2026-09-20"
