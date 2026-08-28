"""
MechKernel Adaptive Renderer（M1 阶段）

C 方案的核心：决定每步操作该用哪个 render_level。
- none: 不渲染（默认）
- iso_only: 1 张 iso 视角
- full: 4 视角

策略：
1. 拓扑变化必渲染 full
2. 失败恢复必渲染 full
3. 间隔 N 步渲染一次 iso_only
4. 草图/查询/状态操作不渲染
"""
from typing import Optional, Dict
import time

from .features import FeatureType, TOPOLOGY_CHANGING_OPS, NON_RENDERING_OPS


# 需要 full 渲染的"关键决策点"操作
KEY_DECISION_OPS = frozenset({
    "delete_feature",
    "update_feature",
})


class AdaptiveRenderer:
    """
    自适应渲染策略器。
    
    用法：
        ar = AdaptiveRenderer(interval=5)
        level = ar.should_render(op, params, current_step)
        # level 是 "none" | "iso_only" | "full"
    """
    
    def __init__(self, interval: int = 5):
        """
        Args:
            interval: 每隔多少步渲染一次 full（默认 5）
        """
        self.interval = interval
        self._last_render_step = 0  # 改为 0，避免第一次触发间隔逻辑
        self._last_op: Optional[str] = None
        self._last_was_failure = False
        self._step_counter = 0
        self.suspended = False
    
    def should_render(
        self, 
        op: str, 
        op_params: Optional[dict] = None,
        has_geometry: bool = False,
    ) -> str:
        """
        决定渲染级别。
        
        Args:
            op: 操作名（如 "extrude", "add_circle"）
            op_params: 操作参数
            has_geometry: 当前是否有几何可渲染
        
        Returns:
            "none" | "iso_only" | "full"
        """
        self._step_counter += 1
        op_params = op_params or {}

        if self.suspended:
            self._last_op = op
            self._last_was_failure = False
            return "none"
        
        # 0. 没有几何：无论如何不渲染
        if not has_geometry:
            self._last_op = op
            self._last_was_failure = False
            return "none"
        
        # 1. 拓扑变化需要完整多视图，供视觉校验拓扑结果
        if self._is_topology_changing(op):
            level = "full"
            self._last_render_step = self._step_counter
            self._last_op = op
            self._last_was_failure = False
            return level
        
        # 2. 失败恢复后必渲染完整视图
        if self._last_was_failure and self._is_recovery(op):
            level = "full"
            self._last_render_step = self._step_counter
            self._last_op = op
            self._last_was_failure = False
            return level
        
        # 3. 关键决策点
        if op in KEY_DECISION_OPS:
            level = "full"
            self._last_render_step = self._step_counter
            self._last_op = op
            self._last_was_failure = False
            return level
        
        # 4. 间隔 N 步渲染一次 iso 快照，控制视觉 token 成本
        # 注意：step_counter >= last_render_step + interval 才触发
        if self._step_counter - self._last_render_step >= self.interval:
            level = "iso_only"
            self._last_render_step = self._step_counter
            self._last_op = op
            self._last_was_failure = False
            return level
        
        # 5. 草图/查询/状态操作不渲染
        if self._is_non_rendering_op(op):
            self._last_op = op
            self._last_was_failure = False
            return "none"
        
        # 6. 默认不渲染
        self._last_op = op
        self._last_was_failure = False
        return "none"
    
    def mark_failure(self, op: Optional[str] = None):
        """标记上一步失败（影响下次恢复判断）。"""
        self._last_was_failure = True
        if op is not None:
            self._last_op = op
    
    def reset(self):
        """重置策略器状态"""
        self._last_render_step = 0
        self._last_op = None
        self._last_was_failure = False
        self._step_counter = 0
        self.suspended = False
    
    def _is_topology_changing(self, op: str) -> bool:
        """是否是拓扑变化操作"""
        # 直接用 API 名映射到 FeatureType
        mapping = {
            "extrude": FeatureType.EXTRUDE,
            "revolve": FeatureType.REVOLVE,
            "sweep": FeatureType.SWEEP,
            "boolean": FeatureType.BOOLEAN,
            "hole": FeatureType.HOLE,
            "fillet": FeatureType.FILLET,
            "chamfer": FeatureType.CHAMFER,
            "shell": FeatureType.SHELL,
            "linear_pattern": FeatureType.LINEAR_PATTERN,
            "circular_pattern": FeatureType.CIRCULAR_PATTERN,
            "mirror": FeatureType.MIRROR,
            "offset_face": FeatureType.OFFSET_FACE,
            "assemble": FeatureType.ASSEMBLY,
        }
        ft = mapping.get(op)
        if ft is None:
            return False
        return ft in TOPOLOGY_CHANGING_OPS
    
    def _is_non_rendering_op(self, op: str) -> bool:
        """是否是不渲染操作（操作名为字符串）"""
        non_render = {
            "create_workplane", "new_sketch",
            "add_circle", "add_rectangle", "add_line",
            "close_sketch",
            "query", "select", "measure",
            "undo", "redo",
        }
        return op in non_render
    
    def _is_recovery(self, op: str) -> bool:
        """是否是恢复操作（用了 RECOVERABLE suggestion）"""
        # 简化判断：相同 op 名
        return op == self._last_op
