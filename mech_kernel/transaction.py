"""
MechKernel 事务管理（v1.1 修复版 #2）

P3 原则：任何操作都在事务中，失败整体回滚。

P0 修复（专家第 3 轮）：
- `__exit__` 必须有 `finally` 保护 _txn_depth（rollback 抛异常时不递减）
- 删除 _push_undo 尸体 API（不再静默 pass）
- savepoint 明确语义：
  - 默认嵌套：内层 join 外层（只在外层 commit 时入 undo）
  - savepoint 显式 API：`Transaction.savepoint()` 标记内层独立点

P1 优化：
- 只在 commit 时才深拷贝完整状态（之前每次都拍两次）
- rollback 用 swap 避免额外拷贝
"""
from typing import Optional
from contextlib import contextmanager

from .errors import (
    InvalidRequestError, KernelBugError, StateCorruptionError
)


class Transaction:
    """
    事务对象（savepoint 模型）。
    
    用法 1：单层事务
        with Transaction(kernel, "op") as txn:
            kernel._do(...)
            txn.commit()
    
    用法 2：嵌套事务（默认 join 外层）
        with Transaction(kernel, "outer") as outer:
            with Transaction(kernel, "inner") as inner:
                kernel._do(...)
                inner.commit()
            # inner 提交，但不立即入 undo
            outer.commit()
        # outer 提交时，整体作为一个 undo entry
    
    用法 3：显式 savepoint（内层独立）
        with Transaction(kernel, "outer") as outer:
            outer.commit()    # outer 先提交，入 undo
            with Transaction(kernel, "inner", savepoint=True) as inner:
                kernel._do(...)
                inner.commit()  # inner 独立入 undo
    """
    
    def __init__(self, kernel, description: str = "", savepoint: bool = False):
        self.kernel = kernel
        self.description = description
        self.savepoint = savepoint  # 显式 savepoint：独立入 undo
        self._committed = False
        self._rolled_back = False
        self._pre_snapshot = None
        self._depth = 0
        self._in_outer_committed = False  # 外层已 commit 的标志
    
    # P2-6 修复（v8 DeepSeek）：事务嵌套最大深度
    MAX_NESTING_DEPTH = 10
    
    def __enter__(self):
        # 深度限制
        if self.kernel._txn_depth >= Transaction.MAX_NESTING_DEPTH:
            raise StateCorruptionError(
                f"事务嵌套过深（>{Transaction.MAX_NESTING_DEPTH}），"
                "可能是递归调用未终止。请检查代码。"
            )
        if self.kernel._txn_depth == 0:
            # 外层：拍快照
            self._pre_snapshot = self.kernel._snapshot()
        else:
            # 内层：默认 join，不拍快照
            # savepoint 模式：内层独立拍快照
            if self.savepoint:
                self._pre_snapshot = self.kernel._snapshot()
            else:
                self._pre_snapshot = None
        self.kernel._txn_depth += 1
        self._depth = self.kernel._txn_depth
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is not None:
                # 异常：自动回滚
                self.rollback()
                # 必抛异常继续传播
                if issubclass(exc_type, (InvalidRequestError, KernelBugError, StateCorruptionError)):
                    return False
                return False
            else:
                if not self._committed:
                    # 未 commit：自动回滚
                    self.rollback()
        finally:
            # 关键修复：必须用 finally 保护 _txn_depth 递减
            # 即使 rollback 抛异常，也要递减
            self.kernel._txn_depth = max(0, self.kernel._txn_depth - 1)
        return False
    
    def commit(self) -> None:
        """提交事务。
        
        - 默认嵌套模式：内层 commit 什么都不做（外层 commit 时统一处理）
        - savepoint 模式 / 外层：把 pre_snapshot 放入 undo 栈
        """
        if self._committed:
            raise KernelBugError("事务已 commit，不能再次 commit")
        if self._rolled_back:
            raise KernelBugError("事务已 rollback，不能 commit")
        
        # Validate before publishing the state. If this raises, __exit__
        # restores the transaction snapshot automatically.
        if hasattr(self.kernel, "_validate_transaction_state"):
            self.kernel._validate_transaction_state(self.description)

        self._committed = True
        
        # 入 undo 的条件：
        # 1. savepoint 模式：内层独立入
        # 2. 外层（depth == 1）入
        should_push = self.savepoint or self._depth == 1
        
        if should_push and self._pre_snapshot is not None:
            self.kernel._undo_stack.append({
                "snapshot": self._pre_snapshot,
                "description": self.description,
            })
            # 限制 undo 栈深度
            if len(self.kernel._undo_stack) > self.kernel._max_undo_depth:
                self.kernel._undo_stack.pop(0)
            # 清空 redo
            self.kernel._redo_stack.clear()
            # P0-3 修复：事务 commit 后几何状态可能变化，bump revision
            if hasattr(self.kernel, '_bump_geometry_revision'):
                self.kernel._bump_geometry_revision()
    
    def rollback(self) -> None:
        """回滚事务。
        
        - 外层 / savepoint 回滚：恢复 pre_snapshot
        - 内层 join 模式回滚：什么都不做（外层统一处理）
        """
        if self._committed:
            raise KernelBugError("事务已 commit，不能 rollback")
        if self._rolled_back:
            return  # 幂等
        
        self._rolled_back = True
        
        should_restore = self.savepoint or self._depth == 1
        if should_restore and self._pre_snapshot is not None:
            self.kernel._restore(self._pre_snapshot)
            # P0-3 修复：事务 rollback 后几何状态变了，bump revision
            if hasattr(self.kernel, '_bump_geometry_revision'):
                self.kernel._bump_geometry_revision()


@contextmanager
def transaction(kernel, description: str = "", savepoint: bool = False):
    """便捷的事务上下文管理器"""
    txn = Transaction(kernel, description, savepoint=savepoint)
    try:
        with txn:
            yield txn
    except (InvalidRequestError, KernelBugError, StateCorruptionError):
        raise
    except Exception:
        raise
