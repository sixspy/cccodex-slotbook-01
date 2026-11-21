"""正式册释放/重开 —— 甲路径：整笔事务释放（见 docs/释放还席说明.md，已当日锁死）。

同一连接同一事务内四步同生共死：
  1) 抢 slot:{code} 档锁，抢不到只留争用痕（contention_log），业务零写；
  2) 正式单 bookings 改“已作废”；
  3) 档期盘 slots.booked 扣减（slots_svc.return_seats 同事务还席原语）；
  4) 写审计 booking_release + 落 release_jobs（状态“已释放”）。
任一步抛错 → rollback，作废与扣减同滚，正式册/档期盘零改。
明确禁用乙路径（释放影子稿 + 原子切换），全仓不得出现两套混用。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user, require_writer
from db import connect, write_audit
from locks import release, try_acquire
from slots_svc import return_seats

router = APIRouter(tags=["release"])


class ReleaseIn(BaseModel):
    booking_id: int
    note: str = ""


@router.get("/api/release/jobs")
def list_jobs(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM release_jobs ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/release")
def release_booking(body: ReleaseIn, authorization: AuthHeader = None):
    """甲路径：整笔事务释放，席位真还；失败整体回滚。"""
    user = require_writer(authorization)
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as conn:
        pre = conn.execute(
            "SELECT * FROM bookings WHERE id=?", (body.booking_id,)
        ).fetchone()
        if not pre:
            raise HTTPException(404, "正式预订不存在")
        slot = conn.execute(
            "SELECT * FROM slots WHERE id=?", (pre["slot_id"],)
        ).fetchone()
        if not slot:
            raise HTTPException(404, "档期不存在")
        lock_key = f"slot:{slot['code']}"

        # ① 抢档锁：后到者只留争用痕，不写任何业务
        if not try_acquire(conn, lock_key, user):
            conn.commit()  # 仅持久化 contention_log 争用痕
            raise HTTPException(409, "争用：后到只留争用，不写业务")

        try:
            # 锁内重读，防持锁等待期间状态已变（双人抢释/重复释放）
            b = conn.execute(
                "SELECT * FROM bookings WHERE id=?", (body.booking_id,)
            ).fetchone()
            if not b:
                raise HTTPException(404, "正式预订不存在")
            if b["status"] != "已确认":
                raise HTTPException(409, f"正式单状态为“{b['status']}”，禁止重复释放")

            # ②③④ 同一事务：正式作废 → 档期盘扣占用 → 审计 → 任务单
            conn.execute(
                "UPDATE bookings SET status='已作废' WHERE id=?", (b["id"],)
            )
            return_seats(conn, b["slot_id"], b["seats"])
            write_audit(
                conn,
                user,
                "booking_release",
                str(b["id"]),
                {
                    "slot": slot["code"],
                    "party_key": b["party_key"],
                    "returned_seats": b["seats"],
                    "note": body.note or "",
                    "path": "A_SINGLE_TX",
                },
            )
            cur = conn.execute(
                """INSERT INTO release_jobs(booking_id,status,note,created_at)
                   VALUES (?,?,?,?)""",
                (b["id"], "已释放", body.note or "整笔事务释放还席", now),
            )
            conn.commit()
            return {
                "ok": True,
                "job_id": cur.lastrowid,
                "booking_id": b["id"],
                "slot_code": slot["code"],
                "returned_seats": b["seats"],
            }
        except Exception:
            # 任一步失败：作废与扣减同滚，正式零改
            conn.rollback()
            raise
        finally:
            release(conn, lock_key, user)
            conn.commit()
