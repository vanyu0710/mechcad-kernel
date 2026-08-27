"""
测试 P0 修复：
1. NOT_IMPLEMENTED 错误类型
2. 事务 / undo 栈不被污染
3. 失败渲染策略（按 error_kind）
4. Reference 的 frozen=True
"""
import pytest
from mech_kernel import (
    MechKernel, InvalidRequestError, KernelBugError, StateCorruptionError
)
from mech_kernel.errors import make_not_implemented, get_render_level_for_error
from mech_kernel.features import Reference
from mech_kernel.transaction import Transaction


# === NOT_IMPLEMENTED 测试 ===

def test_make_not_implemented_factory():
    """make_not_implemented 工厂函数"""
    nd = make_not_implemented("revolve", "M2")
    assert nd["error_kind"] == "NOT_IMPLEMENTED"
    assert nd["api_name"] == "revolve"
    assert nd["planned_version"] == "M2"
    assert "revolve" in nd["error"]
    assert "M2" in nd["error"]


def test_not_implemented_in_step_result():
    """M0 占位 API 返回 NOT_IMPLEMENTED，不是 GEOMETRY_FAILURE
    注：revolve/sweep/shell/fillet/chamfer 已在 v1.3-1.6 真实实现
    """
    k = MechKernel()
    r = k.boolean("union", "F_001", "F_002")  # boolean 仍占位
    assert not r.success
    assert r.error_kind == "NOT_IMPLEMENTED"
    assert r.api_name == "boolean"


def test_all_placeholder_apis_return_not_implemented():
    """所有占位 API 都不再伪装成 GEOMETRY_FAILURE
    注：fillet/chamfer/circular_pattern 已在 v1.4 实现，不在此测试
    """
    k = MechKernel()
    # 准备一个合法 workplane
    k.create_workplane("base", "XY")
    
    # 试未实现 API（fillet/chamfer/revolve/circular_pattern/shell/sweep 已在 v1.3-1.6 实现）
    placeholders = [
        lambda: k.boolean("union", "F_001", "F_002"),
        lambda: k.hole("base", (0, 0), 10),
        lambda: k.linear_pattern("F_001", 4, (1, 0, 0), 10),
        lambda: k.mirror(["F_001"], "base"),
    ]
    for call in placeholders:
        r = call()
        assert not r.success
        assert r.error_kind == "NOT_IMPLEMENTED", \
            f"{call.__name__ if hasattr(call, '__name__') else 'call'} 返回 {r.error_kind}，应该是 NOT_IMPLEMENTED"


# === 事务 / undo 栈污染测试 ===

def test_failed_transaction_does_not_pollute_undo():
    """失败的事务不应该留 undo entry"""
    k = MechKernel()
    k.create_workplane("base", "XY")
    initial_undo_count = len(k._undo_stack)
    
    # 触发失败：重名
    try:
        k.create_workplane("base", "YZ")
    except InvalidRequestError:
        pass
    
    # undo 栈不应该增长
    assert len(k._undo_stack) == initial_undo_count, \
        f"undo 栈被污染：从 {initial_undo_count} 增长到 {len(k._undo_stack)}"


def test_successful_transaction_adds_one_undo():
    """成功的事务应该增加 1 个 undo entry"""
    k = MechKernel()
    k.create_workplane("base", "XY")
    initial_undo_count = len(k._undo_stack)
    
    k.create_workplane("top", "XY")
    
    # undo 栈应该 +1
    assert len(k._undo_stack) == initial_undo_count + 1


def test_undo_after_failed_transaction():
    """失败事务后，undo 应该跳过空记录"""
    k = MechKernel()
    k.create_workplane("base", "XY")
    
    # 触发失败
    try:
        k.create_workplane("base", "YZ")  # 重名
    except InvalidRequestError:
        pass
    
    # 撤销应该撤销"base"的创建
    initial_narrative_len = len(k.narrative)
    r = k.undo()
    assert r.success
    # narrative 应该减少（撤销了"base"的创建）
    assert len(k.narrative) < initial_narrative_len


def test_nested_transaction_only_outer_pushes_undo():
    """嵌套事务只有外层 commit 推 undo"""
    k = MechKernel()
    k.create_workplane("base", "XY")
    initial_undo_count = len(k._undo_stack)
    
    with Transaction(k, "outer") as outer:
        with Transaction(k, "inner") as inner:
            k.create_workplane("top", "XY")
            inner.commit()
        # outer 也 commit
        k.create_workplane("side", "XY")
        outer.commit()
    
    # 整个 outer 作为一个 undo entry
    assert len(k._undo_stack) == initial_undo_count + 1, \
        f"嵌套事务应该只产生 1 个 undo，实际 {len(k._undo_stack) - initial_undo_count}"


# === 失败渲染策略测试 ===

def test_geometry_failure_suggests_iso_render():
    """GEOMETRY_FAILURE 默认建议 iso 渲染"""
    from mech_kernel import make_geometry_failure
    nd = make_geometry_failure("self_intersection", "自相交")
    assert nd["render_level"] == "iso_only"


def test_recoverable_suggests_iso_render():
    """RECOVERABLE 默认建议 iso 渲染"""
    from mech_kernel import make_recoverable
    nd = make_recoverable("fillet_too_large", {"new_radius": 2.0})
    assert nd["render_level"] == "iso_only"


def test_not_implemented_no_render():
    """NOT_IMPLEMENTED 不需要渲染"""
    nd = make_not_implemented("revolve", "M2")
    assert nd["render_level"] == "none"


def test_error_render_hint_table():
    """所有错误类型都有对应的 render_level 建议"""
    for kind in ["GEOMETRY_FAILURE", "RECOVERABLE", "NOT_IMPLEMENTED", 
                 "INVALID_REQUEST", "KERNEL_BUG", "STATE_CORRUPTION"]:
        level = get_render_level_for_error(kind)
        assert level in ("none", "iso_only", "full"), \
            f"{kind} -> {level} 不合法"


# === Reference frozen=True 测试 ===

def test_reference_is_frozen():
    """Reference 不可变"""
    r = Reference(kind="face", semantic_name="top_face")
    with pytest.raises(Exception):  # FrozenInstanceError
        r.semantic_name = "modified"


def test_reference_hash_eq_consistent():
    """Reference 的 hash 和 eq 一致"""
    r1 = Reference(kind="face", semantic_name="top_face", owner_feature_id="F_001")
    r2 = Reference(kind="face", semantic_name="top_face", owner_feature_id="F_001")
    r3 = Reference(kind="face", semantic_name="top_face", owner_feature_id="F_002")
    
    assert r1 == r2
    assert hash(r1) == hash(r2)
    assert r1 != r3
    # 关键：相等对象必须有相等 hash
    s = {r1, r2, r3}
    assert len(s) == 2


def test_reference_in_set():
    """Reference 可以正常用作 set/dict key"""
    r1 = Reference(kind="face", semantic_name="top_face", owner_feature_id="F_001")
    r2 = Reference(kind="edge", semantic_name="bottom_edge", owner_feature_id="F_001")
    
    s = {r1, r2}
    assert len(s) == 2
    assert r1 in s
    assert r2 in s
