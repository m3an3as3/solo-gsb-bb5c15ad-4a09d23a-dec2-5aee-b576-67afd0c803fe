"""事件承诺集合证明（Merkle）模块测试。

覆盖：
  - 未过提交截止：根与成员证明均 409 PROOF_NOT_AVAILABLE
  - 提交截止后：根、数量、叶排序、空集合固定空根与零数量
  - 成员证明：每个承诺可签发，本地校验接口对服务端证明全部 valid
  - 严格校验：篡改根 / 兄弟 / 位置、路径过长过短、越界下标、
    跨事件重放、大写哈希归一化
  - 404：事件不存在、承诺不存在或不属于事件
  - 不影响现有提交 / 揭示 / 结算流程
  - 延长截止时间后以新截止时间为准
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from uuid import UUID

_tmpdir = tempfile.mkdtemp(prefix="sealed_predictions_merkle_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/merkle.db"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.clock as clock_module  # noqa: E402
import app.main as main_module  # noqa: E402
from app import merkle  # noqa: E402
from app.hashing import compute_commitment  # noqa: E402

T0 = datetime(2030, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
COMMIT_DL = T0 + timedelta(hours=1)
REVEAL_DL = T0 + timedelta(hours=2)


class MutableClock:
    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t


clock = MutableClock(T0)

client = TestClient(main_module.app)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    clock.t = T0
    monkeypatch.setattr(clock_module, "now_utc", clock)
    with client:
        yield


def create_event(commit_dl: datetime = COMMIT_DL, reveal_dl: datetime = REVEAL_DL) -> str:
    r = client.post("/events", json={
        "title": "集合证明事件",
        "commit_deadline": commit_dl.isoformat(),
        "reveal_deadline": reveal_dl.isoformat(),
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def commit(event_id: str, text: str, salt: str) -> tuple[str, str]:
    h = compute_commitment(text, salt)
    r = client.post(f"/events/{event_id}/commitments", json={"commitment_hash": h})
    assert r.status_code == 201, r.text
    return r.json()["id"], h


def commit_n(event_id: str, n: int) -> list[tuple[str, str]]:
    return [commit(event_id, f"prediction-{i}", f"salt-{i}") for i in range(n)]


def verify(body: dict, **overrides) -> dict:
    payload = {
        "event_id": body["event_id"],
        "commitment_id": body["commitment_id"],
        "commitment_hash": body["commitment_hash"],
        "leaf_index": body["leaf_index"],
        "leaf_count": body["leaf_count"],
        "merkle_root": body["merkle_root"],
        "proof": body["proof"],
    }
    payload.update(overrides)
    r = client.post("/utils/merkle-proof/verify", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# 阶段门禁
# --------------------------------------------------------------------------- #

def test_proof_unavailable_before_commit_deadline():
    eid = create_event()
    cid, _ = commit(eid, "a", "s")

    r = client.get(f"/events/{eid}/commitments/proof/root")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PROOF_NOT_AVAILABLE"

    r = client.get(f"/events/{eid}/commitments/{cid}/proof")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PROOF_NOT_AVAILABLE"


def test_proof_available_at_and_after_commit_deadline():
    eid = create_event()
    commit_n(eid, 2)

    clock.t = COMMIT_DL  # 到达截止瞬间：提交已关闭、证明可签发
    r = client.get(f"/events/{eid}/commitments/proof/root")
    assert r.status_code == 200, r.text
    assert r.json()["commitment_count"] == 2

    clock.t = REVEAL_DL  # closed 阶段同样可签发
    r = client.get(f"/events/{eid}/commitments/proof/root")
    assert r.status_code == 200


def test_extension_moves_gate():
    # commit 阶段延长截止后，原截止时刻仍未关闭，证明仍不可签发
    eid = create_event()
    new_commit_dl = COMMIT_DL + timedelta(hours=1)
    new_reveal_dl = REVEAL_DL + timedelta(hours=1)
    r = client.post(f"/events/{eid}/extend", json={
        "commit_deadline": new_commit_dl.isoformat(),
        "reveal_deadline": new_reveal_dl.isoformat(),
        "reason": "顺延",
    })
    assert r.status_code == 200, r.text

    clock.t = COMMIT_DL
    r = client.get(f"/events/{eid}/commitments/proof/root")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PROOF_NOT_AVAILABLE"

    clock.t = new_commit_dl
    r = client.get(f"/events/{eid}/commitments/proof/root")
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# 根端点
# --------------------------------------------------------------------------- #

def test_root_for_empty_set():
    eid = create_event()
    clock.t = COMMIT_DL
    r = client.get(f"/events/{eid}/commitments/proof/root")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["commitment_count"] == 0
    assert body["merkle_root"] == merkle.EMPTY_ROOT.hex()
    assert body["empty_root"] == merkle.EMPTY_ROOT.hex()
    assert body["event_id"] == eid
    assert "tree_scheme" in body and "leaf_scheme" in body and body["generated_at"]


def test_root_matches_locally_rebuilt_tree():
    eid = create_event()
    committed = commit_n(eid, 7)
    clock.t = COMMIT_DL

    r = client.get(f"/events/{eid}/commitments/proof/root")
    body = r.json()
    assert body["commitment_count"] == 7

    # 用端点之外的途径独立重建：列表接口拿到 (id, hash)，按 ID 升序构树
    listing = client.get(f"/events/{eid}/commitments").json()
    ordered = sorted(listing, key=lambda c: int(c["id"].replace("-", ""), 16))
    leaves = [
        merkle.hash_commitment_leaf(UUID(eid), UUID(c["id"]), c["commitment_hash"])
        for c in ordered
    ]
    assert merkle.merkle_root(leaves).hex() == body["merkle_root"]


def test_root_deterministic_and_set_order_independent():
    eid = create_event()
    commit_n(eid, 5)
    clock.t = COMMIT_DL
    r1 = client.get(f"/events/{eid}/commitments/proof/root").json()
    r2 = client.get(f"/events/{eid}/commitments/proof/root").json()
    assert r1["merkle_root"] == r2["merkle_root"]
    assert r1["commitment_count"] == r2["commitment_count"] == 5


# --------------------------------------------------------------------------- #
# 成员证明端点
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("n", [1, 2, 3, 5, 8])
def test_membership_proofs_for_all_commitments(n):
    eid = create_event()
    committed = commit_n(eid, n)
    clock.t = COMMIT_DL

    root_body = client.get(f"/events/{eid}/commitments/proof/root").json()
    seen_indices = set()
    for cid, chash in committed:
        r = client.get(f"/events/{eid}/commitments/{cid}/proof")
        assert r.status_code == 200, r.text
        p = r.json()
        assert p["commitment_id"] == cid
        assert p["commitment_hash"] == chash
        assert p["leaf_count"] == n
        assert p["merkle_root"] == root_body["merkle_root"]
        assert 0 <= p["leaf_index"] < n
        seen_indices.add(p["leaf_index"])
        # 每一步格式：64 位十六进制 + left/right
        for step in p["proof"]:
            assert len(step["sibling"]) == 64
            assert step["position"] in ("left", "right")
        # 本地校验接口必须通过
        v = verify(p)
        assert v["valid"] is True and v["reason"] is None, v
        assert v["leaf_hash"] == p["leaf_hash"]
        assert v["computed_root"] == p["merkle_root"]
    assert seen_indices == set(range(n))  # 下标覆盖 0..n-1 且无重复


def test_leaves_ordered_by_commitment_id_ascending():
    eid = create_event()
    committed = commit_n(eid, 6)
    clock.t = COMMIT_DL
    proofs = []
    for cid, _ in committed:
        proofs.append(client.get(f"/events/{eid}/commitments/{cid}/proof").json())
    by_index = sorted(proofs, key=lambda p: p["leaf_index"])
    ids_in_order = [p["commitment_id"] for p in by_index]
    assert ids_in_order == sorted(ids_in_order, key=lambda s: int(s.replace("-", ""), 16))


def test_single_leaf_proof_has_empty_path():
    eid = create_event()
    cid, _ = commit(eid, "only", "x")
    clock.t = COMMIT_DL
    p = client.get(f"/events/{eid}/commitments/{cid}/proof").json()
    assert p["leaf_index"] == 0
    assert p["proof"] == []
    assert p["merkle_root"] == p["leaf_hash"]
    assert verify(p)["valid"] is True


def test_commitment_not_found():
    eid = create_event()
    other_eid = create_event()
    cid, _ = commit(other_eid, "x", "y")
    clock.t = COMMIT_DL

    missing = "00000000-0000-0000-0000-000000000000"
    r = client.get(f"/events/{eid}/commitments/{missing}/proof")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "COMMITMENT_NOT_FOUND"

    # 承诺存在但属于另一事件 -> 同样 404
    r = client.get(f"/events/{eid}/commitments/{cid}/proof")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "COMMITMENT_NOT_FOUND"


def test_event_not_found():
    missing = "00000000-0000-0000-0000-000000000000"
    r = client.get(f"/events/{missing}/commitments/proof/root")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "EVENT_NOT_FOUND"
    r = client.get(f"/events/{missing}/commitments/{missing}/proof")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "EVENT_NOT_FOUND"


# --------------------------------------------------------------------------- #
# 本地校验接口的严格校验
# --------------------------------------------------------------------------- #

@pytest.fixture
def proven_tree():
    eid = create_event()
    committed = commit_n(eid, 5)
    clock.t = COMMIT_DL
    proofs = {
        cid: client.get(f"/events/{eid}/commitments/{cid}/proof").json()
        for cid, _ in committed
    }
    return eid, committed, proofs


def test_verify_tampered_root(proven_tree):
    _, _, proofs = proven_tree
    body = next(iter(proofs.values()))
    v = verify(body, merkle_root="00" * 32)
    assert v["valid"] is False and v["reason"] == "ROOT_MISMATCH"
    assert v["computed_root"] == body["merkle_root"]


def test_verify_tampered_leaf_fields(proven_tree):
    _, _, proofs = proven_tree
    body = next(iter(proofs.values()))
    # 换承诺哈希：叶哈希变化。若首层恰为末项复制，会在复制配对层被拦截
    # （DUPLICATION_MISMATCH）；否则重算到根后 ROOT_MISMATCH——两者均拒绝
    other_hash = compute_commitment("totally-different", "salt")
    v = verify(body, commitment_hash=other_hash)
    assert v["valid"] is False
    assert v["reason"] in ("ROOT_MISMATCH", "DUPLICATION_MISMATCH")


def test_verify_rebind_other_event(proven_tree):
    # 时钟此时已在截止后，先回拨创建另一个事件再恢复
    clock.t = T0
    other_eid = create_event()
    clock.t = COMMIT_DL
    _, _, proofs = proven_tree
    body = next(iter(proofs.values()))
    v = verify(body, event_id=other_eid)
    assert v["valid"] is False


def test_verify_wrong_position(proven_tree):
    _, _, proofs = proven_tree
    # 找一个首层非复制配对的叶（偶数下标中取左孩子）
    body = next(p for p in proofs.values() if p["proof"] and p["leaf_index"] % 2 == 0)
    flipped = [
        {**s, "position": "left" if s["position"] == "right" else "right"}
        for s in body["proof"]
    ]
    v = verify(body, proof=flipped)
    assert v["valid"] is False
    assert v["reason"] in ("UNEXPECTED_POSITION", "DUPLICATION_MISMATCH")


def test_verify_duplication_layer_strictness(proven_tree):
    _, _, proofs = proven_tree
    # 5 叶：下标 4 首层为末项复制，兄弟必须等于叶本身
    body = next(p for p in proofs.values() if p["leaf_index"] == 4)
    tampered = [{**body["proof"][0], "sibling": "11" * 32}] + body["proof"][1:]
    v = verify(body, proof=tampered)
    assert v["valid"] is False and v["reason"] == "DUPLICATION_MISMATCH"


def test_verify_path_length_mismatch(proven_tree):
    _, _, proofs = proven_tree
    body = next(iter(proofs.values()))
    v_short = verify(body, proof=body["proof"][:-1])
    assert v_short["valid"] is False and v_short["reason"] == "PATH_TOO_SHORT"
    v_long = verify(
        body,
        proof=body["proof"] + [{"sibling": "00" * 32, "position": "left"}],
    )
    assert v_long["valid"] is False and v_long["reason"] == "PATH_TOO_LONG"


def test_verify_leaf_index_out_of_range(proven_tree):
    _, _, proofs = proven_tree
    # 固定取下标 0 的证明：5 叶集合深度为 3
    body = next(p for p in proofs.values() if p["leaf_index"] == 0)
    assert body["leaf_count"] == 5
    v = verify(body, leaf_index=body["leaf_count"])
    assert v["valid"] is False and v["reason"] == "LEAF_INDEX_OUT_OF_RANGE"
    # 少报 leaf_count=4（2 的幂，深度应仅为 2）：第 3 层开始时已收敛 -> 路径过长
    v = verify(body, leaf_count=4)
    assert v["valid"] is False and v["reason"] == "PATH_TOO_LONG"


def test_verify_accepts_uppercase_commitment_hash(proven_tree):
    _, _, proofs = proven_tree
    body = next(iter(proofs.values()))
    v = verify(body, commitment_hash=body["commitment_hash"].upper())
    assert v["valid"] is True


def test_verify_request_format_errors_422(proven_tree):
    _, _, proofs = proven_tree
    body = next(iter(proofs.values()))
    base = {
        "event_id": body["event_id"],
        "commitment_id": body["commitment_id"],
        "commitment_hash": body["commitment_hash"],
        "leaf_index": body["leaf_index"],
        "leaf_count": body["leaf_count"],
        "merkle_root": body["merkle_root"],
        "proof": body["proof"],
    }

    def post(payload):
        return client.post("/utils/merkle-proof/verify", json=payload)

    r = post({**base, "commitment_hash": "xyz"})
    assert r.status_code == 422
    r = post({**base, "merkle_root": "Z" * 64})
    assert r.status_code == 422
    r = post({**base, "leaf_index": -1})
    assert r.status_code == 422
    r = post({**base, "leaf_count": 0})
    assert r.status_code == 422
    bad_pos = [{**base["proof"][0], "position": "up"}] if base["proof"] else []
    if bad_pos:
        r = post({**base, "proof": bad_pos + base["proof"][1:]})
        assert r.status_code == 422
    r = post({**base, "event_id": "not-a-uuid"})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# 不改变现有流程
# --------------------------------------------------------------------------- #

def test_existing_flow_unchanged():
    eid = create_event()
    cid, h = commit(eid, "明天", "盐")
    # commit 阶段证明不可用，但提交正常
    assert client.get(f"/events/{eid}/commitments/proof/root").status_code == 409

    clock.t = COMMIT_DL
    # reveal 阶段揭示正常，证明只读且不新增承诺
    r = client.post(f"/events/{eid}/reveals", json={"original_text": "明天", "salt": "盐"})
    assert r.status_code == 201, r.text

    root1 = client.get(f"/events/{eid}/commitments/proof/root").json()
    proof1 = client.get(f"/events/{eid}/commitments/{cid}/proof").json()
    assert verify(proof1)["valid"] is True

    clock.t = REVEAL_DL
    # closed 阶段结算正常；证明内容不受揭示 / 结算影响
    r = client.post(f"/events/{eid}/settlement", json={"standard_result": "明天"})
    assert r.status_code == 200, r.text

    root2 = client.get(f"/events/{eid}/commitments/proof/root").json()
    proof2 = client.get(f"/events/{eid}/commitments/{cid}/proof").json()
    assert root2["merkle_root"] == root1["merkle_root"]
    assert proof2["merkle_root"] == proof1["merkle_root"]
    assert verify(proof2)["valid"] is True
    # 揭示未改变集合
    assert client.get(f"/events/{eid}").json()["commitment_count"] == 1


def test_late_commit_still_rejected_after_deadline():
    eid = create_event()
    clock.t = COMMIT_DL
    r = client.post(
        f"/events/{eid}/commitments",
        json={"commitment_hash": compute_commitment("late", "s")},
    )
    assert r.status_code == 409 and r.json()["detail"]["code"] == "COMMIT_PHASE_CLOSED"
    body = client.get(f"/events/{eid}/commitments/proof/root").json()
    assert body["commitment_count"] == 0
    assert body["merkle_root"] == merkle.EMPTY_ROOT.hex()
