"""真实多线程并发确认：两个批次针对同一样本同时确认。

SQLite 不支持 SELECT ... FOR UPDATE，因此用文件库 + WAL + busy_timeout，并在
每个事务开始时执行 BEGIN IMMEDIATE：事务一开启即持有库级写锁，后到的事务在
busy_timeout 内等待，先到者提交后才开始读取——等价模拟 MySQL/InnoDB 研究行锁
串行化两个确认事务的效果。期望：一个成功 200，另一个读到漂移后整批 409，
样本只被写一次，留痕为一 confirmed 一 conflicted。
"""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import jwt
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from src.config.settings import JWT_SECRET
from src.models import User, CompositionStudy, ComponentSpec, CompositionSample, SampleCorrection
from src.routes import corrections
from src.config.database import Base, get_db

DB_PATH = os.path.join(tempfile.gettempdir(), "composition_concurrency_test.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _sqlite_pragma(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()


@event.listens_for(engine, "begin")
def _begin_immediate(conn):
    # 事务开启即取写锁，模拟 InnoDB 行锁对并发确认的串行化
    conn.exec_driver_sql("BEGIN IMMEDIATE")


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
app.dependency_overrides[get_db] = _override_db
client = TestClient(app)
CODES = ["char", "silicate", "carbonate", "phosphate", "other"]

with TestSession() as db:
    u = User(email="a@t.demo", name="admin", role="admin", password_hash="x")
    db.add(u)
    db.flush()
    token = jwt.encode({"sub": str(u.id), "exp": 9999999999}, JWT_SECRET, algorithm="HS256")
    st = CompositionStudy(code="LS-CC", name="并发", description="d", source="csv", default_delta=0.001)
    db.add(st)
    db.flush()
    for i, c in enumerate(CODES):
        db.add(ComponentSpec(study_id=st.id, code=c, label=c, unit="u", position=i, meaning="m"))
    vals = {c: 10.0 + i for i, c in enumerate(CODES)}
    db.add(CompositionSample(study_id=st.id, code="LS-CC-0-1", group="工艺A", values=vals, note="n"))
    db.commit()
    sid, sample_id = st.id, db.query(CompositionSample).first().id

HEADERS = {"Authorization": "Bearer " + token}


def make_batch(char_value):
    body = {"study_id": sid, "reason": "化验复测后批量更正并发冲突验证用例。",
            "items": [{"sample_id": sample_id, "group": "工艺B",
                       "values": {**{c: 30.0 + i for i, c in enumerate(CODES)}, "char": char_value}}]}
    return client.post("/corrections/preview", json=body, headers=HEADERS).json()["id"]


def test_parallel_confirms_one_wins_one_rejected():
    cid1, cid2 = make_batch(41.0), make_batch(42.0)
    barrier = threading.Barrier(2)
    results = {}
    errors = []

    def worker(key, cid):
        barrier.wait()
        try:
            r = client.post(f"/corrections/{cid}/confirm", headers=HEADERS)
            results[key] = r.status_code
        except Exception as exc:  # 并发下不允许出现连接/事务级异常
            errors.append(repr(exc))

    t1 = threading.Thread(target=worker, args=("a", cid1))
    t2 = threading.Thread(target=worker, args=("b", cid2))
    t1.start()
    t2.start()
    t1.join(timeout=40)
    t2.join(timeout=40)

    assert not errors, errors
    assert sorted(results.values()) == [200, 409], results
    with TestSession() as db:
        rows = {r.id: r for r in db.query(SampleCorrection).all()}
        assert sorted(r.status for r in rows.values()) == ["confirmed", "conflicted"]
        loser = next(r for r in rows.values() if r.status == "conflicted")
        assert loser.conflict["drift"][0]["sample_id"] == sample_id
        sample = db.get(CompositionSample, sample_id)
        # 只保留先确认者写入的值，且为两个候选值之一
        assert sample.group == "工艺B" and sample.values["char"] in (41.0, 42.0)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
