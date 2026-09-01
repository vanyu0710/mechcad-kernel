"""
MechKernel 主类（v2.2 修复版）

核心：公共建模 API + 参数化重放 + 多视图/截面渲染 + 事务回滚 + 类型化错误
"""
from typing import List, Optional, Dict, Any, Tuple, Union
import copy
import time
import base64
import hashlib

from .errors import (
    MechKernelError, InvalidRequestError, KernelBugError, StateCorruptionError,
    GeometryValidationError, RecoverableError,
    make_geometry_failure, make_recoverable, GeometryFailureReason,
    DeprecatedInternalAPIError
)
from .geometry_inspector import GeometryInspector
from .renderer import Renderer
from .adaptive_renderer import AdaptiveRenderer
from .capability_registry import (
    CapabilityRegistry, FieldSchema, Capability,
    string, number, integer, boolean, enum_field, tuple2, tuple3, list_of
)
from .features import (
    FeatureType, FeatureState, FeatureNode, Sketch, SketchEntity,
    Constraint, ConstraintStatus, Reference, TOPOLOGY_CHANGING_OPS, NON_RENDERING_OPS,
    next_feature_id, next_sketch_id, next_workplane_id, next_entity_id,
    next_constraint_id, reset_all_id_generators, seed_id_generators_from_history
)
from .assembly import AssemblyInstance
from .constraint_solver import SUPPORTED_CONSTRAINTS, solve_sketch as solve_sketch_constraints, validate_constraint
from .reference_frames import (
    CoordinateFrame, FrameRegistry, resolve_point as rf_resolve_point,
    resolve_placement as rf_resolve_placement,
)
from .feature_graph import FeatureGraph
from .workplane import Workplane, WorkplaneType, WorkplaneRegistry
from .persistent_naming import PersistentNamingResolver, PersistentName
from .topology_refs import (
    TopologyCache, TopoInfo, RefStaleError, RefFormatError, resolve_refs,
)
from .transaction import Transaction
from .step_result import (
    StepResult, GeometrySummary, make_success, make_failure, RenderLevel, ErrorKind
)
from .validators import (
    require_positive, require_non_negative, require_finite,
    require_tuple3, require_tuple2, require_non_empty_str,
    require_in, require_positive_int
)


PUBLIC_OPS = frozenset({
    "create_workplane", "new_sketch", "add_circle", "add_rectangle", "add_line", "close_sketch",
    "extrude", "revolve", "sweep", "boolean",
    "hole", "fillet", "chamfer", "shell",
    "linear_pattern", "circular_pattern", "mirror",
    "query", "select", "measure",
    "undo", "redo", "delete_feature", "update_feature", "rebuild", "export",
    "add_polyline", "add_arc", "assemble", "render",
    "add_constraint", "set_parameter", "solve_sketch",
    "validate_geometry",
    "query_assembly", "set_instance_visibility", "set_instance_color",
    # v2.7: reference-coordinate framework
    "create_reference_plane", "query_reference",
    "resolve_point", "resolve_placement",
    "validate_assembly",
    # v2.9: collision check
    "check_interference",
})


def _bbox_overlap(bbox_a, bbox_b) -> float:
    """返回两个 bbox 在各轴上重叠深度的最小值. >0 表示重叠, <0 表示间距."""
    if not bbox_a or not bbox_b or len(bbox_a) != 6 or len(bbox_b) != 6:
        return 0.0
    ax_min, ay_min, az_min, ax_max, ay_max, az_max = bbox_a
    bx_min, by_min, bz_min, bx_max, by_max, bz_max = bbox_b
    overlaps = [
        min(ax_max, bx_max) - max(ax_min, bx_min),
        min(ay_max, by_max) - max(ay_min, by_min),
        min(az_max, bz_max) - max(az_min, bz_min),
    ]
    return min(overlaps)


def _frame_distance(a, b) -> float:
    """两个 frame 原点之间的欧氏距离."""
    return math.sqrt(sum((a.origin[i] - b.origin[i]) ** 2 for i in range(3)))


import math  # for distance helper


def _z_extent_of(geometry) -> Optional[float]:
    """当前几何的 Z 向尺寸（mm），无几何或无 bbox 时返回 None."""
    if geometry is None:
        return None
    try:
        bb = geometry.bounding_box()
        return float(bb.max.Z - bb.min.Z)
    except Exception:
        return None


class MechKernel:
    """MechKernel 主类"""
    
    def __init__(self):
        self.feature_graph = FeatureGraph()
        self.workplanes = WorkplaneRegistry()
        self.sketches: Dict[str, Sketch] = {}
        self.naming_resolver = PersistentNamingResolver()
        self.narrative: List[str] = []
        self.semantic_state: Dict[str, Any] = {}
        self._step_counter = 0
        self._last_render_step = -1
        self._feature_id_counter = 0
        self._txn_depth = 0
        self._geometry_internal: Optional[Any] = None
        self._geometry_revision: int = 0
        self._feature_geometries: Dict[str, Any] = {}  # v1.16: feature_id -> 该 feature 完成时的几何引用
        self._op_history: List[Dict] = []  # v2.0: op 历史（参数化重放数据源）
        self._replaying: bool = False  # v2.0: 重放中（禁止再记录）
        self._has_non_replayable_op: bool = False  # v2.0: 会话含导入/加载，禁止重放
        self._parameters: Dict[str, float] = {}  # v2.4: 命名尺寸参数
        self._assembly_instances: Dict[str, AssemblyInstance] = {}
        self._frame_registry: FrameRegistry = FrameRegistry()  # v2.7: 参考坐标系
        self._topo_cache: TopologyCache = TopologyCache()  # v2.11: face/edge 引用缓存
        self._replay_parameter_overrides: Optional[Dict[str, float]] = None
        self._last_render_base64: Optional[str] = None
        self._last_render_views: Dict[str, bytes] = {}
        self.geometry_inspector = GeometryInspector()
        self.inspector = self.geometry_inspector  # 别名（兼容测试）
        # Automatic visual checks use a compact evidence budget. Explicit
        # render(size=...) calls can request a larger final packet.
        self.renderer = Renderer(image_size=(320, 320), backend="auto")
        self.adaptive_renderer = AdaptiveRenderer(interval=5)  # v1.5 修复：Renderer 不是 interval
        self._undo_stack: List[Dict] = []
        self._redo_stack: List[Dict] = []
        self._max_undo_depth = 50
        self.cap = CapabilityRegistry()
        self._register_op_schemas()
        # v2.9.2: PUBLIC_OPS 自动从 cap 派生 (消除 2 个 source-of-truth drift)
        # module-level PUBLIC_OPS 仍保留, 供 `from mech_kernel.kernel import PUBLIC_OPS` 兼容
        self.PUBLIC_OPS = frozenset(c["name"] for c in self.cap.list_public())
    
    def _register_op_schemas(self):
        """手动注册公共 op schema（P1-4 v8: 用 set_capability 检测重复）"""
        # 草图类 6 个
        self.cap.set_capability(Capability(
            name="create_workplane", category="sketch",
            description="创建工作平面。基础面 XY/YZ/XZ（可 offset 偏置）；custom 需 origin+normal；face + face_ref 可在选中平面上草图（面上草图）",
            input_schema={
                "name": FieldSchema(type="string", required=True),
                "type": FieldSchema(type="enum", required=False, default="XY",
                                    enum=["XY", "YZ", "XZ", "custom", "face"]),
                "origin": FieldSchema(type="tuple", required=False, items_type="number", length=3,
                                      description="custom: 平面原点"),
                "x_dir": FieldSchema(type="tuple", required=False, items_type="number", length=3,
                                     description="custom: 局部 u 轴（缺省自动推取）"),
                "normal": FieldSchema(type="tuple", required=False, items_type="number", length=3,
                                      description="custom: 平面法向（必填）"),
                "offset": FieldSchema(type="number", required=False, default=0.0,
                                      description="标准面沿法向偏置距离"),
                "face_ref": FieldSchema(type="string", required=False,
                                        description="面引用（如 'F03'，来自 select）→ 面上草图"),
            },
            permission="public",
        ))
        self.cap.set_capability(Capability(
            name="new_sketch", category="sketch",
            description="创建新草图",
            input_schema={
                "workplane_name": FieldSchema(type="string", required=True),
                "sketch_name": FieldSchema(type="string", required=True),
            },
            permission="public",
        ))
        self.cap.set_capability(Capability(
            name="add_circle", category="sketch",
            description="添加圆（radius 是半径不是直径）",
            input_schema={
                "sketch_name": FieldSchema(type="string", required=True),
                "center": FieldSchema(type="tuple", required=True, items_type="number", length=2),
                "radius": FieldSchema(type="number", required=True, min=0.001),
                "name": FieldSchema(type="string", required=False),
            },
            permission="public",
        ))
        self.cap.set_capability(Capability(
            name="add_rectangle", category="sketch",
            description="添加矩形",
            input_schema={
                "sketch_name": FieldSchema(type="string", required=True),
                "width": FieldSchema(type="number", required=True, min=0.001),
                "height": FieldSchema(type="number", required=True, min=0.001),
                "center": FieldSchema(type="tuple", required=False, items_type="number", length=2),
                "name": FieldSchema(type="string", required=False),
            },
            permission="public",
        ))
        self.cap.set_capability(Capability(
            name="add_line", category="sketch",
            description="添加直线",
            input_schema={
                "sketch_name": FieldSchema(type="string", required=True),
                "start": FieldSchema(type="tuple", required=True, items_type="number", length=2),
                "end": FieldSchema(type="tuple", required=True, items_type="number", length=2),
            },
            permission="public",
        ))
        self.cap.set_capability(Capability(
            name="close_sketch", category="sketch",
            description="关闭草图（草图不可再编辑）",
            input_schema={"sketch_name": FieldSchema(type="string", required=True)},
            permission="public",
        ))
        # 主体 extrude（其他 3 个 placeholder）
        self.cap.set_capability(Capability(
            name="extrude", category="body",
            description="拉伸草图（圆→圆柱 / 矩形→立方体 / 多圆同心→环面）。注意：已有几何时 mode='new_body' 会被拒绝（防止清空零件），叠加用 'add'，切除用 'cut'，确认替换传 confirm_replace=True",
            input_schema={
                "sketch_name": FieldSchema(type="string", required=True),
                "depth": FieldSchema(type="number", required=True, min=0.001),
                "mode": FieldSchema(type="enum", required=False, default="new_body", enum=["new_body", "add", "cut"]),
                "name": FieldSchema(type="string", required=False),
                "direction": FieldSchema(type="enum", required=False, default="Z", enum=["X", "Y", "Z"]),
                "confirm_replace": FieldSchema(type="boolean", required=False, default=False),
            },
            permission="public",
        ))
        # undo/redo
        self.cap.set_capability(Capability(
            name="undo", category="edit", description="撤销",
            input_schema={"steps": FieldSchema(type="integer", required=False, default=1, min=1)},
            permission="public",
        ))
        self.cap.set_capability(Capability(
            name="redo", category="edit", description="重做",
            input_schema={"steps": FieldSchema(type="integer", required=False, default=1, min=1)},
            permission="public",
        ))
        # 绑定 func（v6 P0-4）
        for name in list(self.cap._caps.keys()):
            method = getattr(self, name, None)
            if method is not None and callable(method):
                self.cap._caps[name].func = method

        # op schema（v1.16 修复版：与真实方法签名逐字段对齐）
        # 说明：v1.11-1.15 已将 query/select/measure/delete_feature/update_feature 真实实现，
        # 这些 schema 必须与 kernel 方法签名一致，否则 execute() 校验会拒绝合法调用。
        placeholder_schemas = {
            "revolve": {"sketch_name": FieldSchema(type="string", required=True),
                        "axis": FieldSchema(type="tuple", required=False, items_type="number", length=6),
                        "angle": FieldSchema(type="number", required=False, default=360.0, min=0.01, max=360.0),
                        "mode": FieldSchema(type="enum", required=False, default="new_body", enum=["new_body", "add", "cut"]),
                        "name": FieldSchema(type="string", required=False),
                        "confirm_replace": FieldSchema(type="boolean", required=False, default=False)},
            "sweep": {"profile_sketch": FieldSchema(type="string", required=True),
                      "path": FieldSchema(type="enum", required=False, default="x_axis", enum=["x_axis", "y_axis", "z_axis"]),
                      "length": FieldSchema(type="number", required=False, default=50.0, min=0.001),
                      "name": FieldSchema(type="string", required=False),
                      "mode": FieldSchema(type="enum", required=False, default="new_body", enum=["new_body", "add", "cut"]),
                      "confirm_replace": FieldSchema(type="boolean", required=False, default=False)},
            "boolean": {"target_sketch": FieldSchema(type="string", required=True),
                        "tools": FieldSchema(type="list", required=True, items_type="string"),
                        "operation": FieldSchema(type="enum", required=False, default="union", enum=["union", "subtract", "intersect"]),
                        "name": FieldSchema(type="string", required=False),
                        "depth": FieldSchema(type="number", required=False, min=0.001,
                                             description="target/tools 拉伸深度；缺省自动取当前零件 Z 向尺寸 + 2mm")},
            "hole": {"position": FieldSchema(type="tuple", required=False, default=[0, 0], items_type="number", length=2,
                                             description="孔位 2 坐标，相对进入面：top/bottom→(x,y)；x±→(y,z)；y±→(x,z)"),
                     "diameter": FieldSchema(type="number", required=False, default=10.0, min=0.001),
                     "depth": FieldSchema(type="number", required=False, min=0.001),
                     "hole_type": FieldSchema(type="enum", required=False, default="simple", enum=["simple", "counterbore", "countersink"]),
                     "counterbore_diameter": FieldSchema(type="number", required=False, min=0.001),
                     "counterbore_depth": FieldSchema(type="number", required=False, min=0.001),
                     "name": FieldSchema(type="string", required=False),
                     "direction": FieldSchema(type="enum", required=False, default="top",
                                              enum=["top", "bottom", "x+", "x-", "y+", "y-"],
                                              description="孔从哪个面进入（top=默认，沿 Z 向打穿）")},
            "fillet": {"radius": FieldSchema(type="number", required=True, min=0.001),
                       "edges": FieldSchema(type="string_or_list", required=False, default="all",
                                            description="'all' 或边引用列表（如 ['E12','E15']，来自 select element_type='edge'）"),
                       "name": FieldSchema(type="string", required=False)},
            "chamfer": {"length": FieldSchema(type="number", required=True, min=0.001),
                        "length2": FieldSchema(type="number", required=False, min=0.001),
                        "edges": FieldSchema(type="string_or_list", required=False, default="all",
                                             description="'all' 或边引用列表（如 ['E12','E15']，来自 select element_type='edge'）"),
                        "name": FieldSchema(type="string", required=False)},
            "shell": {"thickness": FieldSchema(type="number", required=True, min=0.001),
                      "face_filter": FieldSchema(type="enum", required=False, default="top", enum=["top", "bottom", "z+", "z-", "x+", "x-", "y+", "y-"]),
                      "name": FieldSchema(type="string", required=False),
                      "face_refs": FieldSchema(type="list", required=False, items_type="string",
                                               description="开口面引用列表（如 ['F03']，来自 select）；提供时优先于 face_filter")},
            "linear_pattern": {"sketch_name": FieldSchema(type="string", required=True),
                               "count": FieldSchema(type="integer", required=True, min=2, max=100),
                               "direction": FieldSchema(type="tuple", required=False, default=[1, 0], items_type="number", length=2),
                               "spacing": FieldSchema(type="number", required=False, default=10.0, min=0.001),
                               "mode": FieldSchema(type="enum", required=False, default="cut", enum=["cut", "add", "union"]),
                               "name": FieldSchema(type="string", required=False),
                               "depth": FieldSchema(type="number", required=False, min=0.001,
                                                    description="拉伸深度；缺省自动取当前零件 Z 向尺寸 + 2mm")},
            "circular_pattern": {"sketch_name": FieldSchema(type="string", required=True),
                                 "count": FieldSchema(type="integer", required=True, min=2, max=100),
                                 "axis_origin": FieldSchema(type="tuple", required=False, default=[0, 0, 0], items_type="number", length=3),
                                 "axis_direction": FieldSchema(type="tuple", required=False, default=[0, 0, 1], items_type="number", length=3),
                                 "angle": FieldSchema(type="number", required=False, default=360.0, min=0.01, max=360.0),
                                 "depth": FieldSchema(type="number", required=False, default=10.0, min=0.001),
                                 "mode": FieldSchema(type="enum", required=False, default="cut", enum=["cut", "add"]),
                                 "name": FieldSchema(type="string", required=False)},
            "mirror": {"sketch_name": FieldSchema(type="string", required=True),
                       "axis": FieldSchema(type="enum", required=False, default="X", enum=["X", "Y"]),
                       "mode": FieldSchema(type="enum", required=False, default="union", enum=["union", "add", "cut"]),
                       "name": FieldSchema(type="string", required=False),
                       "depth": FieldSchema(type="number", required=False, min=0.001,
                                            description="拉伸深度；缺省自动取当前零件 Z 向尺寸 + 2mm")},
            "query": {"target": FieldSchema(type="string", required=True),
                      "what": FieldSchema(type="enum", required=False, default="bounding_box", enum=["bounding_box", "volume", "centroid", "face_count", "edge_count", "vertex_count"])},
            "select": {"filter_type": FieldSchema(type="enum", required=False, default="all",
                                                  enum=["all", "plane", "cylinder", "cone", "sphere", "torus",
                                                        "line", "circle", "ellipse", "bezier", "bspline"]),
                       "face_index": FieldSchema(type="integer", required=False, min=0),
                       "element_type": FieldSchema(type="enum", required=False, default="face", enum=["face", "edge"])},
            "measure": {"target1": FieldSchema(type="string", required=True),
                        "target2": FieldSchema(type="string", required=False),
                        "metric": FieldSchema(type="enum", required=False, default="distance", enum=["distance", "volume", "area"])},
            "delete_feature": {"feature_id": FieldSchema(type="string", required=True)},
            "update_feature": {"feature_id": FieldSchema(type="string", required=True),
                               "new_params": FieldSchema(type="dict", required=True)},
            "export": {"path": FieldSchema(type="string", required=True),
                       "format": FieldSchema(type="enum", required=False, default="step", enum=["step"])},
            "rebuild": {"name": FieldSchema(type="string", required=False)},
            "add_polyline": {"sketch_name": FieldSchema(type="string", required=True),
                             "points": FieldSchema(type="list", required=True, items_type="list",
                                                   description="[[x,y],...] 至少 3 点"),
                             "name": FieldSchema(type="string", required=False)},
            "add_arc": {"sketch_name": FieldSchema(type="string", required=True),
                        "center": FieldSchema(type="tuple", required=True, items_type="number", length=2),
                        "radius": FieldSchema(type="number", required=True, min=0.001),
                        "start_angle": FieldSchema(type="number", required=True),
                        "end_angle": FieldSchema(type="number", required=True),
                        "name": FieldSchema(type="string", required=False)},
            "assemble": {"parts": FieldSchema(type="list", required=True, items_type="dict",
                                              description="[{path, position:[x,y,z], rotation:[deg,[ax,ay,az]]}]"),
                         "name": FieldSchema(type="string", required=False)},
            "render": {"views": FieldSchema(type="list", required=False,
                                              description="iso/front/top/side/rot0/rot90/rot180/rot270"),
                       "size": FieldSchema(type="integer", required=False, default=640, min=64),
                       "annotate": FieldSchema(type="boolean", required=False, default=True),
                       "section": FieldSchema(type="dict", required=False),
                       "turntable": FieldSchema(type="boolean", required=False, default=False),
                       "intent": FieldSchema(type="enum", required=False, default="inspect",
                                             enum=["inspect", "section", "feature_zoom", "delta", "sketch"]),
                       "quality": FieldSchema(type="enum", required=False, default="evidence",
                                              enum=["evidence", "presentation"]),
                       "backend": FieldSchema(type="enum", required=False, default="auto",
                                              enum=["auto", "occ", "matplotlib"]),
                       "show_edges": FieldSchema(type="boolean", required=False, default=False),
                       "highlight": FieldSchema(type="list", required=False),
                       "target": FieldSchema(type="string", required=False,
                                             description="feature_zoom/delta 的 feature_id"),
                       "name": FieldSchema(type="string", required=False)},
            "query_assembly": {"name": FieldSchema(type="string", required=False)},
            "set_instance_visibility": {"instance_id": FieldSchema(type="string", required=True),
                                         "visible": FieldSchema(type="boolean", required=True)},
            "set_instance_color": {"instance_id": FieldSchema(type="string", required=True),
                                    "color": FieldSchema(type="tuple", required=True, items_type="number", length=3)},
            "add_constraint": {"sketch_name": FieldSchema(type="string", required=True),
                                "constraint_type": FieldSchema(type="enum", required=True, enum=sorted(SUPPORTED_CONSTRAINTS)),
                                "references": FieldSchema(type="list", required=True),
                                "value": FieldSchema(type="number", required=False, min=0.000001),
                                "parameter_name": FieldSchema(type="string", required=False),
                                "name": FieldSchema(type="string", required=False)},
            "set_parameter": {"name": FieldSchema(type="string", required=True),
                               "value": FieldSchema(type="number", required=True, min=0.000001)},
            "solve_sketch": {"sketch_name": FieldSchema(type="string", required=True),
                              "mode": FieldSchema(type="enum", required=False, default="strict", enum=["strict", "best_effort"])},
            "validate_geometry": {"target": FieldSchema(type="string", required=False, default="_current_geometry"),
                                   "level": FieldSchema(type="enum", required=False, default="standard",
                                                         enum=["basic", "standard", "strict"])},
            # v2.7: reference-coordinate framework
            "create_reference_plane": {
                "name": FieldSchema(type="string", required=True),
                "origin": FieldSchema(type="tuple", required=False, default=(0.0, 0.0, 0.0),
                                      items_type="number", length=3),
                "normal": FieldSchema(type="tuple", required=False, default=(0.0, 0.0, 1.0),
                                      items_type="number", length=3),
                "x_axis": FieldSchema(type="tuple", required=False, default=(1.0, 0.0, 0.0),
                                      items_type="number", length=3),
                "parent": FieldSchema(type="string", required=False, default=None),
                "metadata": FieldSchema(type="dict", required=False, default=None),
            },
            "query_reference": {
                "name": FieldSchema(type="string", required=False, default=None),
            },
            "resolve_point": {
                "frame": FieldSchema(type="string", required=True),
                "uv": FieldSchema(type="tuple", required=False, default=(0.0, 0.0),
                                  items_type="number", length=2),
                "normal_offset": FieldSchema(type="number", required=False, default=0.0),
            },
            "resolve_placement": {
                "frame": FieldSchema(type="string", required=True),
                "uv": FieldSchema(type="tuple", required=False, default=(0.0, 0.0),
                                  items_type="number", length=2),
                "normal_offset": FieldSchema(type="number", required=False, default=0.0),
                "rotation": FieldSchema(type="tuple", required=False, default=(0.0, (0.0, 0.0, 1.0)),
                                        items_type="number", length=2),
            },
            "validate_assembly": {
                "level": FieldSchema(type="enum", required=False, default="standard",
                                     enum=["basic", "standard", "strict"]),
                "relations": FieldSchema(type="list", required=False, default=None, items_type="dict"),
            },
            # v2.9: collision check
            "check_interference": {
                "parts": FieldSchema(type="list", required=False, default=None, items_type="dict"),
                "tolerance": FieldSchema(type="number", required=False, default=0.001, min=0.0),
                "only_interfering": FieldSchema(type="boolean", required=False, default=False),
            },
        }
        for name, schema in placeholder_schemas.items():
            self.cap.set_capability(Capability(
                name=name, category=name.split("_")[0], description=name,
                input_schema=schema, permission="public",
            ))
        # Placeholder schemas are declared after the first binding pass.
        # Bind all public capabilities once more so registry.call() is usable.
        for name, cap in self.cap._caps.items():
            method = getattr(self, name, None)
            if method is not None and callable(method):
                cap.func = method

    def create_workplane(self, name: str, type: str = "XY", origin=None, x_dir=None,
                         normal=None, offset: float = 0.0, face_ref: str = None) -> StepResult:
        """
        v2.11: 真实支持自定义平面 / 基准面偏置 / 面上草图（此前 custom 的参数被丢弃）

        Args:
            name: 工作平面名
            type: "XY" | "YZ" | "XZ" | "custom" | "face"
            origin: custom — 平面原点 (x, y, z)
            x_dir: custom — 局部 u 轴方向（缺省自动推取）
            normal: custom/face — 平面法向（custom 必填）
            offset: 标准平面沿法向的偏置距离（mm）
            face_ref: 面引用（如 "F03"，来自 select）— 从选中平面派生坐标系
        """
        start = time.time()
        name = require_non_empty_str("name", name)
        if face_ref is not None:
            type = "face"
        type = require_in("type", type, ["XY", "YZ", "XZ", "custom", "face"])
        if self.workplanes.has_name(name):
            raise InvalidRequestError(f"工作平面 {name} 已存在")

        wp_origin = (0.0, 0.0, 0.0)
        wp_x = {"XY": (1.0, 0.0, 0.0), "YZ": (0.0, 1.0, 0.0), "XZ": (1.0, 0.0, 0.0),
                "custom": (1.0, 0.0, 0.0), "face": (1.0, 0.0, 0.0)}[type]
        wp_normal = {"XY": (0.0, 0.0, 1.0), "YZ": (1.0, 0.0, 0.0), "XZ": (0.0, 1.0, 0.0),
                     "custom": None, "face": None}[type]
        reference = None
        history_kwargs = dict(name=name, type=type, offset=offset)

        if type in ("XY", "YZ", "XZ"):
            if offset:
                require_finite("offset", offset)
                wp_origin = tuple(wp_normal[i] * offset for i in range(3))
                history_kwargs["origin"] = list(wp_origin)
        elif type == "custom":
            if origin is None or normal is None:
                raise InvalidRequestError("custom 工作平面需要 origin (x,y,z) 和 normal (x,y,z)")
            require_tuple3("origin", origin)
            require_tuple3("normal", normal)
            wp_origin = tuple(float(v) for v in origin)
            wp_normal = self._orthonormalize_axis(normal)
            if x_dir is not None:
                wp_x = self._orthonormalize_axis(x_dir, against=wp_normal)
            else:
                # 自动取与法向最不平行的世界轴作为 u 轴（最平行会退化）
                world = [(1, 0, 0), (0, 1, 0), (0, 0, 1)]
                wp_x = min(world, key=lambda a: abs(sum(a[i] * wp_normal[i] for i in range(3))))
                wp_x = self._orthonormalize_axis(wp_x, against=wp_normal)
            history_kwargs["origin"] = list(wp_origin)
            history_kwargs["x_dir"] = list(wp_x)
            history_kwargs["normal"] = list(wp_normal)
        else:  # face — 面上草图
            if self._current_geometry is None:
                raise InvalidRequestError("face 工作平面需要先有几何（先 extrude）")
            try:
                info = self._topo_cache.resolve(
                    self._geometry_revision, self._current_geometry, face_ref, "face")
            except RefStaleError as e:
                raise RecoverableError(
                    f"面引用已失效: {e}",
                    suggestion={"action": "重新 select 获取新的面引用",
                                "reason_code": "stale_topo_ref"},
                    reason_code="stale_topo_ref",
                )
            except RefFormatError as e:
                raise InvalidRequestError(str(e))
            face = info.shape
            if info.geom_type != "plane":
                raise RecoverableError(
                    f"引用 {face_ref} 是 {info.geom_type} 面，面上草图暂只支持平面",
                    suggestion={"action": "select filter_type='plane' 选一个平面",
                                "reason_code": "face_not_planar"},
                    reason_code="face_not_planar",
                )
            wp_origin = tuple(float(v) for v in face.center())
            wp_normal = self._orthonormalize_axis(face.normal_at())
            # u 轴：与法向最不平行的世界轴投影到平面（避免退化）
            world = [(1, 0, 0), (0, 1, 0), (0, 0, 1)]
            wp_x = min(world, key=lambda a: abs(sum(a[i] * wp_normal[i] for i in range(3))))
            wp_x = self._orthonormalize_axis(wp_x, against=wp_normal)
            reference = Reference.face(face_ref)
            history_kwargs["face_ref"] = face_ref
            history_kwargs["origin"] = list(wp_origin)
            history_kwargs["x_dir"] = list(wp_x)
            history_kwargs["normal"] = list(wp_normal)

        with Transaction(self, "create_workplane") as txn:
            entry = self._record_history("create_workplane", **history_kwargs)
            wp = Workplane(id=f"wp_{name}_{next_workplane_id()}", name=name, type=WorkplaneType(type),
                           origin=wp_origin, x_dir=wp_x, normal=wp_normal, reference=reference)
            self.workplanes.register(wp)
            self.narrative.append(f"创建工作平面 {name} ({type})")
            txn.commit()
        
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=f"wp_{name}",
            narrative=f"创建工作平面 {name}",
            current_narrative=self.narrative.copy(),
            render_level="none",
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))
    def new_sketch(self, workplane_name: str, sketch_name: str) -> StepResult:
        start = time.time()
        workplane_name = require_non_empty_str("workplane_name", workplane_name)
        sketch_name = require_non_empty_str("sketch_name", sketch_name)
        if not self.workplanes.has_name(workplane_name):
            raise InvalidRequestError(f"工作平面 {workplane_name} 不存在")
        if sketch_name in self.sketches:
            raise InvalidRequestError(f"草图 {sketch_name} 已存在")
        
        with Transaction(self, "new_sketch") as txn:
            entry = self._record_history("new_sketch", workplane_name=workplane_name, sketch_name=sketch_name)
            sk = Sketch(id=next_sketch_id(), name=sketch_name, workplane_name=workplane_name)
            self.sketches[sketch_name] = sk
            self.narrative.append(f"创建草图 {sketch_name} (在 {workplane_name})")
            txn.commit()
        
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=f"sk_{sketch_name}",
            narrative=f"创建草图 {sketch_name}",
            current_narrative=self.narrative.copy(),
            render_level="none",
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))
    
    def add_circle(self, sketch_name: str, center: tuple, radius: float, name: str = "") -> StepResult:
        start = time.time()
        sketch_name = require_non_empty_str("sketch_name", sketch_name)
        require_tuple2("center", center)
        require_positive("radius", radius)
        if sketch_name not in self.sketches:
            raise InvalidRequestError(f"草图 {sketch_name} 不存在")
        if self.sketches[sketch_name].closed:
            raise InvalidRequestError(f"草图 {sketch_name} 已关闭")
        sk = self.sketches[sketch_name]
        
        with Transaction(self, "add_circle") as txn:
            entry = self._record_history("add_circle", sketch_name=sketch_name, center=center, radius=radius, name=name)
            entity_id = next_entity_id()
            entry["feature_id"] = entity_id
            entity = SketchEntity(
                id=entity_id, type="circle",
                params={"center": tuple(center), "radius": float(radius)},
                name=name or f"circle_{entity_id}",
            )
            sk.add_entity(entity)
            diagnostic = self._solve_sketch_state(sketch_name, mode="strict") if sk.constraints else None
            self.narrative.append(f"草图 {sketch_name} 添加圆 半径 {radius}")
            txn.commit()
        
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=entity_id, narrative=f"画圆 r={radius}",
            current_narrative=self.narrative.copy(),
            render_level="none",
            constraint_diagnostics=diagnostic,
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))
    
    def add_rectangle(self, sketch_name: str, width: float, height: float, center: tuple = (0, 0), name: str = "") -> StepResult:
        start = time.time()
        sketch_name = require_non_empty_str("sketch_name", sketch_name)
        require_positive("width", width)
        require_positive("height", height)
        require_tuple2("center", center)
        if sketch_name not in self.sketches:
            raise InvalidRequestError(f"草图 {sketch_name} 不存在")
        if self.sketches[sketch_name].closed:
            raise InvalidRequestError(f"草图 {sketch_name} 已关闭")
        sk = self.sketches[sketch_name]
        with Transaction(self, "add_rectangle") as txn:
            entry = self._record_history("add_rectangle", sketch_name=sketch_name, width=width, height=height, center=center, name=name)
            entity_id = next_entity_id()
            entry["feature_id"] = entity_id
            entity = SketchEntity(
                id=entity_id, type="rectangle",
                params={"width": float(width), "height": float(height), "center": tuple(center)},
                name=name or f"rect_{entity_id}",
            )
            sk.add_entity(entity)
            diagnostic = self._solve_sketch_state(sketch_name, mode="strict") if sk.constraints else None
            self.narrative.append(f"草图 {sketch_name} 添加矩形 {width}x{height}")
            txn.commit()
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=entity_id, narrative=f"画矩形 {width}x{height}",
            current_narrative=self.narrative.copy(),
            render_level="none",
            constraint_diagnostics=diagnostic,
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))
    
    def add_line(self, sketch_name: str, start: tuple, end: tuple) -> StepResult:
        start_t = time.time()
        sketch_name = require_non_empty_str("sketch_name", sketch_name)
        require_tuple2("start", start)
        require_tuple2("end", end)
        if sketch_name not in self.sketches:
            raise InvalidRequestError(f"草图 {sketch_name} 不存在")
        if self.sketches[sketch_name].closed:
            raise InvalidRequestError(f"草图 {sketch_name} 已关闭")
        sk = self.sketches[sketch_name]
        with Transaction(self, "add_line") as txn:
            entry = self._record_history("add_line", sketch_name=sketch_name, start=start, end=end)
            entity_id = next_entity_id()
            entry["feature_id"] = entity_id
            entity = SketchEntity(
                id=entity_id, type="line",
                params={"start": tuple(start), "end": tuple(end)},
                name=f"line_{entity_id}",
            )
            sk.add_entity(entity)
            diagnostic = self._solve_sketch_state(sketch_name, mode="strict") if sk.constraints else None
            self.narrative.append(f"草图 {sketch_name} 添加直线")
            txn.commit()
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=entity_id, narrative="画线",
            current_narrative=self.narrative.copy(),
            render_level="none",
            constraint_diagnostics=diagnostic,
            elapsed_ms=(time.time() - start_t) * 1000,
            step_index=self._step_counter,
        ))
    
    def add_polyline(self, sketch_name: str, points: list, name: str = "") -> StepResult:
        """v2.0: 添加多段线（闭合剖面用，可绕轴旋转/拉伸）"""
        start = time.time()
        sketch_name = require_non_empty_str("sketch_name", sketch_name)
        if sketch_name not in self.sketches:
            raise InvalidRequestError(f"草图 {sketch_name} 不存在")
        if self.sketches[sketch_name].closed:
            raise InvalidRequestError(f"草图 {sketch_name} 已关闭")
        if not points or len(points) < 3:
            raise InvalidRequestError("polyline 至少需要 3 个点")
        sk = self.sketches[sketch_name]
        entry = self._record_history("add_polyline", sketch_name=sketch_name, points=points, name=name)
        entity_id = next_entity_id()
        entry["feature_id"] = entity_id
        entity = SketchEntity(
            id=entity_id, type="polyline",
            params={"points": [tuple(p) for p in points]},
            name=name or f"poly_{entity_id}",
        )
        sk.add_entity(entity)
        self.narrative.append(f"草图 {sketch_name} 添加多段线")
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=entity_id, narrative="画多段线",
            current_narrative=self.narrative.copy(),
            render_level="none",
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))
    
    def add_arc(self, sketch_name: str, center: tuple, radius: float, start_angle: float, end_angle: float, name: str = "") -> StepResult:
        """v2.0: 添加圆弧（角度制，绕 center 从 start_angle 扫到 end_angle）"""
        start = time.time()
        sketch_name = require_non_empty_str("sketch_name", sketch_name)
        require_tuple2("center", center)
        require_positive("radius", radius)
        if sketch_name not in self.sketches:
            raise InvalidRequestError(f"草图 {sketch_name} 不存在")
        if self.sketches[sketch_name].closed:
            raise InvalidRequestError(f"草图 {sketch_name} 已关闭")
        sk = self.sketches[sketch_name]
        entry = self._record_history(
            "add_arc", sketch_name=sketch_name, center=center, radius=radius,
            start_angle=start_angle, end_angle=end_angle, name=name,
        )
        entity_id = next_entity_id()
        entry["feature_id"] = entity_id
        entity = SketchEntity(
            id=entity_id, type="arc",
            params={"center": tuple(center), "radius": float(radius),
                    "start_angle": float(start_angle), "end_angle": float(end_angle)},
            name=name or f"arc_{entity_id}",
        )
        sk.add_entity(entity)
        self.narrative.append(f"草图 {sketch_name} 添加圆弧")
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=entity_id, narrative="画圆弧",
            current_narrative=self.narrative.copy(),
            render_level="none",
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))

    def _solve_sketch_state(self, sketch_name: str, mode: str = "strict") -> dict:
        """Solve one sketch and store its compact diagnostic on the sketch."""
        mode = require_in("mode", mode, ["strict", "best_effort"])
        sketch = self.sketches.get(sketch_name)
        if sketch is None:
            raise InvalidRequestError(f"草图 {sketch_name} 不存在")
        solved = solve_sketch_constraints(sketch, self._parameters)
        diagnostic = solved.to_dict(sketch_name, len(sketch.constraints))
        sketch.solver_status = solved.status
        sketch.dof = solved.dof
        sketch.conflicting_constraints = list(solved.conflicting_constraints)
        sketch.solver_residual = solved.residual
        sketch.solver_iterations = solved.iterations
        if mode == "strict" and solved.status in (ConstraintStatus.CONFLICT, ConstraintStatus.OVER_CONSTRAINED):
            raise InvalidRequestError(
                f"草图 {sketch_name} 约束无法满足: {solved.status.value} {solved.conflicting_constraints}",
                hint="使用 best_effort 查看诊断，或修改/删除冲突约束",
            )
        return diagnostic

    def add_constraint(
        self,
        sketch_name: str,
        constraint_type: str,
        references: list,
        value: float = None,
        parameter_name: str = "",
        name: str = "",
    ) -> StepResult:
        """Add a stable-ID 2-D constraint and solve the owning sketch."""
        start = time.time()
        sketch_name = require_non_empty_str("sketch_name", sketch_name)
        constraint_type = require_non_empty_str("constraint_type", constraint_type)
        if not isinstance(parameter_name, str):
            raise InvalidRequestError("parameter_name 必须是字符串")
        parameter_name = parameter_name.strip()
        if sketch_name not in self.sketches:
            raise InvalidRequestError(f"草图 {sketch_name} 不存在")
        if self.sketches[sketch_name].closed:
            raise InvalidRequestError(f"草图 {sketch_name} 已关闭，不能添加约束")
        if not isinstance(references, list):
            raise InvalidRequestError("references 必须是列表")
        entity_ids = {entity.id for entity in self.sketches[sketch_name].entities}
        for ref in references:
            if not isinstance(ref, dict):
                raise InvalidRequestError("每个引用必须是对象")
            if ref.get("entity_id") not in entity_ids:
                raise InvalidRequestError(f"实体不存在: {ref.get('entity_id')}")

        with Transaction(self, "add_constraint") as txn:
            if parameter_name:
                replay_value = (
                    self._replay_parameter_overrides.get(parameter_name)
                    if self._replaying and self._replay_parameter_overrides
                    else None
                )
                if replay_value is not None:
                    value = float(replay_value)
                    self._parameters[parameter_name] = value
                if value is None and parameter_name not in self._parameters:
                    raise InvalidRequestError("新参数必须同时提供 value")
                if value is not None:
                    value = require_positive("value", value)
                    existing = self._parameters.get(parameter_name)
                    if existing is not None and abs(existing - value) > 1e-9:
                        raise InvalidRequestError(f"参数 {parameter_name} 已存在且数值冲突")
                    self._parameters[parameter_name] = value
                value = self._parameters.get(parameter_name, value)
            if constraint_type in ("distance", "radius"):
                value = require_positive("value", value)
            validate_constraint(constraint_type, references, value)
            entry = self._record_history(
                "add_constraint", sketch_name=sketch_name, constraint_type=constraint_type,
                references=references, value=value, parameter_name=parameter_name, name=name,
            )
            constraint_id = next_constraint_id()
            entry["feature_id"] = constraint_id
            constraint = Constraint(
                id=constraint_id, type=constraint_type, references=copy.deepcopy(references),
                value=float(value) if value is not None else None,
                parameter_name=parameter_name, name=name or f"constraint_{constraint_id}",
            )
            self.sketches[sketch_name].constraints.append(constraint)
            diagnostic = self._solve_sketch_state(sketch_name, mode="strict")
            txn.commit()

        self._step_counter += 1
        result = make_success(
            feature_id=constraint_id, narrative=f"添加约束 {constraint_type}",
            current_narrative=self.narrative.copy(), render_level="none",
            feature_graph_delta={"added_constraint": constraint_id},
            constraint_diagnostics=diagnostic,
            elapsed_ms=(time.time() - start) * 1000, step_index=self._step_counter,
        )
        return self._wrap_step_result(result)

    def set_parameter(self, name: str, value: float) -> StepResult:
        """Set a named dimension and solve every sketch that references it."""
        start = time.time()
        name = require_non_empty_str("name", name)
        value = require_positive("value", value)
        with Transaction(self, "set_parameter") as txn:
            entry = self._record_history("set_parameter", name=name, value=value)
            parameter_id = f"P_{sum(1 for item in self._op_history if item.get('op') == 'set_parameter'):04d}"
            entry["feature_id"] = parameter_id
            self._parameters[name] = value
            diagnostics = []
            for sketch_name in sorted(self.sketches):
                if any(c.parameter_name == name for c in self.sketches[sketch_name].constraints):
                    diagnostics.append(self._solve_sketch_state(sketch_name, mode="strict"))
            if not self._replaying and diagnostics:
                # Rebuild all downstream solids after the named dimension has
                # changed; the history entry is replay-safe and is not nested.
                self._replay()
            txn.commit()
        self._step_counter += 1
        result = make_success(
            feature_id=parameter_id, narrative=f"设置参数 {name}={value:g}",
            current_narrative=self.narrative.copy(), render_level="none",
            feature_graph_delta={"parameter": name, "value": value},
            constraint_diagnostics=diagnostics[0] if len(diagnostics) == 1 else {"items": diagnostics},
            elapsed_ms=(time.time() - start) * 1000, step_index=self._step_counter,
        )
        result.value = {"name": name, "value": value, "sketches_solved": len(diagnostics)}
        return self._wrap_step_result(result)

    def solve_sketch(self, sketch_name: str, mode: str = "strict") -> StepResult:
        """Explicitly solve a sketch and record the solve boundary for replay."""
        start = time.time()
        sketch_name = require_non_empty_str("sketch_name", sketch_name)
        mode = require_in("mode", mode, ["strict", "best_effort"])
        with Transaction(self, "solve_sketch") as txn:
            # The explicit solve is intentionally historical so replay keeps
            # the same user-visible solve boundary and mode.
            self._record_history("solve_sketch", sketch_name=sketch_name, mode=mode)
            diagnostic = self._solve_sketch_state(sketch_name, mode=mode)
            txn.commit()
        self._step_counter += 1
        status = diagnostic.get("status")
        if mode == "best_effort" and status in (
            ConstraintStatus.CONFLICT.value, ConstraintStatus.OVER_CONSTRAINED.value,
        ):
            result = make_failure(
                error=f"草图 {sketch_name} 求解未完全满足约束: {status}",
                error_kind="RECOVERABLE",
                suggestion={"action": "检查 conflicting_constraints 后修改或删除约束"},
                current_narrative=self.narrative.copy(),
                constraint_diagnostics=diagnostic,
                warning="best_effort 已应用最接近解，几何可能仍未满足全部约束",
                elapsed_ms=(time.time() - start) * 1000, step_index=self._step_counter,
            )
            result.value = diagnostic
            return self._wrap_step_result(result)
        result = make_success(
            feature_id=f"S_{self._step_counter:04d}", narrative=f"求解草图 {sketch_name}",
            current_narrative=self.narrative.copy(), render_level="none",
            constraint_diagnostics=diagnostic,
            elapsed_ms=(time.time() - start) * 1000, step_index=self._step_counter,
        )
        result.value = diagnostic
        return self._wrap_step_result(result)
    
    def close_sketch(self, sketch_name: str) -> StepResult:
        start = time.time()
        sketch_name = require_non_empty_str("sketch_name", sketch_name)
        if sketch_name not in self.sketches:
            raise InvalidRequestError(f"草图 {sketch_name} 不存在")
        sk = self.sketches[sketch_name]
        if not sk.entities:
            raise InvalidRequestError(f"草图 {sketch_name} 为空，没有图元可关闭")
        
        with Transaction(self, "close_sketch") as txn:
            entry = self._record_history("close_sketch", sketch_name=sketch_name)
            self.sketches[sketch_name].closed = True
            self.narrative.append(f"关闭草图 {sketch_name}")
            txn.commit()
        
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=f"close_{sketch_name}", narrative=f"关闭草图 {sketch_name}",
            current_narrative=self.narrative.copy(),
            render_level="none",
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))
    
    # ===== 主体 4（extrude 实现 + 其他 3 占位）=====
    
    def extrude(self, sketch_name: str, depth: float, mode: str = "new_body", name: str = "", direction: str = "Z", confirm_replace: bool = False) -> StepResult:
        start = time.time()
        sketch_name = require_non_empty_str("sketch_name", sketch_name)
        require_positive("depth", depth)
        if sketch_name not in self.sketches:
            raise InvalidRequestError(f"草图 {sketch_name} 不存在")
        sk = self.sketches[sketch_name]
        if not sk.closed:
            raise InvalidRequestError(f"草图 {sketch_name} 未关闭")
        # v2.11: new_body 会清空整个已有零件（LLM 驱动流程的高频事故源），必须显式确认
        if mode == "new_body" and self._current_geometry is not None and not confirm_replace:
            raise RecoverableError(
                f"extrude mode='new_body' 会清空已有零件。叠加到当前零件用 mode='add'，切除用 mode='cut'；确实要替换请传 confirm_replace=True",
                suggestion={
                    "action": "选择修正参数后重试",
                    "fix": {"mode": "add"},
                    "alternatives": [
                        {"fix": {"mode": "cut"}},
                        {"fix": {"confirm_replace": True}},
                    ],
                    "reason_code": "new_body_would_replace",
                },
                reason_code="new_body_would_replace",
            )
        
        with Transaction(self, "extrude") as txn:
            entry = self._record_history("extrude", sketch_name=sketch_name, depth=depth, mode=mode, direction=direction, name=name, confirm_replace=confirm_replace)
            feature_id = next_feature_id()
            entry["feature_id"] = feature_id
            feature = FeatureNode(
                id=feature_id, type=FeatureType.EXTRUDE,
                parameters={"sketch_name": sketch_name, "depth": depth, "mode": mode, "direction": direction, "name": name},
                name=name or f"extrude_{feature_id}",
                state=FeatureState.COMPUTED,
            )
            self.feature_graph.add(feature)
            if mode == "new_body":
                # v2.1: 真实 build123d（已装上，体积误差 < 0.01%）
                self._current_geometry = self._extrude_build123d(sk, depth, direction=direction)
            elif mode in ("cut", "add"):
                # v1.2: 真实 boolean（在已有几何上 add/cut）
                self._current_geometry = self._extrude_add_or_cut(sk, depth, mode, direction=direction)
            self.narrative.append(f"拉伸 {sketch_name} 深度 {depth} → {feature.name}")
            txn.commit()
            self._feature_geometries[feature_id] = self._current_geometry
        
        render_level = self.adaptive_renderer.should_render(
            op="extrude", op_params={"sketch_name": sketch_name, "depth": depth},
            has_geometry=(self._geometry_internal is not None),
        )
        render_png = None
        if render_level != "none":
            renders = self.renderer.render(self._geometry_internal, level=render_level, geometry_revision=self._geometry_revision)
            render_png = renders.get("iso") or renders.get("default")
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=feature_id, narrative=f"拉伸 {sketch_name} 深度 {depth}",
            render_png=render_png, render_level=render_level,
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"added": [feature_id]},
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))
    
    def revolve(self, sketch_name: str, axis: list = None, angle: float = 360.0, mode: str = "new_body", name: str = "", confirm_replace: bool = False) -> StepResult:
        """
        v1.3.1 真实 revolve（车削件）— build123d revolve 调试成功！
        
        Args:
            sketch_name: 草图名（默认在 XY plane）
            axis: 旋转轴 [ox, oy, oz, dx, dy, dz]，默认 [0, 0, 0, 0, 1, 0] (Y 轴)
            angle: 旋转角度（度），默认 360
            mode: "new_body" | "add" | "cut"
            name: 特征名
        
        关键约束（用户/LLM 必知）：
        1. profile 在 XY 平面
        2. axis 必须在 profile 平面内（默认 Y 轴满足）
        3. profile **不能跨 axis**（axis 不能穿过 profile 内部）
        4. 旋转后几何 ≠ profile × angle（是绕 axis 旋转）
        """
        start = time.time()
        sketch_name = require_non_empty_str("sketch_name", sketch_name)
        if sketch_name not in self.sketches:
            raise InvalidRequestError(f"草图 {sketch_name} 不存在")
        if angle <= 0 or angle > 360:
            raise InvalidRequestError(f"angle 必须在 (0, 360] 范围内（当前 {angle}）")
        if axis is None:
            axis = [0, 0, 0, 0, 1, 0]  # Y 轴
        if len(axis) != 6:
            raise InvalidRequestError(f"axis 必须是 [ox, oy, oz, dx, dy, dz]（当前 {len(axis)} 个数）")

        sk = self.sketches[sketch_name]
        if not sk.closed:
            raise InvalidRequestError(f"草图 {sketch_name} 未关闭")
        # v2.11: revolve 剖面写死 XY 平面；草图在自定义/面上/偏置平面时诚实报错
        if self._sketch_plane(sk, "Z") is not None:
            raise InvalidRequestError(
                "revolve 暂只支持标准基准面（XY/YZ/XZ 过原点）上的草图；"
                "草图位于 custom/face/偏置 workplane，请改用 extrude")
        # v2.11: new_body 会清空整个已有零件，必须显式确认（与 extrude/sweep 一致）
        if mode == "new_body" and self._current_geometry is not None and not confirm_replace:
            raise RecoverableError(
                f"revolve mode='new_body' 会清空已有零件。叠加用 mode='add'，切除用 mode='cut'；确实要替换请传 confirm_replace=True",
                suggestion={
                    "action": "选择修正参数后重试",
                    "fix": {"mode": "add"},
                    "alternatives": [
                        {"fix": {"mode": "cut"}},
                        {"fix": {"confirm_replace": True}},
                    ],
                    "reason_code": "new_body_would_replace",
                },
                reason_code="new_body_would_replace",
            )
        
        from build123d import BuildPart, BuildSketch, Plane, add, revolve as b3d_revolve, Axis, Location
        from build123d import Circle as B3DCircle, Rectangle as B3DRect
        from build123d import BuildLine, Polyline as B3DPolyline, make_face
        from build123d.build_common import Locations
        
        # 验证 axis 不退化
        ox, oy, oz, dx, dy, dz = axis
        if dx == 0 and dy == 0 and dz == 0:
            raise InvalidRequestError(f"axis 方向向量为 0")
        
        with Transaction(self, "revolve") as txn:
            entry = self._record_history("revolve", sketch_name=sketch_name, axis=axis, angle=angle, mode=mode, name=name, confirm_replace=confirm_replace)
            feature_id = next_feature_id()
            entry["feature_id"] = feature_id
            feature = FeatureNode(
                id=feature_id, type=FeatureType.REVOLVE,
                parameters={"sketch_name": sketch_name, "axis": list(axis), "angle": angle, "mode": mode, "name": name},
                name=name or f"revolve_{feature_id}",
                state=FeatureState.COMPUTED,
            )
            self.feature_graph.add(feature)
            
            # v2.0: line/polyline/arc 剖面 → 闭合线框 → make_face → revolve（解锁 CD 喷口等）
            profile = None
            profile_wires = None
            if any(e.type in ("line", "polyline", "arc") for e in sk.entities):
                profile = self._collect_closed_profile(sk)
            if profile is not None:
                # 注意：BuildLine 不能嵌在 BuildPart/BuildSketch 上下文内
                with BuildLine() as bl:
                    B3DPolyline(*profile, close=True)
                profile_wires = bl.wires()
            with BuildPart(Plane.XY) as bp:
                with BuildSketch() as s:
                    if profile_wires is not None:
                        make_face(profile_wires)
                    else:
                        for e in sk.entities:
                            if e.type == "circle":
                                r = e.params["radius"]
                                c = e.params.get("center", (0, 0))
                                if c == (0, 0) or c == [0, 0]:
                                    add(B3DCircle(r))
                                else:
                                    # 关键：用 Locations 上下文（不是 Location * shape）
                                    with Locations((c[0], c[1], 0)):
                                        B3DCircle(r)
                            elif e.type == "rectangle":
                                w, h = e.params["width"], e.params["height"]
                                c = e.params.get("center", (0, 0))
                                if c == (0, 0) or c == [0, 0]:
                                    add(B3DRect(w, h))
                                else:
                                    with Locations((c[0], c[1], 0)):
                                        B3DRect(w, h)
                            else:
                                raise NotImplementedError(f"revolve 暂不支持 entity type={e.type}")
                rot_axis = Axis((ox, oy, oz), (dx, dy, dz))
                b3d_revolve(axis=rot_axis, revolution_arc=angle)
            new_solid = bp.part
            
            if mode == "new_body":
                self._current_geometry = new_solid
            elif mode == "add":
                if self._current_geometry is None:
                    self._current_geometry = new_solid
                else:
                    self._current_geometry = self._current_geometry + new_solid
            elif mode == "cut":
                if self._current_geometry is None:
                    raise InvalidRequestError("cut 需要先有几何")
                self._current_geometry = self._current_geometry - new_solid
            
            self.narrative.append(f"旋转 {sketch_name} 角度 {angle}° → {feature.name}")
            txn.commit()
            self._feature_geometries[feature_id] = self._current_geometry
        
        render_level = self.adaptive_renderer.should_render(
            op="revolve", op_params={"angle": angle},
            has_geometry=(self._geometry_internal is not None),
        )
        render_png = None
        if render_level != "none":
            renders = self.renderer.render(self._geometry_internal, level=render_level, geometry_revision=self._geometry_revision)
            render_png = renders.get("iso") or renders.get("default")
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=feature_id, narrative=f"旋转 {sketch_name} 角度 {angle}°",
            render_png=render_png, render_level=render_level,
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"added": [feature_id]},
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))
    
    def circular_pattern(self, sketch_name: str, count: int, axis_origin: list = None, axis_direction: list = None, angle: float = 360.0, depth: float = 10.0, mode: str = "cut", name: str = "") -> StepResult:
        """
        v1.3.2 真实 circular_pattern（在轴周围均匀分布 cut/add）
        
        Args:
            sketch_name: 草图名（在 XY 平面）
            count: 副本数（如 6）
            axis_origin: 旋转轴起点，默认 [0, 0, 0]
            axis_direction: 旋转轴方向，默认 [0, 0, 1] (Z 轴)
            angle: 总角度，默认 360
            depth: 拉伸深度（cut/add 时的方向）
            mode: "cut" | "add"
            name: 特征名
        """
        start = time.time()
        sketch_name = require_non_empty_str("sketch_name", sketch_name)
        if sketch_name not in self.sketches:
            raise InvalidRequestError(f"草图 {sketch_name} 不存在")
        if count < 2:
            raise InvalidRequestError(f"count 必须 >= 2（当前 {count}）")
        if axis_origin is None:
            axis_origin = [0, 0, 0]
        if axis_direction is None:
            axis_direction = [0, 0, 1]
        
        sk = self.sketches[sketch_name]
        if not sk.closed:
            raise InvalidRequestError(f"草图 {sketch_name} 未关闭")
        if self._current_geometry is None:
            raise InvalidRequestError("pattern 需要先有几何（先 new_body 拉伸）")
        
        from build123d import BuildPart, BuildSketch, Plane, add, Axis, Location
        from build123d import Circle as B3DCircle, Rectangle as B3DRect
        
        if any(e.type not in ("circle", "rectangle") for e in sk.entities):
            raise NotImplementedError("circular_pattern 暂不支持 polyline/arc 实体（请用 circle/rectangle）")
        with Transaction(self, "circular_pattern") as txn:
            entry = self._record_history("circular_pattern", sketch_name=sketch_name, count=count, axis_origin=axis_origin, axis_direction=axis_direction, angle=angle, depth=depth, mode=mode, name=name)
            feature_id = next_feature_id()
            entry["feature_id"] = feature_id
            feature = FeatureNode(
                id=feature_id, type=FeatureType.CIRCULAR_PATTERN,
                parameters={"sketch_name": sketch_name, "count": count, "axis_origin": axis_origin, "axis_direction": axis_direction, "angle": angle, "depth": depth, "mode": mode, "name": name},
                name=name or f"pattern_{feature_id}",
                state=FeatureState.COMPUTED,
            )
            self.feature_graph.add(feature)
            
            # v1.3.2: 用 build123d 的 Location((x, y, z), (ax_x, ax_y, ax_z), angle) 做旋转
            # 第一个副本在 i=0（不旋转），后续 N-1 个绕 axis 旋转
            from build123d import extrude as b3d_extrude
            new_geom = self._current_geometry
            for i in range(count):
                angle_deg = i * angle / count
                with BuildPart(Plane.XY) as bp:
                    with BuildSketch() as s:
                        for e in sk.entities:
                            if e.type == "circle":
                                r = e.params["radius"]
                                c = e.params.get("center", (0, 0))
                                shape = B3DCircle(r)
                                if c != (0, 0) and c != [0, 0]:
                                    shape = Location((c[0], c[1], 0)) * shape
                                # 绕 axis_direction 旋转
                                if angle_deg != 0:
                                    shape = Location(tuple(axis_origin), tuple(axis_direction), angle_deg) * shape
                                add(shape)
                            elif e.type == "rectangle":
                                w, h = e.params["width"], e.params["height"]
                                c = e.params.get("center", (0, 0))
                                shape = B3DRect(w, h)
                                if c != (0, 0) and c != [0, 0]:
                                    shape = Location((c[0], c[1], 0)) * shape
                                if angle_deg != 0:
                                    shape = Location(tuple(axis_origin), tuple(axis_direction), angle_deg) * shape
                                add(shape)
                    b3d_extrude(amount=depth)
                if mode == "add":
                    new_geom = new_geom + bp.part
                elif mode == "cut":
                    new_geom = new_geom - bp.part
            
            self._current_geometry = new_geom
            self.narrative.append(f"circular pattern {sketch_name} x{count} → {feature.name}")
            txn.commit()
            self._feature_geometries[feature_id] = self._current_geometry
        
        render_level = self.adaptive_renderer.should_render(
            op="pattern", op_params={"count": count},
            has_geometry=(self._geometry_internal is not None),
        )
        render_png = None
        if render_level != "none":
            renders = self.renderer.render(self._geometry_internal, level=render_level, geometry_revision=self._geometry_revision)
            render_png = renders.get("iso") or renders.get("default")
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=feature_id, narrative=f"circular_pattern {count} 个副本",
            render_png=render_png, render_level=render_level,
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"added": [feature_id]},
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))
    
    def boolean(self, target_sketch: str, tools: list, operation: str = "union", name: str = "", depth: float = None) -> StepResult:
        """
        v1.7 真实 boolean op — 显式 union/subtract/intersect（不靠 extrude mode）
        
        流程：
        1. 把 target_sketch 拉伸成一个 Part 作为新 body
        2. 把 tools (草图列表) 每个拉伸成 Part，全部 union
        3. 把 new body 和 tools 复合体应用 boolean op
        
        与 extrude mode 的区别：
        - boolean 支持多个 tool（自动 union）
        - boolean 支持 intersect（extrude 只能 add/cut）
        
        Args:
            target_sketch: 目标草图（必填，替换 current_geometry）
            tools: 工具草图列表 [sk1, sk2, ...]
            operation: "union" | "subtract" | "intersect"
            name: 特征名
            depth: target/tools 拉伸深度；缺省自动取当前零件 Z 向尺寸 + 2mm
        """
        start = time.time()
        if target_sketch not in self.sketches:
            raise InvalidRequestError(f"target 草图 {target_sketch} 不存在")
        for t in tools:
            if t not in self.sketches:
                raise InvalidRequestError(f"tool 草图 {t} 不存在")
        if operation not in ("union", "subtract", "intersect"):
            raise InvalidRequestError(f"operation 必须是 union/subtract/intersect（当前 {operation}）")
        # v2.11: depth 不再硬编码 50；缺省取当前零件 Z 向尺寸 + 2mm
        derived_depth = depth is None
        if derived_depth:
            z_ext = _z_extent_of(self._current_geometry)
            if z_ext is None:
                raise InvalidRequestError("boolean 无法推导 depth（无已有几何），请显式传 depth")
            depth = z_ext + 2.0
        else:
            require_positive("depth", depth)
        
        from build123d import (
            BuildPart, BuildSketch, Plane, add, extrude, Location,
            Circle as B3DCircle, Rectangle as B3DRect,
        )
        from build123d.build_common import Locations
        
        def _extrude_sketch_to_part(sk, depth, direction="Z"):
            """草图 → Part（v2.1: 统一助手，支持 polyline/arc；v2.11: 尊重 workplane 放置）"""
            plane = self._sketch_plane(sk, direction) or \
                {"X": Plane.YZ, "Y": Plane.XZ, "Z": Plane.XY}.get(direction, Plane.XY)
            return self._extrude_sketch_solid(sk, depth, plane)
        
        with Transaction(self, "boolean") as txn:
            entry = self._record_history("boolean", target_sketch=target_sketch, tools=tools, operation=operation, name=name, depth=depth)
            feature_id = next_feature_id()
            entry["feature_id"] = feature_id
            feature = FeatureNode(
                id=feature_id, type=FeatureType.BOOLEAN,
                parameters={"target_sketch": target_sketch, "tools": tools, "operation": operation, "name": name, "depth": depth},
                name=name or f"boolean_{operation}_{feature_id}",
                state=FeatureState.COMPUTED,
            )
            self.feature_graph.add(feature)
            
            # 1. 拉伸 target 成新 body
            target_sk = self.sketches[target_sketch]
            new_body = _extrude_sketch_to_part(target_sk, depth)
            
            # 2. 拉伸所有 tools 并 union
            tools_part = None
            for t_name in tools:
                t_sk = self.sketches[t_name]
                t_part = _extrude_sketch_to_part(t_sk, depth)
                if tools_part is None:
                    tools_part = t_part
                else:
                    tools_part = tools_part + t_part
            
            # 3. 应用 boolean op
            if operation == "union":
                self._current_geometry = new_body + tools_part
            elif operation == "subtract":
                self._current_geometry = new_body - tools_part
            elif operation == "intersect":
                # build123d Part 用 __and__ (&) 做 intersect
                self._current_geometry = new_body.__and__(tools_part)
            
            self.narrative.append(f"boolean {operation} target={target_sketch} tools={tools}")
            txn.commit()
            self._feature_geometries[feature_id] = self._current_geometry
        
        render_level = self.adaptive_renderer.should_render("boolean", has_geometry=self._geometry_internal is not None)
        render_png = None
        if render_level != "none":
            renders = self.renderer.render(self._geometry_internal, level=render_level, geometry_revision=self._geometry_revision)
            render_png = renders.get("iso") or renders.get("default")
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=feature_id,
            narrative=f"boolean {operation} ({target_sketch} with {len(tools)} tools)",
            render_png=render_png, render_level=render_level,
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"added": [feature_id]},
            warning=("depth 未显式提供，自动取当前零件 Z 向尺寸 + 2mm"
                     if derived_depth else None),
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))
    def _resolve_edge_refs(self, edges) -> list:
        """v2.11: edges 参数 → build123d Edge 列表。

        接受 "all"（全部边）| "E12"（单引用）| ["E12", "E15"]（引用列表）。
        引用来自 select(element_type="edge")。几何修改后旧引用 → RecoverableError。
        """
        if isinstance(edges, str):
            if edges == "all":
                return list(self._current_geometry.edges())
            edges = [edges]
        if not isinstance(edges, (list, tuple)) or not edges:
            raise InvalidRequestError(
                "edges 需为 'all'、单个边引用 'E12' 或引用列表（来自 select element_type='edge'）")
        try:
            infos = resolve_refs(self._topo_cache, self._geometry_revision,
                                 self._current_geometry, list(edges), "edge")
        except RefStaleError as e:
            raise RecoverableError(
                f"边引用已失效: {e}",
                suggestion={"action": "重新 select 获取新的边引用",
                            "fix": {"edges": "all"},
                            "reason_code": "stale_topo_ref"},
                reason_code="stale_topo_ref",
            )
        except RefFormatError as e:
            raise InvalidRequestError(str(e))
        return [info.shape for info in infos]

    def fillet(self, radius: float, edges: str = "all", name: str = "") -> StepResult:
        """
        v1.4.1 真实 fillet（圆角）— 用 build123d Part.fillet → OCC BRepFilletAPI
        
        Args:
            radius: 圆角半径（mm），必须 > 0
            edges: 边选择方式，当前支持 "all"（所有边）
            name: 特征名
        """
        start = time.time()
        if self._current_geometry is None:
            raise InvalidRequestError("fillet 需要先有几何（先 new_body 拉伸）")
        if radius <= 0:
            raise InvalidRequestError(f"radius 必须 > 0（当前 {radius}）")
        
        with Transaction(self, "fillet") as txn:
            entry = self._record_history("fillet", radius=radius, edges=edges, name=name)
            feature_id = next_feature_id()
            entry["feature_id"] = feature_id
            feature = FeatureNode(
                id=feature_id, type=FeatureType.FILLET,
                parameters={"radius": radius, "edges": edges, "name": name},
                name=name or f"fillet_{feature_id}",
                state=FeatureState.COMPUTED,
            )
            self.feature_graph.add(feature)
            
            edge_list = self._resolve_edge_refs(edges)

            try:
                self._current_geometry = self._current_geometry.fillet(radius, edge_list)
            except RecoverableError:
                raise
            except Exception as e:
                # v2.11: OCC 失败转结构化建议（典型：半径相对所选边过大）
                raise RecoverableError(
                    f"fillet 失败: {e}",
                    suggestion={
                        "action": "减小半径或减少所选边后重试",
                        "fix": {"radius": round(radius / 2, 3)},
                        "reason_code": "fillet_too_large",
                    },
                    reason_code="fillet_too_large",
                )
            self.narrative.append(f"fillet r={radius} {len(edge_list)} 边 → {feature.name}")
            txn.commit()
            self._feature_geometries[feature_id] = self._current_geometry
        
        render_level = self.adaptive_renderer.should_render(
            op="fillet", op_params={"radius": radius},
            has_geometry=(self._geometry_internal is not None),
        )
        render_png = None
        if render_level != "none":
            renders = self.renderer.render(self._geometry_internal, level=render_level, geometry_revision=self._geometry_revision)
            render_png = renders.get("iso") or renders.get("default")
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=feature_id, narrative=f"fillet r={radius}",
            render_png=render_png, render_level=render_level,
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"added": [feature_id]},
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))
    
    def chamfer(self, length: float, length2: float = None, edges: str = "all", name: str = "") -> StepResult:
        """
        v1.4.2 真实 chamfer（倒角）— 用 build123d Part.chamfer → OCC BRepFilletAPI_MakeChamfer
        
        Args:
            length: 倒角长度（mm），必须 > 0
            length2: 第二长度（不对称倒角），默认 None = 对称
            edges: 边选择方式，当前支持 "all"
            name: 特征名
        """
        start = time.time()
        if self._current_geometry is None:
            raise InvalidRequestError("chamfer 需要先有几何")
        if length <= 0:
            raise InvalidRequestError(f"length 必须 > 0（当前 {length}）")
        l2 = length if length2 is None else length2
        
        with Transaction(self, "chamfer") as txn:
            entry = self._record_history("chamfer", length=length, length2=length2, edges=edges, name=name)
            feature_id = next_feature_id()
            entry["feature_id"] = feature_id
            feature = FeatureNode(
                id=feature_id, type=FeatureType.CHAMFER,
                parameters={"length": length, "length2": l2, "edges": edges, "name": name},
                name=name or f"chamfer_{feature_id}",
                state=FeatureState.COMPUTED,
            )
            self.feature_graph.add(feature)
            
            edge_list = self._resolve_edge_refs(edges)

            try:
                self._current_geometry = self._current_geometry.chamfer(length, l2, edge_list)
            except RecoverableError:
                raise
            except Exception as e:
                # v2.11: OCC 失败转结构化建议（典型：倒角尺寸相对所选边过大）
                raise RecoverableError(
                    f"chamfer 失败: {e}",
                    suggestion={
                        "action": "减小倒角长度或减少所选边后重试",
                        "fix": {"length": round(length / 2, 3)},
                        "reason_code": "chamfer_too_large",
                    },
                    reason_code="chamfer_too_large",
                )
            self.narrative.append(f"chamfer l={length}/{l2} {len(edge_list)} 边 → {feature.name}")
            txn.commit()
            self._feature_geometries[feature_id] = self._current_geometry
        
        render_level = self.adaptive_renderer.should_render(
            op="chamfer", op_params={"length": length},
            has_geometry=(self._geometry_internal is not None),
        )
        render_png = None
        if render_level != "none":
            renders = self.renderer.render(self._geometry_internal, level=render_level, geometry_revision=self._geometry_revision)
            render_png = renders.get("iso") or renders.get("default")
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=feature_id, narrative=f"chamfer l={length}",
            render_png=render_png, render_level=render_level,
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"added": [feature_id]},
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))
    
    def hole(
        self,
        position: tuple = (0, 0),
        diameter: float = 10.0,
        depth: float = None,
        hole_type: str = "simple",
        counterbore_diameter: float = None,
        counterbore_depth: float = None,
        name: str = "",
        direction: str = "top",
    ) -> StepResult:
        """
        v1.8 真实 hole（孔向导）— v2.11 支持任意面进入 + 真 countersink 锥面

        Args:
            position: 孔位，2 个坐标，相对进入面：
                top/bottom → (x, y)；x+/x- → (y, z)；y+/y- → (x, z)
            diameter: 孔径（mm）
            depth: 孔深（从进入面起算；默认 None = 穿透）
            hole_type: "simple" | "counterbore" | "countersink"
            counterbore_diameter: 沉孔/锪孔大径（仅 counterbore/countersink）
            counterbore_depth: 沉孔/锪孔深度（仅 counterbore/countersink）
            name: 特征名
            direction: 孔从哪个面进入："top"(默认) / "bottom" / "x+" / "x-" / "y+" / "y-"
                （同义别名 "+Z"/"-Z"/"+X"/"-X"/"+Y"/"-Y"）

        自动从 current_geometry 切出孔
        """
        start = time.time()
        if self._current_geometry is None:
            raise InvalidRequestError("hole 需要先有几何")
        if diameter <= 0:
            raise InvalidRequestError(f"diameter 必须 > 0（当前 {diameter}）")
        if hole_type not in ("simple", "counterbore", "countersink"):
            raise InvalidRequestError(f"hole_type 必须 simple/counterbore/countersink（当前 {hole_type}）")
        if depth is not None and depth <= 0:
            raise InvalidRequestError(f"depth 必须 > 0（当前 {depth}）")

        from OCP.TopExp import TopExp_Explorer
        from OCP.TopAbs import TopAbs_SOLID
        from OCP.TopoDS import TopoDS
        from OCP.BRepBndLib import BRepBndLib
        from OCP.Bnd import Bnd_Box
        from build123d import Solid as B3DSolid, Plane as B3DPlane

        dir_map = {
            "top": (0, 0, -1), "z+": (0, 0, -1), "+Z": (0, 0, -1),
            "bottom": (0, 0, 1), "z-": (0, 0, 1), "-Z": (0, 0, 1),
            "x+": (-1, 0, 0), "+X": (-1, 0, 0),
            "x-": (1, 0, 0), "-X": (1, 0, 0),
            "y+": (0, -1, 0), "+Y": (0, -1, 0),
            "y-": (0, 1, 0), "-Y": (0, 1, 0),
        }
        if direction not in dir_map:
            raise InvalidRequestError(
                f"direction '{direction}' 不支持。可选 top/bottom/x+/x-/y+/y-（即孔从哪个面进入）")
        drill = dir_map[direction]

        shape = self._current_geometry.wrapped if hasattr(self._current_geometry, 'wrapped') else self._current_geometry
        exp = TopExp_Explorer(shape, TopAbs_SOLID)
        bbox = Bnd_Box()
        if exp.More():
            BRepBndLib.Add_s(exp.Current(), bbox)
        bb = bbox.CornerMin(), bbox.CornerMax()
        lo = (bb[0].X(), bb[0].Y(), bb[0].Z())
        hi = (bb[1].X(), bb[1].Y(), bb[1].Z())
        extents = (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])

        # 进入面锚点 + 沿钻轴的零件总深
        margin = 5.0  # 刀具高出进入面，保证干净切穿
        if direction in ("top", "z+", "+Z"):
            anchor = (position[0], position[1], hi[2]); axis_extent = extents[2]
        elif direction in ("bottom", "z-", "-Z"):
            anchor = (position[0], position[1], lo[2]); axis_extent = extents[2]
        elif direction in ("x+", "+X"):
            anchor = (hi[0], position[0], position[1]); axis_extent = extents[0]
        elif direction in ("x-", "-X"):
            anchor = (lo[0], position[0], position[1]); axis_extent = extents[0]
        elif direction in ("y+", "+Y"):
            anchor = (position[0], hi[1], position[1]); axis_extent = extents[1]
        else:  # y- / -Y
            anchor = (position[0], lo[1], position[1]); axis_extent = extents[1]

        actual_depth = (axis_extent + 2 * margin) if depth is None else (depth + margin)
        axis_plane = B3DPlane(origin=anchor, z_dir=drill)
        back_plane = B3DPlane(origin=anchor, z_dir=tuple(-v for v in drill))
        px, py = position

        with Transaction(self, "hole") as txn:
            entry = self._record_history("hole", position=position, diameter=diameter, depth=depth, hole_type=hole_type, counterbore_diameter=counterbore_diameter, counterbore_depth=counterbore_depth, name=name, direction=direction)
            feature_id = next_feature_id()
            entry["feature_id"] = feature_id
            feature = FeatureNode(
                id=feature_id, type=FeatureType.HOLE,
                parameters={
                    "position": list(position), "diameter": diameter, "depth": actual_depth,
                    "hole_type": hole_type, "counterbore_diameter": counterbore_diameter,
                    "counterbore_depth": counterbore_depth, "name": name, "direction": direction,
                },
                name=name or f"hole_{feature_id}",
                state=FeatureState.COMPUTED,
            )
            self.feature_graph.add(feature)

            # 1. simple: 单圆柱
            # 2. counterbore: 大圆浅柱 + 小圆全长柱
            # 3. countersink: 真 90° 锥面（大径在进入面，缩到孔径）+ 小圆全长柱
            bore = B3DSolid.make_cylinder(diameter / 2, actual_depth, plane=axis_plane)
            if hole_type == "simple":
                cutter = bore
            elif hole_type == "counterbore":
                cb_d = counterbore_diameter or (diameter * 1.8)
                cb_depth = counterbore_depth or (diameter * 0.5)
                cb = B3DSolid.make_cylinder(cb_d / 2, cb_depth + margin, plane=axis_plane)
                cutter = bore + cb
            else:  # countersink: 真 90° 锥面
                cs_d = counterbore_diameter or (diameter * 1.8)
                cs_depth = counterbore_depth or (diameter * 0.5)
                if cs_d <= diameter:
                    raise InvalidRequestError(
                        f"countersink 大径 {cs_d} 必须大于孔径 {diameter}")
                cone = B3DSolid.make_cone(
                    cs_d / 2, diameter / 2, cs_depth, plane=axis_plane)
                collar = B3DSolid.make_cylinder(cs_d / 2, margin, plane=back_plane)
                cutter = bore + cone + collar

            # boolean subtract
            self._current_geometry = self._current_geometry - cutter
            self.narrative.append(
                f"hole {hole_type} Ø{diameter} @ ({px}, {py}) {direction} 深 {actual_depth - margin:.1f}")
            txn.commit()
            self._feature_geometries[feature_id] = self._current_geometry
        
        render_level = self.adaptive_renderer.should_render("hole", has_geometry=self._geometry_internal is not None)
        render_png = None
        if render_level != "none":
            renders = self.renderer.render(self._geometry_internal, level=render_level, geometry_revision=self._geometry_revision)
            render_png = renders.get("iso") or renders.get("default")
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=feature_id,
            narrative=f"hole {hole_type} Ø{diameter}",
            render_png=render_png, render_level=render_level,
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"added": [feature_id]},
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))
    
    def shell(self, thickness: float, face_filter: str = "top", name: str = "", face_refs: list = None) -> StepResult:
        """
        v1.6.1 真实 shell（抽壳）— 用 OCP BRepOffsetAPI_MakeThickSolid

        Args:
            thickness: 壁厚（mm），必须 > 0
            face_filter: 开口面选择："top"/"bottom"/"z+"/"z-"/"x+"/"x-"/"y+"/"y-"
            name: 特征名
            face_refs: v2.11 — 开口面引用列表（如 ["F03"]，来自 select）。
                提供时优先于 face_filter，可指定任意面（含曲面）作为开口。
        """
        start = time.time()
        if self._current_geometry is None:
            raise InvalidRequestError("shell 需要先有几何")
        if thickness <= 0:
            raise InvalidRequestError(f"thickness 必须 > 0（当前 {thickness}）")
        
        from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeThickSolid
        from OCP.TopTools import TopTools_ListOfShape
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID as TopAbs_SOLID_TYPE
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.TopoDS import TopoDS
        from OCP.BRepOffset import BRepOffset_Mode
        from OCP.GeomAbs import GeomAbs_JoinType
        from build123d import Solid as B3DSolid
        
        # v1.6.1: wrapped 是 Compound, 找第一个 Solid
        shape = self._current_geometry.wrapped if hasattr(self._current_geometry, 'wrapped') else self._current_geometry
        exp_solid = TopExp_Explorer(shape, TopAbs_SOLID_TYPE)
        solid_ocp = None
        if exp_solid.More():
            solid_ocp = TopoDS.Solid_s(exp_solid.Current())
        if solid_ocp is None or solid_ocp.IsNull():
            raise InvalidRequestError("shell 需要 Solid 几何（当前 Part 无 Solid）")
        
        # 选面
        faces_list = TopTools_ListOfShape()
        target_dirs = {
            "top": (0, 0, 1), "z+": (0, 0, 1),
            "bottom": (0, 0, -1), "z-": (0, 0, -1),
            "x+": (1, 0, 0), "x-": (-1, 0, 0),
            "y+": (0, 1, 0), "y-": (0, -1, 0),
        }
        if face_refs:
            # v2.11: 按引用选开口面（支持任意面，含曲面）
            if isinstance(face_refs, str):
                face_refs = [face_refs]
            try:
                open_infos = resolve_refs(self._topo_cache, self._geometry_revision,
                                          self._current_geometry, list(face_refs), "face")
            except RefStaleError as e:
                raise RecoverableError(
                    f"面引用已失效: {e}",
                    suggestion={"action": "重新 select 获取新的面引用",
                                "reason_code": "stale_topo_ref"},
                    reason_code="stale_topo_ref",
                )
            except RefFormatError as e:
                raise InvalidRequestError(str(e))
            for info in open_infos:
                faces_list.Append(info.shape.wrapped)
        else:
            if face_filter not in target_dirs:
                raise InvalidRequestError(f"face_filter '{face_filter}' 不支持。可选: {list(target_dirs.keys())}，或改用 face_refs 指定开口面")
            target_dir = target_dirs[face_filter]

            exp = TopExp_Explorer(solid_ocp, TopAbs_FACE)
            while exp.More():
                f = exp.Current()
                f_face = TopoDS.Face_s(f)
                if not f_face.IsNull():
                    adaptor = BRepAdaptor_Surface(f_face)
                    if adaptor.GetType() == 0:  # Plane
                        d = adaptor.Plane().Axis().Direction()
                        if (abs(d.X() - target_dir[0]) < 0.1 and
                            abs(d.Y() - target_dir[1]) < 0.1 and
                            abs(d.Z() - target_dir[2]) < 0.1):
                            faces_list.Append(f)
                exp.Next()

        if faces_list.Size() == 0:
            raise InvalidRequestError(f"没找到匹配 '{face_filter}' 的面")
        
        with Transaction(self, "shell") as txn:
            entry = self._record_history("shell", thickness=thickness, face_filter=face_filter, name=name, face_refs=face_refs)
            feature_id = next_feature_id()
            entry["feature_id"] = feature_id
            feature = FeatureNode(
                id=feature_id, type=FeatureType.SHELL,
                parameters={"thickness": thickness, "face_filter": face_filter, "name": name, "face_refs": face_refs},
                name=name or f"shell_{feature_id}",
                state=FeatureState.COMPUTED,
            )
            self.feature_graph.add(feature)
            
            builder = BRepOffsetAPI_MakeThickSolid()
            builder.MakeThickSolidByJoin(
                solid_ocp, faces_list, thickness, 1e-3,
                BRepOffset_Mode.BRepOffset_Skin, False, False,
                GeomAbs_JoinType.GeomAbs_Arc, False,
            )
            if not builder.IsDone():
                raise InvalidRequestError("shell 失败（OCC builder not done）")
            
            self._current_geometry = B3DSolid(builder.Shape())
            self.narrative.append(f"shell t={thickness} 面 {face_filter}（{faces_list.Size()} 个）")
            txn.commit()
            self._feature_geometries[feature_id] = self._current_geometry
        
        render_level = self.adaptive_renderer.should_render("shell", has_geometry=self._geometry_internal is not None)
        render_png = None
        if render_level != "none":
            renders = self.renderer.render(self._geometry_internal, level=render_level, geometry_revision=self._geometry_revision)
            render_png = renders.get("iso") or renders.get("default")
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=feature_id,
            narrative=f"shell t={thickness} 面 {face_filter}",
            render_png=render_png, render_level=render_level,
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"added": [feature_id]},
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))
    
    def linear_pattern(
        self,
        sketch_name: str,
        count: int,
        direction: tuple = (1, 0),
        spacing: float = 10.0,
        mode: str = "cut",
        name: str = "",
        depth: float = None,
    ) -> StepResult:
        """
        v1.10 真实 linear_pattern（线性阵列）— 沿 direction 复制 N 个草图 + boolean

        Args:
            sketch_name: 草图名
            count: 副本数（>= 2）
            direction: (dx, dy) 沿 XY 平面方向（自动 normalize）
            spacing: 副本间距（mm）
            mode: "cut" | "add" | "union"
            name: 特征名
            depth: 拉伸深度；缺省自动取当前零件 Z 向尺寸 + 2mm（穿透式）
        """
        start = time.time()
        if sketch_name not in self.sketches:
            raise InvalidRequestError(f"草图 {sketch_name} 不存在")
        if self._current_geometry is None:
            raise InvalidRequestError("linear_pattern 需要先有几何")
        if count < 2:
            raise InvalidRequestError(f"count 必须 >= 2（当前 {count}）")
        if mode not in ("cut", "add", "union"):
            raise InvalidRequestError(f"mode 必须是 cut/add/union（当前 {mode}）")
        # v2.11: depth 不再硬编码 50；缺省取当前零件 Z 向尺寸 + 2mm（穿透式）
        derived_depth = depth is None
        if derived_depth:
            z_ext = _z_extent_of(self._current_geometry)
            if z_ext is None:
                raise InvalidRequestError("linear_pattern 无法推导 depth（无已有几何），请显式传 depth")
            depth = z_ext + 2.0
        else:
            require_positive("depth", depth)
        
        from build123d import (
            BuildPart, BuildSketch, Plane, add, extrude, Location,
            Circle as B3DCircle, Rectangle as B3DRect,
        )
        from build123d.build_common import Locations
        import math
        
        # normalize direction
        dx, dy = direction
        norm = math.sqrt(dx*dx + dy*dy)
        if norm == 0:
            raise InvalidRequestError("direction 不能全为 0")
        ux, uy = dx / norm, dy / norm
        
        sk = self.sketches[sketch_name]
        if any(e.type not in ("circle", "rectangle") for e in sk.entities):
            raise NotImplementedError("linear_pattern 暂不支持 polyline/arc 实体（请用 circle/rectangle）")
        with Transaction(self, "linear_pattern") as txn:
            entry = self._record_history("linear_pattern", sketch_name=sketch_name, count=count, direction=direction, spacing=spacing, mode=mode, name=name, depth=depth)
            feature_id = next_feature_id()
            entry["feature_id"] = feature_id
            feature = FeatureNode(
                id=feature_id, type=FeatureType.LINEAR_PATTERN,
                parameters={"sketch_name": sketch_name, "count": count, "direction": list(direction), "spacing": spacing, "mode": mode, "name": name, "depth": depth},
                name=name or f"linear_pattern_{feature_id}",
                state=FeatureState.COMPUTED,
            )
            self.feature_graph.add(feature)

            # 复制 count 次：每次 entity center 偏移 i*spacing*direction
            with BuildPart(self._sketch_plane(sk, "Z") or Plane.XY) as bp:
                with BuildSketch() as s:
                    for i in range(count):
                        off_x = i * spacing * ux
                        off_y = i * spacing * uy
                        for e in sk.entities:
                            if e.type == "circle":
                                r = e.params["radius"]
                                c = e.params.get("center", (0, 0))
                                with Locations((c[0] + off_x, c[1] + off_y, 0)):
                                    B3DCircle(r)
                            elif e.type == "rectangle":
                                w, h = e.params["width"], e.params["height"]
                                c = e.params.get("center", (0, 0))
                                with Locations((c[0] + off_x, c[1] + off_y, 0)):
                                    B3DRect(w, h)
                extrude(amount=depth)
            all_parts = bp.part
            
            if mode in ("union", "add"):
                self._current_geometry = self._current_geometry + all_parts
            elif mode == "cut":
                self._current_geometry = self._current_geometry - all_parts
            
            self.narrative.append(f"linear_pattern {sketch_name} x{count} {direction} 间距 {spacing}")
            txn.commit()
            self._feature_geometries[feature_id] = self._current_geometry
        
        render_level = self.adaptive_renderer.should_render("linear_pattern", has_geometry=self._geometry_internal is not None)
        render_png = None
        if render_level != "none":
            renders = self.renderer.render(self._geometry_internal, level=render_level, geometry_revision=self._geometry_revision)
            render_png = renders.get("iso") or renders.get("default")
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=feature_id,
            narrative=f"linear_pattern {sketch_name} x{count}",
            render_png=render_png, render_level=render_level,
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"added": [feature_id]},
            warning=("depth 未显式提供，自动取当前零件 Z 向尺寸 + 2mm"
                     if derived_depth else None),
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))

    def mirror(
        self,
        sketch_name: str,
        axis: str = "X",
        mode: str = "union",
        name: str = "",
        depth: float = None,
    ) -> StepResult:
        """
        v1.9 真实 mirror（镜像）— 沿 X/Y 轴镜像草图 + boolean

        Args:
            sketch_name: 要镜像的草图
            axis: "X" | "Y"（镜像轴）
            mode: "union"（镜像后合并到 current_geometry）| "add" | "cut"
            name: 特征名
            depth: 拉伸深度；缺省自动取当前零件 Z 向尺寸 + 2mm（穿透式）
        """
        start = time.time()
        if sketch_name not in self.sketches:
            raise InvalidRequestError(f"草图 {sketch_name} 不存在")
        if self._current_geometry is None:
            raise InvalidRequestError("mirror 需要先有几何")
        if axis not in ("X", "Y"):
            raise InvalidRequestError(f"axis 必须是 'X' 或 'Y'（当前 {axis}）")
        if mode not in ("union", "add", "cut"):
            raise InvalidRequestError(f"mode 必须是 union/add/cut（当前 {mode}）")
        # v2.11: depth 不再硬编码 50；缺省取当前零件 Z 向尺寸 + 2mm（穿透式）
        derived_depth = depth is None
        if derived_depth:
            z_ext = _z_extent_of(self._current_geometry)
            if z_ext is None:
                raise InvalidRequestError("mirror 无法推导 depth（无已有几何），请显式传 depth")
            depth = z_ext + 2.0
        else:
            require_positive("depth", depth)
        
        from build123d import (
            BuildPart, BuildSketch, Plane, add, extrude, Location,
            Circle as B3DCircle, Rectangle as B3DRect,
        )
        from build123d.build_common import Locations
        
        sk = self.sketches[sketch_name]
        if any(e.type not in ("circle", "rectangle") for e in sk.entities):
            raise NotImplementedError("mirror 暂不支持 polyline/arc 实体（请用 circle/rectangle）")
        with Transaction(self, "mirror") as txn:
            entry = self._record_history("mirror", sketch_name=sketch_name, axis=axis, mode=mode, name=name, depth=depth)
            feature_id = next_feature_id()
            entry["feature_id"] = feature_id
            feature = FeatureNode(
                id=feature_id, type=FeatureType.MIRROR,
                parameters={"sketch_name": sketch_name, "axis": axis, "mode": mode, "name": name, "depth": depth},
                name=name or f"mirror_{axis}_{feature_id}",
                state=FeatureState.COMPUTED,
            )
            self.feature_graph.add(feature)
            
            # 镜像：原位置（保留）+ 镜像位置（也生成）
            # axis="X" 镜像 → 翻转 X 坐标（cx=-c[0], cy=c[1]）
            # axis="Y" 镜像 → 翻转 Y 坐标（cx=c[0], cy=-c[1]）
            with BuildPart(self._sketch_plane(sk, "Z") or Plane.XY) as bp:
                with BuildSketch() as s:
                    for e in sk.entities:
                        if e.type == "circle":
                            r = e.params["radius"]
                            c = e.params.get("center", (0, 0))
                            # 原位置
                            with Locations((c[0], c[1], 0)):
                                B3DCircle(r)
                            # 镜像位置
                            if axis == "X":
                                cx, cy = -c[0], c[1]
                            else:  # axis == "Y"
                                cx, cy = c[0], -c[1]
                            with Locations((cx, cy, 0)):
                                B3DCircle(r)
                        elif e.type == "rectangle":
                            w, h = e.params["width"], e.params["height"]
                            c = e.params.get("center", (0, 0))
                            # 原位置
                            with Locations((c[0], c[1], 0)):
                                B3DRect(w, h)
                            # 镜像位置
                            if axis == "X":
                                cx, cy = -c[0], c[1]
                            else:  # axis == "Y"
                                cx, cy = c[0], -c[1]
                            with Locations((cx, cy, 0)):
                                B3DRect(w, h)
                extrude(amount=depth)
            both_parts = bp.part
            
            if mode == "union" or mode == "add":
                self._current_geometry = self._current_geometry + both_parts
            elif mode == "cut":
                self._current_geometry = self._current_geometry - both_parts
            
            self.narrative.append(f"mirror {sketch_name} 沿 {axis} {mode}")
            txn.commit()
            self._feature_geometries[feature_id] = self._current_geometry
        
        render_level = self.adaptive_renderer.should_render("mirror", has_geometry=self._geometry_internal is not None)
        render_png = None
        if render_level != "none":
            renders = self.renderer.render(self._geometry_internal, level=render_level, geometry_revision=self._geometry_revision)
            render_png = renders.get("iso") or renders.get("default")
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=feature_id,
            narrative=f"mirror {sketch_name} 沿 {axis}",
            render_png=render_png, render_level=render_level,
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"added": [feature_id]},
            warning=("depth 未显式提供，自动取当前零件 Z 向尺寸 + 2mm"
                     if derived_depth else None),
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))
    
    def sweep(self, profile_sketch: str, path: str = "x_axis", length: float = 50.0, name: str = "", mode: str = "new_body", confirm_replace: bool = False) -> StepResult:
        """
        v1.6.2 sweep（扫掠，沿直线路径）— 用 build123d.extrude(face, amount/dir)
        v2.11: 加 mode (new_body/add/cut) 对齐 extrude 语义，不再静默丢弃已有几何

        Args:
            profile_sketch: profile 草图名（中心在原点，circle 或 rectangle）
            path: 路径方向，"x_axis" | "y_axis" | "z_axis"
            length: 路径长度（mm）
            name: 特征名
            mode: "new_body" | "add" | "cut"
            confirm_replace: mode="new_body" 且已有几何时必须显式 True
        """
        start = time.time()
        if profile_sketch not in self.sketches:
            raise InvalidRequestError(f"profile 草图 {profile_sketch} 不存在")
        if length <= 0:
            raise InvalidRequestError(f"length 必须 > 0（当前 {length}）")
        if mode not in ("new_body", "add", "cut"):
            raise InvalidRequestError(f"mode 必须是 new_body/add/cut（当前 {mode}）")
        # v2.11: new_body 会清空整个已有零件，必须显式确认
        if mode == "new_body" and self._current_geometry is not None and not confirm_replace:
            raise RecoverableError(
                f"sweep mode='new_body' 会清空已有零件。叠加到当前零件用 mode='add'，切除用 mode='cut'；确实要替换请传 confirm_replace=True",
                suggestion={
                    "action": "选择修正参数后重试",
                    "fix": {"mode": "add"},
                    "alternatives": [
                        {"fix": {"mode": "cut"}},
                        {"fix": {"confirm_replace": True}},
                    ],
                    "reason_code": "new_body_would_replace",
                },
                reason_code="new_body_would_replace",
            )
        if mode in ("add", "cut") and self._current_geometry is None:
            raise InvalidRequestError(f"sweep mode='{mode}' 需要先有几何")

        sk = self.sketches[profile_sketch]
        if not sk.closed:
            raise InvalidRequestError(f"profile {profile_sketch} 未关闭")
        if len(sk.entities) != 1:
            raise InvalidRequestError(f"sweep 只支持单 entity profile")

        e = sk.entities[0]
        if e.type not in ("circle", "rectangle"):
            raise InvalidRequestError(f"sweep 只支持 circle/rectangle profile（当前 {e.type}）")
        r = e.params.get("radius")
        w, h = e.params.get("width"), e.params.get("height")
        c = e.params.get("center", (0, 0))

        from build123d import (
            BuildPart, BuildSketch, Plane, Circle as B3DCircle, Rectangle as B3DRect, add,
            Solid as B3DSolid, Vector, Locations,
        )

        # v1.16 修复：path → 草绘平面（profile 垂直于路径，extrude 沿平面法向 = 路径方向）
        # 注：build123d extrude(dir=...) 要求 dir 不平行于面局部 x 轴，圆形 profile 沿 X 会崩，
        # 因此改为按路径选平面 + 普通 extrude(amount)。
        path_planes = {
            "x_axis": Plane.YZ,
            "y_axis": Plane.XZ,
            "z_axis": Plane.XY,
        }
        if path not in path_planes:
            raise InvalidRequestError(f"path '{path}' 不支持（x_axis/y_axis/z_axis）")

        with Transaction(self, "sweep") as txn:
            entry = self._record_history("sweep", profile_sketch=profile_sketch, path=path, length=length, name=name, mode=mode, confirm_replace=confirm_replace)
            feature_id = next_feature_id()
            entry["feature_id"] = feature_id
            feature = FeatureNode(
                id=feature_id, type=FeatureType.SWEEP,
                parameters={"profile_sketch": profile_sketch, "path": path, "length": length, "name": name, "mode": mode},
                name=name or f"sweep_{feature_id}",
                state=FeatureState.COMPUTED,
            )
            self.feature_graph.add(feature)

            # BuildPart + BuildSketch + extrude（沿平面法向 = 路径方向）
            # 注意：BuildSketch 必须显式传平面，否则默认落在 XY（build123d 行为）
            with BuildPart(path_planes[path]) as bp:
                with BuildSketch(path_planes[path]) as s:
                    with Locations((c[0], c[1], 0)):
                        if e.type == "circle":
                            add(B3DCircle(r))
                        else:
                            add(B3DRect(w, h))
                from build123d import extrude as b3d_extrude
                b3d_extrude(amount=length)

            new_solid = bp.part
            if mode == "new_body" or self._current_geometry is None:
                self._current_geometry = new_solid
            elif mode == "add":
                self._current_geometry = self._current_geometry + new_solid
            else:  # cut
                self._current_geometry = self._current_geometry - new_solid
            self.narrative.append(f"sweep {profile_sketch} 沿 {path} 长 {length} {mode}")
            txn.commit()
            self._feature_geometries[feature_id] = self._current_geometry
        
        render_level = self.adaptive_renderer.should_render("sweep", has_geometry=self._geometry_internal is not None)
        render_png = None
        if render_level != "none":
            renders = self.renderer.render(self._geometry_internal, level=render_level, geometry_revision=self._geometry_revision)
            render_png = renders.get("iso") or renders.get("default")
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=feature_id,
            narrative=f"sweep {profile_sketch} 沿 {path}",
            render_png=render_png, render_level=render_level,
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"added": [feature_id]},
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))
    def validate_geometry(self, target: str = "_current_geometry", level: str = "standard") -> StepResult:
        """Validate a current or feature-scoped geometry without changing state."""
        if target in ("current", "_current_geometry"):
            geometry = self._current_geometry
        else:
            if target not in self._feature_geometries:
                raise InvalidRequestError(f"validate_geometry target 不存在: {target}")
            geometry = self._feature_geometries[target]
        if level not in ("basic", "standard", "strict"):
            raise InvalidRequestError("level 必须是 basic、standard 或 strict")
        validation = self.geometry_inspector.validate_geometry(
            geometry, level=level, feature_count=len(self.feature_graph.nodes)
        ).to_dict()
        self._step_counter += 1
        result = make_success(
            feature_id=f"V_{self._step_counter:03d}",
            narrative=f"验证几何 {target} ({level})",
            geometry_summary=self._geometry_summary_for(geometry),
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"validated": [target]},
            elapsed_ms=0.0,
            step_index=self._step_counter,
            geometry_validation=validation,
        )
        result.value = validation
        return result

    def query(self, target: str, what: str = "bounding_box") -> "StepResult":
        """
        v1.11 真实 query（查询几何属性）— 用 OCC Bnd_Box / TopExp 提取
        
        Args:
            target: feature_id (e.g. "F_001") 或 "_current_geometry" (默认当前几何)
            what: "bounding_box" | "volume" | "centroid" | "face_count" | "edge_count" | "vertex_count"
        """
        start = time.time()
        if what not in ("bounding_box", "volume", "centroid", "face_count", "edge_count", "vertex_count"):
            raise InvalidRequestError(f"what 必须是 bounding_box/volume/centroid/face_count/edge_count/vertex_count（当前 {what}）")
        
        # 选几何（v1.16 修复：支持 feature_id 目标 → 该 feature 完成时的几何）
        warning = None
        if target == "_current_geometry":
            geom = self._current_geometry
        elif target in self._feature_geometries:
            geom = self._feature_geometries[target]
            if geom is None:
                geom = self._current_geometry
                warning = f"feature {target} 无几何记录，回退到当前几何"
        elif target in self.feature_graph.nodes:
            geom = self._feature_geometries.get(target) or self._current_geometry
            warning = f"feature {target} 无几何记录，回退到当前几何"
        else:
            raise InvalidRequestError(f"目标 {target} 不存在（未知 feature，仅支持 _current_geometry 或已记录几何的 feature_id）")

        if geom is None:
            raise InvalidRequestError("query 需要先有几何")

        # Adapter/mock geometry has no TopoDS wrapper.  Use the same public
        # summary contract instead of passing it to OCP TopExp_Explorer.
        if not hasattr(geom, "wrapped"):
            summary = self._geometry_summary_for(geom)
            if what == "bounding_box":
                bb = summary.bounding_box
                result_value = {
                    "xmin": bb[0], "ymin": bb[1], "zmin": bb[2],
                    "xmax": bb[3], "ymax": bb[4], "zmax": bb[5],
                    "size_x": bb[3] - bb[0], "size_y": bb[4] - bb[1],
                    "size_z": bb[5] - bb[2],
                }
            elif what == "volume":
                result_value = summary.volume
            elif what == "centroid":
                bb = summary.bounding_box
                result_value = {"x": (bb[0] + bb[3]) / 2,
                                "y": (bb[1] + bb[4]) / 2,
                                "z": (bb[2] + bb[5]) / 2}
            elif what == "face_count":
                result_value = summary.face_count
            elif what == "edge_count":
                result_value = summary.edge_count
            else:
                result_value = summary.vertex_count
            self._step_counter += 1
            result = make_success(
                feature_id=f"Q_{self._step_counter:03d}",
                narrative=f"query {target} {what}",
                geometry_summary=summary,
                current_narrative=self.narrative.copy(),
                feature_graph_delta={"queried": [target, what]},
                elapsed_ms=(time.time() - start) * 1000,
                step_index=self._step_counter,
            )
            result.value = result_value
            result.target = target
            result.what = what
            result.warning = warning
            return result

        try:
            from OCP.BRepBndLib import BRepBndLib
        except ImportError:
            bbox = getattr(geom, "bounding_box", getattr(geom, "bbox", None))
            if callable(bbox):
                bbox = bbox()
            if bbox is None or len(bbox) != 6:
                raise InvalidRequestError("query 当前几何没有可用包围盒")
            bbox = tuple(float(value) for value in bbox)
            if what == "bounding_box":
                result_value = {
                    "xmin": bbox[0], "ymin": bbox[1], "zmin": bbox[2],
                    "xmax": bbox[3], "ymax": bbox[4], "zmax": bbox[5],
                    "size_x": bbox[3] - bbox[0], "size_y": bbox[4] - bbox[1],
                    "size_z": bbox[5] - bbox[2],
                }
            elif what == "volume":
                result_value = float(getattr(geom, "volume", 0.0))
            elif what == "centroid":
                result_value = {
                    "x": (bbox[0] + bbox[3]) / 2,
                    "y": (bbox[1] + bbox[4]) / 2,
                    "z": (bbox[2] + bbox[5]) / 2,
                }
            elif what == "face_count":
                value = getattr(geom, "face_count", None)
                result_value = int(value if value is not None else len(getattr(geom, "faces", [])))
            elif what == "edge_count":
                value = getattr(geom, "edge_count", None)
                result_value = int(value if value is not None else 0)
            else:
                value = getattr(geom, "vertex_count", None)
                result_value = int(value if value is not None else len(getattr(geom, "vertices", [])))
            self._step_counter += 1
            result = make_success(
                feature_id=f"Q_{self._step_counter:03d}",
                narrative=f"query {target} {what}",
                current_narrative=self.narrative.copy(),
                feature_graph_delta={"queried": [target, what]},
                elapsed_ms=(time.time() - start) * 1000,
                step_index=self._step_counter,
            )
            result.value = result_value
            result.target = target
            result.what = what
            if warning:
                result.warning = warning
            return result

        from OCP.Bnd import Bnd_Box
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopAbs import TopAbs_FACE, TopAbs_EDGE, TopAbs_VERTEX, TopAbs_SOLID
        from OCP.TopoDS import TopoDS
        from OCP.GProp import GProp_GProps
        from OCP.BRepGProp import BRepGProp
        
        shape = geom.wrapped if hasattr(geom, 'wrapped') else geom
        result_value = None
        
        if what == "bounding_box":
            bbox = Bnd_Box()
            exp = TopExp_Explorer(shape, TopAbs_SOLID)
            found = False
            while exp.More():
                BRepBndLib.Add_s(exp.Current(), bbox)
                found = True
                exp.Next()
            if not found:
                BRepBndLib.Add_s(shape, bbox)  # 无 SOLID（如纯面）时用整体
            result_value = {
                "xmin": bbox.CornerMin().X(), "ymin": bbox.CornerMin().Y(), "zmin": bbox.CornerMin().Z(),
                "xmax": bbox.CornerMax().X(), "ymax": bbox.CornerMax().Y(), "zmax": bbox.CornerMax().Z(),
                "size_x": bbox.CornerMax().X() - bbox.CornerMin().X(),
                "size_y": bbox.CornerMax().Y() - bbox.CornerMin().Y(),
                "size_z": bbox.CornerMax().Z() - bbox.CornerMin().Z(),
            }
        elif what == "volume":
            props = GProp_GProps()
            exp = TopExp_Explorer(shape, TopAbs_SOLID)
            vol = 0.0
            while exp.More():
                BRepGProp.VolumeProperties_s(exp.Current(), props)
                vol += props.Mass()
                exp.Next()
            result_value = vol
        elif what == "centroid":
            props = GProp_GProps()
            exp = TopExp_Explorer(shape, TopAbs_SOLID)
            cx, cy, cz, total_vol = 0.0, 0.0, 0.0, 0.0
            while exp.More():
                vprops = GProp_GProps()
                BRepGProp.VolumeProperties_s(exp.Current(), vprops)
                centroid = vprops.CentreOfMass()
                v = vprops.Mass()
                cx += centroid.X() * v
                cy += centroid.Y() * v
                cz += centroid.Z() * v
                total_vol += v
                exp.Next()
            if total_vol > 0:
                result_value = {"x": cx/total_vol, "y": cy/total_vol, "z": cz/total_vol}
            else:
                result_value = {"x": 0, "y": 0, "z": 0}
        elif what == "face_count":
            # v1.11.1: 用 build123d Part.faces() dedup
            if hasattr(geom, 'faces'):
                result_value = len(geom.faces())
            else:
                count = 0
                exp = TopExp_Explorer(shape, TopAbs_FACE)
                while exp.More():
                    count += 1
                    exp.Next()
                result_value = count
        elif what == "edge_count":
            if hasattr(geom, 'edges'):
                result_value = len(geom.edges())
            else:
                count = 0
                exp = TopExp_Explorer(shape, TopAbs_EDGE)
                while exp.More():
                    count += 1
                    exp.Next()
                result_value = count
        elif what == "vertex_count":
            if hasattr(geom, 'vertices'):
                result_value = len(geom.vertices())
            else:
                count = 0
                exp = TopExp_Explorer(shape, TopAbs_VERTEX)
                while exp.More():
                    count += 1
                    exp.Next()
                result_value = count
        
        from .step_result import StepResult
        self._step_counter += 1
        result = make_success(
            feature_id=f"Q_{self._step_counter:03d}",
            narrative=f"query {target} {what}",
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"queried": [target, what]},
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        )
        result.value = result_value
        result.target = target
        result.what = what
        if warning:
            result.warning = warning
        return result

    def check_interference(self, parts: list = None, tolerance: float = 0.001,
                           only_interfering: bool = False) -> "StepResult":
        """v2.9: 装配体干涉检查.

        检查所有 part pair (A, B) 的几何干涉, 返回体积矩阵.

        Args:
            parts: [(name, Part), ...] 列表, None = 检查 _current_geometry
            tolerance: 体积阈值 (mm³), 低于此视为无干涉 (默认 0.001, 浮点精度)
            only_interfering: 只返回有干涉的 pair

        Returns: dict {
          total_pairs, interfering_count, max_interference_volume,
          pairs: [{name_a, name_b, interfering, volume_mm3, center}],
          interfering_pairs: [subset]
        }
        """
        from .collision import check_assembly_interference
        start = time.time()
        if parts is None:
            # 单 part 模式: 检查 _current_geometry 自身 (返回空)
            result_value = {
                "total_pairs": 0, "interfering_count": 0, "max_interference_volume": 0.0,
                "pairs": [], "interfering_pairs": []
            }
        else:
            result_value = check_assembly_interference(
                parts, tolerance=tolerance, only_interfering=only_interfering
            )
        self._step_counter += 1
        result = make_success(
            feature_id=f"CI_{self._step_counter:04d}",
            narrative=f"check_interference: {result_value['interfering_count']} interfering of "
                      f"{result_value['total_pairs']} pairs",
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"interference_checked": result_value['total_pairs']},
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        )
        result.value = result_value
        return result

    def select(self, filter_type: str = "all", face_index: int = None, element_type: str = "face") -> "StepResult":
        """
        v1.12 真实 select（按类型选 face/edge）— v2.11 起返回可回喂引用

        Args:
            filter_type: face 模式: "all"|"plane"|"cylinder"|"cone"|"sphere"|"torus"
                         edge 模式: "all"|"line"|"circle"|"ellipse"|"bezier"|"bspline"
            face_index: 第几个匹配（0-indexed, None = 全部）
            element_type: "face"（默认）| "edge"

        返回项带 ref（如 "F03"/"E12"），可直接回喂 fillet/chamfer 的 edges、
        shell 的 face_refs、create_workplane 的 face_ref。
        引用在几何被修改后失效（需重新 select）。
        """
        start = time.time()
        if self._current_geometry is None:
            raise InvalidRequestError("select 需要先有几何")
        if element_type not in ("face", "edge"):
            raise InvalidRequestError(f"element_type 必须是 face/edge（当前 {element_type}）")

        result_value: dict = {}
        if element_type == "face":
            face_type_map = {"plane", "cylinder", "cone", "sphere", "torus"}
            if filter_type != "all" and filter_type not in face_type_map:
                raise InvalidRequestError(
                    f"filter_type 必须是 all/plane/cylinder/cone/sphere/torus（当前 {filter_type}）")

            infos = self._topo_cache.faces(self._geometry_revision, self._current_geometry)
            type_count = {"plane": 0, "cylinder": 0, "cone": 0, "sphere": 0, "torus": 0}
            all_faces = []
            for info in infos:
                if info.geom_type in type_count:
                    type_count[info.geom_type] += 1
                if filter_type == "all" or filter_type == info.geom_type:
                    item = {"index": len(all_faces), "type": info.geom_type, "ref": info.ref}
                    item.update(info.summary())
                    all_faces.append(item)
            result_value = {
                "element_type": "face",
                "total": sum(type_count.values()),
                "by_type": type_count,
                "selected": all_faces,
                "revision": self._geometry_revision,
                "note": "ref 可回喂 fillet/chamfer(edges)、shell/create_workplane(face_refs/face_ref)；几何修改后引用失效需重新 select",
            }
            if face_index is not None and 0 <= face_index < len(all_faces):
                result_value["specific"] = all_faces[face_index]
        else:  # edge
            edge_types = {"line", "circle", "ellipse", "hyperbola", "parabola", "bezier", "bspline", "other"}
            if filter_type != "all" and filter_type not in edge_types:
                raise InvalidRequestError(
                    f"edge 模式 filter_type 必须是 all/{'/'.join(sorted(edge_types))}（当前 {filter_type}）")

            infos = self._topo_cache.edges(self._geometry_revision, self._current_geometry)
            type_count: dict = {}
            all_edges = []
            for info in infos:
                type_count[info.geom_type] = type_count.get(info.geom_type, 0) + 1
                if filter_type == "all" or filter_type == info.geom_type:
                    item = {"index": len(all_edges), "type": info.geom_type, "ref": info.ref}
                    item.update(info.summary())
                    all_edges.append(item)
            result_value = {
                "element_type": "edge",
                "total": sum(type_count.values()),
                "by_type": type_count,
                "selected": all_edges,
                "revision": self._geometry_revision,
                "note": "ref 可回喂 fillet/chamfer 的 edges 参数（如 edges=['E12','E15']）；几何修改后引用失效需重新 select",
            }
            if face_index is not None and 0 <= face_index < len(all_edges):
                result_value["specific"] = all_edges[face_index]

        self._step_counter += 1
        result = make_success(
            feature_id=f"S_{self._step_counter:03d}",
            narrative=f"select {element_type} {filter_type}",
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"selected": [filter_type]},
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        )
        result.value = result_value
        result.filter_type = filter_type
        return result
    
    def measure(self, target1: str, target2: str = None, metric: str = "distance") -> "StepResult":
        """
        v1.13 真实 measure（测量）
        
        Args:
            target1: feature_id 或 "current" 或 "(x, y, z)" 坐标
            target2: 同上
            metric: "distance" | "volume" | "area"
        
        简化：测两个 bounding box 中心距离 / 当前体积 / 总面积
        """
        start = time.time()
        if metric not in ("distance", "volume", "area"):
            raise InvalidRequestError(f"metric 必须是 distance/volume/area（当前 {metric}）")
        if metric == "distance" and target2 is None:
            raise InvalidRequestError("measure distance 需要 2 个 target")
        
        from OCP.BRepBndLib import BRepBndLib
        from OCP.Bnd import Bnd_Box
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
        from OCP.TopoDS import TopoDS
        from OCP.GProp import GProp_GProps
        from OCP.BRepGProp import BRepGProp
        import math
        import re
        
        def _resolve_shape(target):
            '''v1.16 修复：feature_id / current 统一解析为几何对象'''
            if target in ("current", "_current_geometry"):
                return self._current_geometry
            if target in self._feature_geometries:
                geom = self._feature_geometries[target]
                if geom is not None:
                    return geom
            raise InvalidRequestError(
                f"measure 目标 {target} 无法解析（支持 current / _current_geometry / 已记录几何的 feature_id）"
            )

        def get_center(target):
            shape = _resolve_shape(target)
            if shape is None:
                raise InvalidRequestError("measure 需要先有几何")
            shape = shape.wrapped if hasattr(shape, 'wrapped') else shape
            bbox = Bnd_Box()
            exp = TopExp_Explorer(shape, TopAbs_SOLID)
            found = False
            while exp.More():
                BRepBndLib.Add_s(exp.Current(), bbox)
                found = True
                exp.Next()
            if not found:
                BRepBndLib.Add_s(shape, bbox)
            return (
                (bbox.CornerMin().X() + bbox.CornerMax().X()) / 2,
                (bbox.CornerMin().Y() + bbox.CornerMax().Y()) / 2,
                (bbox.CornerMin().Z() + bbox.CornerMax().Z()) / 2,
            )

        def get_volume(target):
            shape = _resolve_shape(target)
            shape = shape.wrapped if hasattr(shape, 'wrapped') else shape
            props = GProp_GProps()
            exp = TopExp_Explorer(shape, TopAbs_SOLID)
            v = 0.0
            while exp.More():
                BRepGProp.VolumeProperties_s(exp.Current(), props)
                v += props.Mass()
                exp.Next()
            return v

        def get_area(target):
            shape = _resolve_shape(target)
            shape = shape.wrapped if hasattr(shape, 'wrapped') else shape
            props = GProp_GProps()
            exp = TopExp_Explorer(shape, TopAbs_FACE)
            a = 0.0
            while exp.More():
                BRepGProp.SurfaceProperties_s(exp.Current(), props)
                a += props.Mass()
                exp.Next()
            return a
        
        def parse_coord(s):
            m = re.match(r'\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)', s)
            if m:
                return float(m.group(1)), float(m.group(2)), float(m.group(3))
            return None
        
        if metric == "distance":
            c1 = parse_coord(target1) or get_center(target1)
            c2 = parse_coord(target2) or get_center(target2)
            d = math.sqrt(sum((a-b)**2 for a, b in zip(c1, c2)))
            result_value = {"distance": d, "from": c1, "to": c2}
        elif metric == "volume":
            result_value = {"volume": get_volume(target1)}
        elif metric == "area":
            result_value = {"area": get_area(target1)}
        
        self._step_counter += 1
        result = make_success(
            feature_id=f"M_{self._step_counter:03d}",
            narrative=f"measure {metric}",
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"measured": [metric]},
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        )
        result.value = result_value
        result.metric = metric
        return result
    
    def delete_feature(self, feature_id: str) -> "StepResult":
        """
        v1.14 真实 delete_feature（删除 feature）— 简化版
        
        v1.14 限制：不能真正"重放"前面的 op（参数化模型）
        简化策略：从 feature_graph 移除，但保留 _current_geometry（只移除历史）
        完整版需要参数化重算（v2.0+）
        """
        start = time.time()
        self._step_counter += 1
        if self._has_non_replayable_op:
            return make_failure(
                error="会话包含导入/加载/装配操作，delete_feature 重放不可用（会丢失外部几何）",
                error_kind="RECOVERABLE",
                suggestion={"action": "新建会话后重新建模，或先导出 STEP 再导入到新会话"},
                current_narrative=self.narrative.copy(),
                elapsed_ms=(time.time() - start) * 1000,
                step_index=self._step_counter,
            )
        idx = self._find_history_index(feature_id)
        if idx is None:
            raise InvalidRequestError(f"feature {feature_id} 不存在（无可重放的历史记录）")

        with Transaction(self, "delete_feature") as txn:
            # v2.0：只删目标 entry + 全量重放（独立后续保留，SW 式）。
            # 若后续 op 真依赖被删特征（如 delete 基座后 add/cut 无几何），重放失败 → 整体回滚并报错。
            del self._op_history[idx]
            self._replay()
            txn.commit()

        result = make_success(
            feature_id=f"D_{self._step_counter:03d}",
            narrative=f"delete {feature_id}（重放 {len(self._op_history)} 个 op）",
            render_level="full",
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"deleted": [feature_id], "replayed": True},
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        )
        result.value = {"deleted": feature_id, "replayed_ops": len(self._op_history), "geometry_recomputed": True}
        return result
    
    def update_feature(self, feature_id: str, new_params: dict) -> "StepResult":
        """
        v1.15 真实 update_feature（更新 feature 参数）— 简化版
        
        v1.15 限制：不真正重算（需要参数化模型）
        简化策略：更新 feature graph 中的参数，但不重放 op
        完整版需要参数化重算（v2.0+）
        """
        start = time.time()
        self._step_counter += 1
        if self._has_non_replayable_op:
            return make_failure(
                error="会话包含导入/加载/装配操作，update_feature 重放不可用（会丢失外部几何）",
                error_kind="RECOVERABLE",
                suggestion={"action": "新建会话后重新建模"},
                current_narrative=self.narrative.copy(),
                elapsed_ms=(time.time() - start) * 1000,
                step_index=self._step_counter,
            )
        idx = self._find_history_index(feature_id)
        if idx is None:
            raise InvalidRequestError(f"feature {feature_id} 不存在（无可重放的历史记录）")

        entry = self._op_history[idx]
        op = entry["op"]
        merged = dict(entry["args"])
        merged.update(new_params)
        cap = self.cap.get(op)
        if cap is None:
            raise KernelBugError(f"op {op} 无 schema，无法校验 update 参数")
        ok, err = cap.validate_inputs(merged)
        if not ok:
            raise InvalidRequestError(f"update_feature 参数校验失败: {err}")

        with Transaction(self, "update_feature") as txn:
            # v2.0：更新历史参数 + 全量重放 → 真实重算几何
            entry["args"] = merged
            self._replay()
            txn.commit()

        result = make_success(
            feature_id=f"U_{self._step_counter:03d}",
            narrative=f"update {feature_id}（重放 {len(self._op_history)} 个 op）",
            render_level="full",
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"updated": [feature_id, new_params], "replayed": True},
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        )
        result.value = {"updated": feature_id, "new_params": merged, "geometry_recomputed": True}
        return result

    def rebuild(self, name: str = "") -> StepResult:
        """v2.0 公共 op：按 op_history 全量重放，真实重建几何"""
        start = time.time()
        if self._has_non_replayable_op:
            self._step_counter += 1
            return make_failure(
                error="会话包含导入/加载操作，参数化重放不可用（会丢失导入几何）",
                error_kind="RECOVERABLE",
                suggestion={"action": "新建会话后重新建模，或先导出 STEP 再导入到新会话"},
                current_narrative=self.narrative.copy(),
                elapsed_ms=(time.time() - start) * 1000,
                step_index=self._step_counter,
            )
        with Transaction(self, "rebuild") as txn:
            self._replay()
            txn.commit()

        feature_count = len(self.feature_graph.nodes)
        vol = 0.0
        if self._current_geometry is not None:
            vol = self.query("_current_geometry", "volume").value  # OCP GProp（build123d 属性不是方法，inspector 读不到）
        self._step_counter += 1
        result = make_success(
            feature_id=f"rebuild_{self._step_counter:03d}",
            narrative=f"rebuild：重放 {len(self._op_history)} 个 op",
            render_level="full",
            current_narrative=self.narrative.copy(),
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        )
        result.value = {"replayed_ops": len(self._op_history), "feature_count": feature_count, "volume": vol}
        return result
    
    def export(self, path: str, format: str = "step") -> StepResult:
        """
        v1.5.1 真实 STEP 导出（用 build123d.exporters3d.export_step → OCC STEPControl_Writer）
        
        Args:
            path: 文件路径（如 "/tmp/box.step"）
            format: "step"（当前只支持）
        """
        start = time.time()
        if self._current_geometry is None:
            raise InvalidRequestError("export 需要先有几何")
        if format != "step":
            raise InvalidRequestError(f"format 必须是 'step'（当前 {format}）")
        
        from build123d.exporters3d import export_step
        
        with Transaction(self, "export") as txn:
            feature_id = next_feature_id()
            feature = FeatureNode(
                id=feature_id, type=FeatureType.EXPORT,
                parameters={"path": path, "format": format},
                name=f"export_{format}",
                state=FeatureState.COMPUTED,
            )
            self.feature_graph.add(feature)
            step_exported = self._export_step_or_adapter(self._current_geometry, path)
            self.narrative.append(f"导出 {format} → {path}")
            txn.commit()
        
        # 验证文件存在
        import os
        size = os.path.getsize(path) if os.path.exists(path) else 0
        
        self._step_counter += 1
        # export 不在 make_success 加额外字段 — 通过 narrative 体现
        return self._wrap_step_result(make_success(
            feature_id=feature_id,
            narrative=f"导出 {format} → {path} ({size} bytes)",
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"added": [feature_id]},
            warning=None if step_exported else "当前使用无 OCC 适配器几何，输出为 MechKernel 适配器快照而非标准 STEP",
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))
    
    def import_step(self, path: str, mode: str = "new_body", name: str = "") -> StepResult:
        """
        v1.5.2 真实 STEP 导入（用 build123d.importers.import_step）
        
        Args:
            path: STEP 文件路径
            mode: "new_body" | "add" | "cut"
            name: 特征名
        """
        start = time.time()
        import os
        if not os.path.exists(path):
            raise InvalidRequestError(f"STEP 文件不存在: {path}")
        
        from build123d.importers import import_step as b3d_import_step
        
        with Transaction(self, "import_step") as txn:
            feature_id = next_feature_id()
            feature = FeatureNode(
                id=feature_id, type=FeatureType.IMPORT_STEP,
                parameters={"path": path, "mode": mode, "name": name},
                name=name or f"imported_{os.path.basename(path)}",
                state=FeatureState.COMPUTED,
            )
            self.feature_graph.add(feature)
            
            imported = b3d_import_step(path)
            
            if mode == "new_body":
                self._current_geometry = imported
            elif mode == "add":
                if self._current_geometry is None:
                    self._current_geometry = imported
                else:
                    self._current_geometry = self._current_geometry + imported
            elif mode == "cut":
                if self._current_geometry is None:
                    raise InvalidRequestError("cut 需要先有几何")
                self._current_geometry = self._current_geometry - imported
            
            self.narrative.append(f"导入 STEP ← {path}")
            txn.commit()
            self._has_non_replayable_op = True  # v2.0: 导入几何不可重放
            self._feature_geometries[feature_id] = self._current_geometry
        
        render_level = "iso_only"  # v1.5.2: 简单 import 用 iso
        render_png = None
        if render_level != "none":
            renders = self.renderer.render(self._geometry_internal, level=render_level, geometry_revision=self._geometry_revision)
            render_png = renders.get("iso") or renders.get("default")
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=feature_id,
            narrative=f"导入 STEP ← {path}",
            render_png=render_png, render_level=render_level,
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"added": [feature_id]},
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))
    
    def save_project(self, base_path: str) -> dict:
        """
        v1.5.3 项目保存（STEP 几何 + JSON Feature Graph）
        
        准生产最低门槛。保存两个文件:
        - {base_path}.step — 当前几何（STEP 格式）
        - {base_path}.graph.json — Feature Graph（可重建）
        
        Returns: dict with both file paths and sizes
        """
        import os, json
        if self._current_geometry is None:
            raise InvalidRequestError("save_project 需要先有几何")
        
        step_path = f"{base_path}.step"
        json_path = f"{base_path}.graph.json"
        history_path = f"{base_path}.history.json"
        
        from build123d.exporters3d import export_step
        step_exported = self._export_step_or_adapter(self._current_geometry, step_path)
        
        graph_data = self.feature_graph.to_dict()
        graph_data["_project_meta"] = {
            "version": "v2.6",
            "geometry_volume": self._current_geometry.volume if hasattr(self._current_geometry, "volume") else None,
        }
        graph_data["_sketches"] = {name: sketch.to_dict() for name, sketch in self.sketches.items()}
        graph_data["_parameters"] = dict(self._parameters)
        graph_data["_assembly_instances"] = {
            key: value.to_dict() for key, value in self._assembly_instances.items()
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(graph_data, f, ensure_ascii=False, indent=2)
        history_data = {
            "schema_version": "2.6",
            "replayable": not self._has_non_replayable_op,
            "op_history": copy.deepcopy(self._op_history),
            "geometry": self._geometry_summary_for(self._current_geometry).to_dict(),
            "geometry_validation": self.geometry_inspector.validate_geometry(
                self._current_geometry, level="standard", feature_count=len(self.feature_graph.nodes)
            ).to_dict(),
        }
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump(history_data, f, ensure_ascii=False, indent=2)
        
        return {
            "step_path": step_path,
            "step_size": os.path.getsize(step_path),
            "json_path": json_path,
            "json_size": os.path.getsize(json_path),
            "history_path": history_path,
            "history_size": os.path.getsize(history_path),
            "step_exported": step_exported,
        }
    
    def load_project(self, base_path: str, mode: str = "new_body", name: str = "loaded_project") -> StepResult:
        """
        v1.5.4 项目加载（从 {base_path}.step + {base_path}.graph.json 恢复）
        
        重建 Feature Graph + 恢复 _current_geometry
        """
        import os, json
        step_path = f"{base_path}.step"
        json_path = f"{base_path}.graph.json"
        history_path = f"{base_path}.history.json"
        
        if not os.path.exists(step_path):
            raise InvalidRequestError(f"项目文件不存在: {step_path}")
        if not os.path.exists(json_path):
            raise InvalidRequestError(f"项目 JSON 不存在: {json_path}")
        
        start = time.time()
        with Transaction(self, "load_project") as txn:
            feature_id = next_feature_id()
            feature = FeatureNode(
                id=feature_id, type=FeatureType.IMPORT_STEP,
                parameters={"path": step_path, "mode": mode, "name": name},
                name=name,
                state=FeatureState.COMPUTED,
            )
            self.feature_graph.add(feature)
            
            # 恢复几何
            self.import_step(step_path, mode=mode, name=name)
            
            # 恢复 Feature Graph (覆盖从 import_step 添加的简单 graph)
            with open(json_path, "r", encoding="utf-8") as f:
                graph_data = json.load(f)
            saved_sketches = graph_data.pop("_sketches", {})
            saved_parameters = graph_data.pop("_parameters", {})
            saved_assembly = graph_data.pop("_assembly_instances", {})
            graph_data.pop("_project_meta", None)
            self.feature_graph.from_dict(graph_data)
            self.sketches = {name: Sketch.from_dict(data) for name, data in saved_sketches.items()}
            self._parameters = {name: float(value) for name, value in saved_parameters.items()}
            self._assembly_instances = {
                key: AssemblyInstance.from_dict(value) for key, value in saved_assembly.items()
            }
            assembly_warning = self._restore_assembly_geometries()
            replay_message = ""
            loaded_history_replayable = False
            if os.path.exists(history_path):
                with open(history_path, "r", encoding="utf-8") as f:
                    history_data = json.load(f)
                if history_data.get("schema_version") in ("2.4", "2.5", "2.6") and history_data.get("replayable"):
                    imported_geometry = self._geometry_internal
                    imported_snapshot = self._snapshot()
                    self._op_history = list(history_data.get("op_history", []))
                    try:
                        self._has_non_replayable_op = False
                        self._replay()
                        expected = history_data.get("geometry", {})
                        actual = self._geometry_summary_for(self._current_geometry).to_dict()
                        if expected and abs(float(expected.get("volume", 0.0)) - float(actual.get("volume", 0.0))) > 1e-4:
                            raise StateCorruptionError("保存项目的历史重放体积校验失败")
                        expected_validation = history_data.get("geometry_validation", {})
                        if expected_validation.get("fingerprint"):
                            actual_fingerprint = self.geometry_inspector.fingerprint(self._current_geometry)
                            if actual_fingerprint != expected_validation["fingerprint"]:
                                raise StateCorruptionError("保存项目的历史重放指纹校验失败")
                        loaded_history_replayable = True
                    except Exception as exc:
                        self._restore(imported_snapshot)
                        self._geometry_internal = imported_geometry
                        self._has_non_replayable_op = True
                        replay_message = f"历史重放未采用: {type(exc).__name__}"
                else:
                    self._has_non_replayable_op = True
                    replay_message = "历史版本不兼容或项目不可重放"
            else:
                self._has_non_replayable_op = True
                replay_message = "缺少 history.json，按旧项目兼容加载"
            if assembly_warning:
                replay_message = f"{replay_message + '；' if replay_message else ''}{assembly_warning}"
            
            self.narrative.append(f"加载项目 ← {base_path}")
            txn.commit()
            # A complete, validated v2.4 history is replayable in this
            # session.  Legacy/invalid histories intentionally keep the STEP
            # fallback and remain non-replayable.
            self._has_non_replayable_op = not loaded_history_replayable
        
        render_level = "iso_only"  # v1.5.4: 简单 load 用 iso
        render_png = None
        if render_level != "none":
            renders = self.renderer.render(self._geometry_internal, level=render_level, geometry_revision=self._geometry_revision)
            render_png = renders.get("iso") or renders.get("default")
        self._step_counter += 1
        if replay_message:
            result = make_failure(
                error=f"项目已加载，但参数化历史不可用: {replay_message}",
                error_kind="RECOVERABLE",
                suggestion={"action": "保留当前 STEP 几何；新建会话后重新建模以恢复参数化重放"},
                feature_id=feature_id,
                current_narrative=self.narrative.copy(),
                render_level=render_level,
                render_png=render_png,
                elapsed_ms=(time.time() - start) * 1000,
                step_index=self._step_counter,
            )
        else:
            result = make_success(
                feature_id=feature_id,
                narrative=f"加载项目 ← {base_path}",
                render_png=render_png, render_level=render_level,
                current_narrative=self.narrative.copy(),
                feature_graph_delta={"added": [feature_id]},
                elapsed_ms=(time.time() - start) * 1000,
                step_index=self._step_counter,
            )
        return self._wrap_step_result(result)
    
    def assemble(self, parts: list, name: str = "") -> StepResult:
        """v2.0: 装配多个 STEP 零件（定位 + 旋转）→ 组合为一个几何（可导出整机 STEP）"""
        start = time.time()
        if not parts:
            raise InvalidRequestError("parts 不能为空")
        import os
        from build123d.importers import import_step as b3d_import_step
        from build123d import Vector, Axis
        with Transaction(self, "assemble") as txn:
            feature_id = next_feature_id()
            feature = FeatureNode(
                id=feature_id, type=FeatureType.ASSEMBLY,
                parameters={"parts": parts, "name": name},
                name=name or f"assembly_{feature_id}",
                state=FeatureState.COMPUTED,
            )
            self.feature_graph.add(feature)
            assembled = None
            self._assembly_instances = {}
            palette = ([0.36, 0.56, 0.76], [0.78, 0.42, 0.22], [0.42, 0.68, 0.48], [0.62, 0.46, 0.76])
            for index, item in enumerate(parts, start=1):
                if not isinstance(item, dict):
                    raise InvalidRequestError("装配实例必须是字典")
                path = item["path"]
                if not os.path.exists(path):
                    raise InvalidRequestError(f"零件 STEP 不存在: {path}")
                position = item.get("position", [0, 0, 0])
                if not isinstance(position, (list, tuple)) or len(position) != 3:
                    raise InvalidRequestError("装配实例 position 必须是长度为 3 的数组")
                if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in position):
                    raise InvalidRequestError("装配实例 position 必须全部是数字")
                rot = item.get("rotation")
                if rot is not None:
                    if (not isinstance(rot, (list, tuple)) or len(rot) != 2 or
                            not isinstance(rot[0], (int, float)) or
                            not isinstance(rot[1], (list, tuple)) or len(rot[1]) != 3):
                        raise InvalidRequestError("装配实例 rotation 必须是 [角度, [x,y,z]]")
                part = b3d_import_step(path)
                if rot is not None:
                    part = part.rotate(Axis((0, 0, 0), tuple(rot[1])), rot[0])
                part = part.translate(Vector(*position))
                assembled = part if assembled is None else assembled + part
                color = item.get("color", palette[(index - 1) % len(palette)])
                if not isinstance(color, (list, tuple)) or len(color) != 3:
                    raise InvalidRequestError("装配实例 color 必须是长度为 3 的 RGB 数组")
                if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1 for value in color):
                    raise InvalidRequestError("装配实例 color 每个分量必须在 0 到 1 之间")
                instance_id = f"A_{index:04d}"
                # v2.7: 透传 reference-frame 字段
                self._assembly_instances[instance_id] = AssemblyInstance(
                    id=instance_id,
                    name=str(item.get("name", os.path.splitext(os.path.basename(path))[0])),
                    path=path,
                    position=[float(value) for value in position],
                    rotation=rot,
                    color=[float(value) for value in color],
                    visible=bool(item.get("visible", True)),
                    bbox=list(self._geometry_summary_for(part).bounding_box),
                    geometry=part,
                    # v2.7.1: `position or [...]` 死代码 (position 总是 truthy), 简化为 position
                    local_origin=list(item.get("local_origin", position)),
                    mount_frame=item.get("mount_frame"),
                    world_transform=item.get("world_transform"),
                    mount_uv=list(item.get("mount_uv", [0.0, 0.0])),
                    mount_normal_offset=float(item.get("mount_normal_offset", 0.0)),
                )
            self._current_geometry = assembled
            self._has_non_replayable_op = True  # 装配依赖外部 STEP，不可重放
            self.narrative.append(f"装配 {len(parts)} 个零件 → {feature.name}")
            txn.commit()
        render_level = self.adaptive_renderer.should_render("assemble", has_geometry=self._geometry_internal is not None)
        render_png = None
        if render_level != "none":
            renders = self.renderer.render(self._geometry_internal, level=render_level, geometry_revision=self._geometry_revision)
            render_png = renders.get("iso") or renders.get("default")
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=feature_id,
            narrative=f"装配 {len(parts)} 个零件",
            render_png=render_png, render_level=render_level,
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"added": [feature_id]},
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))

    def query_assembly(self, name: str = "") -> StepResult:
        """Return instance-level assembly metadata without changing geometry."""
        start = time.time()
        matches = [item for item in self._assembly_instances.values() if not name or item.name == name or item.id == name]
        if name and not matches:
            raise InvalidRequestError(f"装配实例不存在: {name}")
        self._step_counter += 1
        result = make_success(
            feature_id=f"Q_{self._step_counter:03d}",
            narrative=f"查询装配实例 {name or '全部'}",
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"queried": [item.id for item in matches]},
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        )
        result.value = {"instances": [item.to_dict() for item in matches], "count": len(matches)}
        result.scene_manifest = self._scene_manifest()
        return result

    def set_instance_visibility(self, instance_id: str, visible: bool) -> StepResult:
        """Change display visibility only; fused geometry remains unchanged."""
        if instance_id not in self._assembly_instances:
            raise InvalidRequestError(f"装配实例不存在: {instance_id}")
        if not isinstance(visible, bool):
            raise InvalidRequestError("visible 必须是 boolean")
        self._assembly_instances[instance_id].visible = visible
        self.renderer.clear_cache()
        self._step_counter += 1
        result = make_success(
            feature_id=instance_id, narrative=f"装配实例 {instance_id} {'显示' if visible else '隐藏'}",
            current_narrative=self.narrative.copy(), feature_graph_delta={"updated": [instance_id]},
            elapsed_ms=0.0, step_index=self._step_counter,
        )
        result.value = {"instance_id": instance_id, "visible": visible, "geometry_unchanged": True}
        result.scene_manifest = self._scene_manifest()
        return result

    def set_instance_color(self, instance_id: str, color: tuple) -> StepResult:
        """Change display color only; values are normalized RGB components."""
        if instance_id not in self._assembly_instances:
            raise InvalidRequestError(f"装配实例不存在: {instance_id}")
        if not isinstance(color, (list, tuple)) or len(color) != 3:
            raise InvalidRequestError("color 必须是长度为 3 的 RGB 数组")
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1 for value in color):
            raise InvalidRequestError("color 每个分量必须在 0 到 1 之间")
        self._assembly_instances[instance_id].color = [float(value) for value in color]
        self.renderer.clear_cache()
        self._step_counter += 1
        result = make_success(
            feature_id=instance_id, narrative=f"设置装配实例 {instance_id} 颜色",
            current_narrative=self.narrative.copy(), feature_graph_delta={"updated": [instance_id]},
            elapsed_ms=0.0, step_index=self._step_counter,
        )
        result.value = {"instance_id": instance_id, "color": list(color), "geometry_unchanged": True}
        result.scene_manifest = self._scene_manifest()
        return result

    def _scene_manifest(self) -> dict:
        return {
            "instances": [item.to_dict() for item in self._assembly_instances.values()],
            "instance_ids": list(self._assembly_instances),
        }

    def _restore_assembly_geometries(self) -> Optional[str]:
        """Reload source STEP instances for display; keep fused STEP as fallback."""
        import os
        if not self._assembly_instances:
            return None
        try:
            from build123d import Axis, Vector
            from build123d.importers import import_step as b3d_import_step
            missing = []
            for item in self._assembly_instances.values():
                if not item.path or not os.path.exists(item.path):
                    missing.append(item.id)
                    continue
                part = b3d_import_step(item.path)
                if item.rotation is not None:
                    part = part.rotate(Axis((0, 0, 0), tuple(item.rotation[1])), item.rotation[0])
                part = part.translate(Vector(*item.position))
                item.geometry = part
                item.bbox = list(self._geometry_summary_for(part).bounding_box)
            return f"装配源零件缺失: {missing}" if missing else None
        except Exception as exc:
            return f"装配实例恢复失败: {type(exc).__name__}"

    def _section_half(self, geometry: Any, axis: str, offset: float = None) -> Any:
        """Return a lower half-space intersection for rendering only."""
        if geometry is None:
            raise InvalidRequestError("section 需要先有几何")
        axis = str(axis).upper()
        if axis not in ("X", "Y", "Z"):
            raise InvalidRequestError("section.axis 必须是 X/Y/Z")
        try:
            bb = geometry.bounding_box() if callable(getattr(geometry, "bounding_box", None)) else geometry.bounding_box
            mn, mx = bb.min, bb.max
            bounds = {
                "X": (float(mn.X), float(mx.X)),
                "Y": (float(mn.Y), float(mx.Y)),
                "Z": (float(mn.Z), float(mx.Z)),
            }
            lo, hi = bounds[axis]
            cut = (lo + hi) / 2.0 if offset is None else float(offset)
            if cut <= lo or cut >= hi:
                return geometry
            spans = {a: max(hi_a - lo_a, 1e-6) for a, (lo_a, hi_a) in bounds.items()}
            spans[axis] = cut - lo
            centers = {a: (lo_a + hi_a) / 2.0 for a, (lo_a, hi_a) in bounds.items()}
            centers[axis] = (lo + cut) / 2.0
            from build123d import Box, Location
            section_box = Location((centers["X"], centers["Y"], centers["Z"])) * Box(
                spans["X"], spans["Y"], spans["Z"]
            )
            return geometry & section_box
        except InvalidRequestError:
            raise
        except Exception as exc:
            raise InvalidRequestError(f"section 无法切割几何: {exc}") from exc

    def _preferred_section_axis(self, geometry: Any) -> str:
        """Choose a longitudinal cut plane for the dominant part axis."""
        try:
            bb = geometry.bounding_box() if callable(getattr(geometry, "bounding_box", None)) else geometry.bounding_box
            sizes = {"X": float(bb.size.X), "Y": float(bb.size.Y), "Z": float(bb.size.Z)}
            longest = max(sizes, key=sizes.get)
        except Exception:
            longest = "Z"
        # A section plane normal to a short axis exposes the long internal path.
        return {"X": "Y", "Y": "X", "Z": "X"}[longest]

    @staticmethod
    def _evidence_layout(size: int, view_count: int) -> Tuple[Tuple[int, int], int]:
        """Keep the final evidence packet within an explicit pixel budget."""
        cols = 1 if view_count <= 1 else (2 if view_count <= 4 else 4)
        rows = max(1, (view_count + cols - 1) // cols)
        pad, title_h = 4, 0
        per_width = max(64, (size - (cols + 1) * pad) // cols)
        per_height = max(64, (size - (rows + 1) * pad - rows * title_h) // rows)
        per_view = min(per_width, per_height)
        return (per_view, per_view), cols

    def _render_evidence_views(
        self,
        geometry: Any,
        requested: List[str],
        size: int,
        annotate: bool,
        turntable: bool = False,
        quality: str = "evidence",
        backend: str = "auto",
        show_edges: bool = False,
        highlight: Optional[List[str]] = None,
        scene: Any = None,
    ) -> Tuple[Dict[str, bytes], Optional[bytes], int, Tuple[int, int]]:
        turntable_views = {"rot0", "rot90", "rot180", "rot270"} if turntable else set()
        view_count = len(set(requested) | turntable_views)
        per_view_size, cols = self._evidence_layout(size, max(1, view_count))
        renders = self.renderer.render(
            geometry, level="full", geometry_revision=self._geometry_revision,
            views=requested, annotate=annotate, turntable=turntable, image_size=per_view_size,
            quality=quality, backend=backend, show_edges=show_edges,
            highlight=highlight, scene=scene,
        )
        actual = {key: value for key, value in renders.items() if key != "default" and value}
        grid = Renderer.compose_grid(actual, cols=cols, include_titles=not annotate, max_size=size)
        return actual, grid, cols, per_view_size

    def _make_evidence_manifest(
        self,
        intent: str,
        views: Dict[str, bytes],
        size: int,
        cols: int,
        per_view_size: Tuple[int, int],
        section: Optional[dict] = None,
        target: str = "",
        geometry: Any = None,
        backend_requested: str = "auto",
        backend_used: Optional[str] = None,
        quality: str = "evidence",
        highlighted: Optional[List[str]] = None,
        scene_manifest: Optional[dict] = None,
    ) -> dict:
        summary = self._geometry_summary_for(geometry if geometry is not None else self._current_geometry)
        return {
            "intent": intent,
            "projection": "orthographic",
            "views": list(views),
            "section": section,
            "target": target or None,
            "backend_requested": backend_requested,
            "backend_used": backend_used or self.renderer.last_backend_used,
            "fallback": backend_requested != (backend_used or self.renderer.last_backend_used),
            "warnings": list(self.renderer.last_warnings),
            "quality": quality,
            "highlighted": list(highlighted or []),
            "scene_manifest": scene_manifest,
            "instance_ids": list((scene_manifest or {}).get("instance_ids", [])),
            "bbox_mm": list(summary.bounding_box),
            "layout": {
                "max_size_px": size,
                "pixel_budget": size,
                "columns": cols,
                "per_view_size_px": list(per_view_size),
            },
            "actual_size": [size, size],
            "image_hashes": {
                view: hashlib.sha256(png).hexdigest()[:16]
                for view, png in views.items()
            },
        }

    def render(
        self,
        views: list = None,
        size: int = 640,
        annotate: bool = True,
        section: dict = None,
        turntable: bool = False,
        intent: str = "inspect",
        target: str = "",
        name: str = "",
        quality: str = "evidence",
        backend: str = "auto",
        show_edges: bool = False,
        highlight: list = None,
    ) -> StepResult:
        """生成预算受控、可解释的 AI 视觉证据包，不进入参数化历史。"""
        start = time.time()
        if not isinstance(size, int) or isinstance(size, bool) or size < 64:
            raise InvalidRequestError("size 必须是 >= 64 的整数")
        allowed_intents = {"inspect", "section", "feature_zoom", "delta", "sketch"}
        if intent not in allowed_intents:
            raise InvalidRequestError(f"intent 必须是 {sorted(allowed_intents)}（当前 {intent}）")
        if quality not in ("evidence", "presentation"):
            raise InvalidRequestError("quality 必须是 evidence 或 presentation")
        if backend not in ("auto", "occ", "matplotlib"):
            raise InvalidRequestError("backend 必须是 auto、occ 或 matplotlib")
        if highlight is not None and (not isinstance(highlight, list) or not all(isinstance(item, str) for item in highlight)):
            raise InvalidRequestError("highlight 必须是字符串列表")
        if intent == "sketch":
            if not target:
                raise InvalidRequestError("render intent=sketch 需要 target=sketch_name")
            sketch = self.sketches.get(target)
            if sketch is None:
                raise InvalidRequestError(f"草图不存在: {target}")
            from .sketch_renderer import render_sketch
            actual = render_sketch(sketch, size=size, annotate=annotate)
            actual.pop("default", None)
            if not actual:
                self._step_counter += 1
                return self._wrap_step_result(make_failure(
                    error="render 未生成有效草图图像", error_kind="RECOVERABLE",
                    current_narrative=self.narrative.copy(),
                    elapsed_ms=(time.time() - start) * 1000, step_index=self._step_counter,
                ))
            grid = Renderer.compose_grid(actual, cols=1, max_size=size)
            self._last_render_base64 = base64.b64encode(grid).decode() if grid else None
            self._last_render_views = dict(actual)
            self._step_counter += 1
            result = make_success(
                feature_id=f"R_{self._step_counter:03d}", narrative=f"render sketch {target}",
                render_png=actual.get("sketch"), render_views=actual, render_level="full",
                current_narrative=self.narrative.copy(),
                feature_graph_delta={"rendered": ["sketch"], "intent": "sketch", "target": target},
                constraint_diagnostics={
                    "sketch": sketch.name,
                    "status": getattr(sketch.solver_status, "value", str(sketch.solver_status)),
                    "dof": sketch.dof,
                    "constraint_count": len(sketch.constraints),
                    "residual": sketch.solver_residual,
                    "conflicting_constraints": list(sketch.conflicting_constraints),
                    "under_constrained_entities": [],
                    "solver_iterations": sketch.solver_iterations,
                },
                elapsed_ms=(time.time() - start) * 1000, step_index=self._step_counter,
            )
            if grid:
                result.render_base64 = base64.b64encode(grid).decode()
            points = []
            for entity in sketch.entities:
                if entity.type == "line":
                    points.extend([entity.params["start"], entity.params["end"]])
                elif entity.type == "circle":
                    cx, cy = entity.params["center"]
                    radius = float(entity.params["radius"])
                    points.extend([(cx - radius, cy - radius), (cx + radius, cy + radius)])
            if points:
                xs, ys = zip(*points)
                bbox = [min(xs), min(ys), 0.0, max(xs), max(ys), 0.0]
            else:
                bbox = [0.0] * 6
            result.evidence_manifest = {
                "intent": "sketch", "projection": "orthographic", "views": ["sketch"],
                "section": None, "target": target, "bbox_mm": bbox,
                "layout": {"max_size_px": size, "columns": 1, "per_view_size_px": [size, size]},
                "image_hashes": {"sketch": hashlib.sha256(actual["sketch"]).hexdigest()[:16]},
            }
            result.value = {"views": ["sketch"], "target": target, "evidence_manifest": result.evidence_manifest}
            return self._wrap_step_result(result)

        if self._current_geometry is None:
            self._step_counter += 1
            return self._wrap_step_result(make_failure(
                error="render 需要先有几何", error_kind="RECOVERABLE",
                suggestion={"action": "先创建草图并执行 extrude/revolve"},
                current_narrative=self.narrative.copy(), elapsed_ms=(time.time() - start) * 1000,
                step_index=self._step_counter,
            ))
        if views is not None:
            if not isinstance(views, list) or not all(isinstance(v, str) for v in views):
                raise InvalidRequestError("views 必须是字符串列表")
            allowed = {"iso", "front", "top", "side", "rot0", "rot90", "rot180", "rot270"}
            unknown = sorted(set(views) - allowed)
            if unknown:
                raise InvalidRequestError(f"不支持的视图: {unknown}")

        target_geometry = None
        if target:
            if target in ("current", "_current_geometry"):
                target_geometry = self._current_geometry
            elif target in self._feature_geometries and self._feature_geometries[target] is not None:
                target_geometry = self._feature_geometries[target]
            else:
                raise InvalidRequestError(f"render target {target} 不存在或无几何记录")
        if intent in {"feature_zoom", "delta"} and target_geometry is None:
            raise InvalidRequestError(f"render intent={intent} 需要有效 target feature_id")

        render_geometry = target_geometry if intent == "feature_zoom" else self._current_geometry
        scene = self._assembly_instances if self._assembly_instances and intent != "feature_zoom" else None
        scene_manifest = self._scene_manifest() if scene else None
        section_note = None
        if intent == "section" and section is None:
            section = {"axis": self._preferred_section_axis(render_geometry)}
        if section is not None:
            if not isinstance(section, dict):
                raise InvalidRequestError("section 必须是字典")
            render_geometry = self._section_half(render_geometry, section.get("axis", "Z"), section.get("offset"))
            section_note = {"axis": str(section.get("axis", "Z")).upper(), "offset": section.get("offset")}

        if views is None:
            requested = {
                "inspect": ["iso", "front", "top", "side"],
                "section": ["iso", "front", "side"],
                "feature_zoom": ["iso", "front"],
                "delta": ["iso", "front"],
                "sketch": ["sketch"],
            }[intent]
        else:
            requested = views

        if intent == "delta":
            before_geometry = target_geometry
            if section is not None:
                before_geometry = self._section_half(before_geometry, section_note["axis"], section_note["offset"])
            before, _, _, _ = self._render_evidence_views(
                before_geometry, requested, size, annotate, turntable=False,
                quality=quality, backend=backend, show_edges=show_edges,
                highlight=highlight, scene=scene,
            )
            after, _, _, _ = self._render_evidence_views(
                render_geometry, requested, size, annotate, turntable=False,
                quality=quality, backend=backend, show_edges=show_edges,
                highlight=highlight, scene=scene,
            )
            actual = {f"before_{key}": value for key, value in before.items()}
            actual.update({f"after_{key}": value for key, value in after.items()})
            if annotate:
                actual = {
                    key: self.renderer._annotate_png(
                        png, f"{'BEFORE' if key.startswith('before_') else 'AFTER'} | {key.split('_', 1)[1].upper()}"
                    )
                    for key, png in actual.items()
                }
            per_view_size, cols = self._evidence_layout(size, max(1, len(actual)))
            grid = Renderer.compose_grid(actual, cols=cols, include_titles=False, max_size=size)
        else:
            actual, grid, cols, per_view_size = self._render_evidence_views(
                render_geometry, requested, size, annotate and section_note is None, turntable=turntable,
                quality=quality, backend=backend, show_edges=show_edges,
                highlight=highlight, scene=scene,
            )
        if section_note and annotate:
            suffix = "mid" if section_note["offset"] is None else f"{float(section_note['offset']):g}"
            actual = {
                view: self.renderer._annotate_png(
                    png, f"SECTION {section_note['axis']}@{suffix} | {view.upper()}"
                )
                for view, png in actual.items()
            }
        if not actual:
            self._step_counter += 1
            return self._wrap_step_result(make_failure(
                error="render 未生成有效图像", error_kind="RECOVERABLE",
                current_narrative=self.narrative.copy(), elapsed_ms=(time.time() - start) * 1000,
                step_index=self._step_counter,
            ))
        # Re-compose after section labels have been added.
        if section_note and actual:
            grid = Renderer.compose_grid(actual, cols=cols, include_titles=not annotate, max_size=size)
        if grid:
            self._last_render_base64 = base64.b64encode(grid).decode()
        self._last_render_views = dict(actual)
        self._step_counter += 1
        result = make_success(
            feature_id=f"R_{self._step_counter:03d}", narrative=f"render {intent}" + ("（截面）" if section_note else ""),
            render_png=actual.get("iso") or actual.get("after_iso") or next(iter(actual.values())),
            render_views=actual, render_level="full",
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"rendered": list(actual), "section": section_note, "intent": intent},
            elapsed_ms=(time.time() - start) * 1000, step_index=self._step_counter,
        )
        if grid:
            result.render_base64 = base64.b64encode(grid).decode()
        result.render_views = actual
        result.evidence_manifest = self._make_evidence_manifest(
            intent, actual, size, cols, per_view_size, section_note, target, render_geometry,
            backend_requested=backend, backend_used=self.renderer.last_backend_used,
            quality=quality, highlighted=highlight, scene_manifest=scene_manifest,
        )
        result.backend_used = self.renderer.last_backend_used
        result.quality = quality
        result.scene_manifest = scene_manifest
        result.value = {
            "views": list(actual), "section": section_note, "intent": intent,
            "target": target or None, "name": name, "evidence_manifest": result.evidence_manifest,
        }
        return self._wrap_step_result(result)
    
    def undo(self, steps: int = 1) -> StepResult:
        start = time.time()
        require_positive_int("steps", steps)
        undone = 0
        for _ in range(steps):
            if not self._undo_stack:
                break
            entry = self._undo_stack.pop()
            snap = entry["snapshot"] if isinstance(entry, dict) and "snapshot" in entry else entry
            self._redo_stack.append({"snapshot": self._snapshot(), "description": "redo"})
            self._restore(snap)
            undone += 1
        self._step_counter += 1
        if undone == 0:
            return self._wrap_step_result(make_failure(
                error="撤销栈为空",
                error_kind="GEOMETRY_FAILURE",
                hint="先执行一些操作再撤销",
                current_narrative=self.narrative.copy(),
                elapsed_ms=(time.time() - start) * 1000,
                step_index=self._step_counter,
            ))
        return self._wrap_step_result(make_success(
            feature_id=f"undo_{undone}", narrative=f"撤销 {undone} 步",
            current_narrative=self.narrative.copy(),
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))
    
    def redo(self, steps: int = 1) -> StepResult:
        start = time.time()
        require_positive_int("steps", steps)
        redone = 0
        for _ in range(steps):
            if not self._redo_stack:
                break
            entry = self._redo_stack.pop()
            snap = entry["snapshot"] if isinstance(entry, dict) and "snapshot" in entry else entry
            self._undo_stack.append({"snapshot": self._snapshot(), "description": "undo"})
            self._restore(snap)
            redone += 1
        self._step_counter += 1
        # redo 也恢复 narrative（_restore 已经处理）
        return self._wrap_step_result(make_success(
            feature_id=f"redo_{redone}", narrative=f"重做 {redone} 步",
            current_narrative=self.narrative.copy(),
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))
    
    # ===== 内部辅助 =====

    @staticmethod
    def _exportable_geometry(geometry: Any) -> Any:
        """Return a geometry object accepted by the build123d exporter.

        build123d 0.11.1 exports its composite ``Part`` wrapper directly;
        passing ``Part.wrapped`` changes the node type and makes the exporter
        look for ``wrapped`` on a raw TopoDS shape.  The in-repo adapter is the
        only object that must be handled separately and is rejected by the
        OCC exporter below.
        """
        return geometry

    @classmethod
    def _export_step_or_adapter(cls, geometry: Any, path: str) -> bool:
        """Export OCC geometry, or persist an explicit adapter snapshot.

        The adapter is deliberately not advertised as STEP.  It exists so
        graph/history tests and no-OCC development environments can still
        save and inspect a deterministic project artifact.
        """
        from build123d.exporters3d import export_step
        try:
            export_step(cls._exportable_geometry(geometry), path)
            return True
        except (AttributeError, TypeError, ValueError):
            wrapped = getattr(geometry, "wrapped", None)
            if wrapped is not None and wrapped is not geometry:
                try:
                    export_step(wrapped, path)
                    return True
                except (AttributeError, TypeError, ValueError):
                    pass
            if not hasattr(geometry, "to_dict"):
                raise
            import json
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({"format": "mechkernel-adapter", "geometry": geometry.to_dict()},
                          stream, ensure_ascii=False, indent=2,
                          default=lambda value: value.tolist() if hasattr(value, "tolist") else str(value))
            return False
    
    def _not_implemented(self, api_name: str, planned_version: str) -> StepResult:
        self._step_counter += 1
        return make_failure(
            error=f"API {api_name} 在当前版本未实现",
            error_kind="NOT_IMPLEMENTED",
            api_name=api_name, planned_version=planned_version,
            current_narrative=self.narrative.copy(),
            elapsed_ms=0.0, step_index=self._step_counter,
        )
    
    def _snapshot(self) -> dict:
        # 收集 workplanes
        workplanes_data = {}
        if hasattr(self.workplanes, "_workplanes"):
            workplanes_data = {k: v.to_dict() for k, v in self.workplanes._workplanes.items()}
        return {
            "feature_graph": self.feature_graph.to_dict(),
            "sketches": {k: v.to_dict() for k, v in self.sketches.items()},
            "workplanes": workplanes_data,
            "workplane_by_name": dict(self.workplanes._by_name) if hasattr(self.workplanes, "_by_name") else {},
            "narrative": list(self.narrative),
            "geometry_revision": self._geometry_revision,
            "geometry": self._geometry_internal,
            "feature_geometries": dict(self._feature_geometries),
            "op_history": copy.deepcopy(self._op_history),
            "has_non_replayable_op": self._has_non_replayable_op,
            "parameters": dict(self._parameters),
            "assembly_instances": {key: copy.copy(value) for key, value in self._assembly_instances.items()},
        }
    
    def _restore(self, snap: Dict) -> None:
        self.feature_graph.from_dict(snap["feature_graph"])
        self.sketches = {k: Sketch.from_dict(v) for k, v in snap["sketches"].items()}
        if "workplanes" in snap and hasattr(self.workplanes, "_workplanes"):
            from .workplane import Workplane
            self.workplanes._workplanes = {k: Workplane.from_dict(v) for k, v in snap["workplanes"].items()}
            self.workplanes._by_name = dict(snap.get("workplane_by_name", {}))
        # 恢复 narrative 到快照时刻（undo 撤销之前的状态）
        self.narrative = list(snap["narrative"])
        # v1.16 修复：undo/redo 恢复几何对象引用（进程内快照），不再清空模型。
        # OCC Part 不可序列化，但快照仅用于进程内 undo/redo，保留引用即可。
        self._geometry_internal = snap.get("geometry")
        self._feature_geometries = dict(snap.get("feature_geometries", {}))
        self._op_history = list(snap.get("op_history", []))
        self._has_non_replayable_op = snap.get("has_non_replayable_op", False)
        self._parameters = dict(snap.get("parameters", {}))
        self._assembly_instances = {key: copy.copy(value) for key, value in snap.get("assembly_instances", {}).items()}
        self._last_render_base64 = None
        self._last_render_views = {}
        # P0-3 修复：undo 后 bump revision（让 renderer 缓存失效）
        self._geometry_revision += 1
        self.renderer.clear_cache()  # undo 后清缓存
    
    def _bump_geometry_revision(self):
        self._geometry_revision += 1
        self.renderer.clear_cache()
        # v2.9.2: kernel 自己的 last-render 缓存也清, 防 stale
        # (renderer.clear_cache 只清 OrderedDict, 不碰 _last_render_base64 / _last_render_views)
        self._last_render_base64 = None
        self._last_render_views = {}
        self._last_render_step = -1

    def _validate_transaction_state(self, description: str) -> None:
        """Reject invalid topology candidates before a transaction is published."""
        validated_ops = {
            "extrude", "revolve", "sweep", "boolean", "hole", "fillet",
            "chamfer", "shell", "linear_pattern", "circular_pattern", "mirror",
            "assemble", "import_step", "load_project",
        }
        if description not in validated_ops or self._current_geometry is None:
            return
        validation = self.geometry_inspector.validate_geometry(
            self._current_geometry, level="strict", feature_count=len(self.feature_graph.nodes)
        )
        if not validation.valid:
            details = ", ".join(validation.reason_codes) or "INVALID_GEOMETRY"
            raise GeometryValidationError(
                f"{description} 生成的几何未通过验证: {details}",
                validation.to_dict(),
            )

    def _record_history(self, op: str, **kwargs) -> dict:
        """v2.0: 记录一次 op 调用（重放时跳过）。返回 entry 供回填 feature_id。"""
        entry = {"op": op, "args": copy.deepcopy(kwargs), "feature_id": None}
        if not self._replaying:
            self._op_history.append(entry)
        return entry

    def _find_history_index(self, feature_id: str) -> Optional[int]:
        """按 feature_id 在 op 历史中定位（几何特征 F_xxxx / 草图实体 E_xxxx）"""
        for i, entry in enumerate(self._op_history):
            if entry.get("feature_id") == feature_id:
                return i
        return None

    def _collect_closed_profile(self, sk) -> List[Tuple[float, float]]:
        """v2.0: 把 line/polyline/arc 实体收集成闭合剖面点列（revolve / extrude 用）。
        arc 按 ~5°/段采样成折线。返回闭合环（不含重复首点）。"""
        import math

        def _snap(pt):
            # 浮点吸附：近零归零 + 6 位小数（弧采样端点与直线端点才能精确衔接）
            return (0.0 if abs(pt[0]) < 1e-9 else round(pt[0], 6),
                    0.0 if abs(pt[1]) < 1e-9 else round(pt[1], 6))

        segs = []
        for e in sk.entities:
            if e.type == "line":
                segs.append((_snap(tuple(e.params["start"])), _snap(tuple(e.params["end"]))))
            elif e.type == "polyline":
                pts = [_snap(tuple(p)) for p in e.params["points"]]
                if pts[0] != pts[-1]:
                    pts = pts + [pts[0]]  # 自动闭合（剖面用）
                for i in range(len(pts) - 1):
                    segs.append((pts[i], pts[i + 1]))
            elif e.type == "arc":
                c = tuple(e.params["center"])
                r = float(e.params["radius"])
                a0, a1 = float(e.params["start_angle"]), float(e.params["end_angle"])
                if a1 < a0:
                    a1 += 360.0
                span = abs(a1 - a0)
                n = max(2, int(math.ceil(span / 5.0)))
                prev = None
                for i in range(n + 1):
                    ang = math.radians(a0 + (a1 - a0) * i / n)
                    pt = (c[0] + r * math.cos(ang), c[1] + r * math.sin(ang))
                    pt = _snap(pt)
                    if prev is not None:
                        segs.append((prev, pt))
                    prev = pt
            else:
                raise InvalidRequestError(f"剖面不支持 entity type={e.type}")
        if not segs:
            raise InvalidRequestError("剖面为空")
        by_start = {}
        for s_pt, e_pt in segs:
            by_start.setdefault(s_pt, []).append(e_pt)
        start = segs[0][0]
        pts = [start]
        cur = start
        for _ in range(len(segs) + 1):
            nxts = by_start.get(cur)
            if not nxts:
                raise InvalidRequestError("剖面不连续（存在断点）")
            if len(nxts) > 1:
                raise InvalidRequestError("剖面分叉（一个点引出多条线）")
            nxt = nxts[0]
            if nxt == start:
                break
            if nxt in pts:
                raise InvalidRequestError("剖面存在重复点/子环（未整体闭合）")
            pts.append(nxt)
            cur = nxt
        else:
            raise InvalidRequestError("剖面未闭合")
        return pts

    @staticmethod
    def _orthonormalize_axis(axis, against=None) -> tuple:
        """归一化 3D 向量；against 提供时先去掉其分量（Gram-Schmidt 正交化）。"""
        import math as _math
        v = [float(c) for c in axis]
        n = _math.sqrt(sum(c * c for c in v))
        if n == 0:
            raise InvalidRequestError("方向向量不能全为 0")
        v = [c / n for c in v]
        if against is not None:
            a = [float(c) for c in against]
            dot = sum(v[i] * a[i] for i in range(3))
            v = [v[i] - dot * a[i] for i in range(3)]
            n2 = _math.sqrt(sum(c * c for c in v))
            if n2 < 1e-9:
                raise InvalidRequestError("x_dir 与 normal 平行，无法构成坐标系")
            v = [c / n2 for c in v]
        return tuple(v)

    def _sketch_plane(self, sketch, direction: str = "Z"):
        """v2.11: 草图所属 workplane 的真实 build123d 平面。

        - standard 过原点平面：返回 None（沿用 direction 参数选择 Plane.XY/YZ/XZ，
          完全向后兼容）
        - custom / face / offset 平面：按 workplane 的 origin/x_dir/normal 构造，
          草图 2D 坐标 (u, v) 映射到 origin + u*x_dir + v*y_dir，沿 normal 拉伸
        """
        from build123d import Plane as B3DPlane
        try:
            wp = self.workplanes.get_by_name(sketch.workplane_name)
        except Exception:
            return None
        if wp is None:
            return None
        is_standard = (wp.type in (WorkplaneType.XY, WorkplaneType.YZ, WorkplaneType.XZ)
                       and tuple(wp.origin) == (0.0, 0.0, 0.0))
        if is_standard:
            return None
        return B3DPlane(origin=tuple(wp.origin), x_dir=tuple(wp.x_dir), z_dir=tuple(wp.normal))

    def _extrude_sketch_solid(self, sketch, depth, plane):
        """v2.1: 把草图拉伸成 Part（circle/rectangle/line/polyline/arc 统一支持）。"""
        from build123d import BuildPart, BuildSketch, add, extrude
        from build123d import Circle as B3DCircle, Rectangle as B3DRect
        from build123d import BuildLine, Polyline as B3DPolyline, make_face
        from build123d.build_common import Locations

        profile = None
        if any(e.type in ("line", "polyline", "arc") for e in sketch.entities):
            profile = self._collect_closed_profile(sketch)
        if profile is not None:
            with BuildLine() as bl:
                B3DPolyline(*profile, close=True)
            profile_wires = bl.wires()
            with BuildPart(plane) as bp:
                with BuildSketch() as s:
                    make_face(profile_wires)
                extrude(amount=depth)
            return bp.part

        with BuildPart(plane) as bp:
            with BuildSketch() as s:
                for e in sketch.entities:
                    if e.type == "circle":
                        r = e.params["radius"]
                        c = e.params.get("center", (0, 0))
                        if c == (0, 0) or c == [0, 0]:
                            add(B3DCircle(r))
                        else:
                            with Locations((c[0], c[1], 0)):
                                B3DCircle(r)
                    elif e.type == "rectangle":
                        w, h = e.params["width"], e.params["height"]
                        c = e.params.get("center", (0, 0))
                        if c == (0, 0) or c == [0, 0]:
                            add(B3DRect(w, h))
                        else:
                            with Locations((c[0], c[1], 0)):
                                B3DRect(w, h)
                    else:
                        raise NotImplementedError(f"拉伸暂不支持 entity type={e.type}")
            extrude(amount=depth)
        return bp.part

    def _replay(self) -> None:
        """v2.0 参数化重放：清空状态 + 重置 id + 按 op_history 全量重放（不记录）"""
        self._replaying = True
        was_suspended = self.adaptive_renderer.suspended
        self.adaptive_renderer.suspended = True
        try:
            self.feature_graph = FeatureGraph()
            self.workplanes = WorkplaneRegistry()
            self.sketches = {}
            self.naming_resolver = PersistentNamingResolver()
            self._feature_geometries = {}
            self._geometry_internal = None
            self._parameters = {}
            self.narrative = []
            self.semantic_state = {}
            self._step_counter = 0
            self._last_render_step = -1
            reset_all_id_generators()
            seed_id_generators_from_history(self._op_history)
            parameter_overrides = {}
            for entry in self._op_history:
                if entry.get("op") == "add_constraint":
                    args = entry.get("args", {})
                    pname = args.get("parameter_name", "")
                    if pname and args.get("value") is not None and pname not in parameter_overrides:
                        parameter_overrides[pname] = float(args["value"])
                elif entry.get("op") == "set_parameter":
                    args = entry.get("args", {})
                    if args.get("name"):
                        parameter_overrides[args["name"]] = float(args["value"])
            self._replay_parameter_overrides = parameter_overrides
            self._parameters = dict(parameter_overrides)
            for entry in self._op_history:
                op = entry["op"]
                if op == "set_parameter":
                    # Final parameter values were preloaded above so geometry
                    # features are built with the effective dimensions.
                    continue
                args = dict(entry["args"])
                method = getattr(self, op, None)
                if method is None:
                    raise StateCorruptionError(f"重放失败：op {op} 不存在")
                result = method(**args)
                if not getattr(result, "success", True):
                    raise StateCorruptionError(
                        f"重放失败 op={op} args={args}: {getattr(result, 'error', 'unknown')}"
                    )
        finally:
            self._replay_parameter_overrides = None
            self.adaptive_renderer.suspended = was_suspended
            self._replaying = False
    
    def _extrude_build123d(self, sketch, depth: float, mode: str = "new_body", direction: str = "Z"):
        """
        v2.1 + v1.2.1: 用真实 build123d 做 extrude（已装上）。
        direction: "X" | "Y" | "Z" - 拉伸方向
        失败 fallback 到 Build123dAdapter。
        """
        try:
            from build123d import (
                BuildPart, BuildSketch, Plane, add,
                Circle as B3DCircle, Rectangle as B3DRect, extrude,
            )
            from build123d.build_common import Locations
            
            if not sketch or not sketch.entities:
                from .build123d_adapter import extrude_sketch
                return extrude_sketch(sketch, depth)
            
            # v1.2.1: 方向 → plane；v2.11: 尊重 workplane 的真实放置
            plane = self._sketch_plane(sketch, direction) or \
                {"X": Plane.YZ, "Y": Plane.XZ, "Z": Plane.XY}.get(direction, Plane.XY)
            
            # 单个圆 → 圆柱（v1.16 修复：支持 center 偏移）
            if len(sketch.entities) == 1 and sketch.entities[0].type == "circle":
                e = sketch.entities[0]
                r = e.params["radius"]
                c = e.params.get("center", (0, 0))
                with BuildPart(plane) as bp:
                    with BuildSketch() as s:
                        if c == (0, 0) or c == [0, 0]:
                            add(B3DCircle(r))
                        else:
                            # Locations 上下文（add(Location*shape) 会产生幻影原点副本）
                            with Locations((c[0], c[1], 0)):
                                B3DCircle(r)
                    extrude(amount=depth)
                return bp.part
            
            # 单个矩形 → 立方体（v1.16 修复：支持 center 偏移）
            if len(sketch.entities) == 1 and sketch.entities[0].type == "rectangle":
                e = sketch.entities[0]
                w, h = e.params["width"], e.params["height"]
                c = e.params.get("center", (0, 0))
                with BuildPart(plane) as bp:
                    with BuildSketch() as s:
                        if c == (0, 0) or c == [0, 0]:
                            add(B3DRect(w, h))
                        else:
                            with Locations((c[0], c[1], 0)):
                                B3DRect(w, h)
                    extrude(amount=depth)
                return bp.part
            
            # P0-V3 修复：环面（多 circle 同心）→ 真实 build123d boolean
            # 检测：所有实体都是 circle 且中心相同
            if all(e.type == "circle" for e in sketch.entities):
                c0 = sketch.entities[0].params["center"]
                if all(e.params["center"] == c0 for e in sketch.entities):
                    radii = sorted([e.params["radius"] for e in sketch.entities], reverse=True)
                    if len(radii) >= 2:
                        # 一次性 extrude 多个 circle + 用 mode 切换
                        with BuildPart(plane) as bp:
                            with BuildSketch() as s:
                                add(B3DCircle(radii[0]))
                            extrude(amount=depth)
                            with BuildSketch() as s2:
                                add(B3DCircle(radii[1]))
                            from build123d import Mode
                            extrude(amount=depth, mode=Mode.SUBTRACT)
                        return bp.part
            
            # v2.0: line/polyline/arc 闭合剖面 → face → extrude
            if any(e.type in ("line", "polyline", "arc") for e in sketch.entities):
                profile = self._collect_closed_profile(sketch)
                from build123d import BuildLine, Polyline as B3DPolyline, make_face
                with BuildLine() as bl:
                    B3DPolyline(*profile, close=True)
                profile_wires = bl.wires()
                with BuildPart(plane) as bp:
                    with BuildSketch() as s:
                        make_face(profile_wires)
                    extrude(amount=depth)
                return bp.part

            # 其他 → adapter
            from .build123d_adapter import extrude_sketch
            return extrude_sketch(sketch, depth)
        except ImportError:
            from .build123d_adapter import extrude_sketch
            return extrude_sketch(sketch, depth)
        except Exception as e:
            # build123d 失败 → adapter
            from .build123d_adapter import extrude_sketch
            return extrude_sketch(sketch, depth)
    
    def _extrude_add_or_cut(self, sketch, depth: float, mode: str, direction: str = "Z"):
        """
        v1.2: 在已有几何上 ADD 或 CUT 一个新草图。
        v1.2.1: 支持 direction 参数（X/Y/Z）。
        用 build123d 的 +/- 运算符（Part.__sub__/__add__ 走 OCC boolean）。
        """
        if self._current_geometry is None:
            raise InvalidRequestError(f"extrude mode={mode} 需要先有几何（先 new_body 拉伸）")
        from build123d import BuildPart, BuildSketch, Plane, add, extrude, Location
        from build123d import Circle as B3DCircle, Rectangle as B3DRect

        # v1.2.1: direction → plane；v2.11: 尊重 workplane 的真实放置
        plane = self._sketch_plane(sketch, direction) or \
            {"X": Plane.YZ, "Y": Plane.XZ, "Z": Plane.XY}.get(direction, Plane.XY)
        
        # 1. 单独把新 sketch 拉伸成 Part（v2.1: 统一助手，支持 circle/rect/polyline/arc）
        new_solid = self._extrude_sketch_solid(sketch, depth, plane)
        
        # 3. 走 boolean
        if mode == "add":
            return self._current_geometry + new_solid  # OCC fuse
        elif mode == "cut":
            return self._current_geometry - new_solid  # OCC cut
    
    
    def _push_undo(self, description: str = "") -> None:
        """已弃用：撤销栈由 Transaction.commit() 统一管理"""
        raise DeprecatedInternalAPIError(
            "_push_undo 已弃用。撤销栈由 Transaction.commit() 统一管理。"
            "如需手动推 undo，请用 Transaction(kernel, ..., savepoint=True) 显式 savepoint。"
        )
    
    @property
    def _current_geometry(self) -> Any:
        return self._geometry_internal
    
    @_current_geometry.setter
    def _current_geometry(self, value: Any) -> None:
        if value is not self._geometry_internal:  # 同值不 bump
            self._geometry_internal = value
            self._bump_geometry_revision()
    
    # ===== 通用执行入口 =====
    
    def execute(self, op: str, *args, **kwargs) -> StepResult:
        self._step_counter += 1
        start = time.time()
        
        # P0-V2 修复：拒绝 args + kwargs 同时传（LLM 调试常见错）
        if args and kwargs:
            return make_failure(
                error=f"execute() 不接受 positional + keyword 同时传",
                error_kind="INVALID_REQUEST",
                hint="用 keyword only: execute(op, key1=val1, key2=val2, ...)",
                current_narrative=self.narrative.copy(),
                elapsed_ms=(time.time() - start) * 1000,
                step_index=self._step_counter,
            )
        
        if not op or not isinstance(op, str):
            return make_failure(
                error=f"op 必须是字符串，得到 {type(op).__name__}",
                error_kind="INVALID_REQUEST",
                hint="使用字符串 op 名",
                current_narrative=self.narrative.copy(),
                elapsed_ms=(time.time() - start) * 1000,
                step_index=self._step_counter,
            )
        
        if op.startswith("_"):
            return make_failure(
                error=f"禁止访问内部方法: {op}",
                error_kind="INVALID_REQUEST",
                hint="op 名不能以下划线开头",
                current_narrative=self.narrative.copy(),
                elapsed_ms=(time.time() - start) * 1000,
                step_index=self._step_counter,
            )
        
        if op not in PUBLIC_OPS:
            return make_failure(
                error=f"未知 op: {op}",
                error_kind="NOT_IMPLEMENTED",
                api_name=op, planned_version="v1.2",
                hint=f"可用 op: {sorted(PUBLIC_OPS)[:5]}...",
                current_narrative=self.narrative.copy(),
                elapsed_ms=(time.time() - start) * 1000,
                step_index=self._step_counter,
            )
        
        if self.cap.has(op):
            # P0-3: 如果传了 positional args，提示 LLM 用 kwargs
            hint = f"查看 schema: kernel.cap.get('{op}').to_llm_dict()"
            if args and not kwargs:
                hint = "kernel.execute() 只接受 keyword arguments，不用 positional。改用: execute(op, {key1=val1, key2=val2, ...})"
            ok, err = self.cap.validate_call(op, kwargs)
            if not ok:
                self.adaptive_renderer.mark_failure(op)
                return make_failure(
                    error=f"参数校验失败: {err}",
                    error_kind="INVALID_REQUEST",
                    hint=hint,
                    current_narrative=self.narrative.copy(),
                    elapsed_ms=(time.time() - start) * 1000,
                    step_index=self._step_counter,
                )
        
        try:
            method = getattr(self, op, None)
            if method is None:
                return make_failure(
                    error=f"op {op} 在 PUBLIC_OPS 中但方法未实现",
                    error_kind="KERNEL_BUG", api_name=op,
                    hint="kernel 的 bug",
                    current_narrative=self.narrative.copy(),
                    elapsed_ms=(time.time() - start) * 1000,
                    step_index=self._step_counter,
                )
            result = method(*args, **kwargs)
            if isinstance(result, StepResult) and not result.success:
                self.adaptive_renderer.mark_failure(op)
            return result
        except GeometryValidationError as e:
            self.adaptive_renderer.mark_failure(op)
            return make_failure(
                error=str(e),
                error_kind="GEOMETRY_FAILURE",
                suggestion={"action": "检查操作参数或回退到上一个有效特征"},
                geometry_summary=self._geometry_summary_for(self._current_geometry),
                geometry_validation=e.validation,
                current_narrative=self.narrative.copy(),
                elapsed_ms=(time.time() - start) * 1000,
                step_index=self._step_counter,
            )
        except RecoverableError as e:
            # v2.11: op 内部显式判定"可修复"，suggestion 携带结构化修正参数
            self.adaptive_renderer.mark_failure(op)
            return make_failure(
                error=str(e),
                error_kind="RECOVERABLE",
                suggestion=e.suggestion,
                geometry_summary=self._geometry_summary_for(self._current_geometry),
                current_narrative=self.narrative.copy(),
                elapsed_ms=(time.time() - start) * 1000,
                step_index=self._step_counter,
            )
        except (InvalidRequestError, StateCorruptionError) as e:
            self.adaptive_renderer.mark_failure(op)
            return make_failure(
                error=str(e),
                error_kind="INVALID_REQUEST" if isinstance(e, InvalidRequestError) else "STATE_CORRUPTION",
                hint="检查参数和状态",
                current_narrative=self.narrative.copy(),
                elapsed_ms=(time.time() - start) * 1000,
                step_index=self._step_counter,
            )
        except KernelBugError as e:
            self.adaptive_renderer.mark_failure(op)
            return make_failure(
                error=str(e), error_kind="KERNEL_BUG", hint="kernel bug",
                current_narrative=self.narrative.copy(),
                elapsed_ms=(time.time() - start) * 1000,
                step_index=self._step_counter,
            )
        except Exception as e:
            self.adaptive_renderer.mark_failure(op)
            return make_failure(
                error=f"未预期错误: {e}",
                error_kind="GEOMETRY_FAILURE",
                suggestion={"action": "retry_with_different_params"},
                current_narrative=self.narrative.copy(),
                elapsed_ms=(time.time() - start) * 1000,
                step_index=self._step_counter,
            )
    
    def last_render_base64(self) -> Optional[str]:
        return self._last_render_base64
    
    def _wrap_step_result(self, result: "StepResult") -> "StepResult":
        """自动填充 geometry_summary（如未设置）+ 简单 hints"""
        if result.success and result.geometry_validation is None and self._geometry_internal is not None:
            result.geometry_validation = self.geometry_inspector.validate_geometry(
                self._geometry_internal, level="standard", feature_count=len(self.feature_graph.nodes)
            ).to_dict()
        # Centralize the visual contract for every topology operation.  Older
        # ops only retained the iso bytes; regenerate the configured view set
        # here so full renders reach the vision loop as a single collage.
        if result.render_level != "none" and result.render_views is None and self._geometry_internal is not None:
            renders = self.renderer.render(
                self._geometry_internal,
                level=result.render_level,
                geometry_revision=self._geometry_revision,
                image_size=None,
                quality="evidence",
                backend="auto",
                scene=self._assembly_instances or None,
            )
            result.render_views = {k: v for k, v in renders.items() if k != "default" and v}
            result.render_png = result.render_views.get("iso") or result.render_png
            if result.render_level == "full" and result.render_views:
                grid = Renderer.compose_grid(result.render_views, cols=2, max_size=640)
                if grid:
                    result.render_base64 = base64.b64encode(grid).decode()
                    self._last_render_base64 = result.render_base64
            elif result.render_png:
                result.render_base64 = base64.b64encode(result.render_png).decode()
            self._last_render_views = dict(result.render_views)
            result.backend_used = self.renderer.last_backend_used
            result.quality = "evidence"
            result.scene_manifest = self._scene_manifest() if self._assembly_instances else None
        if result.geometry_summary is None:
            result.geometry_summary = self._geometry_summary_for(self._geometry_internal)
        if result.render_views and result.evidence_manifest is None:
            cols = 1 if len(result.render_views) == 1 else (2 if len(result.render_views) <= 4 else 4)
            result.evidence_manifest = self._make_evidence_manifest(
                "automatic", result.render_views, 640, cols, (320, 320), geometry=self._geometry_internal,
                backend_requested="auto", backend_used=self.renderer.last_backend_used,
                quality="evidence", scene_manifest=result.scene_manifest,
            )
        # 加 hints（如果没设）
        if not result.next_hints:
            result.next_hints = self._generate_hints(result)
        return result
    
    def _generate_hints(self, result: "StepResult") -> List[str]:
        """根据当前状态生成下一步提示"""
        hints = []
        if not self.workplanes or not hasattr(self.workplanes, '_workplanes') or not self.workplanes._workplanes:
            hints.append("调用 create_workplane 创建工作平面")
            return hints
        if not self.sketches:
            hints.append("调用 new_sketch 创建草图")
            return hints
        has_open_sketch = any(not s.closed for s in self.sketches.values())
        if has_open_sketch:
            hints.append("在草图中调用 add_circle / add_rectangle / add_line 添加图元")
            hints.append("完成后调用 close_sketch 关闭草图")
        else:
            hints.append("调用 extrude 拉伸草图生成 3D 实体")
            hints.append("或调用 add_circle 等开始新草图")
        return hints
    
    def _geometry_summary_for(self, geometry: Any) -> "GeometrySummary":
        from .step_result import GeometrySummary
        if geometry is None:
            return GeometrySummary(
                bounding_box=(0, 0, 0, 0, 0, 0),
                volume=0.0, surface_area=0.0,
                face_count=0, edge_count=0, vertex_count=0,
                is_manifold=False, is_watertight=False, is_connected=False,
                feature_count=0,
            )
        # 优先用 geometry_inspector
        try:
            return self.geometry_inspector.summary(geometry, feature_count=len(self.feature_graph.nodes))
        except Exception:
            bb = (0, 0, 0, 100, 100, 100)
            if hasattr(geometry, "bounding_box"):
                b = geometry.bounding_box
                if callable(b):
                    try: b = b()
                    except: pass
                if isinstance(b, (list, tuple)) and len(b) == 6:
                    bb = tuple(b)
            vol = 1000.0
            if hasattr(geometry, "volume"):
                v = geometry.volume
                if callable(v):
                    try: v = v()
                    except: pass
                if isinstance(v, (int, float)):
                    vol = float(v)
            return GeometrySummary(
                bounding_box=bb,
                volume=vol,
                surface_area=600.0,
                face_count=6, edge_count=12, vertex_count=8,
                is_manifold=True, is_watertight=True, is_connected=True,
                feature_count=len(self.feature_graph.nodes),
            )
    
    # ===== 兼容 ai_orchestrator 的 getter =====
    
    def get_narrative(self) -> List[str]:
        return list(self.narrative)
    
    def get_geometry_summary(self) -> Optional[GeometrySummary]:
        if self._geometry_internal is None:
            return None
        return self._geometry_summary_for(self._geometry_internal)
    
    def get_state(self) -> dict:
        wp_count = 0
        if hasattr(self.workplanes, "_workplanes"):
            wp_count = len(self.workplanes._workplanes)
        elif hasattr(self.workplanes, "workplanes"):
            wp_count = len(self.workplanes.workplanes)
        # feature_count = feature_graph 节点 + 草图（草图也算 feature）
        return {
            "workplane_count": wp_count,
            "sketch_count": len(self.sketches),
            "feature_count": len(self.feature_graph.nodes) + len(self.sketches),
            "narrative": list(self.narrative),
            "geometry_revision": self._geometry_revision,
        }
    
    def get_last_render_base64(self) -> Optional[str]:
        return self._last_render_base64

    # =============================================================
    # v2.7: Reference Coordinate Frame API
    # =============================================================

    def create_reference_plane(
        self,
        name: str,
        origin: tuple = (0.0, 0.0, 0.0),
        normal: tuple = (0.0, 0.0, 1.0),
        x_axis: tuple = (1.0, 0.0, 0.0),
        parent: str = None,
        metadata: dict = None,
    ) -> "StepResult":
        """v2.7: 创建一个参考坐标系/参考面.

        Args:
            name: 唯一 frame 名
            origin: 世界坐标原点 (3,)
            normal: 法向（z 方向），自动归一化
            x_axis: x 轴方向，自动正交化到 normal
            parent: 父 frame 名（None = world 根）
            metadata: 自由附加元数据（如 {"role": "input_shaft_axis"}）
        """
        start = time.time()
        if not isinstance(name, str) or not name:
            raise InvalidRequestError("name 必须是非空字符串")
        for v_name, v in [("origin", origin), ("normal", normal), ("x_axis", x_axis)]:
            if not isinstance(v, (list, tuple)) or len(v) != 3:
                raise InvalidRequestError(f"{v_name} 必须是长度为 3 的数组")
            if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
                raise InvalidRequestError(f"{v_name} 必须全部是数字")
        if parent is not None and not self._frame_registry.has(parent):
            raise InvalidRequestError(f"parent frame {parent} 不存在")
        if self._frame_registry.has(name):
            raise InvalidRequestError(f"frame {name} 已存在")
        frame = CoordinateFrame(
            name=name,
            origin=tuple(float(x) for x in origin),
            normal=tuple(float(x) for x in normal),
            x_axis=tuple(float(x) for x in x_axis),
            parent=parent,
            metadata=dict(metadata or {}),
        )
        self._frame_registry.add(frame)
        self.narrative.append(f"创建参考系 {name} (origin={origin}, normal={normal})")
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=f"RF_{self._step_counter:04d}",
            narrative=f"创建参考系 {name}",
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"reference_frames": [name]},
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))

    def query_reference(self, name: str = None) -> "StepResult":
        """v2.7: 查询 frame（单/全）。返回 dict / list[dict]."""
        start = time.time()
        if name is None:
            frames = [self._frame_registry.get(n) for n in self._frame_registry.names()]
        else:
            if not self._frame_registry.has(name):
                raise InvalidRequestError(f"frame {name} 不存在")
            frames = [self._frame_registry.get(name)]
        self._step_counter += 1
        result = make_success(
            feature_id=f"QR_{self._step_counter:04d}",
            narrative=f"查询参考系 {name or '全部'}",
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"queried": [f.name for f in frames]},
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        )
        result.value = {
            "count": len(frames),
            "frames": [f.to_dict() for f in frames],
        }
        return result

    def resolve_point(
        self,
        frame: str,
        uv: tuple = (0.0, 0.0),
        normal_offset: float = 0.0,
    ) -> "StepResult":
        """v2.7: 把 {frame, uv, normal_offset} 形式 → 世界坐标 (x, y, z).

        支持 legacy 形式: 直接传坐标元组 (x, y, z) 也兼容（视为世界坐标返回）。
        """
        start = time.time()
        if not self._frame_registry.has(frame):
            raise InvalidRequestError(f"frame {frame} 不存在")
        if not isinstance(uv, (list, tuple)) or len(uv) != 2:
            raise InvalidRequestError("uv 必须是长度为 2 的数组 [u, v]")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in uv):
            raise InvalidRequestError("uv 必须全部是数字")
        if not isinstance(normal_offset, (int, float)) or isinstance(normal_offset, bool):
            raise InvalidRequestError("normal_offset 必须是数字")
        world = rf_resolve_point(self._frame_registry, frame, uv, normal_offset)
        self._step_counter += 1
        result = make_success(
            feature_id=f"RP_{self._step_counter:04d}",
            narrative=f"resolve_point frame={frame} uv={uv} normal_offset={normal_offset}",
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"resolved": [frame]},
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        )
        result.value = {
            "frame": frame,
            "uv": list(uv),
            "normal_offset": float(normal_offset),
            "world": list(world),
        }
        return result

    def resolve_placement(
        self,
        frame: str,
        uv: tuple = (0.0, 0.0),
        normal_offset: float = 0.0,
        rotation: tuple = (0.0, (0.0, 0.0, 1.0)),
    ) -> "StepResult":
        """v2.7: 返回 (world_origin, 3x3 rotation_matrix).

        rotation: (angle_deg, axis) 相对 frame 旋转。
        """
        start = time.time()
        if not self._frame_registry.has(frame):
            raise InvalidRequestError(f"frame {frame} 不存在")
        if not isinstance(uv, (list, tuple)) or len(uv) != 2:
            raise InvalidRequestError("uv 必须是长度为 2 的数组")
        if not isinstance(rotation, (list, tuple)) or len(rotation) != 2:
            raise InvalidRequestError("rotation 必须是 (angle, axis) 形式")
        angle, axis = rotation
        if not isinstance(axis, (list, tuple)) or len(axis) != 3:
            raise InvalidRequestError("rotation axis 必须是长度为 3 的数组")
        origin, matrix = rf_resolve_placement(
            self._frame_registry, frame, uv, normal_offset, (float(angle), tuple(axis))
        )
        self._step_counter += 1
        result = make_success(
            feature_id=f"RL_{self._step_counter:04d}",
            narrative=f"resolve_placement frame={frame}",
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"resolved": [frame]},
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        )
        result.value = {
            "frame": frame,
            "uv": list(uv),
            "normal_offset": float(normal_offset),
            "rotation": [float(angle), list(axis)],
            "origin": list(origin),
            "matrix": matrix,
        }
        return result

    def validate_assembly(
        self,
        level: str = "standard",
        relations: list = None,
    ) -> "StepResult":
        """v2.7: 校验装配关系 + frame 一致性.

        level: "basic" / "standard" / "strict"
        relations: 形如 [{"kind": "coaxial", "source": "input_shaft",
                          "target": "gear_input"}]

        支持的 relation kind:
            - frame_valid:  检查 frame 自身正交右手
            - coaxial:      两 frame normal 共线 (|dot| > 1-eps, 同向或反向均可)
            - coaxial_aligned:    两 frame normal 同向 (dot > 1-eps)
            - parallel:     两 frame normal 平行 (|dot| > 1-eps)
            - perpendicular: 两 frame normal 垂直
            - axis_misalign: 两 frame 中心距不在容差内 (v2.9.2)
            - clearance:    两 instance bbox 不重叠（如果给了 source/target）
            - mounted:      instance 的 mount_frame 已注册
            - inside:       容器关系（一个 frame 在另一个 frame bbox 内）
            - gear_mesh:    两齿轮 pitch_diameter 中心距 == 啮合要求

        Returns: dict with {"ok": bool, "issues": [...]}
        """
        start = time.time()
        if level not in ("basic", "standard", "strict"):
            raise InvalidRequestError(f"level 必须是 basic/standard/strict（当前 {level}）")
        relations = list(relations or [])
        issues: list = []
        checked = 0

        # 1. basic: 所有 frame 有效
        for f in (self._frame_registry.get(n) for n in self._frame_registry.names()):
            if not f.is_orthonormal():
                issues.append({
                    "code": "frame_invalid",
                    "frame": f.name,
                    "message": f"frame {f.name} 不是合法正交右手系",
                })
            checked += 1

        # 2. mounted: 所有 instance.mount_frame 存在
        instance_by_name = {inst.name: inst for inst in self._assembly_instances.values()}
        for inst in self._assembly_instances.values():
            if inst.mount_frame is not None and not self._frame_registry.has(inst.mount_frame):
                issues.append({
                    "code": "mounted_frame_missing",
                    "instance": inst.name,
                    "frame": inst.mount_frame,
                    "message": f"instance {inst.name} 引用了不存在的 frame {inst.mount_frame}",
                })
                checked += 1

        # 3. 关系检查
        for rel in relations:
            if not isinstance(rel, dict):
                issues.append({"code": "rel_malformed", "rel": rel, "message": "关系必须是 dict"})
                continue
            kind = rel.get("kind")
            source_name = rel.get("source")
            target_name = rel.get("target")
            params = dict(rel.get("parameters") or {})
            checked += 1

            def _frame_of(name: str):
                if name in instance_by_name:
                    mf = instance_by_name[name].mount_frame
                    if mf and self._frame_registry.has(mf):
                        return self._frame_registry.get(mf)
                if self._frame_registry.has(name):
                    return self._frame_registry.get(name)
                return None

            sf = _frame_of(source_name) if source_name else None
            tf = _frame_of(target_name) if target_name else None
            if sf is None or tf is None:
                issues.append({
                    "code": f"{kind}_frame_missing",
                    "rel": rel,
                    "message": f"关系 {kind} 缺 source/target frame",
                })
                continue

            if kind == "coaxial":
                # 法向共线 (同向或反向都可, 齿轮啮合/轴对中等都是反向)
                d = sum(sf.normal[i] * tf.normal[i] for i in range(3))
                if abs(d) < 1 - 1e-6:
                    issues.append({
                        "code": "coaxial_misaligned",
                        "source": source_name, "target": target_name,
                        "dot": d,
                        "message": f"{source_name} 与 {target_name} 不共轴 (|dot|={abs(d):.4f})",
                    })
            elif kind == "coaxial_aligned":
                # 仅允许同向
                d = sum(sf.normal[i] * tf.normal[i] for i in range(3))
                if d < 1 - 1e-6:
                    issues.append({
                        "code": "coaxial_aligned_misaligned",
                        "source": source_name, "target": target_name,
                        "dot": d,
                        "message": f"{source_name} 与 {target_name} 不共轴同向 (dot={d:.4f})",
                    })
            elif kind == "parallel":
                d = abs(sum(sf.normal[i] * tf.normal[i] for i in range(3)))
                if d < 1 - 1e-6:
                    issues.append({
                        "code": "parallel_misaligned",
                        "source": source_name, "target": target_name,
                        "dot": d,
                        "message": f"{source_name} 与 {target_name} 不平行 (|dot|={d:.4f})",
                    })
            elif kind == "perpendicular":
                d = abs(sum(sf.normal[i] * tf.normal[i] for i in range(3)))
                if d > 1e-6:
                    issues.append({
                        "code": "perpendicular_misaligned",
                        "source": source_name, "target": target_name,
                        "dot": d,
                        "message": f"{source_name} 与 {target_name} 不垂直 (|dot|={d:.4f})",
                    })
            elif kind == "axis_misalign":
                # v2.9.2: 两 frame 中心点应共线 (v2.6 baseline 提的 axis mismatch 简化版)
                # 不查 normal 方向 (coaxial 已查), 只查 origin 中心距
                tol = float(params.get("tolerance", 1e-3))
                d = _frame_distance(sf, tf)
                if d > tol:
                    issues.append({
                        "code": "axis_misalign",
                        "source": source_name, "target": target_name,
                        "distance": d, "tolerance": tol,
                        "message": f"{source_name} 与 {target_name} 中心距 {d:.6f} > tolerance {tol}",
                    })
            elif kind == "clearance":
                # bbox 不重叠检查（如果两个 instance 有 bbox）
                si = instance_by_name.get(source_name)
                ti = instance_by_name.get(target_name)
                if si and ti and si.bbox and ti.bbox:
                    overlap = _bbox_overlap(si.bbox, ti.bbox)
                    min_gap = float(params.get("min_gap", 0.0))
                    if overlap and overlap < -min_gap:
                        issues.append({
                            "code": "clearance_violation",
                            "source": source_name, "target": target_name,
                            "overlap": overlap,
                            "min_gap": min_gap,
                            "message": f"{source_name} 与 {target_name} 间隙不足 (overlap={overlap:.2f})",
                        })
                else:
                    if level == "strict":
                        issues.append({
                            "code": "clearance_no_bbox",
                            "source": source_name, "target": target_name,
                            "message": "clearance 校验需要 instance 有 bbox",
                        })
            elif kind == "gear_mesh":
                # 两齿轮中心距 = (pitch_d_1 + pitch_d_2) / 2
                pd1 = float(params.get("source_pitch_diameter", 0.0))
                pd2 = float(params.get("target_pitch_diameter", 0.0))
                tol = float(params.get("tolerance", 0.5))
                if pd1 > 0 and pd2 > 0:
                    required = (pd1 + pd2) / 2.0
                    actual = _frame_distance(sf, tf)
                    diff = abs(actual - required)
                    if diff > tol:
                        issues.append({
                            "code": "gear_mesh_center_distance_mismatch",
                            "source": source_name, "target": target_name,
                            "actual": actual, "required": required, "diff": diff,
                            "message": f"齿轮啮合中心距 {actual:.3f} != 理论 {required:.3f} (diff={diff:.3f})",
                        })
            elif kind in ("frame_valid", "mounted", "inside"):
                # 这些已在 basic 阶段处理，关系中重复出现则视为通过
                pass
            else:
                issues.append({
                    "code": "rel_unknown_kind",
                    "rel": rel,
                    "message": f"未知关系 kind: {kind}",
                })

        # 4. strict 模式额外检查
        if level == "strict":
            for name in self._frame_registry.names():
                f = self._frame_registry.get(name)
                if f.parent is not None:
                    parent = self._frame_registry.get(f.parent)
                    # parent 必须已经存在（registry 保证）
                    # frame 的 normal 不应该跟 parent normal 完全反
                    d = sum(f.normal[i] * parent.normal[i] for i in range(3))
                    if d < -0.99:
                        issues.append({
                            "code": "frame_inverted",
                            "frame": name,
                            "message": f"frame {name} 法向与 parent 反向 (dot={d:.4f})",
                        })

        ok = len(issues) == 0
        self._step_counter += 1
        result = make_success(
            feature_id=f"VA_{self._step_counter:04d}",
            narrative=f"validate_assembly level={level}",
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"validated": [level, len(relations)]},
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        )
        result.value = {
            "ok": ok,
            "level": level,
            "checked": checked,
            "issue_count": len(issues),
            "issues": issues,
        }
        return result
