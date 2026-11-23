"""冷链线路计划。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from models import RouteIn
from window_svc import _occupancy, snapshot, window_ordered

router = APIRouter(tags=["routes"])


@router.get("/api/routes")
def list_routes(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM route_plans ORDER BY id"
        ).fetchall()
        states = {}
        for s in snapshot(conn):
            if s["route_code"]:
                states.setdefault(s["route_code"], []).append(s)
        items = []
        for r in rows:
            d = dict(r)
            d["occupancy"] = _occupancy(conn, r["id"])
            d["window_batches"] = [
                s["batch_code"] for s in states.get(r["route_code"], [])
            ]
            items.append(d)
        return {"items": items}


@router.get("/api/routes/{route_id}")
def get_route(route_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM route_plans WHERE id=?", (route_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "线路不存在")
        data = dict(row)
        data["occupancy"] = _occupancy(conn, route_id)
        data["window_batches"] = [
            s for s in snapshot(conn) if s["route_code"] == row["route_code"]
        ]
        return data


@router.post("/api/routes")
def create_route(body: RouteIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    if (body.window_start or body.window_end) and not (
        body.window_start and body.window_end
    ):
        raise HTTPException(422, "线路窗须同时给出起讫")
    if body.window_start and body.window_end and not window_ordered(
        body.window_start, body.window_end
    ):
        raise HTTPException(422, "线路窗起点须早于终点")
    with connect() as conn:
        if conn.execute(
            "SELECT id FROM route_plans WHERE route_code=?", (body.route_code,)
        ).fetchone():
            raise HTTPException(409, "线路编码已存在")
        cur = conn.execute(
            """INSERT INTO route_plans(route_code,origin_region,dest_region,cold_hours,
               vehicle_type,status,created_at,window_start,window_end,capacity)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                body.route_code,
                body.origin_region,
                body.dest_region,
                body.cold_hours,
                body.vehicle_type,
                "启用",
                now_iso(),
                body.window_start,
                body.window_end,
                body.capacity,
            ),
        )
        write_audit(
            conn,
            user,
            "route_create",
            body.route_code,
            {
                "id": cur.lastrowid,
                "window": [body.window_start, body.window_end],
                "capacity": body.capacity,
            },
        )
        conn.commit()
        return {
            "id": cur.lastrowid,
            "route_code": body.route_code,
            "status": "启用",
            "window_start": body.window_start,
            "window_end": body.window_end,
            "capacity": body.capacity,
        }