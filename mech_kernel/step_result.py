"""
MechKernel StepResult 数据结构

C 方案：自适应渲染 - 默认不渲染，拓扑变化 / 关键决策点才渲染
"""
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Literal
import time
import base64

RenderLevel = Literal["none", "iso_only", "full"]
ErrorKind = Literal["INVALID_REQUEST", "GEOMETRY_FAILURE", "RECOVERABLE", "KERNEL_BUG", "STATE_CORRUPTION"]


@dataclass
class GeometrySummary:
    """结构化几何摘要（默认返回，替代渲染）"""
    bounding_box: tuple = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)   # (xmin, ymin, zmin, xmax, ymax, zmax)
    volume: float = 0.0        # mm³
    surface_area: float = 0.0  # mm²
    face_count: int = 0
    edge_count: int = 0
    vertex_count: int = 0
    is_manifold: bool = False
    is_watertight: bool = False
    is_connected: bool = False
    feature_count: int = 0
    
    def to_dict(self) -> dict:
        return {
            "bounding_box": list(self.bounding_box),
            "volume": self.volume,
            "surface_area": self.surface_area,
            "face_count": self.face_count,
            "edge_count": self.edge_count,
            "vertex_count": self.vertex_count,
            "is_manifold": self.is_manifold,
            "is_watertight": self.is_watertight,
            "is_connected": self.is_connected,
            "feature_count": self.feature_count,
        }


@dataclass
class StepResult:
    """
    每步操作的结果。
    兼容 subscript: result["value"] 等同于 result.value
    """
    
    def __getitem__(self, key):
        """兼容测试：result["value"] = result.value"""
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)
    """
    每步操作的结果，是 AI 决策的核心输入。
    
    C 方案渲染策略：
    - 默认 render_level="none"，只返回结构化数据
    - 拓扑变化、失败恢复、5 步间隔 → "iso_only" 或 "full"
    """
    
    # === 基础状态 ===
    success: bool
    feature_id: Optional[str] = None
    error_kind: Optional[ErrorKind] = None
    error: Optional[str] = None
    suggestion: Optional[dict] = None
    api_name: Optional[str] = None
    planned_version: Optional[str] = None
    hint: Optional[str] = None  # 给 AI 的提示（如"用 PUBLIC_OPS 替代内部方法"）
    warning: Optional[str] = None  # v1.16: 成功但需注意的事项（如"几何未重算"）
    
    # === 渲染策略（专家 C 方案）===
    render_level: RenderLevel = "none"
    render_png: Optional[bytes] = None
    render_base64: Optional[str] = None
    render_views: Optional[Dict[str, bytes]] = None  # full 时 4 视角
    evidence_manifest: Optional[dict] = None  # v2.3: 投影、截面、预算与图像指纹
    
    # === 结构化数据（默认必有）===
    geometry_summary: Optional[GeometrySummary] = None
    feature_graph_delta: Optional[dict] = None
    
    # === 叙事（给 LLM 读）===
    narrative: str = ""
    current_narrative: List[str] = field(default_factory=list)
    
    # === 决策辅助 ===
    next_hints: List[str] = field(default_factory=list)
    semantic_state: Dict[str, Any] = field(default_factory=dict)
    
    # === 元信息 ===
    elapsed_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)
    step_index: int = 0
    
    def has_render(self) -> bool:
        """是否有渲染图"""
        return self.render_png is not None and len(self.render_png) > 0
    
    def to_summary_dict(self) -> dict:
        """精简版（用于 AI 决策时减少 token）"""
        return {
            "success": self.success,
            "feature_id": self.feature_id,
            "error_kind": self.error_kind,
            "error": self.error,
            "suggestion": self.suggestion,
            "api_name": self.api_name,
            "planned_version": self.planned_version,
            "warning": self.warning,
            "render_level": self.render_level,
            "has_render": self.has_render(),
            "views": list(self.render_views.keys()) if self.render_views else [],
            "evidence_manifest": self.evidence_manifest,
            "narrative": self.narrative,
            "hints": self.next_hints,
            "geometry_summary": self.geometry_summary.to_dict() if self.geometry_summary else None,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "step_index": self.step_index,
        }
    
    @staticmethod
    def empty_geometry_summary_dict() -> dict:
        return {
            "bounding_box": [0, 0, 0, 0, 0, 0],
            "volume": 0.0,
            "surface_area": 0.0,
            "face_count": 0,
            "edge_count": 0,
            "vertex_count": 0,
            "is_manifold": False,
            "is_watertight": False,
            "is_connected": False,
            "feature_count": 0,
        }


def make_success(
    feature_id: str,
    narrative: str,
    geometry_summary: Optional[GeometrySummary] = None,
    render_png: Optional[bytes] = None,
    render_views: Optional[Dict[str, bytes]] = None,
    render_level: RenderLevel = "none",
    next_hints: Optional[List[str]] = None,
    semantic_state: Optional[Dict] = None,
    current_narrative: Optional[List[str]] = None,
    warning: Optional[str] = None,
    feature_graph_delta: Optional[dict] = None,
    elapsed_ms: float = 0.0,
    step_index: int = 0,
) -> StepResult:
    """构造成功 StepResult"""
    render_base64 = None
    if render_png:
        render_base64 = base64.b64encode(render_png).decode()
    return StepResult(
        success=True,
        feature_id=feature_id,
        narrative=narrative,
        current_narrative=current_narrative or [],
        render_level=render_level,
        render_png=render_png,
        render_base64=render_base64,
        render_views=render_views,
        geometry_summary=geometry_summary,
        next_hints=next_hints or [],
        semantic_state=semantic_state or {},
        feature_graph_delta=feature_graph_delta,
        warning=warning,
        elapsed_ms=elapsed_ms,
        step_index=step_index,
    )


def make_failure(
    error: str,
    error_kind: ErrorKind = "GEOMETRY_FAILURE",
    suggestion: Optional[dict] = None,
    feature_id: Optional[str] = None,
    current_narrative: Optional[List[str]] = None,
    geometry_summary: Optional[GeometrySummary] = None,
    render_level: Optional[RenderLevel] = None,
    render_png: Optional[bytes] = None,
    api_name: Optional[str] = None,
    planned_version: Optional[str] = None,
    hint: Optional[str] = None,
    warning: Optional[str] = None,
    elapsed_ms: float = 0.0,
    step_index: int = 0,
) -> StepResult:
    """构造失败 StepResult"""
    if render_level is None:
        from .errors import get_render_level_for_error
        render_level = get_render_level_for_error(error_kind)
    
    render_base64 = None
    if render_png:
        render_base64 = base64.b64encode(render_png).decode()
    
    return StepResult(
        success=False,
        error=error,
        error_kind=error_kind,
        suggestion=suggestion,
        feature_id=feature_id,
        current_narrative=current_narrative or [],
        geometry_summary=geometry_summary,
        render_level=render_level,
        render_png=render_png,
        render_base64=render_base64,
        api_name=api_name,
        planned_version=planned_version,
        hint=hint,
        warning=warning,
        elapsed_ms=elapsed_ms,
        step_index=step_index,
    )
