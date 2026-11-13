"""批量复测更正的服务与 HTTP 规则测试。

用 SQLite 内存库 + FastAPI TestClient 跑真实路由（含 JWT 鉴权、422 校验、
409 冲突与幂等）。SQLite 不支持 SELECT ... FOR UPDATE，SQLAlchemy 会忽略该
子句，因此并发场景通过“两个预览批次先后确认”的真实调用路径验证冲突标记。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import jwt
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.config.database as database
from src.config.settings import JWT_SECRET
from src.models import (
    User, CompositionStudy, ComponentSpec, CompositionSample,
    CompositionRun, SampleCorrection,
)
from src.routes import corrections, audits, samples
from src.config.database import Base, get_db

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSession = sessionmaker(bind=engine, expire_on_commit=False)
Base.metadata.create_all(engine)


def _override_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


app = FastAPI()
app.include_router(corrections.router)
app.include_router(audits.router)
app.include_router(samples.router)
app.dependency_overrides[get_db] = _override_db
client = TestClient(app)

CODES = ["char", "silicate", "carbonate", "phosphate", "other"]


def token(role):
    with TestSession() as db:
        u = db.query(User).filter_by(role=role).first()
        if not u:
            u = User(email=f"{role}@t.demo", name=role, role=role, password_hash="x")
            db.add(u)
            db.commit()
        return jwt.encode({"sub": str(u.id), "exp": 9999999999}, JWT_SECRET, algorithm="HS256")


AUTH = {r: {"Authorization": "Bearer " + token(r)} for r in ("admin", "analyst", "viewer")}


def seed_study(code="LS-T1", groups=("工艺A", "工艺B")):
    with TestSession() as db:
        st = CompositionStudy(code=code, name="测试", description="d", source="csv", default_delta=0.001)
        db.add(st)
        db.flush()
        for i, c in enumerate(CODES):
            db.add(ComponentSpec(study_id=st.id, code=c, label=c, unit="相对量", position=i, meaning="m"))
        for gi, g in enumerate(groups):
            for i in range(3):
                vals = {c: float(10 + gi * 5 + i + k) for k, c in enumerate(CODES)}
                db.add(CompositionSample(study_id=st.id, code=f"{code}-{gi}-{i}", group=g,
                                         values=vals, note="n"))
        db.commit()
        return st.id


def sample_row(sid, code):
    with TestSession() as db:
        row = db.query(CompositionSample).filter_by(study_id=sid, code=code).first()
        return row.id, row.group, dict(row.values)


def good_values():
    return {c: 20.0 + i for i, c in enumerate(CODES)}


def payload(sid, items, reason="化验复测发现分组与相对量登记错误，批量更正。"):
    return {"study_id": sid, "reason": reason, "items": items}


def test_viewer_forbidden_preview_and_confirm():
    sid = seed_study("LS-F1")
    s0_id, _, _ = sample_row(sid, "LS-F1-0-0")
    body = payload(sid, [{"sample_id": s0_id, "group": "工艺A", "values": good_values()}])
    r = client.post("/corrections/preview", json=body, headers=AUTH["viewer"])
    assert r.status_code == 403
    r = client.post("/corrections/1/confirm", headers=AUTH["viewer"])
    assert r.status_code == 403


def test_preview_rejects_all_invalid_rows_and_confirms_only_valid():
    sid = seed_study("LS-P1")
    ids = [sample_row(sid, f"LS-P1-0-{i}")[0] for i in range(3)]
    ids2 = [sample_row(sid, f"LS-P1-1-{i}")[0] for i in range(3)]
    unknown_id = max(ids + ids2) + 500
    items = [
        # 正常更正（组别+一个相）
        {"sample_id": ids[0], "group": "工艺B", "values": {**good_values(), "char": 33.5}},
        # 未知样本
        {"sample_id": unknown_id, "group": "工艺A", "values": good_values()},
        # 缺相
        {"sample_id": ids[1], "group": "工艺A", "values": {k: v for k, v in good_values().items() if k != "char"}},
        # 非法数值：负数
        {"sample_id": ids[2], "group": "工艺A", "values": {**good_values(), "char": -1.0}},
        # 非法数值：全零
        {"sample_id": ids2[0], "group": "工艺A", "values": {c: 0 for c in CODES}},
        # 第二条正常更正（不同样本）
        {"sample_id": ids2[1], "group": "工艺B", "values": good_values()},
        # 同批重复样本（第二次出现，应排除）
        {"sample_id": ids2[1], "group": "工艺B", "values": {**good_values(), "char": 9.9}},
    ]
    r = client.post("/corrections/preview", json=payload(sid, items), headers=AUTH["analyst"])
    assert r.status_code == 201
    view = r.json()
    assert view["status"] == "preview"
    assert view["included_count"] == 2
    assert view["excluded_count"] == 5
    reasons = {i["sample_id"]: i["reject_reason"] for i in view["items"] if i["reject_reason"]}
    assert unknown_id in reasons and "未知" in reasons[unknown_id]
    assert any("缺相" in x for x in reasons.values())
    assert sum("非法数值" in x for x in reasons.values()) == 2
    assert any("重复" in x for x in reasons.values())
    first = next(i for i in view["items"] if i["sample_id"] == ids[0])
    assert first["included"] is True
    fields = [c["field"] for c in first["changes"]]
    assert "group" in fields and "values.char" in fields
    assert first["before_group"] == "工艺A"
    char_change = next(c for c in first["changes"] if c["field"] == "values.char")
    assert char_change["after"] == 33.5

    # 确认前样本未被改动
    _, g_before, v_before = sample_row(sid, "LS-P1-0-0")
    assert g_before == "工艺A" and v_before["char"] != 33.5

    cid = view["id"]
    r2 = client.post(f"/corrections/{cid}/confirm", headers=AUTH["admin"])
    assert r2.status_code == 200
    confirmed = r2.json()
    assert confirmed["status"] == "confirmed" and confirmed["confirmed_by"] == "admin"

    _, g_after, v_after = sample_row(sid, "LS-P1-0-0")
    assert g_after == "工艺B" and v_after["char"] == 33.5
    # 被排除的样本保持原值
    _, g2, v2 = sample_row(sid, "LS-P1-0-1")
    assert g2 == "工艺A" and v2["char"] != 20.0


def test_unchanged_row_is_excluded():
    sid = seed_study("LS-U1")
    s0_id, g0, v0 = sample_row(sid, "LS-U1-0-0")
    r = client.post("/corrections/preview",
                    json=payload(sid, [{"sample_id": s0_id, "group": g0, "values": v0}]),
                    headers=AUTH["analyst"])
    view = r.json()
    assert view["included_count"] == 0
    assert view["items"][0]["reject_reason"].startswith("与当前数据一致")


def test_repeated_confirm_is_idempotent():
    from src.models import Audit
    sid = seed_study("LS-I1")
    s0_id, _, _ = sample_row(sid, "LS-I1-0-0")
    cid = client.post("/corrections/preview",
                      json=payload(sid, [{"sample_id": s0_id, "group": "工艺B", "values": good_values()}]),
                      headers=AUTH["analyst"]).json()["id"]
    first = client.post(f"/corrections/{cid}/confirm", headers=AUTH["analyst"]).json()
    with TestSession() as db:
        confirms_before = db.query(Audit).filter_by(action="correction.confirm").count()
    second = client.post(f"/corrections/{cid}/confirm", headers=AUTH["analyst"]).json()
    assert first["status"] == second["status"] == "confirmed"
    with TestSession() as db:
        # 重复确认不新增确认留痕、不产生重复更正
        assert db.query(Audit).filter_by(action="correction.confirm").count() == confirms_before
        row = db.get(SampleCorrection, cid)
        assert row.status == "confirmed"
        assert row.confirmed_at == second["confirmed_at"]


def test_concurrent_batches_conflict_and_whole_batch_rejected():
    sid = seed_study("LS-C1")
    ids = [sample_row(sid, f"LS-C1-0-{i}")[0] for i in range(3)]
    ids2 = [sample_row(sid, f"LS-C1-1-{i}")[0] for i in range(3)]
    body1 = payload(sid, [
        {"sample_id": ids[0], "group": "工艺B", "values": good_values()},
        {"sample_id": ids[1], "group": "工艺B", "values": good_values()},
    ])
    body2 = payload(sid, [
        {"sample_id": ids[1], "group": "工艺B", "values": {**good_values(), "char": 44.0}},
        {"sample_id": ids[2], "group": "工艺B", "values": good_values()},
    ])
    c1 = client.post("/corrections/preview", json=body1, headers=AUTH["analyst"]).json()
    c2 = client.post("/corrections/preview", json=body2, headers=AUTH["analyst"]).json()
    # 先确认批次1并成功
    ok = client.post(f"/corrections/{c1['id']}/confirm", headers=AUTH["admin"])
    assert ok.status_code == 200 and ok.json()["status"] == "confirmed"
    # 批次2含已被批次1改写的样本，整批拒绝（批次2独有样本也不写入）
    blocked = client.post(f"/corrections/{c2['id']}/confirm", headers=AUTH["admin"])
    assert blocked.status_code == 409
    detail = blocked.json()["detail"]
    assert "冲突" in detail and "整批拒绝" in detail
    with TestSession() as db:
        row = db.get(SampleCorrection, c2["id"])
        assert row.status == "conflicted"
        drift_ids = {d["sample_id"] for d in row.conflict["drift"]}
        assert drift_ids == {ids[1]}
        # 批次2独有样本也不得写入
        s2 = db.get(CompositionSample, ids[2])
        assert s2.group == "工艺A"
    # 已冲突批次再次确认仍是 409
    again = client.post(f"/corrections/{c2['id']}/confirm", headers=AUTH["admin"])
    assert again.status_code == 409

    # 与已确认批次不相交的新批次可以正常确认
    body3 = payload(sid, [{"sample_id": ids2[0], "group": "工艺B", "values": good_values()}])
    c3 = client.post("/corrections/preview", json=body3, headers=AUTH["analyst"]).json()
    ok3 = client.post(f"/corrections/{c3['id']}/confirm", headers=AUTH["admin"])
    assert ok3.status_code == 200 and ok3.json()["status"] == "confirmed"
    _, g3, _ = sample_row(sid, "LS-C1-1-0")
    assert g3 == "工艺B"


def test_confirmed_history_keeps_before_and_after_values():
    sid = seed_study("LS-H1")
    s0_id, g0, v0 = sample_row(sid, "LS-H1-0-0")
    cid = client.post("/corrections/preview",
                      json=payload(sid, [{"sample_id": s0_id, "group": "工艺B",
                                          "values": {**v0, "char": v0["char"] + 7}}]),
                      headers=AUTH["analyst"]).json()["id"]
    client.post(f"/corrections/{cid}/confirm", headers=AUTH["analyst"])
    r = client.get(f"/studies/{sid}/corrections", headers=AUTH["analyst"])
    row = r.json()[0]
    item = row["items"][0]
    assert item["before_group"] == g0 and item["before_values"] == v0
    assert item["group"] == "工艺B" and item["values"]["char"] == v0["char"] + 7
    assert row["confirmed_at"] and row["confirmed_by"]


def test_existing_run_snapshot_keeps_original_basis():
    sid = seed_study("LS-R1")
    with TestSession() as db:
        original = db.query(CompositionSample).filter_by(study_id=sid).first()
        snap_group, snap_value = original.group, original.values["char"]
        db.add(CompositionRun(name="基线运行", study_id=sid, delta=0.001, permutations=199, seed=1,
                              input_snapshot={"samples": [{"code": original.code, "group": original.group,
                                                           "values": dict(original.values)}]},
                              status="completed", result={}, owner="analyst"))
        db.commit()
        sid_target = original.id
    new_vals = good_values()
    cid = client.post("/corrections/preview",
                      json=payload(sid, [{"sample_id": sid_target, "group": "工艺B", "values": new_vals}]),
                      headers=AUTH["analyst"]).json()["id"]
    client.post(f"/corrections/{cid}/confirm", headers=AUTH["analyst"])
    with TestSession() as db:
        run = db.query(CompositionRun).filter_by(study_id=sid).first()
        assert run.input_snapshot["samples"][0]["group"] == snap_group
        assert run.input_snapshot["samples"][0]["values"]["char"] == snap_value


def test_nan_payload_row_is_excluded_and_not_written():
    import json
    sid = seed_study("LS-N1")
    s0_id, _, _ = sample_row(sid, "LS-N1-0-0")
    raw = json.dumps(payload(sid, [{"sample_id": s0_id, "group": "工艺A",
                                    "values": {**good_values(), "char": float("nan")}}]), allow_nan=True)
    r = client.post("/corrections/preview", content=raw,
                    headers={**AUTH["analyst"], "Content-Type": "application/json"})
    assert r.status_code == 201
    view = r.json()
    assert view["included_count"] == 0
    assert "非法数值" in view["items"][0]["reject_reason"]
    # 确认也不会写入该样本
    cid = view["id"]
    cf = client.post(f"/corrections/{cid}/confirm", headers=AUTH["analyst"])
    assert cf.status_code == 200
    _, g, v = sample_row(sid, "LS-N1-0-0")
    assert g == "工艺A" and v["char"] == 10.0


def test_extra_field_is_422():
    sid = seed_study("LS-X1")
    s0_id, _, _ = sample_row(sid, "LS-X1-0-0")
    r = client.post("/corrections/preview",
                    json=payload(sid, [{"sample_id": s0_id, "group": "工艺A", "values": good_values(),
                                        "unexpected": 1}]),
                    headers=AUTH["analyst"])
    assert r.status_code == 422


def test_empty_items_and_bad_reason_are_422():
    sid = seed_study("LS-E1")
    r = client.post("/corrections/preview", json={"study_id": sid, "reason": "复测", "items": []},
                    headers=AUTH["analyst"])
    assert r.status_code == 422
    s0_id, _, _ = sample_row(sid, "LS-E1-0-0")
    r2 = client.post("/corrections/preview",
                     json=payload(sid, [{"sample_id": s0_id, "group": "工艺A", "values": good_values()}],
                                  reason="短"),
                     headers=AUTH["analyst"])
    assert r2.status_code == 422


def test_unknown_study_returns_404():
    r = client.post("/corrections/preview",
                    json=payload(9999, [{"sample_id": 1, "group": "工艺A", "values": good_values()}]),
                    headers=AUTH["analyst"])
    assert r.status_code == 404


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
