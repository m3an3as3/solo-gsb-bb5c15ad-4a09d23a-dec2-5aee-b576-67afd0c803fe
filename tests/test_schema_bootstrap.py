"""启动期持久库结构核对测试（半旧库不得被报告为就绪）。

覆盖：
  - 空库：按模型初始化全部应用表，API 正常就绪；
  - 半旧库：已有 events 表缺少 reveal_deadline 等必需列、或缺少其它应用表时，
    不补列也不补建其它表，/health 返回 503 并明确列出缺失表名与列名，
    业务接口同样被阻止（503 而非访问缺列后的 500）；
  - 完整旧库：结构与模型一致时原样通过，既有数据保留可读。
"""

import tempfile
import uuid
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

import app.main as main_module
from app.database import Base, get_db
from app.models import Event
from app.schema_bootstrap import ensure_schema

T0 = datetime(2030, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def override_get_db(engine):
    """让 get_db 依赖使用测试引擎（默认依赖绑定的是 Postgres 引擎）。"""
    def _get_db():
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()
    main_module.app.dependency_overrides[get_db] = _get_db


def make_engine():
    tmpdir = tempfile.mkdtemp(prefix="sealed_predictions_schema_test_")
    return create_engine(f"sqlite:///{tmpdir}/schema.db")


def table_names(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def columns_of(engine, table: str) -> set[str]:
    return {col["name"] for col in inspect(engine).get_columns(table)}


# --------------------------------------------------------------------------- #
# ensure_schema 直接核对
# --------------------------------------------------------------------------- #

def test_empty_database_is_initialized_with_all_tables():
    engine = make_engine()
    try:
        assert table_names(engine) == set()
        state = ensure_schema(engine)
        assert state.ready is True
        assert state.empty_initialized is True
        assert state.describe() == ""
        assert table_names(engine) == {
            "events",
            "commitments",
            "reveals",
            "deadline_extensions",
            "settlements",
        }
    finally:
        engine.dispose()


def test_stale_events_missing_reveal_deadline_is_reported_not_repaired():
    engine = make_engine()
    try:
        with engine.begin() as conn:
            # 旧版 events：缺少 reveal_deadline（即触发事件接口 500 的半旧结构）
            conn.execute(text(
                "CREATE TABLE events ("
                "id CHAR(32) NOT NULL PRIMARY KEY, "
                "title VARCHAR(200) NOT NULL, "
                "description TEXT, "
                "commit_deadline DATETIME NOT NULL, "
                "created_at DATETIME NOT NULL)"
            ))
            # 半旧库中已有数据，核对不得破坏
            conn.execute(text(
                "INSERT INTO events (id, title, commit_deadline, created_at) "
                "VALUES ('e1', '旧事件', '2030-01-01 00:00:00', '2030-01-01 00:00:00')"
            ))

        state = ensure_schema(engine)
        assert state.ready is False
        assert state.empty_initialized is False
        # 明确列出缺失列名（events.reveal_deadline）
        assert state.missing_columns == {"events": ["reveal_deadline"]}
        # 同时列出其余全部缺失的应用表
        assert set(state.missing_tables) == {
            "commitments",
            "reveals",
            "deadline_extensions",
            "settlements",
        }
        message = state.describe()
        assert "events" in message and "reveal_deadline" in message
        assert "commitments" in message

        # 不补列、不在半旧库上补建其它表
        assert "reveal_deadline" not in columns_of(engine, "events")
        assert table_names(engine) == {"events"}
        # 旧数据原样保留
        with engine.connect() as conn:
            (title,) = conn.execute(text("SELECT title FROM events")).one()
            assert title == "旧事件"
    finally:
        engine.dispose()


def test_stale_table_missing_several_columns_lists_all_of_them():
    engine = make_engine()
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE events (id CHAR(32) NOT NULL PRIMARY KEY, title VARCHAR(200))"
            ))

        state = ensure_schema(engine)
        assert state.ready is False
        assert set(state.missing_columns["events"]) == {
            "description",
            "commit_deadline",
            "reveal_deadline",
            "created_at",
        }
        assert table_names(engine) == {"events"}
    finally:
        engine.dispose()


def test_complete_legacy_database_passes_and_preserves_data():
    engine = make_engine()
    event_id = uuid.uuid4()
    try:
        # 由当前模型建表（等价于历史版本结构恰好完整的旧库），写入一条数据
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(Event(
                id=event_id,
                title="完整旧库事件",
                commit_deadline=T0,
                reveal_deadline=T0.replace(hour=1),
                created_at=T0,
            ))
            session.commit()

        state = ensure_schema(engine)
        assert state.ready is True
        assert state.empty_initialized is False

        # 数据保留且可按模型正常读取
        with Session(engine) as session:
            event = session.get(Event, event_id)
            assert event is not None
            assert event.title == "完整旧库事件"
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- #
# API 就绪门控（lifespan + /health + 业务接口）
# --------------------------------------------------------------------------- #

def test_lifespan_blocks_api_on_stale_database(monkeypatch):
    engine = make_engine()
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE events ("
            "id CHAR(32) NOT NULL PRIMARY KEY, "
            "title VARCHAR(200) NOT NULL, "
            "description TEXT, "
            "commit_deadline DATETIME NOT NULL, "
            "created_at DATETIME NOT NULL)"
        ))

    monkeypatch.setattr(main_module, "engine", engine)
    monkeypatch.setattr(main_module, "schema_state", None)
    client = TestClient(main_module.app)
    try:
        with client:  # 触发 lifespan 核对
            # 半旧库未补建任何东西
            assert table_names(engine) == {"events"}
            assert "reveal_deadline" not in columns_of(engine, "events")

            # /health 不再误报 ok：503 并明确列出表名与列名
            r = client.get("/health")
            assert r.status_code == 503
            body = r.json()
            assert body["status"] == "not_ready"
            assert body["code"] == "SCHEMA_NOT_READY"
            assert body["missing_columns"]["events"] == ["reveal_deadline"]
            assert set(body["missing_tables"]) == {
                "commitments",
                "reveals",
                "deadline_extensions",
                "settlements",
            }

            # 业务接口被阻止为明确的 503（而不是访问缺列后抛出 500）
            r = client.get("/events")
            assert r.status_code == 503
            assert r.json()["code"] == "SCHEMA_NOT_READY"
            r = client.post(
                "/events",
                json={
                    "title": "x",
                    "commit_deadline": "2030-01-01T00:00:00Z",
                    "reveal_deadline": "2030-01-02T00:00:00Z",
                },
            )
            assert r.status_code == 503

            # 核对期间未在半旧库上补建其它表
            assert table_names(engine) == {"events"}
    finally:
        client.close()
        engine.dispose()


def test_lifespan_initializes_empty_database_and_serves(monkeypatch):
    engine = make_engine()
    monkeypatch.setattr(main_module, "engine", engine)
    monkeypatch.setattr(main_module, "schema_state", None)
    override_get_db(engine)
    client = TestClient(main_module.app)
    try:
        with client:
            assert table_names(engine) == {
                "events",
                "commitments",
                "reveals",
                "deadline_extensions",
                "settlements",
            }
            assert client.get("/health").json() == {"status": "ok"}
            r = client.post(
                "/events",
                json={
                    "title": "空库新建",
                    "commit_deadline": "2030-06-01T00:00:00Z",
                    "reveal_deadline": "2030-06-02T00:00:00Z",
                },
            )
            assert r.status_code == 201, r.text
    finally:
        client.close()
        main_module.app.dependency_overrides.clear()
        engine.dispose()


def test_lifespan_on_complete_legacy_database_serves_existing_data(monkeypatch):
    engine = make_engine()
    event_id = uuid.uuid4()
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Event(
            id=event_id,
            title="待保留事件",
            commit_deadline=T0,
            reveal_deadline=T0.replace(hour=1),
            created_at=T0,
        ))
        session.commit()

    monkeypatch.setattr(main_module, "engine", engine)
    monkeypatch.setattr(main_module, "schema_state", None)
    override_get_db(engine)
    client = TestClient(main_module.app)
    try:
        with client:
            assert client.get("/health").json() == {"status": "ok"}
            r = client.get(f"/events/{event_id}")
            assert r.status_code == 200, r.text
            assert r.json()["title"] == "待保留事件"
    finally:
        client.close()
        main_module.app.dependency_overrides.clear()
        engine.dispose()
