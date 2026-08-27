"""
测试 savepoint 模型（P0 事务修复 #2）

- 默认嵌套：内层 join 外层（只有一个 undo entry）
- savepoint 模式：内层独立入 undo
- __exit__ finally：rollback 异常不影响 _txn_depth 递减
"""
import pytest
from mech_kernel import MechKernel, InvalidRequestError, DeprecatedInternalAPIError
from mech_kernel.transaction import Transaction


def test_default_nested_joins_outer():
    """默认嵌套：内层 commit 不单独入 undo，外层 commit 入 1 个 undo"""
    k = MechKernel()
    k.create_workplane("base", "XY")
    initial_undo = len(k._undo_stack)
    
    with Transaction(k, "outer") as outer:
        with Transaction(k, "inner") as inner:
            k.create_workplane("top", "XY")
            inner.commit()
        k.create_workplane("side", "XY")
        outer.commit()
    
    # 默认嵌套：整个 outer 作为 1 个 undo
    assert len(k._undo_stack) == initial_undo + 1


def test_savepoint_inner_independent_undo():
    """savepoint 模式：内层独立入 undo"""
    k = MechKernel()
    k.create_workplane("base", "XY")
    initial_undo = len(k._undo_stack)
    
    with Transaction(k, "outer") as outer:
        outer.commit()  # outer 立即入 undo
        with Transaction(k, "inner", savepoint=True) as inner:
            k.create_workplane("top", "XY")
            inner.commit()  # savepoint 立即入 undo
    
    # 应该是 2 个 undo entry
    assert len(k._undo_stack) == initial_undo + 2


def test_savepoint_can_be_rolled_back_independently():
    """savepoint 失败可独立回滚"""
    k = MechKernel()
    k.create_workplane("base", "XY")
    initial_count = len(k.workplanes.all())
    
    # 外层成功
    with Transaction(k, "outer") as outer:
        k.create_workplane("top", "XY")
        outer.commit()
    
    assert len(k.workplanes.all()) == initial_count + 1
    
    # savepoint 失败：rollback
    try:
        with Transaction(k, "sp", savepoint=True):
            k.create_workplane("will_fail", "XY")
            k.create_workplane("will_fail", "YZ")  # 重名触发失败
    except InvalidRequestError:
        pass
    
    # savepoint 失败回滚，sp 创建的 workplane 都没了
    assert not k.workplanes.has_name("will_fail")
    assert k.workplanes.has_name("top")  # 外层的还在


def test_inner_exception_does_not_pollute_undo():
    """内层异常（被外层捕获）不应影响 undo 栈"""
    k = MechKernel()
    k.create_workplane("base", "XY")
    initial_undo = len(k._undo_stack)
    
    # 外层捕获内层异常
    try:
        with Transaction(k, "outer") as outer:
            with Transaction(k, "inner") as inner:
                k.create_workplane("top", "XY")
                raise InvalidRequestError("inner failed")
                inner.commit()
            # outer 的修改还在
    except InvalidRequestError:
        pass
    
    # inner 失败，但 outer 的修改（如果有）应该回滚
    # 注意：这里 inner 失败时 outer 也跟着回滚（异常传播）
    # 所以 undo 栈应该没增加
    assert len(k._undo_stack) == initial_undo


def test_txn_depth_decremented_even_on_rollback_exception():
    """__exit__ 用 finally，rollback 抛异常时 _txn_depth 仍递减
    
    注：模拟 rollback 内部抛异常的场景。
    实际触发：让 _restore 抛异常（pre_snapshot 损坏）
    """
    k = MechKernel()
    initial_depth = k._txn_depth
    
    # 模拟 rollback 抛异常的情况
    # 通过 subclass 化 Transaction 让 rollback 失败
    class BrokenTransaction(Transaction):
        def rollback(self):
            # 模拟 rollback 中途抛异常
            self._rolled_back = True
            raise RuntimeError("simulated rollback failure")
    
    # 触发异常路径
    try:
        with BrokenTransaction(k, "test") as txn:
            k.create_workplane("base", "XY")
            # 故意不 commit，让 __exit__ 调用 rollback
            # rollback 会抛 RuntimeError
    except RuntimeError:
        pass
    
    # 关键：即使 rollback 抛异常，_txn_depth 仍要递减
    # __exit__ 的 finally 必须执行
    assert k._txn_depth == initial_depth, \
        f"_txn_depth 应回到 {initial_depth}，实际 {k._txn_depth}"


def test_push_undo_is_removed():
    """_push_undo 现在抛 DeprecatedInternalAPIError"""
    k = MechKernel()
    with pytest.raises(DeprecatedInternalAPIError):
        k._push_undo("test")


def test_undo_after_savepoint_rolls_back_inner():
    """savepoint 失败后，可以撤销最近的成功操作"""
    k = MechKernel()
    k.create_workplane("base", "XY")
    
    # 创建 + savepoint
    with Transaction(k, "outer") as outer:
        k.create_workplane("top", "XY")
        outer.commit()
    
    with Transaction(k, "sp", savepoint=True):
        k.create_workplane("side", "XY")
        # rollback
    
    # 此时只有 outer 入 undo
    # undo 应该撤销 outer
    r = k.undo()
    assert r.success
    # top 和 side 都不存在（side 在 savepoint 中没 commit 就被 rollback）
    assert not k.workplanes.has_name("top")
    assert not k.workplanes.has_name("side")
    assert k.workplanes.has_name("base")


def test_savepoint_undo_entry_has_description():
    """savepoint 入 undo 时带 description"""
    k = MechKernel()
    k.create_workplane("base", "XY")
    
    with Transaction(k, "create top with purpose") as txn:
        k.create_workplane("top", "XY")
        txn.commit()
    
    # undo 栈顶应该带 description
    assert k._undo_stack[-1]["description"] == "create top with purpose"
