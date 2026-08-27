"""
测试 5 类类型化错误
"""
import pytest
from mech_kernel.errors import (
    MechKernelError, InvalidRequestError, KernelBugError, StateCorruptionError,
    make_geometry_failure, make_recoverable, GeometryFailureReason
)


def test_invalid_request_error_inherits_base():
    """InvalidRequestError 继承 MechKernelError"""
    err = InvalidRequestError("test error")
    assert isinstance(err, MechKernelError)
    assert str(err) == "test error"


def test_kernel_bug_error_inherits_base():
    """KernelBugError 继承 MechKernelError"""
    err = KernelBugError("kernel bug")
    assert isinstance(err, MechKernelError)


def test_state_corruption_error_inherits_base():
    """StateCorruptionError 继承 MechKernelError"""
    err = StateCorruptionError("state corrupted")
    assert isinstance(err, MechKernelError)


def test_invalid_request_error_with_hint():
    """InvalidRequestError 支持 hint"""
    err = InvalidRequestError("test", hint="看文档")
    assert err.hint == "看文档"


def test_geometry_failure_factory():
    """make_geometry_failure 构造标准错误"""
    f = make_geometry_failure(GeometryFailureReason.SELF_INTERSECTION, "自相交")
    assert f["error_kind"] == "GEOMETRY_FAILURE"
    assert "self_intersection" in f["error"]
    assert "自相交" in f["error"]
    assert f["suggestion"] is None


def test_recoverable_factory():
    """make_recoverable 构造带建议值的错误"""
    r = make_recoverable(
        GeometryFailureReason.FILLET_TOO_LARGE,
        suggestion={"operation": "fillet", "new_radius": 2.0, "original_radius": 5.0},
        detail="R5 过大"
    )
    assert r["error_kind"] == "RECOVERABLE"
    assert r["suggestion"]["new_radius"] == 2.0
    assert "R5 过大" in r["error"]


def test_can_catch_with_base_class():
    """可以用基类 catch 所有 3 类必抛异常"""
    for err_cls in [InvalidRequestError, KernelBugError, StateCorruptionError]:
        try:
            raise err_cls("test")
        except MechKernelError as e:
            assert isinstance(e, err_cls)
