"""
MechKernel Geometry Inspector（M1.1 修复版）

P0-2 修复：manifold/watertight/connected 用三态 (valid/invalid/unknown)
避免欧拉公式误判（圆环 g=1 欧拉=0，不能用 V-E+F==2 判断）

其他修复：
- bbox 解析容错（NaN/Inf → 兜底）
- 异常隔离，绝不让 kernel 崩
"""
from typing import Any, Optional, Tuple, Literal, Dict
import math
import hashlib
import json

from .step_result import GeometrySummary


# 拓扑检查结果：三态
TopologyCheckResult = Literal["valid", "invalid", "unknown"]


VALIDATION_LEVELS = frozenset({"basic", "standard", "strict"})


class GeometryValidation:
    """Stable, serializable validation result for AI and persistence layers."""

    def __init__(self, valid: bool, status: str, reason_codes: list,
                 summary: GeometrySummary, fingerprint: str, solid_count: int = 0):
        self.valid = bool(valid)
        self.status = status
        self.reason_codes = list(reason_codes)
        self.summary = summary
        self.fingerprint = fingerprint
        self.solid_count = int(solid_count)

    def to_dict(self) -> dict:
        data = self.summary.to_dict()
        return {
            "valid": self.valid,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "solid_count": self.solid_count,
            "volume": data["volume"],
            "bounding_box": data["bounding_box"],
            "face_count": data["face_count"],
            "edge_count": data["edge_count"],
            "vertex_count": data["vertex_count"],
            "is_manifold": data["is_manifold"],
            "is_watertight": data["is_watertight"],
            "is_connected": data["is_connected"],
            "fingerprint": self.fingerprint,
        }

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

    def fingerprint(self, geometry: Any, tolerance: float = 1e-6) -> str:
        """Return a deterministic fingerprint from quantized geometry metrics."""
        summary = self.summary(geometry)
        scale = 1.0 / max(float(tolerance), 1e-12)

        def quantize(value):
            if isinstance(value, (int, float)):
                return int(round(float(value) * scale))
            if isinstance(value, (list, tuple)):
                return [quantize(item) for item in value]
            return value

        payload = {
            "bounding_box": quantize(summary.bounding_box),
            "volume": quantize(summary.volume),
            "surface_area": quantize(summary.surface_area),
            "face_count": summary.face_count,
            "edge_count": summary.edge_count,
            "vertex_count": summary.vertex_count,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def validate_geometry(self, geometry: Any, level: str = "standard",
                          feature_count: int = 0) -> GeometryValidation:
        """Validate geometry without treating unknown topology as invalid."""
        if level not in VALIDATION_LEVELS:
            raise ValueError(f"level must be one of {sorted(VALIDATION_LEVELS)}")
        summary = self.summary(geometry, feature_count=feature_count)
        reasons = []
        solid_count = 0
        try:
            solids = getattr(geometry, "solids", None)
            solids = solids() if callable(solids) else solids
            solid_count = len(solids) if solids is not None else 0
        except Exception:
            pass
        if geometry is None:
            return GeometryValidation(True, "unknown", [], summary, self.fingerprint(geometry))

        bbox = summary.bounding_box
        if len(bbox) != 6 or any(not math.isfinite(float(value)) for value in bbox):
            reasons.append("INVALID_BOUNDS")
        elif any(bbox[index] > bbox[index + 3] for index in range(3)):
            reasons.append("INVALID_BOUNDS")
        if not math.isfinite(summary.volume) or summary.volume < -1e-9:
            reasons.append("INVALID_VOLUME")
        elif summary.volume <= 1e-12:
            reasons.append("EMPTY_RESULT")
        if summary.volume > 1e15:
            reasons.append("VOLUME_OUT_OF_RANGE")

        if level in ("standard", "strict"):
            validity = self._safe_call(geometry, "is_valid", default=None)
            if isinstance(validity, bool) and not validity:
                reasons.append("INVALID_TOPOLOGY")
            # Manifold/watertight tri-state values are diagnostic only. OCC
            # compounds and valid fillets can expose conservative false values;
            # explicit is_valid() is the rejection criterion for strict mode.

        status = "invalid" if reasons else "valid"
        return GeometryValidation(
            not reasons, status, sorted(set(reasons)), summary,
            self.fingerprint(geometry), solid_count,
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
            value = getattr(obj, method)
            result = value() if callable(value) else value
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
                value = geometry.bounding_box
                bb = value() if callable(value) else value
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
                value = geometry.bbox
                value = value() if callable(value) else value
                result = tuple(float(x) for x in value)
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
