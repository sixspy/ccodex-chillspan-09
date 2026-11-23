"""线路时效窗 / 批次温控窗冲突：先红后绿。

三张失败测：
- 甲路径：冲突仍落正式窗（应拒写、正式字段零改、线路占用零增）
- 乙路径：影子窗未晋升却计有效
- 争用：双人同线同批抢写窗（只记争用痕，零写）

另含历史夹具：模拟“线路时效压过批次窗仍写入”的旧脏数据，
须被协调窗判无效、对账不假绿、封批拦截。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str = "admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _add_clerk(c: TestClient, username: str) -> dict:
    """造第三个写账号，用于扮演“在途写入者”，与两名争用者区分。"""
    from db import now_iso, sha_password

    with connect() as conn:
        conn.execute(
            "INSERT INTO users(username,password_sha,role) VALUES (?,?,?)",
            (username, sha_password("123456"), "clerk"),
        )
        conn.commit()
    return _auth(c, username)


def _mk_route(
    c: TestClient,
    h: dict,
    code: str = "RT-WIN-01",
    start: str = "2026-11-24T08:00:00",
    end: str = "2026-11-24T12:00:00",
    capacity: int = 4,
) -> dict:
    r = c.post(
        "/api/routes",
        headers=h,
        json={
            "route_code": code,
            "origin_region": "华东",
            "dest_region": "华南",
            "cold_hours": 4,
            "vehicle_type": "冷藏车",
            "window_start": start,
            "window_end": end,
            "capacity": capacity,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _mk_batch(c: TestClient, h: dict, code: str = "TB-WIN-001") -> dict:
    chambers = c.get("/api/chambers", headers=h).json()["items"]
    ch = next(x for x in chambers if x["chamber_code"] == "CK-HN-03")
    probes = c.get(f"/api/probes?chamber_id={ch['id']}", headers=h).json()["items"]
    probe = next(p for p in probes if p["online_status"] == "在线")
    r = c.post(
        "/api/batches",
        headers=h,
        json={
            "chamber_id": ch["id"],
            "probe_id": probe["id"],
            "cargo_ref": "窗冲突夹具货载",
            "batch_code": code,
            "temp_points": [{"t": "08:00", "c": 3.0}],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def win_ctx():
    c = TestClient(app)
    h = _auth(c)
    route = _mk_route(c, h)
    batch = _mk_batch(c, h)
    return c, h, route, batch


# ---- 失败测甲：冲突仍落正式窗 -------------------------------------------
def test_path_a_conflict_rejects_official_window_zero_change(win_ctx):
    c, h, route, batch = win_ctx
    body = {
        "route_code": route["route_code"],
        "window_start": "2026-11-24T07:00:00",
        "window_end": "2026-11-24T13:00:00",
        "decision": "甲",
    }
    r = c.post(f"/api/batches/{batch['id']}/window", headers=h, json=body)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "WINDOW_CONFLICT"

    # 正式字段零改、线路占用零增
    with connect() as conn:
        row = conn.execute(
            "SELECT route_id,window_start,window_end FROM temp_batches WHERE id=?",
            (batch["id"],),
        ).fetchone()
        assert row["route_id"] is None
        assert row["window_start"] is None
        assert row["window_end"] is None
        occ = conn.execute(
            """SELECT COUNT(*) c FROM temp_batches
               WHERE route_id=? AND window_start IS NOT NULL""",
            (route["id"],),
        ).fetchone()["c"]
        assert occ == 0

    coord = c.get(
        f"/api/window-coordination?batch_code={batch['batch_code']}", headers=h
    ).json()["items"][0]
    assert coord["official_window"] is None
    assert coord["effective_valid"] is False

    # 当班纪要已记甲，不再改口
    mins = c.get("/api/route-batch-minutes", headers=h).json()["items"]
    assert any(
        m["route_code"] == route["route_code"]
        and m["batch_code"] == batch["batch_code"]
        and m["decision"] == "甲"
        for m in mins
    )


# ---- 失败测乙：影子窗未晋升却计有效 -------------------------------------
def test_path_b_shadow_not_valid_until_promoted(win_ctx):
    c, h, route, batch = win_ctx
    body = {
        "route_code": route["route_code"],
        "window_start": "2026-11-24T07:00:00",
        "window_end": "2026-11-24T13:00:00",
        "decision": "乙",
    }
    r = c.post(f"/api/batches/{batch['id']}/window", headers=h, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "草案"
    assert r.json()["effective_valid"] is False

    # 正式窗零改
    with connect() as conn:
        row = conn.execute(
            "SELECT route_id,window_start,window_end FROM temp_batches WHERE id=?",
            (batch["id"],),
        ).fetchone()
        assert row["route_id"] is None
        assert row["window_start"] is None
        assert row["window_end"] is None

    # 影子未晋升 -> 不计有效
    coord = c.get(
        f"/api/window-coordination?batch_code={batch['batch_code']}", headers=h
    ).json()
    item = coord["items"][0]
    assert item["shadow"]["status"] == "草案"
    assert item["effective_valid"] is False
    assert item["effective_source"] == "草案未晋升"
    assert coord["summary"]["valid"] == 0

    # 改线消除冲突 -> 原子晋升
    p = c.patch(
        f"/api/routes/{route['id']}/window",
        headers=h,
        json={
            "window_start": "2026-11-24T06:00:00",
            "window_end": "2026-11-24T14:00:00",
        },
    )
    assert p.status_code == 200, p.text
    assert any(
        x["batch_code"] == batch["batch_code"] and x["status"] == "已晋升"
        for x in p.json()["promotions"]
    )

    coord2 = c.get(
        f"/api/window-coordination?batch_code={batch['batch_code']}", headers=h
    ).json()["items"][0]
    assert coord2["shadow"]["status"] == "已晋升"
    assert coord2["official_window"]["start"] == "2026-11-24T07:00:00"
    assert coord2["official_window"]["end"] == "2026-11-24T13:00:00"
    assert coord2["effective_valid"] is True
    assert coord2["occupancy"] == 1

    # 纪要仍是乙，不被晋升改写
    mins = c.get("/api/route-batch-minutes", headers=h).json()["items"]
    assert any(
        m["batch_code"] == batch["batch_code"] and m["decision"] == "乙" for m in mins
    )


def test_path_b_draft_rolls_back_when_route_change_does_not_resolve(win_ctx):
    c, h, route, batch = win_ctx
    body = {
        "route_code": route["route_code"],
        "window_start": "2026-11-24T07:00:00",
        "window_end": "2026-11-24T13:00:00",
        "decision": "乙",
    }
    assert c.post(f"/api/batches/{batch['id']}/window", headers=h, json=body).status_code == 200
    # 改线后仍冲突 -> 草案回滚
    p = c.patch(
        f"/api/routes/{route['id']}/window",
        headers=h,
        json={
            "window_start": "2026-11-24T09:00:00",
            "window_end": "2026-11-24T11:00:00",
        },
    )
    assert p.status_code == 200, p.text
    assert any(
        x["batch_code"] == batch["batch_code"] and x["status"] == "已回滚"
        for x in p.json()["promotions"]
    )
    with connect() as conn:
        row = conn.execute(
            "SELECT route_id,window_start,window_end FROM temp_batches WHERE id=?",
            (batch["id"],),
        ).fetchone()
        assert row["route_id"] is None
        assert row["window_start"] is None
        assert row["window_end"] is None
        occ = conn.execute(
            """SELECT COUNT(*) c FROM temp_batches
               WHERE route_id=? AND window_start IS NOT NULL""",
            (route["id"],),
        ).fetchone()["c"]
        assert occ == 0


# ---- 失败测争用：双人同线同批抢写窗 -------------------------------------
def test_contention_same_route_batch_only_logs_trace(win_ctx):
    c, h, route, batch = win_ctx
    h2 = _auth(c, "ops")
    lock_key = f"route:{route['id']}"
    # 第一人先抢到线路锁
    assert (
        c.post("/api/locks/acquire", headers=h, json={"lock_key": lock_key}).status_code
        == 200
    )
    # 第二人同线同批写窗 -> 争用仅记痕
    body = {
        "route_code": route["route_code"],
        "window_start": "2026-11-24T09:00:00",
        "window_end": "2026-11-24T11:00:00",
        "decision": "甲",
    }
    r = c.post(f"/api/batches/{batch['id']}/window", headers=h2, json=body)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "WINDOW_CONTENTION"

    cont = c.get("/api/contention", headers=h).json()["items"]
    assert any(x["lock_key"] == lock_key for x in cont)

    with connect() as conn:
        row = conn.execute(
            "SELECT route_id,window_start,window_end FROM temp_batches WHERE id=?",
            (batch["id"],),
        ).fetchone()
        assert row["route_id"] is None
        assert row["window_start"] is None
        assert row["window_end"] is None
        occ = conn.execute(
            """SELECT COUNT(*) c FROM temp_batches
               WHERE route_id=? AND window_start IS NOT NULL""",
            (route["id"],),
        ).fetchone()["c"]
        assert occ == 0


def test_contention_two_parallel_requests_same_route_batch(win_ctx):
    """真正并发：双请求同线同批抢写窗，双方只能记痕、零写。"""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    c, h, route, batch = win_ctx
    lock_key = f"route:{route['id']}"
    # 在途写入者（第三人）占住线路锁，锁行已提交可见。
    holder = _add_clerk(c, "holder")
    assert (
        c.post(
            "/api/locks/acquire", headers=holder, json={"lock_key": lock_key}
        ).status_code
        == 200
    )

    tokens = {"admin": h, "ops": _auth(c, "ops")}
    barrier = threading.Barrier(2)

    def attempt(user):
        client = TestClient(app)
        barrier.wait(timeout=10)
        return client.post(
            f"/api/batches/{batch['id']}/window",
            headers=tokens[user],
            json={
                "route_code": route["route_code"],
                "window_start": "2026-11-24T09:00:00",
                "window_end": "2026-11-24T11:00:00",
                "decision": "甲",
            },
        )

    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(attempt, tokens))

    codes = sorted(r.status_code for r in results)
    assert codes == [409, 409], [r.text for r in results]
    for r in results:
        assert r.json()["detail"]["code"] == "WINDOW_CONTENTION"

    cont = c.get("/api/contention", headers=h).json()["items"]
    actors = {
        x["actor"] for x in cont if x["lock_key"] == lock_key
    }
    assert {"admin", "ops"} <= actors

    with connect() as conn:
        row = conn.execute(
            "SELECT route_id,window_start,window_end FROM temp_batches WHERE id=?",
            (batch["id"],),
        ).fetchone()
        assert row["route_id"] is None
        assert row["window_start"] is None
        assert row["window_end"] is None
        occ = conn.execute(
            """SELECT COUNT(*) c FROM temp_batches
               WHERE route_id=? AND window_start IS NOT NULL""",
            (route["id"],),
        ).fetchone()["c"]
        assert occ == 0


# ---- 纪要冻结：同班改口记偏航并拒写 -------------------------------------
def test_same_shift_minutes_drift_is_rejected(win_ctx):
    c, h, route, batch = win_ctx
    conflict = {
        "route_code": route["route_code"],
        "window_start": "2026-11-24T07:00:00",
        "window_end": "2026-11-24T13:00:00",
        "decision": "甲",
    }
    assert c.post(f"/api/batches/{batch['id']}/window", headers=h, json=conflict).status_code == 409
    changed = dict(conflict)
    changed["decision"] = "乙"
    r = c.post(f"/api/batches/{batch['id']}/window", headers=h, json=changed)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "WINDOW_MINUTE_DRIFT"
    events = c.get("/api/window-conflict-events", headers=h).json()["items"]
    assert any(e["kind"] == "窗冲突偏航" for e in events)


# ---- 历史夹具：线路时效压过批次窗仍写入，不得假绿 -----------------------
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "window_conflicts.json"


def test_legacy_conflict_fixture_not_green(win_ctx):
    c, h, route, batch = win_ctx
    # 夹具描述旧缺陷：批次窗两端都越出线路窗，正式窗仍被写入。
    legacy = json.loads(FIXTURE.read_text(encoding="utf-8"))[0]
    assert legacy["window_start"] < route["window_start"]
    assert legacy["window_end"] > route["window_end"]
    with connect() as conn:
        conn.execute(
            """UPDATE temp_batches SET route_id=?, window_start=?, window_end=?
               WHERE id=?""",
            (
                route["id"],
                legacy["window_start"],
                legacy["window_end"],
                batch["id"],
            ),
        )
        conn.commit()
        chamber_code = conn.execute(
            "SELECT c.chamber_code FROM temp_batches b JOIN cold_chambers c ON c.id=b.chamber_id WHERE b.id=?",
            (batch["id"],),
        ).fetchone()["chamber_code"]

    item = c.get(
        f"/api/window-coordination?batch_code={batch['batch_code']}", headers=h
    ).json()["items"][0]
    assert item["conflict"] is not None
    assert item["effective_valid"] is False
    assert item["effective_source"] == "正式冲突"

    # 仓间页列表的冲突计数也必须来自同一快照，不得自算成 0。
    chambers = c.get("/api/chambers", headers=h).json()["items"]
    chamber = next(x for x in chambers if x["chamber_code"] == chamber_code)
    assert chamber["window_conflicts"] >= 1
    chamber_detail = c.get(
        f"/api/chambers/{chamber['id']}", headers=h
    ).json()
    assert chamber_detail["window_conflicts"] == chamber["window_conflicts"]

    # 线路页占用来自同一快照。
    routes = c.get("/api/routes", headers=h).json()["items"]
    route_row = next(x for x in routes if x["route_code"] == route["route_code"])
    assert batch["batch_code"] in route_row["window_batches"]

    recon = c.get(f"/api/reconcile/{chamber_code}", headers=h).json()
    assert recon["balanced"] is False
    assert recon["window_conflicts"] >= 1

    seal = c.post("/api/seals", headers=h, json={"batch_id": batch["id"]})
    assert seal.status_code == 409, seal.text


# ---- 两边不得混用：正式窗落库后旧草案被取代，不得再晋升 ----------------
def test_official_write_supersedes_stale_shadow(win_ctx):
    c, h, route, batch = win_ctx
    conflict = {
        "route_code": route["route_code"],
        "window_start": "2026-11-24T09:00:00",
        "window_end": "2026-11-24T11:00:00",
        "decision": "乙",
    }
    # 先在线路窗 08-12 下落一份与线路冲突的草案（07-13）。
    conflict["window_start"] = "2026-11-24T07:00:00"
    conflict["window_end"] = "2026-11-24T13:00:00"
    assert (
        c.post(f"/api/batches/{batch['id']}/window", headers=h, json=conflict)
        .json()["status"]
        == "草案"
    )

    # 同批改走另一条覆盖充分的线路（正式窗落库）。
    route2 = _mk_route(c, h, code="RT-WIN-02")
    ok = c.post(
        f"/api/batches/{batch['id']}/window",
        headers=h,
        json={
            "route_code": route2["route_code"],
            "window_start": "2026-11-24T09:00:00",
            "window_end": "2026-11-24T11:00:00",
            "decision": "甲",
        },
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["effective_source"] == "正式窗"

    # 再回头放宽旧线路：旧草案已被取代，不得再晋升覆盖正式窗。
    p = c.patch(
        f"/api/routes/{route['id']}/window",
        headers=h,
        json={
            "window_start": "2026-11-24T06:00:00",
            "window_end": "2026-11-24T14:00:00",
        },
    )
    assert p.status_code == 200, p.text
    assert p.json()["promotions"] == []

    state = c.get(
        f"/api/window-coordination?batch_code={batch['batch_code']}", headers=h
    ).json()["items"][0]
    assert state["route_code"] == route2["route_code"]
    assert state["official_window"] == {
        "start": "2026-11-24T09:00:00",
        "end": "2026-11-24T11:00:00",
    }
    assert state["effective_valid"] is True


# ---- 改线收窄致正式窗冲突：不得假绿，封批拦截 -------------------------
def test_route_narrowing_after_promotion_marks_conflict(win_ctx):
    c, h, route, batch = win_ctx
    assert (
        c.post(
            f"/api/batches/{batch['id']}/window",
            headers=h,
            json={
                "route_code": route["route_code"],
                "window_start": "2026-11-24T07:00:00",
                "window_end": "2026-11-24T13:00:00",
                "decision": "乙",
            },
        ).status_code
        == 200
    )
    p = c.patch(
        f"/api/routes/{route['id']}/window",
        headers=h,
        json={
            "window_start": "2026-11-24T06:00:00",
            "window_end": "2026-11-24T14:00:00",
        },
    )
    assert p.status_code == 200
    # 晋升后再收窄线路窗，正式窗越界 -> 判冲突、不计有效。
    n = c.patch(
        f"/api/routes/{route['id']}/window",
        headers=h,
        json={
            "window_start": "2026-11-24T09:00:00",
            "window_end": "2026-11-24T11:00:00",
        },
    )
    assert n.status_code == 200, n.text

    state = c.get(
        f"/api/window-coordination?batch_code={batch['batch_code']}", headers=h
    ).json()["items"][0]
    assert state["conflict"] is not None
    assert state["effective_valid"] is False
    assert state["effective_source"] == "正式冲突"

    seal = c.post("/api/seals", headers=h, json={"batch_id": batch["id"]})
    assert seal.status_code == 409, seal.text


# ---- 五页同源核验 -------------------------------------------------------
def _page_view(c, h, chamber_id, batch_id) -> dict:
    """逐一读线路页、批次页、仓间页、封批页、对账页，抽同一批次窗口径。"""
    routes = c.get("/api/routes", headers=h).json()["items"]
    batches = c.get("/api/batches", headers=h).json()["items"]
    chamber = c.get(f"/api/chambers/{chamber_id}", headers=h).json()
    seals = c.get("/api/seals", headers=h).json()["items"]
    recon = c.get("/api/reconcile", headers=h).json()

    b = next(x for x in batches if x["id"] == batch_id)
    code = b["batch_code"]
    route = next(
        r for r in routes if code in (r.get("window_batches") or [])
    )
    seal = next((s for s in seals if s["batch_code"] == code), None)
    ch_state = next(
        s for s in chamber["window_states"] if s["batch_code"] == code
    )
    rec_state = next(
        s for s in recon["window_states"] if s["batch_code"] == code
    )
    return {
        "routes": (
            route["route_code"],
            route["occupancy"],
        ),
        "batches": (
            b["window_state"]["route_code"],
            b["window_state"]["effective_valid"],
            b["window_state"]["effective_source"],
            b["window_state"]["occupancy"],
        ),
        "chambers": (
            ch_state["route_code"],
            ch_state["effective_valid"],
            ch_state["effective_source"],
            ch_state["occupancy"],
        ),
        "seals": (
            (seal["window_state"]["route_code"],
             seal["window_state"]["effective_valid"],
             seal["window_state"]["effective_source"],
             seal["window_state"]["occupancy"])
            if seal else None
        ),
        "reconcile": (
            rec_state["route_code"],
            rec_state["effective_valid"],
            rec_state["effective_source"],
            rec_state["occupancy"],
        ),
        "batch_id": batch_id,
    }


def test_five_views_share_one_window_source(win_ctx):
    c, h, route, batch = win_ctx
    body = {
        "route_code": route["route_code"],
        "window_start": "2026-11-24T09:00:00",
        "window_end": "2026-11-24T11:00:00",
        "decision": "甲",
    }
    r = c.post(f"/api/batches/{batch['id']}/window", headers=h, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["effective_valid"] is True

    # 封批页只列已封批批次；封批后同源字段仍须一致。
    seal = c.post("/api/seals", headers=h, json={"batch_id": batch["id"]})
    assert seal.status_code == 200, seal.text

    page = c.get("/api/chambers", headers=h).json()["items"]
    chamber_id = next(
        x["id"] for x in page if x["chamber_code"] == "CK-HN-03"
    )
    views = _page_view(c, h, chamber_id, batch["id"])

    expected = (
        route["route_code"], True, "正式窗", 1
    )
    assert views["batches"] == expected
    assert views["chambers"] == expected
    assert views["seals"] == expected
    assert views["reconcile"] == expected
    assert views["routes"] == (route["route_code"], 1)

    cons = c.get("/api/window-coordination/consistency", headers=h).json()
    assert cons["consistent"] is True
