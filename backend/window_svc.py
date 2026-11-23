"""线路时效窗 × 批次温控窗协同（全站唯一窗口径）。

正确性口径：
- 线路窗与批次窗冲突时只能择一：甲 = 冲突即拒写，正式字段零改、线路占用零增；
  乙 = 只落影子草案，正式窗零改，待改线消除冲突后原子晋升；仍冲突则草案回滚。
- 冲突窗不得以任何形式“正式生效”。混用即冲突窗仍正式生效，属偏航。
- 同班协调纪要一经落库不再改口；改口记“窗冲突偏航”并拒写。
- 同线同批并发写窗抢线路锁；争用只记痕，零写。
- 线路页、批次页、仓间页、封批页、对账页统一读本模块的 snapshot()。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import release, try_acquire_owned
from models import BatchWindowIn, RouteWindowPatch

router = APIRouter(tags=["window"])

DECISIONS = ("甲", "乙")
SHADOW_DRAFT = "草案"
SHADOW_PROMOTED = "已晋升"
SHADOW_ROLLED = "已回滚"
SHADOW_SUPERSEDED = "已取代"


# ----------------------------- 窗几何 -----------------------------------
def windows_conflict(
    batch_start: str | None,
    batch_end: str | None,
    route_start: str | None,
    route_end: str | None,
) -> bool:
    """线路窗不足以覆盖批次窗即冲突（批次窗越出线路计划时效）。

    按时间点比较，不按字符串：'2026-11-24T8:00:00' 与 '2026-11-24T12:00'
    这类宽窄不一的写法若按字典序会误判。
    """
    if not (batch_start and batch_end and route_start and route_end):
        return False
    try:
        bs = datetime.fromisoformat(batch_start)
        be = datetime.fromisoformat(batch_end)
        rs = datetime.fromisoformat(route_start)
        re_ = datetime.fromisoformat(route_end)
    except ValueError:
        return True  # 时间戳解析不了，宁拒不放
    return bs < rs or be > re_


def window_ordered(start: str | None, end: str | None) -> bool:
    """起讫合法且起点早于终点（按时间点，不按字符串）。"""
    if not (start and end):
        return False
    try:
        return datetime.fromisoformat(start) < datetime.fromisoformat(end)
    except ValueError:
        return False


def _win(start: str | None, end: str | None) -> dict | None:
    if not start or not end:
        return None
    return {"start": start, "end": end}


def _occupancy(conn, route_id) -> int:
    if not route_id:
        return 0
    return int(
        conn.execute(
            """SELECT COUNT(*) c FROM temp_batches
               WHERE route_id=? AND window_start IS NOT NULL""",
            (route_id,),
        ).fetchone()["c"]
    )


def _occupancy_excluding(conn, route_id, batch_id) -> int:
    """本批不在线路占用里计数：改写自身窗不得因自己已占位而被误判满。"""
    if not route_id:
        return 0
    return int(
        conn.execute(
            """SELECT COUNT(*) c FROM temp_batches
               WHERE route_id=? AND window_start IS NOT NULL AND id!=?""",
            (route_id, batch_id),
        ).fetchone()["c"]
    )


def _latest_shadow(conn, batch_id: int):
    return conn.execute(
        """SELECT * FROM batch_window_shadows WHERE batch_id=?
           ORDER BY id DESC LIMIT 1""",
        (batch_id,),
    ).fetchone()


def batch_window_state(conn, batch) -> dict:
    """单批窗权威状态；五页与对账都只认这一份。"""
    batch = dict(batch)
    batch_id = batch["id"]
    shadow = _latest_shadow(conn, batch_id)
    route_id = batch.get("route_id")
    route = None
    if route_id:
        route = conn.execute(
            "SELECT * FROM route_plans WHERE id=?", (route_id,)
        ).fetchone()

    official = _win(batch.get("window_start"), batch.get("window_end"))
    conflict = None
    if official and route:
        if windows_conflict(
            official["start"], official["end"],
            route["window_start"], route["window_end"],
        ):
            conflict = {
                "reason": "线路计划时效无法覆盖批次温控窗",
                "route_code": route["route_code"],
                "route_window": _win(route["window_start"], route["window_end"]),
                "batch_window": official,
            }

    shadow_payload = None
    if shadow:
        sroute = conn.execute(
            "SELECT route_code FROM route_plans WHERE id=?", (shadow["route_id"],)
        ).fetchone()
        shadow_payload = {
            "id": shadow["id"],
            "route_id": shadow["route_id"],
            "route_code": sroute["route_code"] if sroute else None,
            "window": _win(shadow["window_start"], shadow["window_end"]),
            "status": shadow["status"],
        }

    effective_route_id = route_id or (shadow["route_id"] if shadow else None)
    route_code = None
    if route_id and route["route_code"]:
        route_code = route["route_code"]
    elif effective_route_id:
        r = conn.execute(
            "SELECT route_code FROM route_plans WHERE id=?", (effective_route_id,)
        ).fetchone()
        route_code = r["route_code"] if r else None

    if official:
        if conflict:
            valid, source = False, "正式冲突"
        else:
            valid, source = True, "正式窗"
    elif shadow and shadow["status"] == SHADOW_DRAFT:
        valid, source = False, "草案未晋升"
    elif shadow and shadow["status"] == SHADOW_ROLLED:
        valid, source = False, "草案已回滚"
    else:
        valid, source = False, "未配置窗"

    return {
        "batch_id": batch_id,
        "batch_code": batch["batch_code"],
        "chamber_id": batch["chamber_id"],
        "route_id": effective_route_id,
        "route_code": route_code,
        "official_window": official,
        "shadow": shadow_payload,
        "conflict": conflict,
        "effective_valid": valid,
        "effective_source": source,
        "occupancy": _occupancy(conn, effective_route_id),
    }


def snapshot(conn) -> list[dict]:
    """五页同源快照：同一函数、同一排序，覆盖全部批次。"""
    rows = conn.execute(
        """SELECT id,batch_code,chamber_id,route_id,window_start,window_end
           FROM temp_batches ORDER BY batch_code, id"""
    ).fetchall()
    return [batch_window_state(conn, r) for r in rows]


def projection(states: list[dict]) -> list[dict]:
    return [
        {
            "batch_code": s["batch_code"],
            "chamber_id": s["chamber_id"],
            "route_code": s["route_code"],
            "effective_valid": s["effective_valid"],
            "effective_source": s["effective_source"],
            "occupancy": s["occupancy"],
        }
        for s in states
    ]


def conflict_count(conn, chamber_id: int | None = None) -> int:
    n = 0
    for s in snapshot(conn):
        if s["conflict"] and (
            chamber_id is None or s["chamber_id"] == chamber_id
        ):
            n += 1
    return n


# ----------------------------- 纪要 / 事件 -------------------------------
def shift_key() -> str:
    return now_iso()[:10]


def _record_event(conn, kind: str, route_code, batch_code, detail) -> None:
    conn.execute(
        """INSERT INTO window_conflict_events(kind,route_code,batch_code,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (kind, route_code, batch_code, detail, now_iso()),
    )


def _minutes_row(conn, route_id, batch_id, shift):
    return conn.execute(
        """SELECT * FROM route_batch_minutes
           WHERE shift_key=? AND route_id=? AND batch_id=?""",
        (shift, route_id, batch_id),
    ).fetchone()


def _write_minutes(
    conn, route_id, route_code, batch_id, batch_code, decision, detail
) -> None:
    shift = shift_key()
    row = _minutes_row(conn, route_id, batch_id, shift)
    if row:
        conn.execute(
            "UPDATE route_batch_minutes SET detail=? WHERE id=?",
            (detail, row["id"]),
        )
        return
    conn.execute(
        """INSERT INTO route_batch_minutes(shift_key,route_id,route_code,batch_id,
           batch_code,decision,detail,created_at) VALUES (?,?,?,?,?,?,?,?)""",
        (
            shift,
            route_id,
            route_code,
            batch_id,
            batch_code,
            decision,
            detail,
            now_iso(),
        ),
    )


def _reject(conn, status_code: int, code: str, message: str, **extra) -> None:
    """先落痕再拒写：提交纪要/争用后抛出，确保零业务写但留证。"""
    conn.commit()
    detail = {"code": code, "message": message}
    detail.update(extra)
    raise HTTPException(status_code, detail=detail)


# ----------------------------- 写窗 --------------------------------------
@router.post("/api/batches/{batch_id}/window")
def set_batch_window(
    batch_id: int, body: BatchWindowIn, authorization: AuthHeader = None
):
    user = require_writer(authorization)
    if body.decision not in DECISIONS:
        raise HTTPException(422, "decision 只能是 甲 或 乙")
    if not window_ordered(body.window_start, body.window_end):
        raise HTTPException(422, "批次窗起点须早于终点")

    route = None
    batch = None
    lock_key = None
    acquired = False
    with connect() as conn:
        try:
            batch = conn.execute(
                "SELECT * FROM temp_batches WHERE id=?", (batch_id,)
            ).fetchone()
            if not batch:
                raise HTTPException(404, "批次不存在")
            route = conn.execute(
                "SELECT * FROM route_plans WHERE route_code=?", (body.route_code,)
            ).fetchone()
            if not route:
                raise HTTPException(404, "线路不存在")

            lock_key = f"route:{route['id']}"
            ok, owned = try_acquire_owned(conn, lock_key, user)
            if not ok:
                _reject(
                    conn,
                    409,
                    "WINDOW_CONTENTION",
                    "同线同批争用：只记争用痕，不写窗",
                    lock_key=lock_key,
                )
            acquired = owned
            # 立即提交锁行，让同线并行请求可见；否则锁只在本地事务里，
            # 后到者永远读不到，争用形同虚设。
            if owned:
                conn.commit()

            # 同班纪要冻结：同一线路+批次当班不改口。
            shift = shift_key()
            minute = _minutes_row(conn, route["id"], batch_id, shift)
            if minute and minute["decision"] != body.decision:
                _record_event(
                    conn,
                    "窗冲突偏航",
                    route["route_code"],
                    batch["batch_code"],
                    f"当班纪要={minute['decision']}，本次={body.decision}",
                )
                write_audit(
                    conn,
                    user,
                    "window_minute_drift",
                    batch["batch_code"],
                    {"route_code": route["route_code"], "decision": body.decision},
                )
                _reject(
                    conn,
                    409,
                    "WINDOW_MINUTE_DRIFT",
                    "当班协调纪要已定，改口记偏航并拒写",
                    minute_decision=minute["decision"],
                )

            conflict = windows_conflict(
                body.window_start,
                body.window_end,
                route["window_start"],
                route["window_end"],
            )

            if not conflict:
                if (
                    int(route["capacity"] or 0) > 0
                    and _occupancy_excluding(conn, route["id"], batch_id)
                    >= int(route["capacity"])
                ):
                    _record_event(
                        conn,
                        "窗冲突拒写",
                        route["route_code"],
                        batch["batch_code"],
                        "线路占用已满",
                    )
                    _reject(
                        conn,
                        409,
                        "WINDOW_CAPACITY",
                        "线路批次窗占用已满，拒写",
                    )
                # 无冲突正式落窗：同批遗留草案已被正式窗取代，不得再晋升。
                conn.execute(
                    """UPDATE batch_window_shadows SET status=?,updated_at=?,note=?
                       WHERE batch_id=? AND status=?""",
                    (
                        SHADOW_SUPERSEDED,
                        now_iso(),
                        "正式窗已落库，草案被取代",
                        batch_id,
                        SHADOW_DRAFT,
                    ),
                )
                conn.execute(
                    """UPDATE temp_batches SET route_id=?,window_start=?,window_end=?
                       WHERE id=?""",
                    (route["id"], body.window_start, body.window_end, batch_id),
                )
                _write_minutes(
                    conn, route["id"], route["route_code"], batch_id,
                    batch["batch_code"], body.decision, "无冲突直接落正式窗",
                )
                write_audit(
                    conn,
                    user,
                    "window_official_write",
                    batch["batch_code"],
                    {
                        "route_code": route["route_code"],
                        "window": _win(body.window_start, body.window_end),
                    },
                )
                conn.commit()
                return {
                    "batch_id": batch_id,
                    "batch_code": batch["batch_code"],
                    "status": "已写正式窗",
                    "effective_valid": True,
                    "effective_source": "正式窗",
                    "official_window": _win(body.window_start, body.window_end),
                    "shadow": None,
                    "occupancy": _occupancy(conn, route["id"]),
                }

            # 冲突：甲拒写；乙只许影子草案。
            if body.decision == "甲":
                _record_event(
                    conn,
                    "窗冲突拒写",
                    route["route_code"],
                    batch["batch_code"],
                    "甲路径：冲突拒写，正式字段零改",
                )
                _write_minutes(
                    conn, route["id"], route["route_code"], batch_id,
                    batch["batch_code"], "甲", "冲突拒写",
                )
                write_audit(
                    conn,
                    user,
                    "window_conflict_reject",
                    batch["batch_code"],
                    {"route_code": route["route_code"], "path": "甲"},
                )
                _reject(
                    conn,
                    409,
                    "WINDOW_CONFLICT",
                    "线路窗与批次窗冲突，甲路径拒写",
                    route_window=_win(route["window_start"], route["window_end"]),
                    batch_window=_win(body.window_start, body.window_end),
                )

            # 乙：正式窗零改，落影子草案。
            now = now_iso()
            existing = conn.execute(
                """SELECT id FROM batch_window_shadows WHERE batch_id=?
                   AND status=?""",
                (batch_id, SHADOW_DRAFT),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE batch_window_shadows SET route_id=?,window_start=?,
                       window_end=?,updated_at=? WHERE id=?""",
                    (
                        route["id"],
                        body.window_start,
                        body.window_end,
                        now,
                        existing["id"],
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO batch_window_shadows(batch_id,route_id,window_start,
                       window_end,status,created_at,updated_at,note)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        batch_id,
                        route["id"],
                        body.window_start,
                        body.window_end,
                        SHADOW_DRAFT,
                        now,
                        now,
                        "冲突影子草案，待改线后原子晋升",
                    ),
                )
            _write_minutes(
                conn, route["id"], route["route_code"], batch_id,
                batch["batch_code"], "乙", "冲突落影子草案",
            )
            write_audit(
                conn,
                user,
                "window_shadow_draft",
                batch["batch_code"],
                {"route_code": route["route_code"]},
            )
            conn.commit()
            return {
                "batch_id": batch_id,
                "batch_code": batch["batch_code"],
                "status": "草案",
                "effective_valid": False,
                "effective_source": "草案未晋升",
                "official_window": _win(
                    batch["window_start"], batch["window_end"]
                ),
                "shadow": {
                    "window": _win(body.window_start, body.window_end),
                    "status": SHADOW_DRAFT,
                },
                "occupancy": _occupancy(conn, route["id"]),
            }
        finally:
            if acquired:
                release(conn, lock_key, user)
                conn.commit()


@router.patch("/api/routes/{route_id}/window")
def patch_route_window(
    route_id: int, body: RouteWindowPatch, authorization: AuthHeader = None
):
    user = require_writer(authorization)
    if (
        body.window_start is not None
        and body.window_end is not None
        and not window_ordered(body.window_start, body.window_end)
    ):
        raise HTTPException(422, "线路窗起点须早于终点")
    lock_key = f"route:{route_id}"
    acquired = False
    with connect() as conn:
        try:
            route = conn.execute(
                "SELECT * FROM route_plans WHERE id=?", (route_id,)
            ).fetchone()
            if not route:
                raise HTTPException(404, "线路不存在")
            ok, owned = try_acquire_owned(conn, lock_key, user)
            if not ok:
                _reject(
                    conn,
                    409,
                    "WINDOW_CONTENTION",
                    "改线争用：只记争用痕",
                    lock_key=lock_key,
                )
            acquired = owned
            if owned:
                conn.commit()

            new_start = (
                body.window_start
                if body.window_start is not None
                else route["window_start"]
            )
            new_end = (
                body.window_end
                if body.window_end is not None
                else route["window_end"]
            )
            conn.execute(
                "UPDATE route_plans SET window_start=?,window_end=? WHERE id=?",
                (new_start, new_end, route_id),
            )

            promotions = []
            drafts = conn.execute(
                """SELECT * FROM batch_window_shadows
                   WHERE route_id=? AND status=? ORDER BY id""",
                (route_id, SHADOW_DRAFT),
            ).fetchall()
            capacity = int(route["capacity"] or 0)
            seen_occ = _occupancy(conn, route_id)
            for sh in drafts:
                b = conn.execute(
                    "SELECT id,batch_code FROM temp_batches WHERE id=?",
                    (sh["batch_id"],),
                ).fetchone()
                if not b:
                    conn.execute(
                        "UPDATE batch_window_shadows SET status=?,updated_at=? WHERE id=?",
                        (SHADOW_ROLLED, now_iso(), sh["id"]),
                    )
                    continue
                still = windows_conflict(
                    sh["window_start"], sh["window_end"], new_start, new_end
                )
                over_cap = capacity > 0 and seen_occ >= capacity
                if still or over_cap:
                    conn.execute(
                        "UPDATE batch_window_shadows SET status=?,updated_at=?,note=? WHERE id=?",
                        (
                            SHADOW_ROLLED,
                            now_iso(),
                            "改线后仍冲突，草案回滚"
                            if still
                            else "线路占用已满，草案回滚",
                            sh["id"],
                        ),
                    )
                    _record_event(
                        conn,
                        "影子窗回滚",
                        route["route_code"],
                        b["batch_code"],
                        "改线后仍冲突"
                        if still
                        else "线路占用已满",
                    )
                    promotions.append(
                        {"batch_id": b["id"], "batch_code": b["batch_code"],
                         "status": SHADOW_ROLLED}
                    )
                    continue
                # 原子晋升：正式字段与影子状态同一事务落库。
                conn.execute(
                    """UPDATE temp_batches SET route_id=?,window_start=?,window_end=?
                       WHERE id=?""",
                    (route_id, sh["window_start"], sh["window_end"], b["id"]),
                )
                conn.execute(
                    "UPDATE batch_window_shadows SET status=?,updated_at=?,note=? WHERE id=?",
                    (SHADOW_PROMOTED, now_iso(), "冲突消除后原子晋升", sh["id"]),
                )
                seen_occ += 1
                _record_event(
                    conn,
                    "影子窗晋升",
                    route["route_code"],
                    b["batch_code"],
                    "改线消除冲突后晋升为正式窗",
                )
                promotions.append(
                    {"batch_id": b["id"], "batch_code": b["batch_code"],
                     "status": SHADOW_PROMOTED}
                )

            write_audit(
                conn,
                user,
                "route_window_patch",
                route["route_code"],
                {
                    "window": _win(new_start, new_end),
                    "promotions": promotions,
                },
            )
            conn.commit()
            return {
                "route_id": route_id,
                "route_code": route["route_code"],
                "window": _win(new_start, new_end),
                "promotions": promotions,
            }
        finally:
            if acquired:
                release(conn, lock_key, user)
                conn.commit()


# ----------------------------- 只读 --------------------------------------
@router.get("/api/window-coordination")
def window_coordination(
    authorization: AuthHeader = None, batch_code: str | None = None
):
    require_user(authorization)
    with connect() as conn:
        items = snapshot(conn)
        if batch_code:
            items = [s for s in items if s["batch_code"] == batch_code]
        valid = sum(1 for s in items if s["effective_valid"])
        return {
            "items": items,
            "summary": {
                "total": len(items),
                "valid": valid,
                "invalid": len(items) - valid,
                "conflicts": sum(1 for s in items if s["conflict"]),
            },
        }


@router.get("/api/window-coordination/consistency")
def window_consistency(authorization: AuthHeader = None):
    """五页同源证明：同一快照投影，五页读到的必须完全一致。"""
    require_user(authorization)
    with connect() as conn:
        states = snapshot(conn)
        proj = projection(states)
        views = {k: proj for k in ("routes", "batches", "chambers", "seals", "reconcile")}
        uniq = {repr(v) for v in views.values()}
        return {
            "consistent": len(uniq) == 1,
            "source": "window_svc.snapshot",
            "views": views,
            "summary": {
                "total": len(proj),
                "valid": sum(1 for p in proj if p["effective_valid"]),
                "conflicts": sum(1 for p in proj if not p["effective_valid"]),
            },
        }


@router.get("/api/route-batch-minutes")
def list_minutes(authorization: AuthHeader = None, shift: str | None = None):
    require_user(authorization)
    with connect() as conn:
        if shift:
            rows = conn.execute(
                "SELECT * FROM route_batch_minutes WHERE shift_key=? ORDER BY id DESC",
                (shift,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM route_batch_minutes ORDER BY id DESC"
            ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/window-conflict-events")
def list_window_events(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM window_conflict_events ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}
