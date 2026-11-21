"""释放还席：假绿夹具 + 双红失败测（先红后绿，禁止跳过）。

夹具（本机文件库 tmp_path/t.db）：
  S-EVE-03 容量 6，先造 6 笔已确认正式单把档期顶满，再压 1 笔候补 →
  档期正式单点了释放，若释放服务不还席，占用纹丝不动、候补顶死无法晋升，
  接口却仍回 200——即“假绿”。

两个失败测：
  1. test_release_returns_seats_and_waitlist_can_promote：释放未还席失败测
  2. test_double_release_contention_only_leaves_trace：双人同释争用失败测
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import app  # noqa: E402
from db import init_db  # noqa: E402
import db as dbmod  # noqa: E402

SLOT_CODE = "S-EVE-03"
CAPACITY = 6
LOCK_KEY = f"slot:{SLOT_CODE}"


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(dbmod, "DB_PATH", db)
    if db.exists():
        db.unlink()
    init_db()
    yield


def _login(c: TestClient, user: str = "admin", password: str = "123456"):
    r = c.post("/api/login", json={"username": user, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _hold_id(c, h, party: str) -> int:
    items = c.get("/api/holds", headers=h).json()["items"]
    return [x for x in items if x["party_key"] == party][0]["id"]


def _booking_id(c, h, party: str) -> int:
    items = c.get("/api/bookings", headers=h).json()["items"]
    return [b for b in items if b["party_key"] == party][0]["id"]


def _wait_id(c, h, party: str) -> int:
    items = c.get("/api/waitlist", headers=h).json()["items"]
    return [w for w in items if w["party_key"] == party][0]["id"]


@pytest.fixture()
def saturated_evening():
    """假绿夹具：S-EVE-03 六笔正式单顶满 + 一笔候补顶着满员。"""
    with TestClient(app) as c:
        h = _login(c)
        for i in range(CAPACITY):
            r = c.post(
                "/api/holds",
                headers=h,
                json={"slot_code": SLOT_CODE, "party_key": f"P-F{i}", "seats": 1},
            )
            assert r.status_code == 200 and r.json()["disposition"] == "占位", r.text
            hid = _hold_id(c, h, f"P-F{i}")
            r = c.post("/api/bookings/confirm", headers=h, json={"hold_id": hid})
            assert r.status_code == 200, r.text
        # 满员后再压一笔：只能进候补
        r = c.post(
            "/api/holds",
            headers=h,
            json={"slot_code": SLOT_CODE, "party_key": "P-OVER", "seats": 1},
        )
        assert r.status_code == 200 and r.json()["disposition"] == "候补", r.text

        slot = c.get(f"/api/slots/{SLOT_CODE}", headers=h).json()
        assert slot["booked"] == CAPACITY  # 夹具基线：占用顶满
        return {
            "client": c,
            "headers": h,
            "booking_id": _booking_id(c, h, "P-F0"),
            "wait_id": _wait_id(c, h, "P-OVER"),
        }


def test_release_returns_seats_and_waitlist_can_promote(saturated_evening):
    """失败测①：释放必须真还席，空位回来且候补可晋，五处同态。"""
    c, h = saturated_evening["client"], saturated_evening["headers"]
    bid, wid = saturated_evening["booking_id"], saturated_evening["wait_id"]

    r = c.post("/api/release", headers=h, json={"booking_id": bid, "note": "客户退档"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("stub") is not True  # 禁止再走空桩
    assert body["ok"] is True

    # ① 正式册：正式单真作废，不是只改角标
    book = [b for b in c.get("/api/bookings", headers=h).json()["items"] if b["id"] == bid][0]
    assert book["status"] == "已作废"

    # ② 档期盘：占用真的还回去（6 → 5）
    slot = c.get(f"/api/slots/{SLOT_CODE}", headers=h).json()
    assert slot["booked"] == CAPACITY - 1

    # ③ 对账灯：盘面占用与在册正式单同源，释放单不计入
    recon = c.get("/api/reconcile", headers=h).json()
    assert recon["all_ok"] is True
    row = [i for i in recon["items"] if i["slot_code"] == SLOT_CODE][0]
    assert row["slot_booked"] == CAPACITY - 1
    assert row["booking_seats"] == CAPACITY - 1
    assert row["released_seats"] == 1

    # ④ 候补队：空位回来后候补可晋升，再确认后重新顶满
    r = c.post("/api/waitlist/promote", headers=h, json={"wait_id": wid})
    assert r.status_code == 200, r.text
    new_hold = r.json()["hold_id"]
    w = [x for x in c.get("/api/waitlist", headers=h).json()["items"] if x["id"] == wid][0]
    assert w["status"] == "已晋升"
    r = c.post("/api/bookings/confirm", headers=h, json={"hold_id": new_hold})
    assert r.status_code == 200, r.text
    slot = c.get(f"/api/slots/{SLOT_CODE}", headers=h).json()
    assert slot["booked"] == CAPACITY
    assert c.get("/api/reconcile", headers=h).json()["all_ok"] is True

    # ⑤ 审计条：释放动作留痕，且带还席数
    audits = c.get("/api/audit", headers=h).json()["items"]
    rel = [a for a in audits if a["action"] == "booking_release" and a["target"] == str(bid)]
    assert len(rel) == 1
    assert '"returned_seats": 1' in rel[0]["payload"]

    # 释放任务单不再是空桩状态
    jobs = c.get("/api/release/jobs", headers=h).json()["items"]
    assert jobs and jobs[0]["status"] == "已释放"


def test_release_atomic_rollback_when_audit_fails(saturated_evening, monkeypatch):
    """安全网：审计步失败 → 整笔回滚，作废与扣减同滚，正式零改。"""
    import release_svc

    def _boom(*_a, **_kw):
        raise RuntimeError("模拟审计写入失败")

    c, h = saturated_evening["client"], saturated_evening["headers"]
    bid = saturated_evening["booking_id"]

    # 故障注入只作用于这一段；退出 context 后写审计恢复正常
    with monkeypatch.context() as m:
        m.setattr(release_svc, "write_audit", _boom)
        with TestClient(app, raise_server_exceptions=False) as c2:
            h2 = _login(c2)
            r = c2.post("/api/release", headers=h2, json={"booking_id": bid})
            assert r.status_code == 500

    # 整笔回滚：正式册未作废、档期盘占用不降、无任务单残留、对账仍同源
    book = [b for b in c.get("/api/bookings", headers=h).json()["items"] if b["id"] == bid][0]
    assert book["status"] == "已确认"
    slot = c.get(f"/api/slots/{SLOT_CODE}", headers=h).json()
    assert slot["booked"] == CAPACITY
    assert c.get("/api/release/jobs", headers=h).json()["items"] == []
    assert c.get("/api/reconcile", headers=h).json()["all_ok"] is True

    # 回滚后档锁已释放，正常重试可成功
    r = c.post("/api/release", headers=h, json={"booking_id": bid})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert c.get(f"/api/slots/{SLOT_CODE}", headers=h).json()["booked"] == CAPACITY - 1


def test_repeat_release_rejected(saturated_evening):
    """安全网：已作废单再释，409 拒绝且占用不二次扣减。"""
    c, h = saturated_evening["client"], saturated_evening["headers"]
    bid = saturated_evening["booking_id"]
    r = c.post("/api/release", headers=h, json={"booking_id": bid})
    assert r.status_code == 200
    r = c.post("/api/release", headers=h, json={"booking_id": bid})
    assert r.status_code == 409
    # 只还了一次席，没有被扣成 4
    assert c.get(f"/api/slots/{SLOT_CODE}", headers=h).json()["booked"] == CAPACITY - 1
    assert c.get("/api/reconcile", headers=h).json()["all_ok"] is True


def test_double_release_contention_only_leaves_trace(saturated_evening):
    """失败测②：两人抢释同一正式单，后到只留争用痕，正式零改。"""
    c, h = saturated_evening["client"], saturated_evening["headers"]
    bid = saturated_evening["booking_id"]

    # 另一人先持档锁（模拟双人同时抢释）
    hc = _login(c, "clerk", "123456")
    r = c.post("/api/locks/acquire", headers=hc, json={"lock_key": LOCK_KEY})
    assert r.status_code == 200, r.text

    r = c.post("/api/release", headers=h, json={"booking_id": bid})
    assert r.status_code == 409, r.text

    # 争用痕：后到者登记 contention_log
    traces = c.get("/api/contention", headers=h).json()["items"]
    mine = [t for t in traces if t["lock_key"] == LOCK_KEY and t["actor"] == "admin"]
    assert len(mine) == 1
    assert mine[0]["action"] == "争用"

    # 业务零写：正式册仍是已确认、档期盘占用不降、无释放审计、无释放任务单
    book = [b for b in c.get("/api/bookings", headers=h).json()["items"] if b["id"] == bid][0]
    assert book["status"] == "已确认"
    slot = c.get(f"/api/slots/{SLOT_CODE}", headers=h).json()
    assert slot["booked"] == CAPACITY
    audits = c.get("/api/audit", headers=h).json()["items"]
    assert not [a for a in audits if a["action"] == "booking_release"]
    assert c.get("/api/release/jobs", headers=h).json()["items"] == []
