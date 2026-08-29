"""
MechKernel 类型化错误定义（v1.1 修复版）

5 类 + 1 类：
1. InvalidRequestError    - 编程错误，必须抛
2. KernelBugError         - 内部 bug，必须抛
3. StateCorruptionError   - 状态损坏，必须抛
4. GEOMETRY_FAILURE       - 预期内几何失败，StepResult 表达
5. RECOVERABLE            - 可修复失败，StepResult 表达
6. NOT_IMPLEMENTED        - 能力未实现（占位 API），StepResult 表达（P0 修复新增）
"""
from typing import Optional, Dict, Any
from dataclasses import dataclass, field


class MechKernelError(Exception):
    """MechKernel 异常的基类"""
    pass


class InvalidRequestError(MechKernelError):
    """编程错误：参数非法、状态不一致、内部 invariant 违例。
    必须抛，调用方必须修复。"""
    
    def __init__(self, message: str, hint: Optional[str] = None):
        super().__init__(message)
        self.hint = hint


class KernelBugError(MechKernelError):
    """Kernel 内部 bug，应该不可能发生，发生即记录并中止。"""
    pass


class StateCorruptionError(MechKernelError):
    """状态损坏：Feature Graph 出现环、引用解析失败、事务不一致。
    必须抛，调用方应重建状态。"""
    pass


class GeometryValidationError(MechKernelError):
    """Candidate geometry failed the kernel validation contract."""

    def __init__(self, message: str, validation: Optional[dict] = None):
        super().__init__(message)
        self.validation = validation or {}


class DeprecatedInternalAPIError(KernelBugError):
    """内部 API 已弃用。用于标记不该被调用的方法（如 _push_undo）。"""
    pass


# === 不抛异常的类型（通过 StepResult 表达） ===

class GeometryFailureReason:
    """预期内几何失败的常见原因"""
    SELF_INTERSECTION = "self_intersection"
    EMPTY_RESULT = "empty_result"
    FACE_NOT_FOUND = "face_not_found"
    PARAMETER_OUT_OF_RANGE = "parameter_out_of_range"
    BOOLEAN_FAILED = "boolean_failed"
    FILLET_TOO_LARGE = "fillet_too_large"
    CHAMFER_TOO_LARGE = "chamfer_too_large"
    HOLE_OUTSIDE_FACE = "hole_outside_face"
    INVALID_SKETCH = "invalid_sketch"
    DEGENERATE_GEOMETRY = "degenerate_geometry"
    NOT_MANIFOLD = "not_manifold"
    NOT_WATERTIGHT = "not_watertight"


def make_geometry_failure(reason: str, detail: str = "") -> Dict[str, Any]:
    """构造 GEOMETRY_FAILURE 错误的数据"""
    return {
        "error_kind": "GEOMETRY_FAILURE",
        "error": f"[{reason}] {detail}" if detail else f"[{reason}]",
        "suggestion": None,
        "render_level": "iso_only",   # 几何失败建议看图理解
    }


def make_recoverable(reason: str, suggestion: Dict[str, Any], detail: str = "") -> Dict[str, Any]:
    """构造 RECOVERABLE 错误的数据（含建议值）"""
    return {
        "error_kind": "RECOVERABLE",
        "error": f"[{reason}] {detail}" if detail else f"[{reason}]",
        "suggestion": suggestion,
        "render_level": "iso_only",   # 可恢复错误显示当前几何
    }


def make_not_implemented(api_name: str, planned_version: str, detail: str = "") -> Dict[str, Any]:
    """构造 NOT_IMPLEMENTED 错误的数据（占位 API 使用）。
    
    P0-2 修复：占位 API 不再伪装成 GEOMETRY_FAILURE，
    而是用专门的 NOT_IMPLEMENTED 类型，AI 看到就知道是能力未实现。
    """
    return {
        "error_kind": "NOT_IMPLEMENTED",
        "error": f"[NOT_IMPLEMENTED] {api_name} {detail}（计划在 {planned_version} 实现）",
        "suggestion": None,
        "api_name": api_name,
        "planned_version": planned_version,
        "render_level": "none",   # 占位错误不需要渲染
    }


# === 错误类型 → 推荐渲染级别的映射 ===
# 用于自适应渲染策略
ERROR_RENDER_HINT = {
    "GEOMETRY_FAILURE": "iso_only",          # 几何失败：看图理解
    "RECOVERABLE": "iso_only",               # 可恢复：看图 + suggestion
    "NOT_IMPLEMENTED": "none",               # 未实现：不需要渲染
    "INVALID_REQUEST": "none",               # 编程错误：不需要渲染
    "KERNEL_BUG": "none",                    # 内部 bug：不需要渲染
    "STATE_CORRUPTION": "iso_only",          # 状态损坏：可能需要看当前状态
}


def get_render_level_for_error(error_kind: str) -> str:
    """根据错误类型返回推荐渲染级别"""
    return ERROR_RENDER_HINT.get(error_kind, "none")
