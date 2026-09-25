"""业务规则冒烟测试。

通过 DATABASE_URL 指向临时 SQLite 文件，并猴子补丁 app.clock.now_utc
模拟服务端 UTC 时钟推进，确定性验证各阶段边界行为。
"""

import hashlib
import os
import tempfile
from datetime import datetime, timedelta, timezone

_tmpdir = tempfile.mkdtemp(prefix="sealed_predictions_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"

from fastapi.testclient import TestClient  # noqa: E402

import app.clock as clock_module  # noqa: E402
import app.main as main_module  # noqa: E402
from app.hashing import compute_commitment  # noqa: E402

T0 = datetime(2030, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kwargs):
        self.t += timedelta(**kwargs)


clock = FakeClock(T0)
clock_module.now_utc = clock

client = TestClient(main_module.app)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def sha256_ref(text: str, salt: str) -> str:
    """独立的参考实现，用于交叉验证服务端哈希方案。"""
    tb = text.encode("utf-8")
    return hashlib.sha256(len(tb).to_bytes(8, "big") + tb + salt.encode("utf-8")).hexdigest()


def test_full_lifecycle():
    with client:  # 触发 lifespan 建表
        # --- 创建事件 -----------------------------------------------------
        commit_dl = T0 + timedelta(hours=1)
        reveal_dl = T0 + timedelta(hours=2)
        r = client.post("/events", json={
            "title": "2030 年预测",
            "description": "demo",
            "commit_deadline": iso(commit_dl),
            "reveal_deadline": iso(reveal_dl),
        })
        assert r.status_code == 201, r.text
        event = r.json()
        event_id = event["id"]
        assert event["status"] == "commit"

        # --- 非法截止时间 --------------------------------------------------
        r = client.post("/events", json={
            "title": "x",
            "commit_deadline": iso(commit_dl),
            "reveal_deadline": iso(commit_dl),
        })
        assert r.status_code == 400 and r.json()["detail"]["code"] == "INVALID_DEADLINES"
        r = client.post("/events", json={
            "title": "x",
            "commit_deadline": iso(T0 - timedelta(seconds=1)),
            "reveal_deadline": iso(reveal_dl),
        })
        assert r.status_code == 400 and r.json()["detail"]["code"] == "INVALID_DEADLINES"

        # --- 提交两份承诺 -------------------------------------------------
        text1, salt1 = "明天下雨", "s3cr3t-1"
        text2, salt2 = "明年晴天", "s3cr3t-2"
        h1 = compute_commitment(text1, salt1)
        h2 = compute_commitment(text2, salt2)
        assert h1 == sha256_ref(text1, salt1)  # 与参考实现一致

        r = client.post("/utils/commitment", json={"original_text": text1, "salt": salt1})
        assert r.status_code == 200 and r.json()["commitment_hash"] == h1

        r = client.post(f"/events/{event_id}/commitments", json={"commitment_hash": h1.upper()})
        assert r.status_code == 201, r.text
        assert r.json()["commitment_hash"] == h1  # 归一化为小写
        c1_id = r.json()["id"]

        r = client.post(f"/events/{event_id}/commitments", json={"commitment_hash": h2})
        assert r.status_code == 201

        # 重复承诺 -> 409，状态不变
        r = client.post(f"/events/{event_id}/commitments", json={"commitment_hash": h1})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "DUPLICATE_COMMITMENT"
        r = client.get(f"/events/{event_id}")
        assert r.json()["commitment_count"] == 2

        # 非哈希格式 -> 422
        r = client.post(f"/events/{event_id}/commitments", json={"commitment_hash": "xyz"})
        assert r.status_code == 422

        # --- 提前揭示 -> 409 ----------------------------------------------
        r = client.post(f"/events/{event_id}/reveals",
                        json={"original_text": text1, "salt": salt1})
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "REVEAL_PHASE_NOT_STARTED"

        # --- 推进到提交截止时刻：迟到提交被拒，揭示期开始 -------------------
        clock.t = commit_dl
        r = client.post(f"/events/{event_id}/commitments",
                        json={"commitment_hash": compute_commitment("x", "y")})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "COMMIT_PHASE_CLOSED"
        r = client.get(f"/events/{event_id}")
        assert r.json()["status"] == "reveal" and r.json()["commitment_count"] == 2

        # --- 错误原文揭示 -> 409，状态不变 ---------------------------------
        r = client.post(f"/events/{event_id}/reveals",
                        json={"original_text": "错误原文", "salt": salt1})
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "NO_MATCHING_COMMITMENT"
        r = client.get(f"/events/{event_id}")
        assert r.json()["reveal_count"] == 0

        # --- 正确揭示 -> 201，仅首次生效 -----------------------------------
        r = client.post(f"/events/{event_id}/reveals",
                        json={"original_text": text1, "salt": salt1})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["verified"] is True
        assert body["commitment_id"] == c1_id
        assert body["recomputed_hash"] == h1 == body["commitment_hash"]

        # 重复揭示 -> 409
        r = client.post(f"/events/{event_id}/reveals",
                        json={"original_text": text1, "salt": salt1})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "DUPLICATE_REVEAL"
        r = client.get(f"/events/{event_id}")
        assert r.json()["reveal_count"] == 1

        # --- 推进到揭示截止之后：逾期揭示被拒 -------------------------------
        clock.t = reveal_dl
        r = client.post(f"/events/{event_id}/reveals",
                        json={"original_text": text2, "salt": salt2})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "REVEAL_PHASE_CLOSED"
        r = client.get(f"/events/{event_id}")
        assert r.json()["status"] == "closed" and r.json()["reveal_count"] == 1

        # --- 时间线可核验 --------------------------------------------------
        r = client.get(f"/events/{event_id}/timeline")
        assert r.status_code == 200, r.text
        tl = r.json()
        assert tl["status"] == "closed"
        types = [e["type"] for e in tl["entries"]]
        assert types == [
            "event_created",
            "commitment_submitted",
            "commitment_submitted",
            "reveal_accepted",
        ]
        assert [e["seq"] for e in tl["entries"]] == [1, 2, 3, 4]
        reveal_entry = tl["entries"][3]["detail"]
        # 任何人可用时间线中的 原文+盐 重算并与承诺哈希比对
        assert sha256_ref(reveal_entry["original_text"], reveal_entry["salt"]) \
            == reveal_entry["commitment_hash"] == reveal_entry["recomputed_hash"]

        # --- 404 -----------------------------------------------------------
        missing = "00000000-0000-0000-0000-000000000000"
        r = client.get(f"/events/{missing}")
        assert r.status_code == 404 and r.json()["detail"]["code"] == "EVENT_NOT_FOUND"
        r = client.post(f"/events/{missing}/commitments", json={"commitment_hash": h1})
        assert r.status_code == 404


def test_health():
    with client:
        assert client.get("/health").json() == {"status": "ok"}
