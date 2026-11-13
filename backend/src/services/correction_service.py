import math

from fastapi import HTTPException

from ..models import CompositionSample, ComponentSpec, SampleCorrection, CompositionStudy
from ..utils.clock import now
from .study_service import get_study
from .audit_service import log

REASON_UNKNOWN = "未知样本，不写入"
REASON_DUPLICATE = "同批重复样本，仅保留首条，其余不写入"
REASON_PHASE = "缺相或含未定义相，不写入"
REASON_NUMERIC = "非法数值（非有限数、负数或全零），不写入"
REASON_UNCHANGED = "与当前数据一致，无需更正"


def _numeric_error(values):
    if not isinstance(values, dict) or not values:
        return REASON_NUMERIC
    for v in values.values():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            return REASON_NUMERIC
    if all(v == 0 for v in values.values()):
        return REASON_NUMERIC
    return ""


def _safe_values(values):
    """净化不能写入 JSON 列的值（NaN/Inf 统一记为 None），仅用于留痕展示。"""
    clean = {}
    for k, v in (values or {}).items():
        clean[k] = v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None
    return clean


def _build_items(db, study, payload):
    components = db.query(ComponentSpec).filter_by(study_id=study.id).order_by(ComponentSpec.position).all()
    codes = [c.code for c in components]
    labels = {c.code: c.label for c in components}
    samples = {s.id: s for s in db.query(CompositionSample).filter_by(study_id=study.id)}
    seen, items = set(), []
    for raw in payload:
        data = raw.model_dump()
        sid, group, values = data["sample_id"], data["group"], data["values"]
        sample = samples.get(sid)
        reject, changes, before_group, before_values = "", [], "", {}
        safe_values = _safe_values(values)
        if sample is None:
            reject = REASON_UNKNOWN
        elif sid in seen:
            reject = REASON_DUPLICATE
        else:
            seen.add(sid)
            before_group, before_values = sample.group, sample.values
            if set(values.keys()) != set(codes):
                reject = REASON_PHASE
            else:
                reject = _numeric_error(values)
                if not reject:
                    if group != sample.group:
                        changes.append({"field": "group", "label": "组别", "before": sample.group, "after": group})
                    for code in codes:
                        if values[code] != sample.values[code]:
                            changes.append({"field": "values." + code, "label": labels[code], "before": sample.values[code], "after": values[code]})
                    if not changes:
                        reject = REASON_UNCHANGED
        items.append({
            "sample_id": sid, "code": sample.code if sample else "", "group": group, "values": safe_values,
            "included": not reject and bool(changes), "changed": bool(changes), "reject_reason": reject,
            "changes": changes, "before_group": before_group, "before_values": before_values,
        })
    return items


def _view(row):
    items = row.items or []
    return {
        "id": row.id, "study_id": row.study_id, "reason": row.reason, "status": row.status, "items": items,
        "conflict": row.conflict or {}, "created_by": row.created_by, "confirmed_by": row.confirmed_by,
        "created_at": row.created_at, "confirmed_at": row.confirmed_at,
        "included_count": sum(1 for i in items if i["included"]),
        "excluded_count": sum(1 for i in items if not i["included"]),
        "changed_count": sum(1 for i in items if i["changed"]),
    }


def preview_correction(db, data, user):
    study = get_study(db, data.study_id)
    items = _build_items(db, study, data.items)
    row = SampleCorrection(study_id=study.id, reason=data.reason, status="preview", items=items,
                           conflict={}, created_by=user.name)
    db.add(row)
    db.flush()
    log(db, user.email, "correction.preview", row.id)
    db.commit()
    return _view(row)


def list_corrections(db, sid=None):
    q = db.query(SampleCorrection)
    if sid is not None:
        get_study(db, sid)
        q = q.filter_by(study_id=sid)
    return [_view(r) for r in q.order_by(SampleCorrection.id.desc()).limit(200)]


def confirm_correction(db, cid, user):
    row = db.get(SampleCorrection, cid)
    if not row:
        raise HTTPException(404, "更正批次不存在")
    # 重复确认直接返回既有结果，不产生重复更正
    if row.status == "confirmed":
        return _view(row)
    if row.status == "conflicted":
        raise HTTPException(409, (row.conflict or {}).get("message", "该批次已因并发冲突被整批拒绝，请重新预览"))

    # 研究行串行化同研究的并发确认，再按 id 顺序锁样本行避免死锁。
    # 已预览未确认的批次之间互不阻塞：先确认者按预览快照写入，
    # 后确认者通过下面的前后值漂移检测发现样本已被改写，整批拒绝。
    db.query(CompositionStudy).filter_by(id=row.study_id).with_for_update().first()
    included = [it for it in row.items if it["included"]]
    ids = sorted(it["sample_id"] for it in included)
    locked = {s.id: s for s in db.query(CompositionSample)
              .where(CompositionSample.id.in_(ids)).order_by(CompositionSample.id).with_for_update().all()}

    drift = []
    for it in included:
        s = locked.get(it["sample_id"])
        if s is None:
            drift.append({"sample_id": it["sample_id"], "code": it["code"], "reason": REASON_UNKNOWN})
            continue
        reasons = []
        if s.group != it["before_group"]:
            reasons.append("组别已由先确认的更正修改")
        if s.values != it["before_values"]:
            reasons.append("相对量已由先确认的更正修改")
        if reasons:
            drift.append({"sample_id": it["sample_id"], "code": it["code"], "reason": "；".join(reasons)})

    if drift:
        message = "并发更正冲突，本批整批拒绝且未写入任何样本；以下样本已被先确认的批次修改：" + "、".join(
            f"{d['code'] or d['sample_id']}（{d['reason']}）" for d in drift)
        row.status = "conflicted"
        row.conflict = {"message": message, "drift": drift}
        log(db, user.email, "correction.conflict", row.id)
        db.commit()
        raise HTTPException(409, message)

    sample_rows = [locked[it["sample_id"]] for it in included]
    for it, s in zip(included, sample_rows):
        s.group = it["group"]
        s.values = it["values"]
    row.status = "confirmed"
    row.confirmed_by = user.name
    row.confirmed_at = now()
    log(db, user.email, "correction.confirm", f"{row.id}:{len(included)}")
    db.commit()
    return _view(row)
