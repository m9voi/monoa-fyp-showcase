"""Phase 5H-B6 — low-level privacy / hygiene cleanup.

  * GET /api/settings returns only the preference toggles, never the owning
    user_id (mirrors the existing /api/finance/settings convention) and never a
    column added later via `SELECT *`.
  * The AI / Omni-bar broad exception handlers log the exception CLASS, not
    str(e): a pydantic ValidationError stringifies with the offending
    input_value, which on that path can be submitted financial text, a task /
    event title, or a location.
  * .gitignore covers the transient SQLite sidecar files and runtime log files
    so a local data artifact cannot be staged by accident.
"""
import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# GET /api/settings — no user_id, explicit columns only
# --------------------------------------------------------------------------- #
def test_settings_get_omits_user_id(alice):
    tc, user = alice
    tc.post("/api/settings/update", json={
        "notifications": True, "location_services": False, "dark_mode": True,
    })
    body = tc.get("/api/settings").json()
    assert "user_id" not in body
    assert user["id"] not in body.values()
    assert set(body) == {"notifications", "location_services", "dark_mode", "appearance_mode"}


def test_settings_get_is_empty_object_before_first_save(alice):
    tc, _ = alice
    assert tc.get("/api/settings").json() == {}


# --------------------------------------------------------------------------- #
# AI-path exception logging never echoes the submitted value
# --------------------------------------------------------------------------- #
def test_ai_validation_failure_does_not_log_submitted_values(alice, mock_ai, capfd):
    tc, _ = alice
    marker = "SEKRIT_TASK_TITLE_MARKER_7f3a91"
    # A CREATE_TASK whose title is the wrong type -> TaskParams(**params) raises
    # pydantic ValidationError while Mo is BUILDING the proposal -> the class-only
    # log line; no proposal, no mutation, a plain "error" kind to the client.
    mock_ai("CREATE_TASK", parameters={"title": {"leak": marker}})
    r = tc.post("/api/process-input", json={"user_input": "add a task"})
    assert r.status_code == 200
    assert r.json()["kind"] == "error"

    out = capfd.readouterr().out
    assert "VALIDATION_ERROR:" in out                       # the diagnostic still logs
    assert "VALIDATION_ERROR: ValidationError" in out       # ...but only the class
    assert marker not in out                                # ...never the value


def test_ai_path_source_logs_exception_class_not_str(  # guards against a regression
):
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    for tag in ("AI_PARSE_ERROR", "VALIDATION_ERROR", "UNHANDLED_EXCEPTION"):
        assert f'{tag}: {{type(e).__name__}}' in src, tag
        assert f'{tag}: {{str(e)}}' not in src, tag


# --------------------------------------------------------------------------- #
# .gitignore hygiene
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("pattern", [
    "*.db", "*.db-journal", "*.db-wal", "*.db-shm",
    "*.sqlite", "*.sqlite3", "*.sqlite3-journal",
    "/backups/", "*.log",
])
def test_gitignore_covers_local_data_and_log_artifacts(pattern):
    lines = {
        ln.strip()
        for ln in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    }
    assert pattern in lines, f"missing .gitignore pattern: {pattern}"


def test_no_local_database_or_backup_file_is_tracked():
    import subprocess
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=str(ROOT), capture_output=True, text=True
    ).stdout.splitlines()
    bad = [
        p for p in tracked
        if p.endswith((".db", ".sqlite", ".sqlite3", ".db-journal",
                       ".db-wal", ".db-shm", ".log"))
        or p.startswith("backups/")
        or os.path.basename(p) == ".env"
    ]
    assert not bad, f"local data / secret artifacts are tracked: {bad}"
