"""
MechKernel Feature 数据结构

P4 原则：所有几何引用用 Reference（语义命名），不用裸索引。
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Dict, Any, Tuple
import time


class FeatureType(str, Enum):
    """所有 Feature 类型（18 个 API 对应）"""
    # 草图类
    WORKPLANE = "workplane"
    SKETCH = "sketch"
    SKETCH_CIRCLE = "sketch_circle"
    SKETCH_RECTANGLE = "sketch_rectangle"
    SKETCH_LINE = "sketch_line"
    
    # 主体类
    EXTRUDE = "extrude"
    REVOLVE = "revolve"
    SWEEP = "sweep"
    BOOLEAN = "boolean"
    
    # I/O 类 (v1.5)
    EXPORT = "export"
    IMPORT_STEP = "import_step"
    
    # 细节类
    HOLE = "hole"
    FILLET = "fillet"
    CHAMFER = "chamfer"
    SHELL = "shell"
    
    # 复用类
    LINEAR_PATTERN = "linear_pattern"
    CIRCULAR_PATTERN = "circular_pattern"
    MIRROR = "mirror"
    
    # 其他
    OFFSET_FACE = "offset_face"
    ASSEMBLY = "assembly"  # v2.0: 装配（组合多个零件）


class FeatureState(str, Enum):
    PENDING = "pending"
    COMPUTED = "computed"
    FAILED = "failed"
    SUPPRESSED = "suppressed"


class ConstraintStatus(str, Enum):
    """状态由草图求解器计算，不代表约束本身的类型。"""
    SOLVED = "solved"
    UNDER_CONSTRAINED = "under_constrained"
    OVER_CONSTRAINED = "over_constrained"
    CONFLICT = "conflict"
    NOT_SOLVED = "not_solved"


# Topology-changing operations（决定是否必渲染）
TOPOLOGY_CHANGING_OPS = frozenset({
    FeatureType.EXTRUDE,
    FeatureType.REVOLVE,
    FeatureType.SWEEP,
    FeatureType.BOOLEAN,
    FeatureType.HOLE,
    FeatureType.FILLET,
    FeatureType.CHAMFER,
    FeatureType.SHELL,
    FeatureType.LINEAR_PATTERN,
    FeatureType.CIRCULAR_PATTERN,
    FeatureType.MIRROR,
    FeatureType.OFFSET_FACE,
    FeatureType.ASSEMBLY,
})


# Operations that should NOT render (structure-only)
NON_RENDERING_OPS = frozenset({
    FeatureType.WORKPLANE,
    FeatureType.SKETCH,
    FeatureType.SKETCH_CIRCLE,
    FeatureType.SKETCH_RECTANGLE,
    FeatureType.SKETCH_LINE,
})


@dataclass(frozen=True, eq=True)
class Reference:
    """
    语义引用（P4 原则，P0-3 修复：用 frozen=True）。
    
    不再用 feat_001.face_2 这种易变引用，
    改用 ("top_face", "main_body") 这种语义命名。
    
    frozen=True 让对象不可变，自动生成正确的 __hash__ 和 __eq__，
    避免自定义 hash/eq 与 dataclass 默认行为不一致的问题。
    """
    kind: str                                  # "feature" | "sketch" | "workplane" | "face" | "edge" | "vertex"
    semantic_name: str                         # "top_face" | "main_body" | ...
    owner_feature_id: Optional[str] = None      # 归属 feature（用于重解析）
    
    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "semantic_name": self.semantic_name,
            "owner_feature_id": self.owner_feature_id,
        }
    
    @staticmethod
    def feature(name: str, owner: Optional[str] = None) -> "Reference":
        return Reference(kind="feature", semantic_name=name, owner_feature_id=owner)
    
    @staticmethod
    def face(name: str, owner: Optional[str] = None) -> "Reference":
        return Reference(kind="face", semantic_name=name, owner_feature_id=owner)
    
    @staticmethod
    def edge(name: str, owner: Optional[str] = None) -> "Reference":
        return Reference(kind="edge", semantic_name=name, owner_feature_id=owner)
    
    @staticmethod
    def vertex(name: str, owner: Optional[str] = None) -> "Reference":
        return Reference(kind="vertex", semantic_name=name, owner_feature_id=owner)


@dataclass
class SketchEntity:
    """草图中的基本图元"""
    id: str
    type: str                                  # "line" | "circle" | "arc" | "rectangle" | "polygon" | "point"
    params: Dict[str, Any] = field(default_factory=dict)
    name: str = ""


@dataclass
class Constraint:
    """二维草图约束，references 使用稳定的实体 ID 而不是数组索引。"""
    id: str
    type: str
    references: List[Dict[str, Any]] = field(default_factory=list)
    value: Optional[float] = None
    parameter_name: str = ""
    name: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "references": self.references,
            "value": self.value,
            "parameter_name": self.parameter_name,
            "name": self.name,
        }

    @staticmethod
    def from_dict(data: dict) -> "Constraint":
        return Constraint(
            id=data["id"],
            type=data["type"],
            references=list(data.get("references", [])),
            value=data.get("value"),
            parameter_name=data.get("parameter_name", ""),
            name=data.get("name", ""),
        )


@dataclass
class Sketch:
    """草图数据（M0 阶段，物理几何单独存放）"""
    id: str
    name: str
    workplane_name: str
    entities: List[SketchEntity] = field(default_factory=list)
    constraints: List[Constraint] = field(default_factory=list)
    closed: bool = False
    solver_status: ConstraintStatus = ConstraintStatus.NOT_SOLVED
    dof: int = 0
    conflicting_constraints: List[str] = field(default_factory=list)
    solver_residual: float = 0.0
    solver_iterations: int = 0
    _build123d_object: Optional[Any] = field(default=None, repr=False, compare=False)
    
    def add_entity(self, entity: "SketchEntity") -> None:
        self.entities.append(entity)
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "workplane_name": self.workplane_name,
            "entities": [{"id": e.id, "type": e.type, "params": e.params, "name": e.name} for e in self.entities],
            "constraints": [c.to_dict() for c in self.constraints],
            "closed": self.closed,
            "solver_status": self.solver_status.value if hasattr(self.solver_status, "value") else str(self.solver_status),
            "dof": self.dof,
            "conflicting_constraints": list(self.conflicting_constraints),
            "solver_residual": self.solver_residual,
            "solver_iterations": self.solver_iterations,
        }
    
    @staticmethod
    def from_dict(d: dict) -> "Sketch":
        sk = Sketch(id=d["id"], name=d["name"], workplane_name=d["workplane_name"], closed=d.get("closed", False))
        for e in d.get("entities", []):
            sk.entities.append(SketchEntity(id=e["id"], type=e["type"], params=e.get("params", {}), name=e.get("name", "")))
        for c in d.get("constraints", []):
            sk.constraints.append(Constraint.from_dict(c))
        status = d.get("solver_status", ConstraintStatus.NOT_SOLVED.value)
        try:
            sk.solver_status = ConstraintStatus(status)
        except ValueError:
            sk.solver_status = ConstraintStatus.NOT_SOLVED
        sk.dof = int(d.get("dof", 0))
        sk.conflicting_constraints = list(d.get("conflicting_constraints", []))
        sk.solver_residual = float(d.get("solver_residual", 0.0))
        sk.solver_iterations = int(d.get("solver_iterations", 0))
        return sk


@dataclass
class FeatureNode:
    """
    Feature 树节点。
    
    v1.1 改动：进入 Feature Graph（DAG），不再是孤立 list。
    """
    id: str
    type: FeatureType
    name: str = ""                              # 用户给的语义名（必须）
    parameters: Dict[str, Any] = field(default_factory=dict)
    references: List[Reference] = field(default_factory=list)
    
    state: FeatureState = FeatureState.PENDING
    parent_id: Optional[str] = None
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    
    # 内部
    _geometry: Optional[Any] = field(default=None, repr=False, compare=False)
    _cached_summary: Optional[Any] = field(default=None, repr=False, compare=False)
    
    def is_topology_changing(self) -> bool:
        """是否拓扑变化操作（决定是否必渲染）"""
        return self.type in TOPOLOGY_CHANGING_OPS
    
    def is_non_rendering(self) -> bool:
        """是否不渲染操作"""
        return self.type in NON_RENDERING_OPS

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type.value if hasattr(self.type, "value") else str(self.type),
            "name": self.name,
            "parameters": self.parameters,
            "references": [r.to_dict() for r in self.references],
            "state": self.state.value if hasattr(self.state, "value") else str(self.state),
            "parent_id": self.parent_id,
            "error": self.error,
            "timestamp": self.timestamp,
        }

    @staticmethod
    def from_dict(d: dict) -> "FeatureNode":
        from .features import FeatureType, FeatureState
        return FeatureNode(
            id=d["id"],
            type=FeatureType(d["type"]) if isinstance(d["type"], str) else d["type"],
            name=d.get("name", ""),
            parameters=d.get("parameters", {}),
            references=[],  # 简化：暂时不还原
            state=FeatureState(d["state"]) if isinstance(d["state"], str) else d.get("state", FeatureState.DONE),
            parent_id=d.get("parent_id"),
            error=d.get("error"),
            timestamp=d.get("timestamp", 0.0),
        )


# ID 生成器（v2.11: 每个 MechKernel 实例私有一组；模块级默认组仅供外部兼容）
class _IdGenerator:
    def __init__(self, prefix: str = "feat"):
        self.prefix = prefix
        self.counter = 0

    def next(self) -> str:
        self.counter += 1
        return f"{self.prefix}_{self.counter:04d}"

    def reset(self):
        self.counter = 0


class IdGeneratorSet:
    """一个 kernel 实例私有的 ID 生成器组。

    v2.11 之前是模块级全局单例：同进程多个 MechKernel 实例共享计数器，
    且 _replay() 的全局重置会互相污染。现在每个实例持有独立一组。
    """

    def __init__(self):
        self.feature = _IdGenerator("F")
        self.sketch = _IdGenerator("SK")
        self.workplane = _IdGenerator("WP")
        self.entity = _IdGenerator("E")
        self.constraint = _IdGenerator("C")

    def next_feature_id(self) -> str:
        return self.feature.next()

    def next_sketch_id(self) -> str:
        return self.sketch.next()

    def next_workplane_id(self) -> str:
        return self.workplane.next()

    def next_entity_id(self) -> str:
        return self.entity.next()

    def next_constraint_id(self) -> str:
        return self.constraint.next()

    def reset(self):
        for gen in (self.feature, self.sketch, self.workplane, self.entity, self.constraint):
            gen.reset()

    def seed_from_history(self, history: list) -> None:
        """Seed replay IDs so histories created after another kernel stay stable."""
        import re
        first_seen = {}
        for entry in history:
            value = entry.get("feature_id") if isinstance(entry, dict) else None
            if not isinstance(value, str):
                continue
            match = re.match(r"^(F|E|C)_(\d+)$", value)
            if match and match.group(1) not in first_seen:
                first_seen[match.group(1)] = int(match.group(2)) - 1
        generators = {"F": self.feature, "E": self.entity, "C": self.constraint}
        for prefix, counter in first_seen.items():
            generators[prefix].counter = max(0, counter)


_default_ids = IdGeneratorSet()


def next_feature_id() -> str:
    return _default_ids.next_feature_id()


def next_sketch_id() -> str:
    return _default_ids.next_sketch_id()


def next_workplane_id() -> str:
    return _default_ids.next_workplane_id()


def next_entity_id() -> str:
    return _default_ids.next_entity_id()


def next_constraint_id() -> str:
    return _default_ids.next_constraint_id()


def reset_all_id_generators():
    """重置模块级默认 ID 生成器（兼容旧调用；kernel 实例内部使用 self._ids）"""
    _default_ids.reset()


def seed_id_generators_from_history(history: list) -> None:
    """Seed 模块级默认生成器（兼容旧调用；kernel 实例内部使用 self._ids）"""
    _default_ids.seed_from_history(history)


# ============================================================
# Mock 几何（v1.0 阶段，build123d 装不上时的占位）
# ============================================================

@dataclass
class MockBox:
    """Mock 立方体几何（10x10x10 默认）"""
    width: float = 10.0
    height: float = 10.0
    depth: float = 10.0
    
    @property
    def bounding_box(self) -> Tuple[float, float, float, float, float, float]:
        return (0.0, 0.0, 0.0, self.width, self.height, self.depth)
    
    @property
    def volume(self) -> float:
        return self.width * self.height * self.depth
    
    @property
    def surface_area(self) -> float:
        return 2 * (self.width * self.height + self.height * self.depth + self.width * self.depth)
    
    @property
    def vertices(self) -> List[Tuple[float, float, float]]:
        """8 个立方体顶点（duck-typed，renderer 可提取 mesh）"""
        return [
            (0, 0, 0), (self.width, 0, 0),
            (self.width, self.height, 0), (0, self.height, 0),
            (0, 0, self.depth), (self.width, 0, self.depth),
            (self.width, self.height, self.depth), (0, self.height, self.depth),
        ]
    
    @property
    def faces(self) -> List[List[int]]:
        """6 个面（每个面 4 顶点索引）"""
        return [
            [0, 1, 2, 3],   # 底面
            [4, 5, 6, 7],   # 顶面
            [0, 1, 5, 4],   # 前面
            [2, 3, 7, 6],   # 后面
            [1, 2, 6, 5],   # 右面
            [0, 3, 7, 4],   # 左面
        ]
    
    def __repr__(self) -> str:
        return f"MockBox({self.width}x{self.height}x{self.depth})"


@dataclass
class MockMesh:
    """Mock 网格"""
    vertices: List[Tuple[float, float, float]] = field(default_factory=list)
    faces: List[List[int]] = field(default_factory=list)
    bounding_box: Tuple[float, float, float, float, float, float] = (0, 0, 0, 1, 1, 1)
    
    def __repr__(self) -> str:
        return f"MockMesh(V={len(self.vertices)}, F={len(self.faces)})"
