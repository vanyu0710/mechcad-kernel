"""
MechKernel 单位管理

P7 原则：内部全部毫米，API 不暴露 inch。
所有输入参数默认 mm，返回值默认 mm。
"""
from typing import Union

Number = Union[int, float]

# 内部单位
INTERNAL_UNIT = "mm"

# 单位转换表（输入侧只支持 mm 和 inch）
_TO_MM = {
    "mm": 1.0,
    "cm": 10.0,
    "m": 1000.0,
    "in": 25.4,
    "inch": 25.4,
    "ft": 304.8,
}


def to_mm(value: Number, unit: str = "mm") -> float:
    """将任意单位转换为 mm"""
    if unit not in _TO_MM:
        raise ValueError(f"不支持的单位: {unit}。支持: {list(_TO_MM.keys())}")
    return float(value) * _TO_MM[unit]


def from_mm(value: Number, unit: str = "mm") -> float:
    """将 mm 转换为任意单位"""
    if unit not in _TO_MM:
        raise ValueError(f"不支持的单位: {unit}。支持: {list(_TO_MM.keys())}")
    return float(value) / _TO_MM[unit]


def is_positive(value: Number) -> bool:
    """是否是正数（用于校验）"""
    return float(value) > 0


def is_non_negative(value: Number) -> bool:
    """是否是非负数"""
    return float(value) >= 0
