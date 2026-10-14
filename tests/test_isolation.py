"""Phase 2 — user isolation / IDOR tests.

Two authenticated users (alice, bob). Every check proves that A cannot see or
touch B's private data, and that manipulating any client-supplied identifier
does not change the authenticated owner.
"""
import db as db_layer
import main


def _insert_for(user_id, **kw):
    c = db_layer.get_connection()
    now = "2026-09-01T00:00:00+08:00"
    c.execute("INSERT INTO transactions (id,user_id,amount,category,date,type,description,created_at,updated_at) "
              "VALUES (?,?,?,?,?,?,?,?,?)",
              (kw["tx_id"], user_id, kw.get("amount", 9.0), "Food", now, "expense", kw.get("desc", "x"), now, now))
    c.execute("INSERT INTO ai_recommendations (id,user_id,module,type,message,status) "
              "VALUES (?,?,?,?,?,?)", (kw["rec_id"], user_id, "FINANCE", "INSIGHT", "secret", "PENDING"))
    c.execute("INSERT INTO schedule_events (id,user_id,title,location,start_time,end_time,created_at,updated_at) "
              "VALUES (?,?,?,?,?,?,?,?)", (kw["ev_id"], user_id, "PrivateMeeting", "Room", now, now, now, now))
    c.execute("INSERT INTO tasks (id,user_id,title,is_completed,reminder_at,reminder_status,created_at,updated_at) "
              "VALUES (?,?,?,0,?, 'PENDING',?,?)", (kw["task_id"], user_id, "PrivateTask", now, now, now))
    c.commit(); c.close()


def test_a_cannot_read_b_finance_or_transactions(alice, bob):
    ac, a = alice
    bc, b = bob
    _insert_for(b["id"], tx_id="b_tx", rec_id="b_rec", ev_id="b_ev", task_id="b_task", desc="bob-only")
    ac.post("/api/finance/settings", json={"starting_balance": 111})

    fin = ac.get("/api/finance").json()
    assert all(t["description"] != "bob-only" for t in fin["transactions"])
    assert all("user_id" not in t for t in fin["transactions"])
    assert fin["snapshot"]["starting_balance"] == 111  # A sees only A's number

    hist = ac.get("/api/finance/history").json()["transactions"]
    assert all(t["id"] != "b_tx" for t in hist)


def test_a_cannot_read_b_finance_settings(alice, bob):
    ac, _ = alice
    bc, _ = bob
    bc.post("/api/finance/settings", json={"starting_balance": 9999, "weekly_budget": 888})
    ac.post("/api/finance/settings", json={"starting_balance": 5})
    got = ac.get("/api/finance/settings").json()
    assert got["starting_balance"] == 5
    assert got["weekly_budget"] != 888


def test_a_cannot_update_b_finance_settings_via_body(alice, bob):
    ac, _ = alice
    bc, b = bob
    bc.post("/api/finance/settings", json={"starting_balance": 100})
    # A sends a body that *tries* to carry bob's id — it must be ignored.
    r = ac.post("/api/finance/settings", json={"user_id": b["id"], "starting_balance": 42})
    assert r.status_code == 200
    assert bc.get("/api/finance/settings").json()["starting_balance"] == 100  # unchanged
    assert ac.get("/api/finance/settings").json()["starting_balance"] == 42


def test_a_cannot_read_b_schedule_or_tasks(alice, bob):
    ac, _ = alice
    _, b = bob
    _insert_for(b["id"], tx_id="b_tx2", rec_id="b_rec2", ev_id="b_ev2", task_id="b_task2")
    sched = ac.get("/api/schedule").json()
    assert all(e["title"] not in ("PrivateMeeting", "PrivateTask") for e in sched["events"])


def test_a_cannot_alter_b_recommendation(alice, bob):
    ac, _ = alice
    _, b = bob
    _insert_for(b["id"], tx_id="b_tx3", rec_id="b_rec3", ev_id="b_ev3", task_id="b_task3")
    # A tries to dismiss bob's recommendation by id -> 404 (does not reveal it exists).
    r = ac.post("/api/recommendations/update", json={"recommendation_id": "b_rec3", "action": "DISMISSED"})
    assert r.status_code == 404
    c = db_layer.get_connection()
    assert c.execute("SELECT status FROM ai_recommendations WHERE id='b_rec3'").fetchone()[0] == "PENDING"
    c.close()


def test_a_cannot_edit_or_delete_b_event(alice, bob):
    ac, _ = alice
    _, b = bob
    _insert_for(b["id"], tx_id="b_tx4", rec_id="b_rec4", ev_id="b_ev4", task_id="b_task4")
    assert ac.patch("/api/events/b_ev4", json={"start": "2030-01-01T00:00:00+08:00"}).status_code == 404
    assert ac.delete("/api/events/b_ev4").status_code == 404
    c = db_layer.get_connection()
    ev = c.execute("SELECT start_time FROM schedule_events WHERE id='b_ev4'").fetchone()[0]
    c.close()
    assert ev != "2030-01-01T00:00:00+08:00"


def test_a_cannot_touch_b_tasks(alice, bob):
    ac, _ = alice
    _, b = bob
    _insert_for(b["id"], tx_id="b_tx5", rec_id="b_rec5", ev_id="b_ev5", task_id="b_task5")
    assert ac.patch("/api/tasks/b_task5", json={"title": "hijacked"}).status_code == 404
    assert ac.post("/api/tasks/b_task5/complete").status_code == 404
    assert ac.delete("/api/tasks/b_task5").status_code == 404
    c = db_layer.get_connection()
    row = c.execute("SELECT title, is_completed FROM tasks WHERE id='b_task5'").fetchone()
    c.close()
    assert row[0] == "PrivateTask" and row[1] == 0


def test_a_cannot_read_or_change_b_preferences(alice, bob):
    ac, _ = alice
    bc, _ = bob
    bc.post("/api/settings/update", json={"notifications": True, "location_services": False, "dark_mode": True})
    ac.post("/api/settings/update", json={"notifications": False, "location_services": False, "dark_mode": False})
    assert ac.get("/api/settings").json().get("notifications") == 0
    assert bc.get("/api/settings").json().get("notifications") == 1  # bob unaffected


def test_query_param_user_id_is_ignored(alice, bob):
    ac, a = alice
    _, b = bob
    bc = bob[0]
    bc.post("/api/finance/settings", json={"starting_balance": 777})
    ac.post("/api/finance/settings", json={"starting_balance": 3})
    # Old-style URLs no longer exist; the new ones ignore any ?user_id=
    assert ac.get(f"/api/finance?user_id={b['id']}").json()["snapshot"]["starting_balance"] == 3
    assert ac.get(f"/api/map-locations?user_id={b['id']}&activity=futsal").status_code == 200


def test_old_path_style_routes_are_gone(alice):
    ac, a = alice
    assert ac.get(f"/api/finance/{a['id']}").status_code == 404
    assert ac.get(f"/api/schedule/{a['id']}").status_code == 404
    assert ac.get(f"/api/settings/{a['id']}").status_code == 404


def test_ai_action_cannot_be_redirected_to_b(alice, bob, mock_ai):
    from conftest import mo_run
    ac, a = alice
    _, b = bob
    # The model "returns" a transaction with an injected user_id for bob — it
    # must be ignored at BOTH the proposal and the confirmed write.
    mock_ai("CREATE_TRANSACTION", parameters={
        "amount": 500, "type": "expense", "description": "hijack",
        "category": "Food", "date": "2026-09-01T10:00:00+08:00",
        "user_id": b["id"], "owner": b["id"],
    })
    _p, conf = mo_run(ac, "spent 500 on food")
    assert conf.status_code == 200 and conf.json()["kind"] == "executed"
    c = db_layer.get_connection()
    owner = c.execute("SELECT user_id FROM transactions WHERE description='hijack'").fetchone()[0]
    bob_count = c.execute("SELECT COUNT(*) FROM transactions WHERE user_id=?", (b["id"],)).fetchone()[0]
    bob_props = c.execute("SELECT COUNT(*) FROM mo_proposals WHERE user_id=?", (b["id"],)).fetchone()[0]
    c.close()
    assert owner == a["id"]      # landed on the AUTHENTICATED user
    assert bob_count == 0        # bob got nothing
    assert bob_props == 0        # ...not even a proposal


def test_ai_task_action_scoped_to_caller(alice, bob, mock_ai):
    from conftest import mo_run
    ac, a = alice
    _, b = bob
    mock_ai("CREATE_TASK", parameters={"title": "scoped task", "user_id": b["id"]})
    _p, conf = mo_run(ac, "remind me to do the scoped task")
    assert conf.json()["kind"] == "executed"
    c = db_layer.get_connection()
    row = c.execute("SELECT user_id FROM tasks WHERE title='scoped task'").fetchone()
    c.close()
    assert row[0] == a["id"]
