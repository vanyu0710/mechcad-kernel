"""
MechKernel Workplane 抽象

P8 原则：所有草图必须绑 Workplane，不传裸坐标方向。
替代 v1.0 的"Z 轴限制透明处理"。
"""
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Tuple, Any, Dict

from .features import Reference
from .errors import InvalidRequestError


class WorkplaneType(str, Enum):
    """工作平面类型"""
    XY = "XY"           # 标准 XY 平面（Z 方向法向）
    YZ = "YZ"           # 标准 YZ 平面（X 方向法向）
    XZ = "XZ"           # 标准 XZ 平面（Y 方向法向）
    FACE = "face"       # 绑定到某个 face（语义引用）
    CUSTOM = "custom"   # 自定义坐标系


@dataclass
class Workplane:
    """
    工作平面。
    
    所有草图必须绑 Workplane，拉伸方向默认沿 Workplane 法向。
    """
    id: str
    name: str                                  # 语义名 "base" / "top" / "side"
    type: WorkplaneType
    
    # 局部坐标系（默认值对应 XY 平面）
    origin: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    x_dir: Tuple[float, float, float] = (1.0, 0.0, 0.0)
    y_dir: Tuple[float, float, float] = (0.0, 1.0, 0.0)
    normal: Tuple[float, float, float] = (0.0, 0.0, 1.0)
    
    # 如果是 FACE 类型，绑定的语义引用
    reference: Optional[Reference] = None
    
    # 内部
    _build123d_plane: Optional[Any] = field(default=None, repr=False, compare=False)
    
    def __post_init__(self):
        # 校验方向向量（除了 FACE 类型）
        if self.type in (WorkplaneType.XY, WorkplaneType.YZ, WorkplaneType.XZ):
            self._set_standard_axes()
        elif self.type == WorkplaneType.FACE:
            if self.reference is None:
                raise InvalidRequestError("FACE 类型 Workplane 必须提供 reference")
    
    def _set_standard_axes(self):
        """设置标准平面的方向"""
        if self.type == WorkplaneType.XY:
            self.x_dir = (1.0, 0.0, 0.0)
            self.y_dir = (0.0, 1.0, 0.0)
            self.normal = (0.0, 0.0, 1.0)
        elif self.type == WorkplaneType.YZ:
            self.x_dir = (0.0, 1.0, 0.0)
            self.y_dir = (0.0, 0.0, 1.0)
            self.normal = (1.0, 0.0, 0.0)
        elif self.type == WorkplaneType.XZ:
            self.x_dir = (1.0, 0.0, 0.0)
            self.y_dir = (0.0, 0.0, 1.0)
            self.normal = (0.0, 1.0, 0.0)
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type.value,
            "origin": list(self.origin),
            "x_dir": list(self.x_dir),
            "y_dir": list(self.y_dir),
            "normal": list(self.normal),
            "reference": self.reference.to_dict() if self.reference else None,
        }
    
    @staticmethod
    def from_dict(d: dict) -> "Workplane":
        from .features import Reference
        ref = None
        if d.get("reference"):
            ref = Reference(
                kind=d["reference"]["kind"],
                semantic_name=d["reference"]["semantic_name"],
                owner_feature_id=d["reference"].get("owner_feature_id"),
            )
        return Workplane(
            id=d["id"],
            name=d["name"],
            type=WorkplaneType(d["type"]),
            origin=tuple(d.get("origin", [0, 0, 0])),
            x_dir=tuple(d.get("x_dir", [1, 0, 0])),
            y_dir=tuple(d.get("y_dir", [0, 1, 0])),
            normal=tuple(d.get("normal", [0, 0, 1])),
            reference=ref,
        )


class WorkplaneRegistry:
    """Workplane 注册表"""
    
    def __init__(self):
        self._workplanes: Dict[str, Workplane] = {}     # by id
        self._by_name: Dict[str, str] = {}              # name -> id
    
    def register(self, workplane: Workplane) -> None:
        """注册 Workplane"""
        if workplane.id in self._workplanes:
            raise InvalidRequestError(f"Workplane id 已存在: {workplane.id}")
        if workplane.name in self._by_name:
            raise InvalidRequestError(f"Workplane name 已存在: {workplane.name}")
        self._workplanes[workplane.id] = workplane
        self._by_name[workplane.name] = workplane.id
    
    def get_by_id(self, workplane_id: str) -> Workplane:
        if workplane_id not in self._workplanes:
            raise InvalidRequestError(f"Workplane id 不存在: {workplane_id}")
        return self._workplanes[workplane_id]
    
    def get_by_name(self, name: str) -> Workplane:
        if name not in self._by_name:
            raise InvalidRequestError(f"Workplane name 不存在: {name}")
        return self._workplanes[self._by_name[name]]
    
    def has_name(self, name: str) -> bool:
        return name in self._by_name
    
    def has_id(self, workplane_id: str) -> bool:
        return workplane_id in self._workplanes
    
    def all(self) -> list:
        return list(self._workplanes.values())
    
    def remove(self, workplane_id: str) -> None:
        if workplane_id not in self._workplanes:
            return
        wp = self._workplanes[workplane_id]
        self._by_name.pop(wp.name, None)
        del self._workplanes[workplane_id]
    
    def snapshot(self) -> "WorkplaneRegistry":
        """创建快照"""
        import copy
        new = WorkplaneRegistry()
        for wid, wp in self._workplanes.items():
            new._workplanes[wid] = copy.deepcopy(wp)
        new._by_name = dict(self._by_name)
        return new
    
    def restore(self, other: "WorkplaneRegistry") -> None:
        """从快照恢复"""
        import copy
        self._workplanes = copy.deepcopy(other._workplanes)
        self._by_name = dict(other._by_name)
