"""事件承诺集合的 Merkle 集合证明（纯函数实现，不依赖数据库与 Web 层）。

提交截止后，事件的承诺集合即冻结。本模块给出该集合的确定性 Merkle 表示，
让调用方无需下载完整时间线即可验证「某个承诺确实进入该事件」。

构造规则（全部确定性、与排序无关的歧义均被消除）：

1. 叶节点按承诺 ID（UUID 128 位整数）**升序**排列；
2. 叶哈希同时绑定事件 ID、承诺 ID 与原有 commitment_hash，并带叶域前缀，
   无法把其他事件 / 其他位置的节点挪作他用：

       leaf = SHA-256( LEAF_DOMAIN || uuid128be(event_id)
                       || uuid128be(commitment_id) || commitment_hash_bytes )

3. 内部节点带与叶节点不同的节点域前缀：

       node = SHA-256( NODE_DOMAIN || left || right )

4. 每层节点数为奇数时，**末项复制自身后配对**（与自身哈希配对），
   因此单叶集合的根即该叶哈希本身（零次内部哈希）；
5. 空集合不经过叶 / 内部节点构造，返回固定空根
   ``SHA-256(EMPTY_DOMAIN)``，与任何实际根都不相同。

证明自叶向根逐层给出兄弟哈希及其相对位置（``left`` 表示兄弟在左、
当前节点在右；``right`` 反之）；末项复制层中复制出的兄弟标记为 ``left``。
"""

import hashlib
from dataclasses import dataclass
from uuid import UUID

# 域分离前缀：叶节点、内部节点、空根三者互不相同，杜绝跨类型重放
LEAF_DOMAIN = b"sealed-predictions:leaf:v1:"
NODE_DOMAIN = b"sealed-predictions:node:v1:"
EMPTY_DOMAIN = b"sealed-predictions:empty:v1:"

LEAF_SCHEME = (
    "sha256('sealed-predictions:leaf:v1:' || uuid128be(event_id) "
    "|| uuid128be(commitment_id) || commitment_hash_bytes)"
)
TREE_SCHEME = (
    "merkle-sha256-v1: leaves ordered by commitment_id ascending; "
    "internal=sha256('sealed-predictions:node:v1:' || left || right); "
    "odd layer duplicates the last node; single leaf root is the leaf itself; "
    "empty_root=sha256('sealed-predictions:empty:v1:')"
)

# 结构化校验失败原因（供本地校验接口返回明确的失败类别）
LEAF_INDEX_OUT_OF_RANGE = "LEAF_INDEX_OUT_OF_RANGE"
PATH_TOO_LONG = "PATH_TOO_LONG"
PATH_TOO_SHORT = "PATH_TOO_SHORT"
UNEXPECTED_POSITION = "UNEXPECTED_POSITION"
DUPLICATION_MISMATCH = "DUPLICATION_MISMATCH"
ROOT_MISMATCH = "ROOT_MISMATCH"

# 单条路径允许的最大层数：2^64 个叶已远超任何现实集合，超出必为伪造数据
MAX_PROOF_DEPTH = 64


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


# 空集合的固定根：不经过叶 / 内部哈希，域标签保证不与任何真实根碰撞
EMPTY_ROOT: bytes = _sha256(EMPTY_DOMAIN)


def hash_commitment_leaf(
    event_id: UUID, commitment_id: UUID, commitment_hash: str
) -> bytes:
    """计算叶哈希：绑定事件 ID、承诺 ID 与原有 32 字节承诺哈希。

    commitment_hash 为 64 位十六进制字符串（大小写均可，按存储约定
    调用方应先归一化为小写）；UUID 取 16 字节大端（RFC 4122 字节序）。
    """
    h = hashlib.sha256()
    h.update(LEAF_DOMAIN)
    h.update(event_id.bytes)
    h.update(commitment_id.bytes)
    h.update(bytes.fromhex(commitment_hash))
    return h.digest()


def hash_internal(left: bytes, right: bytes) -> bytes:
    """计算内部节点哈希，节点域前缀与叶域前缀不同。"""
    h = hashlib.sha256()
    h.update(NODE_DOMAIN)
    h.update(left)
    h.update(right)
    return h.digest()


def build_layers(leaves: list[bytes]) -> list[list[bytes]]:
    """自叶向根构造全部层；奇数层末项复制后配对，不修改入参列表。"""
    layers = [list(leaves)]
    current = layers[0]
    while len(current) > 1:
        padded = list(current)
        if len(padded) % 2 == 1:
            padded.append(padded[-1])
        current = [
            hash_internal(padded[i], padded[i + 1])
            for i in range(0, len(padded), 2)
        ]
        layers.append(current)
    return layers


def merkle_root(leaves: list[bytes]) -> bytes:
    """返回集合根；空集合返回固定空根，单叶集合返回该叶哈希本身。"""
    if not leaves:
        return EMPTY_ROOT
    return build_layers(leaves)[-1][0]


@dataclass(frozen=True)
class ProofStep:
    """一层证明：sibling 为兄弟节点哈希，position 为兄弟相对当前节点的位置。"""

    sibling: bytes
    position: str  # left | right


def build_proof(leaves: list[bytes], index: int) -> list[ProofStep]:
    """为升序叶序列中 index 处的叶生成自叶向根的成员证明。"""
    if not 0 <= index < len(leaves):
        raise IndexError(f"叶下标 {index} 超出 [0, {len(leaves)}) 范围")
    steps: list[ProofStep] = []
    current = list(leaves)
    current_index = index
    while len(current) > 1:
        padded = list(current)
        odd_last = len(padded) % 2 == 1 and current_index == len(padded) - 1
        if odd_last:
            # 末项复制自身配对：复制出的兄弟在左，兄弟哈希即当前节点
            steps.append(ProofStep(sibling=padded[-1], position="left"))
        elif current_index % 2 == 1:
            steps.append(ProofStep(sibling=padded[current_index - 1], position="left"))
        else:
            steps.append(ProofStep(sibling=padded[current_index + 1], position="right"))
        if len(padded) % 2 == 1:
            padded.append(padded[-1])
        current = [
            hash_internal(padded[i], padded[i + 1])
            for i in range(0, len(padded), 2)
        ]
        current_index //= 2
    return steps


@dataclass(frozen=True)
class VerificationResult:
    """本地校验结果：valid 为是否通过；失败时 reason 给出首个失败类别。"""

    valid: bool
    reason: str | None = None
    leaf_hash: bytes | None = None
    computed_root: bytes | None = None


def verify_membership(
    event_id: UUID,
    commitment_id: UUID,
    commitment_hash: str,
    leaf_index: int,
    leaf_count: int,
    steps: list[ProofStep],
    expected_root: bytes,
) -> VerificationResult:
    """严格校验成员证明：先做层级结构校验，再比对根。

    层级校验依据叶总数与叶下标重放每层的配对结构（含奇数层末项复制），
    逐层核对：
      - 路径长度必须恰好为从 leaf_count 收敛到 1 所需的层数
        （多一步 PATH_TOO_LONG、少一步 PATH_TOO_SHORT）；
      - 每层兄弟位置必须与该层下标奇偶性一致（UNEXPECTED_POSITION）；
      - 奇数层末项复制时，兄弟必须等于当前节点且位置为 left
        （DUPLICATION_MISMATCH）。
    结构合法后重算根并与 expected_root 逐字节比较（ROOT_MISMATCH）。
    """
    leaf_hash = hash_commitment_leaf(event_id, commitment_id, commitment_hash)

    def fail(reason: str, *, computed_root: bytes | None = None) -> VerificationResult:
        return VerificationResult(
            valid=False,
            reason=reason,
            leaf_hash=leaf_hash,
            computed_root=computed_root,
        )

    if not isinstance(leaf_index, int) or isinstance(leaf_index, bool):
        return fail(LEAF_INDEX_OUT_OF_RANGE)
    if not isinstance(leaf_count, int) or isinstance(leaf_count, bool):
        return fail(LEAF_INDEX_OUT_OF_RANGE)
    if leaf_count < 1 or not 0 <= leaf_index < leaf_count:
        return fail(LEAF_INDEX_OUT_OF_RANGE)
    if len(steps) > MAX_PROOF_DEPTH:
        return fail(PATH_TOO_LONG)

    node = leaf_hash
    index = leaf_index
    layer_size = leaf_count
    for step in steps:
        if layer_size <= 1:
            # 已到达根层却还有证明步骤：路径过长
            return fail(PATH_TOO_LONG)
        duplicated_last = layer_size % 2 == 1 and index == layer_size - 1
        if duplicated_last:
            # 末项复制：兄弟必须就是当前节点本身，且位置标记为 left
            if step.position != "left" or step.sibling != node:
                return fail(DUPLICATION_MISMATCH)
            node = hash_internal(node, node)
        elif index % 2 == 1:
            if step.position != "left":
                return fail(UNEXPECTED_POSITION)
            node = hash_internal(step.sibling, node)
        else:
            if step.position != "right":
                return fail(UNEXPECTED_POSITION)
            node = hash_internal(node, step.sibling)
        index //= 2
        layer_size = (layer_size + 1) // 2

    if layer_size != 1:
        # 步骤耗尽时尚未收敛到单根：路径过短
        return fail(PATH_TOO_SHORT)
    if node != expected_root:
        return fail(ROOT_MISMATCH, computed_root=node)
    return VerificationResult(valid=True, leaf_hash=leaf_hash, computed_root=node)
