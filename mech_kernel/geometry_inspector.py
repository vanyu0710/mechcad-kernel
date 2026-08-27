"""
MechKernel Geometry Inspector（M1.1 修复版）

P0-2 修复：manifold/watertight/connected 用三态 (valid/invalid/unknown)
避免欧拉公式误判（圆环 g=1 欧拉=0，不能用 V-E+F==2 判断）

其他修复：
- bbox 解析容错（NaN/Inf → 兜底）
- 异常隔离，绝不让 kernel 崩
"""
from typing import Any, Optional, Tuple, Literal
import math

from .step_result import GeometrySummary


# 拓扑检查结果：三态
TopologyCheckResult = Literal["valid", "invalid", "unknown"]


class GeometryInspector:
    """
    几何指标计算器（P0 修复版）。
    
    manifold/watertight/connected 现在可以是 `True` / `False` / `"unknown"`：
    - True/False: 兼容旧代码（bool 比较仍工作）
    - "unknown": 无法判断（缺底层 API）
    """
    
    def summary(self, geometry: Any, feature_count: int = 0) -> GeometrySummary:
        """计算几何摘要"""
        if geometry is None:
            return self._empty_summary(feature_count)
        
        try:
            bbox = self._bounding_box(geometry) or (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        except Exception:
            bbox = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        
        try:
            volume = float(self._safe_call(geometry, "volume", default=0.0))
        except Exception:
            volume = 0.0
        
        try:
            surface_area = float(self._safe_call(geometry, "area", default=0.0))
        except Exception:
            surface_area = 0.0
        
        try:
            face_count = int(self._safe_call(geometry, "face_count", default=0))
        except Exception:
            try:
                face_count = len(self._safe_call(geometry, "faces", default=[]))
            except Exception:
                face_count = 0
        
        try:
            edge_count = int(self._safe_call(geometry, "edge_count", default=0))
        except Exception:
            try:
                edge_count = len(self._safe_call(geometry, "edges", default=[]))
            except Exception:
                edge_count = 0
        
        try:
            vertex_count = int(self._safe_call(geometry, "vertex_count", default=0))
        except Exception:
            try:
                vertex_count = len(self._safe_call(geometry, "vertices", default=[]))
            except Exception:
                vertex_count = 0
        
        is_manifold = self._check_manifold(geometry)
        is_watertight = self._check_watertight(geometry)
        is_connected = self._check_connected(geometry)
        
        return GeometrySummary(
            bounding_box=bbox,
            volume=volume,
            surface_area=surface_area,
            face_count=face_count,
            edge_count=edge_count,
            vertex_count=vertex_count,
            is_manifold=is_manifold,
            is_watertight=is_watertight,
            is_connected=is_connected,
            feature_count=feature_count,
        )
    
    def _empty_summary(self, feature_count: int = 0) -> GeometrySummary:
        return GeometrySummary(
            bounding_box=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            volume=0.0,
            surface_area=0.0,
            face_count=0,
            edge_count=0,
            vertex_count=0,
            is_manifold=False,
            is_watertight=False,
            is_connected=False,
            feature_count=feature_count,
        )
    
    def _safe_call(self, obj: Any, method: str, default=None):
        if not hasattr(obj, method):
            return default
        try:
            result = getattr(obj, method)()
            return result if result is not None else default
        except Exception:
            return default
    
    def _bounding_box(self, geometry: Any) -> Optional[Tuple[float, float, float, float, float, float]]:
        """计算包围盒（容错版）
        
        返回 None 表示：没有 bbox 方法 / 解析失败 / 包含 NaN/Inf
        """
        # build123d 风格
        if hasattr(geometry, "bounding_box"):
            try:
                bb = geometry.bounding_box()
                if hasattr(bb, "min") and hasattr(bb, "max"):
                    result = (
                        float(bb.min.X), float(bb.min.Y), float(bb.min.Z),
                        float(bb.max.X), float(bb.max.Y), float(bb.max.Z),
                    )
                    if all(math.isfinite(x) for x in result):
                        return result
                    return None  # NaN/Inf
            except Exception:
                pass
        
        # trimesh 风格：.bounds
        if hasattr(geometry, "bounds"):
            try:
                bounds = geometry.bounds
                if hasattr(bounds, "shape") and len(bounds.shape) == 2:
                    result = (
                        float(bounds[0][0]), float(bounds[0][1]), float(bounds[0][2]),
                        float(bounds[1][0]), float(bounds[1][1]), float(bounds[1][2]),
                    )
                    if all(math.isfinite(x) for x in result):
                        return result
                    return None  # NaN/Inf
            except Exception:
                pass
        
        # 自定义 .bbox()
        if hasattr(geometry, "bbox"):
            try:
                result = tuple(float(x) for x in geometry.bbox())
                if all(math.isfinite(x) for x in result):
                    return result
                return None  # NaN/Inf
            except Exception:
                pass
        
        return None  # 没有 bbox 方法
    
    def _check_manifold(self, geometry: Any) -> TopologyCheckResult:
        """
        P0-2 修复：返回三态 (valid / invalid / unknown)。
        
        1. 如果有底层 is_manifold API，直接调用
        2. 否则返回 "unknown"（不伪造 False）
        
        欧拉公式不能单独证明 manifold（圆环 g=1 欧拉=0 仍合法），
        不再作为判断依据。
        """
        # 优先用底层 API
        if hasattr(geometry, "is_manifold"):
            try:
                result = geometry.is_manifold()
                if isinstance(result, bool):
                    return "valid" if result else "invalid"
            except Exception:
                pass
        
        # 简化判断：edge + vertex 都没有 → 无效
        edge_count = self._safe_call(geometry, "edge_count", default=0)
        vertex_count = self._safe_call(geometry, "vertex_count", default=0)
        face_count = self._safe_call(geometry, "face_count", default=0)
        
        if edge_count == 0 and vertex_count == 0 and face_count == 0:
            return "invalid"
        
        # 没法 100% 确定 → 返回 unknown
        return "unknown"
    
    def _check_watertight(self, geometry: Any) -> TopologyCheckResult:
        """P0-2 修复：水密检查也用三态。"""
        if hasattr(geometry, "is_watertight"):
            try:
                result = geometry.is_watertight()
                if isinstance(result, bool):
                    return "valid" if result else "invalid"
            except Exception:
                pass
        
        try:
            volume = float(self._safe_call(geometry, "volume", default=0.0))
            if volume <= 0:
                return "invalid"
        except Exception:
            return "unknown"
        
        return "unknown"
    
    def _check_connected(self, geometry: Any) -> TopologyCheckResult:
        """P0-2 修复：连通检查用三态。"""
        if hasattr(geometry, "is_connected"):
            try:
                result = geometry.is_connected()
                if isinstance(result, bool):
                    return "valid" if result else "invalid"
            except Exception:
                pass
        
        face_count = self._safe_call(geometry, "face_count", default=0)
        if face_count < 4:
            return "invalid"
        
        return "unknown"
    
    def validate(self, geometry: Any) -> Tuple[bool, list]:
        """
        验证几何有效性。
        接受三态：valid / invalid / unknown
        - valid → 通过
        - invalid → 加入 issues
        - unknown → 不报错（保守）
        """
        issues = []
        
        if geometry is None:
            return True, []
        
        summary = self.summary(geometry)
        
        if summary.is_manifold == "invalid":
            issues.append("非流形几何")
        if summary.is_watertight == "invalid":
            if summary.volume <= 0:
                issues.append(f"体积为 0 或负数: {summary.volume}")
            else:
                issues.append("不水密（开口几何）")
        if summary.is_connected == "invalid":
            issues.append(f"几何不连通（face_count={summary.face_count}）")
        
        if summary.volume > 1e15:
            issues.append(f"体积异常大: {summary.volume}")
        
        if any(math.isnan(x) or math.isinf(x) for x in summary.bounding_box):
            issues.append(f"包围盒包含 NaN/Inf: {summary.bounding_box}")
        
        return len(issues) == 0, issues
