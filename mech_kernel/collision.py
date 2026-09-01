"""
MechKernel v2.9: Collision / Interference Check

提供 API:
  check_pair_interference(part_a, part_b) → 检查两个 Part 的干涉
  check_assembly_interference(parts) → 检查装配体所有 part pair 的干涉

实现: build123d `Part & Part` (boolean intersection).
  - 如果交集 vol = 0 → 不干涉
  - 如果 vol > 0 → 干涉, 返回 (干涉体积, 干涉 Part)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, Union

from build123d import Part


def check_pair_interference(
    part_a: Part,
    part_b: Part,
    name_a: str = "A",
    name_b: str = "B",
    tolerance: float = 0.001,
    strict: bool = False,
) -> Dict[str, Any]:
    """检查两个 Part 是否相互干涉.

    算法: build123d `part_a & part_b` (OCC BRepAlgoAPI_Common)
          如果交集体积 > tolerance, 则视为有干涉.

    Args:
        part_a: 第一个 Part
        part_b: 第二个 Part
        name_a: 第一个的名字 (用于报告)
        name_b: 第二个的名字 (用于报告)
        tolerance: 体积阈值 (mm³), 低于此值视为无干涉
                   默认 0.001 (考虑 OCC 浮点精度)
        strict: True 时区分 expected OCC 错误 (空 shape) vs unexpected
                错误 (raise); 默认 False 保持向后兼容

    Returns:
        {
          "name_a": str,
          "name_b": str,
          "interfering": bool,
          "volume_mm3": float,        # 干涉体积 (0 表示无)
          "intersection_part": Part | None,  # 干涉实体 (None 表示无)
          "center": (x, y, z) | None,  # 干涉中心
          "error": str | None,  # 计算错误信息 (如有)
        }
    """
    # 区分 expected OCC 错误 (空 shape) vs unexpected
    try:
        common = part_a & part_b
    except (ValueError, TypeError, AttributeError) as e:
        # expected: 空 shape / 无效 input
        if strict:
            raise
        return {
            "name_a": name_a,
            "name_b": name_b,
            "interfering": False,
            "volume_mm3": 0.0,
            "intersection_part": None,
            "center": None,
            "error": f"expected: {type(e).__name__}: {e}",
        }
    except Exception as e:
        # unexpected: OCC 内部错误 / kernel bug
        if strict:
            raise
        # 仍记录, 但标为"未知", 让 caller 决定如何处理
        return {
            "name_a": name_a,
            "name_b": name_b,
            "interfering": False,
            "volume_mm3": 0.0,
            "intersection_part": None,
            "center": None,
            "error": f"unexpected: {type(e).__name__}: {e}",
        }

    # 显式检查空 shape (不依赖 hasattr)
    # OCC 的 empty shape (Common 算法返回 null shape) 在 build123d 中:
    #   - 如果两个 shape 不相交: common 是空 Compound (无 Solids)
    #   - 如果一个 shape 包含另一个: common 是空 / 等于较小的
    # 实际检测: 试 bounding_box, 空 shape 抛异常或返回 null bbox
    try:
        bb = common.bounding_box()
        # 空 shape 的 bbox 可能是 (0,0,0) 到 (0,0,0)
        if (bb.max.X - bb.min.X) < 1e-9 and (bb.max.Y - bb.min.Y) < 1e-9 and (bb.max.Z - bb.min.Z) < 1e-9:
            return {
                "name_a": name_a,
                "name_b": name_b,
                "interfering": False,
                "volume_mm3": 0.0,
                "intersection_part": None,
                "center": None,
            }
    except (ValueError, TypeError, AttributeError):
        # 空 shape: 视为无干涉
        return {
            "name_a": name_a,
            "name_b": name_b,
            "interfering": False,
            "volume_mm3": 0.0,
            "intersection_part": None,
            "center": None,
        }

    # 计算体积 — 只在 bbox 有效时
    try:
        vol = float(common.volume) if common.volume else 0.0
    except (ValueError, TypeError, AttributeError) as e:
        if strict:
            raise
        return {
            "name_a": name_a,
            "name_b": name_b,
            "interfering": False,
            "volume_mm3": 0.0,
            "intersection_part": None,
            "center": None,
            "error": f"volume read failed: {type(e).__name__}: {e}",
        }

    interfering = vol > tolerance

    result = {
        "name_a": name_a,
        "name_b": name_b,
        "interfering": interfering,
        "volume_mm3": vol,
        "intersection_part": common if interfering else None,
        "center": None,
    }
    if interfering:
        try:
            result["center"] = (
                (bb.min.X + bb.max.X) / 2,
                (bb.min.Y + bb.max.Y) / 2,
                (bb.min.Z + bb.max.Z) / 2,
            )
        except Exception:
            pass
    return result


def check_assembly_interference(
    parts: List[Tuple[str, Part]],
    tolerance: float = 0.001,
    only_interfering: bool = False,
) -> Dict[str, Any]:
    """检查装配体所有 part pair 的干涉.

    Args:
        parts: [(name, Part), ...] 列表
        tolerance: 体积阈值 (mm³)
        only_interfering: 如果 True, 只返回有干涉的 pair

    Returns:
        {
          "total_pairs": int,
          "interfering_count": int,
          "max_interference_volume": float,
          "pairs": [check_pair_interference(...)],
          "interfering_pairs": [check_pair_interference(...)],  # 子集
        }
    """
    results: List[Dict[str, Any]] = []
    n = len(parts)
    for i in range(n):
        for j in range(i + 1, n):
            name_a, part_a = parts[i]
            name_b, part_b = parts[j]
            r = check_pair_interference(part_a, part_b, name_a, name_b, tolerance)
            if only_interfering and not r.get("interfering", False):
                continue
            results.append(r)

    interfering = [r for r in results if r.get("interfering", False)]
    max_vol = max((r["volume_mm3"] for r in interfering), default=0.0)

    return {
        "total_pairs": n * (n - 1) // 2,
        "interfering_count": len(interfering),
        "max_interference_volume": max_vol,
        "pairs": results,
        "interfering_pairs": interfering,
    }


def check_interference_matrix(
    parts: List[Tuple[str, Part]],
    tolerance: float = 0.001,
) -> Dict[str, Any]:
    """生成 N×N 干涉矩阵 (适合可视化).

    Returns:
        {
          "names": [str],
          "matrix": [[float, ...], ...],  # N×N 体积矩阵, 对角线为 0
          "interfering_pairs": [(i, j, vol), ...]
        }
    """
    n = len(parts)
    names = [p[0] for p in parts]
    matrix = [[0.0] * n for _ in range(n)]
    interfering = []
    for i in range(n):
        for j in range(i + 1, n):
            r = check_pair_interference(parts[i][1], parts[j][1], tolerance=tolerance)
            matrix[i][j] = r["volume_mm3"]
            matrix[j][i] = r["volume_mm3"]
            if r.get("interfering", False):
                interfering.append((i, j, r["volume_mm3"]))
    return {
        "names": names,
        "matrix": matrix,
        "interfering_pairs": interfering,
    }


__all__ = [
    "check_pair_interference",
    "check_assembly_interference",
    "check_interference_matrix",
]
