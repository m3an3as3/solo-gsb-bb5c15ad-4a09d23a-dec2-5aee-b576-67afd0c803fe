"""Merkle 集合证明纯函数测试（不依赖数据库与 Web 层）。

用独立参考实现交叉验证：
  - 空集合固定空根、单叶根即叶哈希本身
  - 叶 / 内部 / 空根三种前缀互不相同（域分离）
  - 奇数层末项复制配对的确定性结构
  - 成员证明对每个叶、各种集合规模均可验证
  - 严格层级校验：越界下标、路径过长/过短、左右位置错误、复制配对错误、
    根不一致、跨事件 / 跨承诺重放
"""

import hashlib
import random
from uuid import UUID, uuid4

import pytest

from app import merkle
from app.merkle import (
    DUPLICATION_MISMATCH,
    EMPTY_ROOT,
    LEAF_DOMAIN,
    NODE_DOMAIN,
    PATH_TOO_LONG,
    PATH_TOO_SHORT,
    ROOT_MISMATCH,
    UNEXPECTED_POSITION,
    ProofStep,
    build_layers,
    build_proof,
    hash_commitment_leaf,
    hash_internal,
    merkle_root,
    verify_membership,
)

EVENT_ID = UUID("11111111-1111-1111-1111-111111111111")


def ref_leaf(event_id: UUID, commitment_id: UUID, commitment_hash: str) -> bytes:
    """与 app.merkle 独立写出的叶哈希参考实现。"""
    return hashlib.sha256(
        LEAF_DOMAIN + event_id.bytes + commitment_id.bytes + bytes.fromhex(commitment_hash)
    ).digest()


def ref_node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(NODE_DOMAIN + left + right).digest()


def ref_root(event_id: UUID, leaves_in: list[tuple[UUID, str]]) -> bytes:
    """按承诺 ID 升序 + 末项复制规则独立重算根。"""
    ordered = sorted(leaves_in, key=lambda x: x[0].int)
    layer = [ref_leaf(event_id, cid, h) for cid, h in ordered]
    if not layer:
        return hashlib.sha256(merkle.EMPTY_DOMAIN).digest()
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer = layer + [layer[-1]]
        layer = [ref_node(layer[i], layer[i + 1]) for i in range(0, len(layer), 2)]
    return layer[0]


def make_leaves(n: int, seed: int = 0) -> list[tuple[UUID, str]]:
    rng = random.Random(seed)
    ids = [uuid4() for _ in range(n)]
    # 刻意乱序构造，验证内部按 ID 排序
    pairs = list(zip(ids, [hashlib.sha256(f"hash-{seed}-{i}".encode()).hexdigest() for i in range(n)]))
    rng.shuffle(pairs)
    return pairs


@pytest.mark.parametrize("n", [0, 1, 2, 3, 4, 5, 6, 7, 8, 13, 100])
def test_root_matches_reference(n):
    pairs = make_leaves(n, seed=n)
    ordered = sorted(pairs, key=lambda x: x[0].int)
    leaves = [hash_commitment_leaf(EVENT_ID, cid, h) for cid, h in ordered]
    assert merkle_root(leaves) == ref_root(EVENT_ID, pairs)


def test_empty_root_is_fixed_and_distinct():
    assert merkle_root([]) == EMPTY_ROOT
    assert EMPTY_ROOT == hashlib.sha256(merkle.EMPTY_DOMAIN).digest()
    # 空根不与任何叶 / 内部哈希共用域
    assert EMPTY_ROOT != hash_commitment_leaf(EVENT_ID, uuid4(), "00" * 32)


def test_single_leaf_root_is_leaf_itself():
    cid, h = make_leaves(1)[0]
    leaf = hash_commitment_leaf(EVENT_ID, cid, h)
    assert merkle_root([leaf]) == leaf
    assert build_layers([leaf]) == [[leaf]]
    assert build_proof([leaf], 0) == []


def test_domain_separation_leaf_vs_internal():
    digest = hashlib.sha256(b"x" * 64).digest()
    # 相同字节、不同前缀 -> 不同节点
    leafish = hashlib.sha256(LEAF_DOMAIN + digest + digest).digest()
    nodeish = hash_internal(digest, digest)
    assert leafish != nodeish
    # 内部哈希对左右顺序敏感
    other = hashlib.sha256(b"y" * 32).digest()
    assert hash_internal(digest, other) != hash_internal(other, digest)


def test_leaf_hash_binds_all_fields():
    cid = uuid4()
    h = "ab" * 32
    base = hash_commitment_leaf(EVENT_ID, cid, h)
    assert base == ref_leaf(EVENT_ID, cid, h)
    # 改事件、改承诺、改承诺哈希，叶哈希都必须变化
    assert hash_commitment_leaf(uuid4(), cid, h) != base
    assert hash_commitment_leaf(EVENT_ID, uuid4(), h) != base
    assert hash_commitment_leaf(EVENT_ID, cid, "cd" * 32) != base


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 7, 8, 17])
def test_proofs_verify_for_every_leaf(n):
    pairs = sorted(make_leaves(n, seed=100 + n), key=lambda x: x[0].int)
    leaves = [hash_commitment_leaf(EVENT_ID, cid, h) for cid, h in pairs]
    root = merkle_root(leaves)
    for index, (cid, h) in enumerate(pairs):
        steps = build_proof(leaves, index)
        result = verify_membership(EVENT_ID, cid, h, index, n, steps, root)
        assert result.valid, (n, index, result.reason)
        assert result.computed_root == root


def test_duplicated_last_layer_structure():
    # 3 叶：首层奇数，下标 2 的兄弟是自身复制（位置 left、哈希相同）
    pairs = sorted(make_leaves(3, seed=7), key=lambda x: x[0].int)
    leaves = [hash_commitment_leaf(EVENT_ID, cid, h) for cid, h in pairs]
    steps = build_proof(leaves, 2)
    assert steps[0].position == "left"
    assert steps[0].sibling == leaves[2]
    root = merkle_root(leaves)
    assert verify_membership(EVENT_ID, pairs[2][0], pairs[2][1], 2, 3, steps, root).valid


def test_build_proof_rejects_bad_index():
    pairs = sorted(make_leaves(3), key=lambda x: x[0].int)
    leaves = [hash_commitment_leaf(EVENT_ID, cid, h) for cid, h in pairs]
    with pytest.raises(IndexError):
        build_proof(leaves, 3)
    with pytest.raises(IndexError):
        build_proof([], 0)


# --------------------------------------------------------------------------- #
# verify_membership 的严格层级校验
# --------------------------------------------------------------------------- #

@pytest.fixture
def tree5():
    pairs = sorted(make_leaves(5, seed=55), key=lambda x: x[0].int)
    leaves = [hash_commitment_leaf(EVENT_ID, cid, h) for cid, h in pairs]
    return pairs, leaves, merkle_root(leaves)


def proof_for(pairs, leaves, root, index):
    return verify_inputs(pairs, leaves, root, index)


def verify_inputs(pairs, leaves, root, index):
    return pairs[index][0], pairs[index][1], index, len(pairs), build_proof(leaves, index), root


def test_verify_rejects_index_out_of_range(tree5):
    pairs, leaves, root = tree5
    cid, h, idx, count, steps, _ = proof_for(pairs, leaves, root, 0)
    assert verify_membership(EVENT_ID, cid, h, 5, 5, steps, root).reason == "LEAF_INDEX_OUT_OF_RANGE"
    assert verify_membership(EVENT_ID, cid, h, -1, 5, steps, root).reason == "LEAF_INDEX_OUT_OF_RANGE"
    assert verify_membership(EVENT_ID, cid, h, 0, 0, steps, root).reason == "LEAF_INDEX_OUT_OF_RANGE"


def test_verify_rejects_path_too_long_and_short(tree5):
    pairs, leaves, root = tree5
    # 5 叶需 3 层
    cid, h, idx, count, steps, _ = proof_for(pairs, leaves, root, 0)
    assert verify_membership(EVENT_ID, cid, h, idx, count, steps[1:], root).reason == PATH_TOO_SHORT
    long_steps = steps + [ProofStep(b"\x00" * 32, "left")]
    assert verify_membership(EVENT_ID, cid, h, idx, count, long_steps, root).reason == PATH_TOO_LONG


def test_verify_rejects_wrong_position(tree5):
    pairs, leaves, root = tree5
    # 下标 1：首层为右孩子，兄弟必须在左；声称兄弟在右即非法
    cid, h, idx, count, steps, _ = proof_for(pairs, leaves, root, 1)
    flipped = [ProofStep(steps[0].sibling, "right")] + steps[1:]
    assert verify_membership(EVENT_ID, cid, h, idx, count, flipped, root).reason == UNEXPECTED_POSITION


def test_verify_rejects_duplication_mismatch(tree5):
    pairs, leaves, root = tree5
    # 下标 4：首层末项复制，兄弟必须就是当前节点且位置为 left
    cid, h, idx, count, steps, _ = proof_for(pairs, leaves, root, 4)
    bad_sibling = [ProofStep(b"\x11" * 32, "left")] + steps[1:]
    assert verify_membership(EVENT_ID, cid, h, idx, count, bad_sibling, root).reason == DUPLICATION_MISMATCH
    bad_pos = [ProofStep(steps[0].sibling, "right")] + steps[1:]
    assert verify_membership(EVENT_ID, cid, h, idx, count, bad_pos, root).reason == DUPLICATION_MISMATCH


def test_verify_rejects_root_mismatch(tree5):
    pairs, leaves, root = tree5
    cid, h, idx, count, steps, _ = proof_for(pairs, leaves, root, 0)
    fake = b"\x00" * 32
    result = verify_membership(EVENT_ID, cid, h, idx, count, steps, fake)
    assert not result.valid and result.reason == ROOT_MISMATCH
    assert result.computed_root == root


def test_verify_rejects_tampered_sibling(tree5):
    pairs, leaves, root = tree5
    cid, h, idx, count, steps, _ = proof_for(pairs, leaves, root, 0)
    tampered = [ProofStep(b"\x22" * 32, steps[0].position)] + steps[1:]
    assert verify_membership(EVENT_ID, cid, h, idx, count, tampered, root).reason == ROOT_MISMATCH


def test_verify_rejects_rebinding_other_event_or_commitment(tree5):
    pairs, leaves, root = tree5
    cid, h, idx, count, steps, _ = proof_for(pairs, leaves, root, 0)
    # 换事件 ID：叶哈希变化，最终重算根不一致
    result = verify_membership(uuid4(), cid, h, idx, count, steps, root)
    assert not result.valid and result.reason == ROOT_MISMATCH
    # 换承诺 ID 但沿用原路径：下标对应的承诺身份不匹配
    result = verify_membership(EVENT_ID, uuid4(), h, idx, count, steps, root)
    assert not result.valid


def test_verify_rejects_index_count_inconsistency(tree5):
    pairs, leaves, root = tree5
    cid, h, idx, count, steps, _ = proof_for(pairs, leaves, root, 4)
    # 声称只有 4 叶却给出下标 4：越界
    assert verify_membership(EVENT_ID, cid, h, 4, 4, steps, root).reason == "LEAF_INDEX_OUT_OF_RANGE"
    # 声称 6 叶时路径长度/结构不再吻合
    result = verify_membership(EVENT_ID, cid, h, 4, 6, steps, root)
    assert not result.valid


def test_odd_sizes_across_power_boundaries():
    # 2^k 与 2^k+1 规模各层奇偶不同，系统性验证所有叶
    for n in (1, 2, 3, 4, 5, 8, 9, 16, 17, 32, 33, 64, 65):
        pairs = sorted(make_leaves(n, seed=n * 7), key=lambda x: x[0].int)
        leaves = [hash_commitment_leaf(EVENT_ID, cid, hh) for cid, hh in pairs]
        root = merkle_root(leaves)
        for i, (cid, hh) in enumerate(pairs):
            r = verify_membership(EVENT_ID, cid, hh, i, n, build_proof(leaves, i), root)
            assert r.valid, (n, i, r.reason)


def test_layers_sizes_for_seven_leaves():
    # 7 -> 4(末项复制) -> 2 -> 1
    pairs = sorted(make_leaves(7, seed=9), key=lambda x: x[0].int)
    leaves = [hash_commitment_leaf(EVENT_ID, cid, hh) for cid, hh in pairs]
    layers = build_layers(leaves)
    assert [len(layer) for layer in layers] == [7, 4, 2, 1]
