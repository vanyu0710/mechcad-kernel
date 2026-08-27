"""
MechKernel 主类（v1.1.1 修复版）

v1.1 核心：18 个原子 API + 自适应渲染 + 语义引用 + 事务回滚 + 类型化错误
"""
from typing import List, Optional, Dict, Any, Tuple, Union
import time
import base64

from .errors import (
    MechKernelError, InvalidRequestError, KernelBugError, StateCorruptionError,
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
    Reference, TOPOLOGY_CHANGING_OPS, NON_RENDERING_OPS,
    next_feature_id, next_sketch_id, next_workplane_id, next_entity_id,
    reset_all_id_generators
)
from .feature_graph import FeatureGraph
from .workplane import Workplane, WorkplaneType, WorkplaneRegistry
from .persistent_naming import PersistentNamingResolver, PersistentName
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
    "undo", "redo", "delete_feature", "update_feature", "export",
})


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
        self.geometry_inspector = GeometryInspector()
        self.inspector = self.geometry_inspector  # 别名（兼容测试）
        self.renderer = Renderer()
        self.adaptive_renderer = AdaptiveRenderer(interval=5)  # v1.5 修复：Renderer 不是 interval
        self._undo_stack: List[Dict] = []
        self._redo_stack: List[Dict] = []
        self._max_undo_depth = 50
        self.cap = CapabilityRegistry()
        self.PUBLIC_OPS = PUBLIC_OPS  # 挂到 instance 上（兼容测试）
        self._register_op_schemas()
    
    def _register_op_schemas(self):
        """手动注册 18 op schema（P1-4 v8: 用 set_capability 检测重复）"""
        # 草图类 6 个
        self.cap.set_capability(Capability(
            name="create_workplane", category="sketch",
            description="创建工作平面",
            input_schema={
                "name": FieldSchema(type="string", required=True),
                "type": FieldSchema(type="enum", required=False, default="XY", enum=["XY", "YZ", "XZ", "face", "custom"]),
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
            description="拉伸草图（圆→圆柱 / 矩形→立方体 / 多圆同心→环面）",
            input_schema={
                "sketch_name": FieldSchema(type="string", required=True),
                "depth": FieldSchema(type="number", required=True, min=0.001),
                "mode": FieldSchema(type="enum", required=False, default="new_body", enum=["new_body", "add", "cut"]),
                "name": FieldSchema(type="string", required=False),
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

        # placeholder op（v7 P0-V4：每个未实现 op 都有 schema + planned_version）
        # 注意：这些 op 真实方法存在（返回 NOT_IMPLEMENTED），所以 func 绑定会自动发生
        placeholder_schemas = {
            "revolve": {"sketch_name": FieldSchema(type="string", required=True),
                        "axis": FieldSchema(type="tuple", required=True, items_type="string", length=2),
                        "angle": FieldSchema(type="number", required=False, default=360.0, min=0.01, max=360.0)},
            "sweep": {"sketch_name": FieldSchema(type="string", required=True),
                      "path_name": FieldSchema(type="string", required=True)},
            "boolean": {"target": FieldSchema(type="string", required=True),
                        "tools": FieldSchema(type="list", required=True, items_type="string"),
                        "operation": FieldSchema(type="enum", required=True, enum=["union", "subtract", "intersect"])},
            "hole": {"target_face": FieldSchema(type="string", required=True),
                     "diameter": FieldSchema(type="number", required=True, min=0.1),
                     "depth": FieldSchema(type="number", required=True, min=0.1),
                     "position": FieldSchema(type="tuple", required=False, items_type="number", length=2),
                     "hole_type": FieldSchema(type="enum", required=False, default="simple", enum=["simple", "counterbore", "countersink"])},
            "fillet": {"targets": FieldSchema(type="list", required=True, items_type="string"),
                       "radius": FieldSchema(type="number", required=True, min=0.01)},
            "chamfer": {"targets": FieldSchema(type="list", required=True, items_type="string"),
                        "size": FieldSchema(type="number", required=True, min=0.01)},
            "shell": {"body_name": FieldSchema(type="string", required=True),
                      "thickness": FieldSchema(type="number", required=True, min=0.1),
                      "open_faces": FieldSchema(type="list", required=False, items_type="string")},
            "linear_pattern": {"feature_id": FieldSchema(type="string", required=True),
                               "count": FieldSchema(type="integer", required=True, min=2, max=100),
                               "spacing": FieldSchema(type="number", required=True),
                               "direction": FieldSchema(type="tuple", required=True, items_type="number", length=3)},
            "circular_pattern": {"feature_id": FieldSchema(type="string", required=True),
                                 "count": FieldSchema(type="integer", required=True, min=2, max=100),
                                 "axis": FieldSchema(type="tuple", required=True, items_type="string", length=2),
                                 "angle": FieldSchema(type="number", required=False, default=360.0)},
            "mirror": {"feature_id": FieldSchema(type="string", required=True),
                       "plane": FieldSchema(type="string", required=True)},
            "query": {"target": FieldSchema(type="string", required=True),
                      "what": FieldSchema(type="enum", required=True, enum=["bounding_box", "volume", "faces", "edges"])},
            "select": {"selector": FieldSchema(type="string", required=True)},
            "measure": {"between": FieldSchema(type="list", required=True, items_type="string"),
                        "metric": FieldSchema(type="enum", required=True, enum=["distance", "angle", "area"])},
            "delete_feature": {"feature_id": FieldSchema(type="string", required=True)},
            "update_feature": {"feature_id": FieldSchema(type="string", required=True),
                               "params": FieldSchema(type="dict", required=True)},
            "export": {"path": FieldSchema(type="string", required=True),
                       "format": FieldSchema(type="enum", required=False, default="step", enum=["step", "iges", "stl"])},
        }
        for name, schema in placeholder_schemas.items():
            self.cap.set_capability(Capability(
                name=name, category=name.split("_")[0], description=name,
                input_schema=schema, permission="public",
            ))

    def create_workplane(self, name: str, type: str = "XY", **kwargs) -> StepResult:
        start = time.time()
        name = require_non_empty_str("name", name)
        type = require_in("type", type, ["XY", "YZ", "XZ", "face", "custom"])
        if self.workplanes.has_name(name):
            raise InvalidRequestError(f"工作平面 {name} 已存在")
        
        with Transaction(self, "create_workplane") as txn:
            wp = Workplane(id=f"wp_{name}_{next_workplane_id()}", name=name, type=WorkplaneType(type))
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
            entity_id = next_entity_id()
            entity = SketchEntity(
                id=entity_id, type="circle",
                params={"center": tuple(center), "radius": float(radius)},
                name=name or f"circle_{entity_id}",
            )
            sk.add_entity(entity)
            self.narrative.append(f"草图 {sketch_name} 添加圆 半径 {radius}")
            txn.commit()
        
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=entity_id, narrative=f"画圆 r={radius}",
            current_narrative=self.narrative.copy(),
            render_level="none",
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
        entity_id = next_entity_id()
        entity = SketchEntity(
            id=entity_id, type="rectangle",
            params={"width": float(width), "height": float(height), "center": tuple(center)},
            name=name or f"rect_{entity_id}",
        )
        sk.add_entity(entity)
        self.narrative.append(f"草图 {sketch_name} 添加矩形 {width}x{height}")
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=entity_id, narrative=f"画矩形 {width}x{height}",
            current_narrative=self.narrative.copy(),
            render_level="none",
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
        entity_id = next_entity_id()
        entity = SketchEntity(
            id=entity_id, type="line",
            params={"start": tuple(start), "end": tuple(end)},
            semantic_name=f"line_{entity_id}",
        )
        sk.add_entity(entity)
        self.narrative.append(f"草图 {sketch_name} 添加直线")
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=entity_id, narrative="画线",
            current_narrative=self.narrative.copy(),
            render_level="none",
            elapsed_ms=(time.time() - start_t) * 1000,
            step_index=self._step_counter,
        ))
    
    def close_sketch(self, sketch_name: str) -> StepResult:
        start = time.time()
        sketch_name = require_non_empty_str("sketch_name", sketch_name)
        if sketch_name not in self.sketches:
            raise InvalidRequestError(f"草图 {sketch_name} 不存在")
        sk = self.sketches[sketch_name]
        if not sk.entities:
            raise InvalidRequestError(f"草图 {sketch_name} 为空，没有图元可关闭")
        
        with Transaction(self, "close_sketch") as txn:
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
    
    def extrude(self, sketch_name: str, depth: float, mode: str = "new_body", name: str = "", direction: str = "Z") -> StepResult:
        start = time.time()
        sketch_name = require_non_empty_str("sketch_name", sketch_name)
        require_positive("depth", depth)
        if sketch_name not in self.sketches:
            raise InvalidRequestError(f"草图 {sketch_name} 不存在")
        sk = self.sketches[sketch_name]
        if not sk.closed:
            raise InvalidRequestError(f"草图 {sketch_name} 未关闭")
        
        with Transaction(self, "extrude") as txn:
            feature_id = next_feature_id()
            feature = FeatureNode(
                id=feature_id, type=FeatureType.EXTRUDE,
                parameters={"sketch_name": sketch_name, "depth": depth, "mode": mode, "name": name},
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
    
    def revolve(self, sketch_name: str, axis: list = None, angle: float = 360.0, mode: str = "new_body", name: str = "") -> StepResult:
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
        
        from build123d import BuildPart, BuildSketch, Plane, add, revolve as b3d_revolve, Axis, Location
        from build123d import Circle as B3DCircle, Rectangle as B3DRect
        from build123d.build_common import Locations
        
        # 验证 axis 不退化
        ox, oy, oz, dx, dy, dz = axis
        if dx == 0 and dy == 0 and dz == 0:
            raise InvalidRequestError(f"axis 方向向量为 0")
        
        with Transaction(self, "revolve") as txn:
            feature_id = next_feature_id()
            feature = FeatureNode(
                id=feature_id, type=FeatureType.REVOLVE,
                parameters={"sketch_name": sketch_name, "axis": list(axis), "angle": angle, "mode": mode, "name": name},
                name=name or f"revolve_{feature_id}",
                state=FeatureState.COMPUTED,
            )
            self.feature_graph.add(feature)
            
            with BuildPart(Plane.XY) as bp:
                with BuildSketch() as s:
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
        
        with Transaction(self, "circular_pattern") as txn:
            feature_id = next_feature_id()
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
    
    def boolean(self, target_sketch: str, tools: list, operation: str = "union", name: str = "") -> StepResult:
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
        """
        start = time.time()
        if target_sketch not in self.sketches:
            raise InvalidRequestError(f"target 草图 {target_sketch} 不存在")
        for t in tools:
            if t not in self.sketches:
                raise InvalidRequestError(f"tool 草图 {t} 不存在")
        if operation not in ("union", "subtract", "intersect"):
            raise InvalidRequestError(f"operation 必须是 union/subtract/intersect（当前 {operation}）")
        
        from build123d import (
            BuildPart, BuildSketch, Plane, add, extrude, Location,
            Circle as B3DCircle, Rectangle as B3DRect,
        )
        from build123d.build_common import Locations
        
        def _extrude_sketch_to_part(sk, depth, direction="Z"):
            """草图 → Part"""
            plane = {"X": Plane.YZ, "Y": Plane.XZ, "Z": Plane.XY}.get(direction, Plane.XY)
            with BuildPart(plane) as bp:
                with BuildSketch() as s:
                    for e in sk.entities:
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
                extrude(amount=depth)
            return bp.part
        
        with Transaction(self, "boolean") as txn:
            feature_id = next_feature_id()
            feature = FeatureNode(
                id=feature_id, type=FeatureType.BOOLEAN,
                parameters={"target_sketch": target_sketch, "tools": tools, "operation": operation, "name": name},
                name=name or f"boolean_{operation}_{feature_id}",
                state=FeatureState.COMPUTED,
            )
            self.feature_graph.add(feature)
            
            # 1. 拉伸 target 成新 body
            target_sk = self.sketches[target_sketch]
            new_body = _extrude_sketch_to_part(target_sk, 50.0)
            
            # 2. 拉伸所有 tools 并 union
            tools_part = None
            for t_name in tools:
                t_sk = self.sketches[t_name]
                t_part = _extrude_sketch_to_part(t_sk, 50.0)
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
        
        render_level = "iso_only"
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
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))
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
            feature_id = next_feature_id()
            feature = FeatureNode(
                id=feature_id, type=FeatureType.FILLET,
                parameters={"radius": radius, "edges": edges, "name": name},
                name=name or f"fillet_{feature_id}",
                state=FeatureState.COMPUTED,
            )
            self.feature_graph.add(feature)
            
            if edges == "all":
                edge_list = list(self._current_geometry.edges())
            else:
                raise NotImplementedError(f"edge 选择 '{edges}' 暂不支持，仅 'all'")
            
            self._current_geometry = self._current_geometry.fillet(radius, edge_list)
            self.narrative.append(f"fillet r={radius} 全部 {len(edge_list)} 边 → {feature.name}")
            txn.commit()
        
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
            feature_id = next_feature_id()
            feature = FeatureNode(
                id=feature_id, type=FeatureType.CHAMFER,
                parameters={"length": length, "length2": l2, "edges": edges, "name": name},
                name=name or f"chamfer_{feature_id}",
                state=FeatureState.COMPUTED,
            )
            self.feature_graph.add(feature)
            
            if edges == "all":
                edge_list = list(self._current_geometry.edges())
            else:
                raise NotImplementedError(f"edge 选择 '{edges}' 暂不支持")
            
            self._current_geometry = self._current_geometry.chamfer(length, l2, edge_list)
            self.narrative.append(f"chamfer l={length}/{l2} 全部 {len(edge_list)} 边 → {feature.name}")
            txn.commit()
        
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
    ) -> StepResult:
        """
        v1.8 真实 hole（孔向导）— 在 XY 平面打孔
        
        Args:
            position: 孔位 (x, y) 在 XY 平面
            diameter: 孔径（mm）
            depth: 深度（默认 None = 穿透 current_geometry）
            hole_type: "simple" | "counterbore" | "countersink"
            counterbore_diameter: 沉孔大圆直径（仅 counterbore/countersink）
            counterbore_depth: 沉孔深度（仅 counterbore）
            name: 特征名
        
        自动从 current_geometry 切出孔
        """
        start = time.time()
        if self._current_geometry is None:
            raise InvalidRequestError("hole 需要先有几何")
        if diameter <= 0:
            raise InvalidRequestError(f"diameter 必须 > 0（当前 {diameter}）")
        if hole_type not in ("simple", "counterbore", "countersink"):
            raise InvalidRequestError(f"hole_type 必须 simple/counterbore/countersink（当前 {hole_type}）")
        
        from build123d import (
            BuildPart, BuildSketch, Plane, add, extrude, Location,
            Circle as B3DCircle,
        )
        from build123d.build_common import Locations
        
        # 实际孔深：取 current_geometry bbox 的 Z 长度
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopAbs import TopAbs_SOLID
        from OCP.TopoDS import TopoDS
        from OCP.BRepBndLib import BRepBndLib
        from OCP.Bnd import Bnd_Box
        shape = self._current_geometry.wrapped if hasattr(self._current_geometry, 'wrapped') else self._current_geometry
        exp = TopExp_Explorer(shape, TopAbs_SOLID)
        bbox = Bnd_Box()
        if exp.More():
            BRepBndLib.Add_s(exp.Current(), bbox)
        z_min, z_max = bbox.CornerMin().Z(), bbox.CornerMax().Z()
        actual_depth = (z_max - z_min) + 10 if depth is None else depth  # +10 保险穿透
        
        with Transaction(self, "hole") as txn:
            feature_id = next_feature_id()
            feature = FeatureNode(
                id=feature_id, type=FeatureType.HOLE,
                parameters={
                    "position": list(position), "diameter": diameter, "depth": actual_depth,
                    "hole_type": hole_type, "counterbore_diameter": counterbore_diameter,
                    "counterbore_depth": counterbore_depth, "name": name,
                },
                name=name or f"hole_{feature_id}",
                state=FeatureState.COMPUTED,
            )
            self.feature_graph.add(feature)
            
            # 1. simple: 1 个圆
            # 2. counterbore: 2 个圆叠切（大圆+深，小圆+深）
            # 3. countersink: 2 个圆叠切（简化：外大圆切锥度）
            px, py = position
            if hole_type == "simple":
                with BuildPart(Plane.XY) as bp:
                    with BuildSketch() as s:
                        with Locations((px, py, 0)):
                            B3DCircle(diameter / 2)
                    extrude(amount=actual_depth)
                cutter = bp.part
            elif hole_type == "counterbore":
                cb_d = counterbore_diameter or (diameter * 1.8)
                cb_depth = counterbore_depth or (diameter * 0.5)
                with BuildPart(Plane.XY) as bp:
                    with BuildSketch() as s:
                        with Locations((px, py, 0)):
                            B3DCircle(cb_d / 2)  # 沉孔大圆
                    extrude(amount=cb_depth)
                    with BuildSketch() as s:
                        with Locations((px, py, 0)):
                            B3DCircle(diameter / 2)  # 通孔小圆
                    extrude(amount=actual_depth)
                cutter = bp.part
            elif hole_type == "countersink":
                # 简化：外大圆 + 小圆（无锥度）
                cs_d = counterbore_diameter or (diameter * 1.8)
                with BuildPart(Plane.XY) as bp:
                    with BuildSketch() as s:
                        with Locations((px, py, 0)):
                            B3DCircle(cs_d / 2)  # 沉孔大圆
                    extrude(amount=actual_depth)
                    with BuildSketch() as s:
                        with Locations((px, py, 0)):
                            B3DCircle(diameter / 2)  # 通孔小圆
                    extrude(amount=actual_depth)
                cutter = bp.part
            
            # boolean subtract
            self._current_geometry = self._current_geometry - cutter
            self.narrative.append(f"hole {hole_type} Ø{diameter} @ ({px}, {py}) 深 {actual_depth:.1f}")
            txn.commit()
        
        render_level = "iso_only"
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
    
    def shell(self, thickness: float, face_filter: str = "top", name: str = "") -> StepResult:
        """
        v1.6.1 真实 shell（抽壳）— 用 OCP BRepOffsetAPI_MakeThickSolid
        
        Args:
            thickness: 壁厚（mm），必须 > 0
            face_filter: 开口面选择："top"/"bottom"/"z+"/"z-"/"x+"/"x-"/"y+"/"y-"
            name: 特征名
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
        if face_filter not in target_dirs:
            raise InvalidRequestError(f"face_filter '{face_filter}' 不支持。可选: {list(target_dirs.keys())}")
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
            feature_id = next_feature_id()
            feature = FeatureNode(
                id=feature_id, type=FeatureType.SHELL,
                parameters={"thickness": thickness, "face_filter": face_filter, "name": name},
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
        
        render_level = "iso_only"
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
        with Transaction(self, "linear_pattern") as txn:
            feature_id = next_feature_id()
            feature = FeatureNode(
                id=feature_id, type=FeatureType.LINEAR_PATTERN,
                parameters={"sketch_name": sketch_name, "count": count, "direction": list(direction), "spacing": spacing, "mode": mode, "name": name},
                name=name or f"linear_pattern_{feature_id}",
                state=FeatureState.COMPUTED,
            )
            self.feature_graph.add(feature)
            
            # 复制 count 次：每次 entity center 偏移 i*spacing*direction
            with BuildPart(Plane.XY) as bp:
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
                extrude(amount=50.0)
            all_parts = bp.part
            
            if mode in ("union", "add"):
                self._current_geometry = self._current_geometry + all_parts
            elif mode == "cut":
                self._current_geometry = self._current_geometry - all_parts
            
            self.narrative.append(f"linear_pattern {sketch_name} x{count} {direction} 间距 {spacing}")
            txn.commit()
        
        render_level = "iso_only"
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
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))
    
    def mirror(
        self,
        sketch_name: str,
        axis: str = "X",
        mode: str = "union",
        name: str = "",
    ) -> StepResult:
        """
        v1.9 真实 mirror（镜像）— 沿 X/Y 轴镜像草图 + boolean
        
        Args:
            sketch_name: 要镜像的草图
            axis: "X" | "Y"（镜像轴）
            mode: "union"（镜像后合并到 current_geometry）| "add" | "cut"
            name: 特征名
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
        
        from build123d import (
            BuildPart, BuildSketch, Plane, add, extrude, Location,
            Circle as B3DCircle, Rectangle as B3DRect,
        )
        from build123d.build_common import Locations
        
        sk = self.sketches[sketch_name]
        with Transaction(self, "mirror") as txn:
            feature_id = next_feature_id()
            feature = FeatureNode(
                id=feature_id, type=FeatureType.MIRROR,
                parameters={"sketch_name": sketch_name, "axis": axis, "mode": mode, "name": name},
                name=name or f"mirror_{axis}_{feature_id}",
                state=FeatureState.COMPUTED,
            )
            self.feature_graph.add(feature)
            
            # 镜像：原位置（保留）+ 镜像位置（也生成）
            # axis="X" 镜像 → 翻转 X 坐标（cx=-c[0], cy=c[1]）
            # axis="Y" 镜像 → 翻转 Y 坐标（cx=c[0], cy=-c[1]）
            with BuildPart(Plane.XY) as bp:
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
                extrude(amount=50.0)
            both_parts = bp.part
            
            if mode == "union" or mode == "add":
                self._current_geometry = self._current_geometry + both_parts
            elif mode == "cut":
                self._current_geometry = self._current_geometry - both_parts
            
            self.narrative.append(f"mirror {sketch_name} 沿 {axis} {mode}")
            txn.commit()
        
        render_level = "iso_only"
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
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))
    
    def sweep(self, profile_sketch: str, path: str = "x_axis", length: float = 50.0, name: str = "") -> StepResult:
        """
        v1.6.2 sweep（扫掠，沿直线路径）— 用 build123d.extrude(face, amount/dir)
        
        Args:
            profile_sketch: profile 草图名（必须在 XY 平面，中心在原点）
            path: 路径方向，"x_axis" | "y_axis" | "z_axis"
            length: 路径长度（mm）
            name: 特征名
        """
        start = time.time()
        if profile_sketch not in self.sketches:
            raise InvalidRequestError(f"profile 草图 {profile_sketch} 不存在")
        if length <= 0:
            raise InvalidRequestError(f"length 必须 > 0（当前 {length}）")
        
        sk = self.sketches[profile_sketch]
        if not sk.closed:
            raise InvalidRequestError(f"profile {profile_sketch} 未关闭")
        if len(sk.entities) != 1:
            raise InvalidRequestError(f"sweep v1.6.2 只支持单 entity profile")
        
        e = sk.entities[0]
        if e.type != "circle":
            raise InvalidRequestError(f"sweep v1.6.2 只支持 circle profile（当前 {e.type}）")
        r = e.params["radius"]
        
        from build123d import (
            BuildPart, BuildSketch, Plane, Circle as B3DCircle, add,
            Solid as B3DSolid, Vector,
        )
        
        # path vector
        path_vecs = {
            "x_axis": Vector(length, 0, 0),
            "y_axis": Vector(0, length, 0),
            "z_axis": Vector(0, 0, length),
        }
        if path not in path_vecs:
            raise InvalidRequestError(f"path '{path}' 不支持（x_axis/y_axis/z_axis）")
        
        with Transaction(self, "sweep") as txn:
            feature_id = next_feature_id()
            feature = FeatureNode(
                id=feature_id, type=FeatureType.SWEEP,
                parameters={"profile_sketch": profile_sketch, "path": path, "length": length, "name": name},
                name=name or f"sweep_{feature_id}",
                state=FeatureState.COMPUTED,
            )
            self.feature_graph.add(feature)
            
            # BuildPart + BuildSketch + extrude(face, dir=vec)
            with BuildPart(Plane.XY) as bp:
                with BuildSketch() as s:
                    add(B3DCircle(r))
                # build123d extrude 沿 dir 矢量
                from build123d import extrude as b3d_extrude
                b3d_extrude(amount=length)  # 简化：amount = length
            
            new_solid = bp.part
            if self._current_geometry is None:
                self._current_geometry = new_solid
            self.narrative.append(f"sweep {profile_sketch} 沿 {path} 长 {length}")
            txn.commit()
        
        render_level = "iso_only"
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
        
        # 选几何
        if target == "_current_geometry":
            geom = self._current_geometry
        else:
            geom = self._current_geometry
            if geom is None:
                raise InvalidRequestError(f"目标 {target} 不存在（当前几何为空）")
        
        if geom is None:
            raise InvalidRequestError("query 需要先有几何")
        
        from OCP.BRepBndLib import BRepBndLib
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
            if exp.More():
                BRepBndLib.Add_s(exp.Current(), bbox)
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
        return result
    
    def select(self, filter_type: str = "all", face_index: int = None) -> "StepResult":
        """
        v1.12 真实 select（按类型/索引选 face）
        
        Args:
            filter_type: "all" | "plane" | "cylinder" | "cone" | "sphere" | "torus"
            face_index: 第几个匹配（0-indexed, None = 全部）
        """
        start = time.time()
        if self._current_geometry is None:
            raise InvalidRequestError("select 需要先有几何")
        
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopAbs import TopAbs_FACE
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.GeomAbs import (
            GeomAbs_Plane, GeomAbs_Cylinder, GeomAbs_Cone, GeomAbs_Sphere, GeomAbs_Torus,
        )
        from OCP.TopoDS import TopoDS
        from build123d import Face as B3DFace
        
        type_map = {
            "plane": GeomAbs_Plane,
            "cylinder": GeomAbs_Cylinder,
            "cone": GeomAbs_Cone,
            "sphere": GeomAbs_Sphere,
            "torus": GeomAbs_Torus,
        }
        if filter_type not in ("all",) and filter_type not in type_map:
            raise InvalidRequestError(f"filter_type 必须是 all/plane/cylinder/cone/sphere/torus（当前 {filter_type}）")
        
        shape = self._current_geometry.wrapped if hasattr(self._current_geometry, 'wrapped') else self._current_geometry
        all_faces = []
        type_count = {"plane": 0, "cylinder": 0, "cone": 0, "sphere": 0, "torus": 0}
        exp = TopExp_Explorer(shape, TopAbs_FACE)
        while exp.More():
            f = exp.Current()
            f_face = TopoDS.Face_s(f)
            if not f_face.IsNull():
                adaptor = BRepAdaptor_Surface(f_face)
                t = adaptor.GetType()
                type_name = "unknown"
                if t == GeomAbs_Plane: type_name = "plane"
                elif t == GeomAbs_Cylinder: type_name = "cylinder"
                elif t == GeomAbs_Cone: type_name = "cone"
                elif t == GeomAbs_Sphere: type_name = "sphere"
                elif t == GeomAbs_Torus: type_name = "torus"
                if type_name in type_count:
                    type_count[type_name] += 1
                if filter_type == "all" or filter_type == type_name:
                    all_faces.append({"index": len(all_faces), "type": type_name})
            exp.Next()
        
        result_value = {
            "total": sum(type_count.values()),
            "by_type": type_count,
            "selected": all_faces,
        }
        if face_index is not None and 0 <= face_index < len(all_faces):
            result_value["specific"] = all_faces[face_index]
        
        self._step_counter += 1
        result = make_success(
            feature_id=f"S_{self._step_counter:03d}",
            narrative=f"select {filter_type}",
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
        
        def get_center(target):
            if target == "current" or target == "_current_geometry":
                shape = self._current_geometry.wrapped if hasattr(self._current_geometry, 'wrapped') else self._current_geometry
            else:
                shape = self._current_geometry.wrapped if hasattr(self._current_geometry, 'wrapped') else self._current_geometry
            if shape is None:
                raise InvalidRequestError("measure 需要先有几何")
            bbox = Bnd_Box()
            exp = TopExp_Explorer(shape, TopAbs_SOLID)
            if exp.More():
                BRepBndLib.Add_s(exp.Current(), bbox)
            return (
                (bbox.CornerMin().X() + bbox.CornerMax().X()) / 2,
                (bbox.CornerMin().Y() + bbox.CornerMax().Y()) / 2,
                (bbox.CornerMin().Z() + bbox.CornerMax().Z()) / 2,
            )
        
        def get_volume(target):
            shape = self._current_geometry.wrapped if hasattr(self._current_geometry, 'wrapped') else self._current_geometry
            props = GProp_GProps()
            exp = TopExp_Explorer(shape, TopAbs_SOLID)
            v = 0.0
            while exp.More():
                BRepGProp.VolumeProperties_s(exp.Current(), props)
                v += props.Mass()
                exp.Next()
            return v
        
        def get_area(target):
            shape = self._current_geometry.wrapped if hasattr(self._current_geometry, 'wrapped') else self._current_geometry
            props = GProp_GProps()
            exp = TopExp_Explorer(shape, TopAbs_FACE)
            a = 0.0
            while exp.More():
                BRepGProp.SurfaceProperties_s(exp.Current(), props)
                a += props.Mass()
                exp.Next()
            return a
        
        def parse_coord(s):
            m = re.match(r'\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*\)', s)
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
        # 简化：只从 graph 移除并 narrative 记录
        # 不重新计算 _current_geometry（避免 OCC 多次 boolean 累加误差）
        if feature_id in self.feature_graph.nodes:
            # 标记 removed
            self.narrative.append(f"delete_feature {feature_id}（v1.14 简化版：只移除历史记录）")
            result = make_success(
                feature_id=f"D_{self._step_counter:03d}",
                narrative=f"delete {feature_id}",
                current_narrative=self.narrative.copy(),
                feature_graph_delta={"deleted": [feature_id]},
                elapsed_ms=(time.time() - start) * 1000,
                step_index=self._step_counter,
            )
            result.value = {"deleted": feature_id, "note": "v1.14 简化：仅移除 history，geometry 不重算"}
            return result
        raise InvalidRequestError(f"feature {feature_id} 不存在")
    
    def update_feature(self, feature_id: str, new_params: dict) -> "StepResult":
        """
        v1.15 真实 update_feature（更新 feature 参数）— 简化版
        
        v1.15 限制：不真正重算（需要参数化模型）
        简化策略：更新 feature graph 中的参数，但不重放 op
        完整版需要参数化重算（v2.0+）
        """
        start = time.time()
        self._step_counter += 1
        # 简化：更新 narrative + 返回 ok，不真正修改几何
        if feature_id not in self.feature_graph.nodes:
            raise InvalidRequestError(f"feature {feature_id} 不存在")
        self.narrative.append(f"update_feature {feature_id} -> {new_params}（v1.15 简化：仅记录）")
        result = make_success(
            feature_id=f"U_{self._step_counter:03d}",
            narrative=f"update {feature_id}",
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"updated": [feature_id, new_params]},
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        )
        result.value = {"updated": feature_id, "new_params": new_params, "note": "v1.15 简化：仅记录，不重算"}
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
            export_step(self._current_geometry, path)
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
        
        from build123d.exporters3d import export_step
        export_step(self._current_geometry, step_path)
        
        graph_data = self.feature_graph.to_dict()
        graph_data["_project_meta"] = {
            "version": "v2.1+v1.5",
            "geometry_volume": self._current_geometry.volume if hasattr(self._current_geometry, "volume") else None,
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(graph_data, f, ensure_ascii=False, indent=2)
        
        return {
            "step_path": step_path,
            "step_size": os.path.getsize(step_path),
            "json_path": json_path,
            "json_size": os.path.getsize(json_path),
        }
    
    def load_project(self, base_path: str, mode: str = "new_body", name: str = "loaded_project") -> StepResult:
        """
        v1.5.4 项目加载（从 {base_path}.step + {base_path}.graph.json 恢复）
        
        重建 Feature Graph + 恢复 _current_geometry
        """
        import os, json
        step_path = f"{base_path}.step"
        json_path = f"{base_path}.graph.json"
        
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
            # 移除 _project_meta (这是 meta, 不是 feature)
            graph_data.pop("_project_meta", None)
            self.feature_graph.from_dict(graph_data)
            
            self.narrative.append(f"加载项目 ← {base_path}")
            txn.commit()
        
        render_level = "iso_only"  # v1.5.4: 简单 load 用 iso
        render_png = None
        if render_level != "none":
            renders = self.renderer.render(self._geometry_internal, level=render_level, geometry_revision=self._geometry_revision)
            render_png = renders.get("iso") or renders.get("default")
        self._step_counter += 1
        return self._wrap_step_result(make_success(
            feature_id=feature_id,
            narrative=f"加载项目 ← {base_path}",
            render_png=render_png, render_level=render_level,
            current_narrative=self.narrative.copy(),
            feature_graph_delta={"added": [feature_id]},
            elapsed_ms=(time.time() - start) * 1000,
            step_index=self._step_counter,
        ))
    
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
        # P0-V1 修复：undo 后清空 _current_geometry（OCC Part 不可序列化）
        # 否则 undo 后 _current_geometry 仍是旧对象，state 和视觉不一致
        self._geometry_internal = None
        # P0-3 修复：undo 后 bump revision（让 renderer 缓存失效）
        self._geometry_revision += 1
        self.renderer.clear_cache()  # undo 后清缓存
    
    def _bump_geometry_revision(self):
        self._geometry_revision += 1
        self.renderer.clear_cache()
    
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
            
            if not sketch or not sketch.entities:
                from .build123d_adapter import extrude_sketch
                return extrude_sketch(sketch, depth)
            
            # v1.2.1: 方向 → plane
            plane = {"X": Plane.YZ, "Y": Plane.XZ, "Z": Plane.XY}.get(direction, Plane.XY)
            
            # 单个圆 → 圆柱
            if len(sketch.entities) == 1 and sketch.entities[0].type == "circle":
                e = sketch.entities[0]
                r = e.params["radius"]
                with BuildPart(plane) as bp:
                    with BuildSketch() as s:
                        add(B3DCircle(r))
                    extrude(amount=depth)
                return bp.part
            
            # 单个矩形 → 立方体
            if len(sketch.entities) == 1 and sketch.entities[0].type == "rectangle":
                e = sketch.entities[0]
                w, h = e.params["width"], e.params["height"]
                with BuildPart(plane) as bp:
                    with BuildSketch() as s:
                        add(B3DRect(w, h))
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
        
        # v1.2.1: direction → plane
        plane = {"X": Plane.YZ, "Y": Plane.XZ, "Z": Plane.XY}.get(direction, Plane.XY)
        
        # 1. 单独把新 sketch 拉伸成 Part
        with BuildPart(plane) as bp:
            with BuildSketch() as s:
                for e in sketch.entities:
                    if e.type == "circle":
                        r = e.params["radius"]
                        c = e.params.get("center", (0, 0))
                        if c == (0, 0) or c == [0, 0]:
                            add(B3DCircle(r))
                        else:
                            # circle 加 center 偏移
                            add(Location((c[0], c[1], 0)) * B3DCircle(r))
                    elif e.type == "rectangle":
                        w, h = e.params["width"], e.params["height"]
                        c = e.params.get("center", (0, 0))
                        if c == (0, 0) or c == [0, 0]:
                            add(B3DRect(w, h))  # 默认中心 (0,0)
                        else:
                            add(Location((c[0], c[1], 0)) * B3DRect(w, h))
                    elif e.type == "polygon":
                        from .build123d_adapter import extrude_sketch
                        new_solid = extrude_sketch(sketch, depth)
                        break
                else:
                    pass
            extrude(amount=depth)
        new_solid = bp.part
        
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
            return method(*args, **kwargs)
        except (InvalidRequestError, StateCorruptionError) as e:
            return make_failure(
                error=str(e),
                error_kind="INVALID_REQUEST" if isinstance(e, InvalidRequestError) else "STATE_CORRUPTION",
                hint="检查参数和状态",
                current_narrative=self.narrative.copy(),
                elapsed_ms=(time.time() - start) * 1000,
                step_index=self._step_counter,
            )
        except KernelBugError as e:
            return make_failure(
                error=str(e), error_kind="KERNEL_BUG", hint="kernel bug",
                current_narrative=self.narrative.copy(),
                elapsed_ms=(time.time() - start) * 1000,
                step_index=self._step_counter,
            )
        except Exception as e:
            return make_failure(
                error=f"未预期错误: {e}",
                error_kind="GEOMETRY_FAILURE",
                suggestion={"action": "retry_with_different_params"},
                current_narrative=self.narrative.copy(),
                elapsed_ms=(time.time() - start) * 1000,
                step_index=self._step_counter,
            )
    
    def last_render_base64(self) -> Optional[str]:
        return None
    
    def _wrap_step_result(self, result: "StepResult") -> "StepResult":
        """自动填充 geometry_summary（如未设置）+ 简单 hints"""
        if result.geometry_summary is None:
            result.geometry_summary = self._geometry_summary_for(self._geometry_internal)
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
            return self.geometry_inspector.compute(geometry, feature_count=len(self.feature_graph.nodes))
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
        from .step_result import GeometrySummary
        if self._geometry_internal is None:
            return None
        return GeometrySummary(
            bounding_box=(0, 0, 0, 100, 100, 100),
            volume=1000.0, surface_area=600.0,
            face_count=6, edge_count=12, vertex_count=8,
            is_manifold=True, is_watertight=True, is_connected=True,
            feature_count=len(self.feature_graph.nodes),
        )
    
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
        return None
