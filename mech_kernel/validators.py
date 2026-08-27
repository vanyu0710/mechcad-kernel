"""
MechKernel 输入校验器（v1.1 修复版）

P1-6 修复：统一行为
- 校验函数：返回规范化值
- 失败时：抛 InvalidRequestError
- 不再做"返回清洗值 vs 抛异常"的混合行为

专家审查原话：
"有的函数抛异常，有的返回清洗后的值，调用方很容易漏处理。
统一为'纯校验返回规范化值，失败统一抛 InvalidRequestError'，不要混合隐式修正。"
"""
from typing import Any, List, Tuple, Optional
from .errors import InvalidRequestError
from .units import is_positive, is_non_negative, Number


def require_positive(name: str, value: Number) -> float:
    """要求正数，返回规范化 float。失败抛 InvalidRequestError。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise InvalidRequestError(f"{name} 必须是数字，收到: {value!r}")
    if not is_positive(v):
        raise InvalidRequestError(f"{name} 必须是正数，收到: {value}")
    return v


def require_non_negative(name: str, value: Number) -> float:
    """要求非负，返回规范化 float。失败抛 InvalidRequestError。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise InvalidRequestError(f"{name} 必须是数字，收到: {value!r}")
    if not is_non_negative(v):
        raise InvalidRequestError(f"{name} 必须是非负数，收到: {value}")
    return v


def require_finite(name: str, value: Number) -> float:
    """要求有限数（不是 inf/nan），返回规范化 float。失败抛 InvalidRequestError。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise InvalidRequestError(f"{name} 必须是数字，收到: {value!r}")
    import math
    if not math.isfinite(v):
        raise InvalidRequestError(f"{name} 必须是有限数，收到: {value}")
    return v


def require_tuple3(name: str, value: Any) -> Tuple[float, float, float]:
    """要求长度为 3 的元组/列表，返回规范化 tuple。失败抛 InvalidRequestError。"""
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise InvalidRequestError(f"{name} 必须是长度为 3 的元组/列表，收到: {value!r}")
    try:
        return (float(value[0]), float(value[1]), float(value[2]))
    except (TypeError, ValueError):
        raise InvalidRequestError(f"{name} 的元素必须是数字，收到: {value!r}")


def require_tuple2(name: str, value: Any) -> Tuple[float, float]:
    """要求长度为 2 的元组/列表，返回规范化 tuple。失败抛 InvalidRequestError。"""
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise InvalidRequestError(f"{name} 必须是长度为 2 的元组/列表，收到: {value!r}")
    try:
        return (float(value[0]), float(value[1]))
    except (TypeError, ValueError):
        raise InvalidRequestError(f"{name} 的元素必须是数字，收到: {value!r}")


def require_non_empty_str(name: str, value: Any) -> str:
    """要求非空字符串，返回规范化（去首尾空格）str。失败抛 InvalidRequestError。"""
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} 必须是字符串，收到: {type(value).__name__}")
    normalized = value.strip()
    if not normalized:
        raise InvalidRequestError(f"{name} 不能为空字符串")
    return normalized


def require_in(name: str, value: Any, allowed: List[Any]) -> Any:
    """要求 value 在 allowed 列表中，原样返回。失败抛 InvalidRequestError。"""
    if value not in allowed:
        raise InvalidRequestError(
            f"{name} 必须是 {allowed} 之一，收到: {value!r}"
        )
    return value


def require_positive_int(name: str, value: Any) -> int:
    """要求正整数，返回规范化 int。失败抛 InvalidRequestError。"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise InvalidRequestError(f"{name} 必须是整数，收到: {value!r}")
    if v <= 0:
        raise InvalidRequestError(f"{name} 必须是正整数，收到: {value}")
    return v


def require_existing_sketch(sketches: dict, sketch_name: str) -> str:
    """要求草图已存在，返回规范化 name。失败抛 InvalidRequestError。"""
    name = require_non_empty_str("sketch_name", sketch_name)
    if name not in sketches:
        raise InvalidRequestError(f"Sketch 不存在: {name}")
    return name


def require_existing_workplane(workplanes, workplane_name: str) -> str:
    """要求工作平面已存在，返回规范化 name。失败抛 InvalidRequestError。"""
    name = require_non_empty_str("workplane_name", workplane_name)
    if not workplanes.has_name(name):
        raise InvalidRequestError(f"Workplane 不存在: {name}")
    return name
