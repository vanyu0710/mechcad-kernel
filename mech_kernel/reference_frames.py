"""
MechKernel v2.7: Reference Coordinate Frames

A `CoordinateFrame` is a named, deterministic local coordinate system placed
inside the world.  Every component, sketch reference, or assembly instance can
attach to a frame instead of relying on raw world coordinates.  Frames are
right-handed orthonormal bases, optionally parented to another frame so that
placements compose through the graph.

Public surface (used by kernel):
  CoordinateFrame(name, origin, normal, x_axis, parent=None)
  FrameRegistry: add / get / resolve / to_dict / from_dict
  resolve_point(frame_name, uv, normal_offset) -> world (x, y, z)
  resolve_placement(frame_name, uv=(0,0), normal_offset=0.0,
                    rotation=(0, (0,0,1))) -> (origin, matrix)

Serialisation keys are stable so saved projects and snapshots round-trip.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ---------- Numerical helpers ----------

def _dot(a: Tuple[float, float, float], b: Tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Tuple[float, float, float], b: Tuple[float, float, float]) -> Tuple[float, float, float]:
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _norm(v: Tuple[float, float, float]) -> float:
    return math.sqrt(_dot(v, v))


def _normalize(v: Tuple[float, float, float]) -> Tuple[float, float, float]:
    n = _norm(v)
    if n < 1e-12:
        raise ValueError("向量长度为零，无法归一化")
    return (v[0] / n, v[1] / n, v[2] / n)


def _is_close(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


# ---------- CoordinateFrame ----------

@dataclass
class CoordinateFrame:
    """一个右手坐标系 / 参考平面.

    Attributes:
        name:    frame 名称（同一 FrameRegistry 内唯一）
        origin:  frame 原点（世界坐标，3 元素）
        normal:  frame 法向（z 方向，3 元素，已归一化）
        x_axis:  frame x 轴方向（已归一化，与 normal 垂直）
        parent:  父 frame 名称（None = world 根）
        metadata: 自由附加元数据（如 frame 的语义含义）
    """
    name: str
    origin: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    normal: Tuple[float, float, float] = (0.0, 0.0, 1.0)
    x_axis: Tuple[float, float, float] = (1.0, 0.0, 0.0)
    parent: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("frame name 必须是非空字符串")
        self.origin = tuple(float(x) for x in self.origin)
        if len(self.origin) != 3:
            raise ValueError("origin 必须是长度为 3 的数组")
        self.normal = tuple(float(x) for x in self.normal)
        if len(self.normal) != 3:
            raise ValueError("normal 必须是长度为 3 的数组")
        self.x_axis = tuple(float(x) for x in self.x_axis)
        if len(self.x_axis) != 3:
            raise ValueError("x_axis 必须是长度为 3 的数组")
        # 归一化 + 右手化
        self.normal = _normalize(self.normal)
        self.x_axis = self._orthogonalize(self.x_axis, self.normal)
        self.x_axis = _normalize(self.x_axis)

    @staticmethod
    def _orthogonalize(vec: Tuple[float, float, float],
                       ref: Tuple[float, float, float]) -> Tuple[float, float, float]:
        """Gram-Schmidt: vec 减去沿 ref 的分量.

        退化保护: 若 vec 与 ref 平行导致结果为零向量, 选一个与 ref 不平行的
        默认种子重新正交化. 默认种子的选择:
        - 若 ref 不沿 X 轴, 用 (1,0,0)
        - 否则用 (0,1,0)
        """
        d = _dot(vec, ref)
        out = (vec[0] - d * ref[0], vec[1] - d * ref[1], vec[2] - d * ref[2])
        if _norm(out) < 1e-12:
            seed = (1.0, 0.0, 0.0) if abs(ref[0]) < 0.9 else (0.0, 1.0, 0.0)
            return CoordinateFrame._orthogonalize(seed, ref)
        return out

    @property
    def y_axis(self) -> Tuple[float, float, float]:
        """y = z × x (右手)"""
        return _cross(self.normal, self.x_axis)

    def basis_matrix(self) -> List[List[float]]:
        """3x3 旋转矩阵（列向量 = [x_axis, y_axis, normal]）"""
        y = self.y_axis
        return [
            [self.x_axis[0], y[0], self.normal[0]],
            [self.x_axis[1], y[1], self.normal[1]],
            [self.x_axis[2], y[2], self.normal[2]],
        ]

    def to_local(self, world: Tuple[float, float, float]) -> Tuple[float, float, float]:
        """世界坐标 → frame 局部坐标 (u, v, n)"""
        d = (world[0] - self.origin[0], world[1] - self.origin[1], world[2] - self.origin[2])
        y = self.y_axis
        return (
            _dot(d, self.x_axis),
            _dot(d, y),
            _dot(d, self.normal),
        )

    def to_world(self, local: Tuple[float, float, float]) -> Tuple[float, float, float]:
        """frame 局部坐标 → 世界坐标"""
        y = self.y_axis
        return (
            self.origin[0] + local[0] * self.x_axis[0] + local[1] * y[0] + local[2] * self.normal[0],
            self.origin[1] + local[0] * self.x_axis[1] + local[1] * y[1] + local[2] * self.normal[1],
            self.origin[2] + local[0] * self.x_axis[2] + local[1] * y[2] + local[2] * self.normal[2],
        )

    def is_orthonormal(self, tol: float = 1e-6) -> bool:
        if not _is_close(_norm(self.x_axis), 1.0, tol):
            return False
        if not _is_close(_norm(self.normal), 1.0, tol):
            return False
        if not _is_close(_dot(self.x_axis, self.normal), 0.0, tol):
            return False
        y = self.y_axis
        if not _is_close(_norm(y), 1.0, tol):
            return False
        # 右手检查: x × y 应当 == normal
        xy = _cross(self.x_axis, y)
        if not (_is_close(xy[0], self.normal[0], tol) and
                _is_close(xy[1], self.normal[1], tol) and
                _is_close(xy[2], self.normal[2], tol)):
            return False
        return True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "origin": list(self.origin),
            "normal": list(self.normal),
            "x_axis": list(self.x_axis),
            "parent": self.parent,
            "metadata": dict(self.metadata),
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "CoordinateFrame":
        return CoordinateFrame(
            name=str(data["name"]),
            origin=tuple(data.get("origin", (0.0, 0.0, 0.0))),
            normal=tuple(data.get("normal", (0.0, 0.0, 1.0))),
            x_axis=tuple(data.get("x_axis", (1.0, 0.0, 0.0))),
            parent=data.get("parent"),
            metadata=dict(data.get("metadata", {})),
        )


# ---------- FrameRegistry ----------

class FrameRegistry:
    """frame 仓库"""

    def __init__(self) -> None:
        self._frames: Dict[str, CoordinateFrame] = {}

    def add(self, frame: CoordinateFrame) -> CoordinateFrame:
        if frame.name in self._frames:
            raise ValueError(f"frame {frame.name} 已存在")
        if frame.parent is not None and frame.parent not in self._frames:
            raise ValueError(f"parent frame {frame.parent} 不存在")
        if not frame.is_orthonormal():
            raise ValueError(f"frame {frame.name} 不是合法正交右手系")
        self._frames[frame.name] = frame
        return frame

    def has(self, name: str) -> bool:
        return name in self._frames

    def get(self, name: str) -> CoordinateFrame:
        if name not in self._frames:
            raise KeyError(f"frame {name} 不存在")
        return self._frames[name]

    def remove(self, name: str) -> None:
        # 检查没有子 frame 引用
        for f in self._frames.values():
            if f.parent == name:
                raise ValueError(f"frame {name} 仍有子 frame {f.name} 引用，无法移除")
        self._frames.pop(name, None)

    def names(self) -> List[str]:
        return sorted(self._frames.keys())

    def to_dict(self) -> dict:
        return {name: f.to_dict() for name, f in self._frames.items()}

    def from_dict(self, data: Dict[str, Any]) -> None:
        # 两段式：先全部创建，再设 parent（避免顺序问题）
        new_frames: Dict[str, CoordinateFrame] = {}
        for name, f_data in data.items():
            tmp = CoordinateFrame.from_dict({**f_data, "parent": None})
            new_frames[name] = tmp
        for name, f in new_frames.items():
            parent = data[name].get("parent")
            if parent is not None:
                if parent not in new_frames:
                    raise ValueError(f"frame {name} 引用未知 parent {parent}")
                f.parent = parent
        for f in new_frames.values():
            if not f.is_orthonormal():
                raise ValueError(f"反序列化失败: frame {f.name} 不正交")
        self._frames = new_frames

    def clear(self) -> None:
        self._frames.clear()

    def parent_chain(self, name: str) -> List[CoordinateFrame]:
        """返回 [root, ..., self] 链"""
        if name not in self._frames:
            raise KeyError(name)
        chain: List[CoordinateFrame] = []
        seen: set = set()
        current: Optional[str] = name
        while current is not None:
            if current in seen:
                raise ValueError(f"frame {name} 存在循环 parent 链")
            seen.add(current)
            frame = self.get(current)
            chain.append(frame)
            current = frame.parent
        return list(reversed(chain))


# ---------- Resolve helpers ----------

def resolve_point(registry: FrameRegistry,
                  frame_name: str,
                  uv: Tuple[float, float] = (0.0, 0.0),
                  normal_offset: float = 0.0) -> Tuple[float, float, float]:
    """将 {frame, uv, normal_offset} 形式 → 世界坐标.

    uv 在 frame 的 x/y 平面内；normal_offset 沿 frame 的 normal 方向。
    支持相对放置，如 {"frame": "housing_mount_plane", "uv": [35, 20],
    "normal_offset": 6}.
    """
    frame = registry.get(frame_name)
    local = (float(uv[0]), float(uv[1]), float(normal_offset))
    return frame.to_world(local)


def resolve_placement(registry: FrameRegistry,
                      frame_name: str,
                      uv: Tuple[float, float] = (0.0, 0.0),
                      normal_offset: float = 0.0,
                      rotation: Tuple[float, Tuple[float, float, float]] = (0.0, (0.0, 0.0, 1.0))
                      ) -> Tuple[Tuple[float, float, float], List[List[float]]]:
    """返回 (world_origin, 3x3 rotation_matrix).

    语义 (v2.7.1 修正):
        - origin: uv 在 frame 平面 + normal_offset 沿 frame.normal, 转世界坐标
        - rotation = (angle_deg, axis):
            * axis 是**世界坐标系**的轴 (不是 frame 本地轴)
            * 把 frame 的 x/y/normal basis 整体绕 axis 旋转 angle 度
            * 合成: out = R_world @ frame.basis  (R 先作用, base 在右)
            * 即: 如果 frame.normal 原来朝 +Z, 旋转 90° 绕 +Y 后, new normal 朝 -X

    例子:
        frame: normal=(1,0,0) (轴沿 +X)
        rotation = (90, (0,1,0))  绕 +Y 旋转 90°
        原 normal (1,0,0) → 经 R (绕 +Y 90°) → (0,0,-1)
        原 x_axis (0,1,0) → (0,1,0) (不变, 因为它在旋转轴上)
    """
    frame = registry.get(frame_name)
    origin = frame.to_world((uv[0], uv[1], normal_offset))
    base = frame.basis_matrix()
    angle = float(rotation[0])
    axis = _normalize(tuple(rotation[1]))
    if _is_close(angle, 0.0, 1e-9):
        return origin, base
    # Rodrigues
    c = math.cos(math.radians(angle))
    s = math.sin(math.radians(angle))
    t = 1.0 - c
    ax, ay, az = axis
    R = [
        [t * ax * ax + c,         t * ax * ay - s * az,  t * ax * az + s * ay],
        [t * ax * ay + s * az,    t * ay * ay + c,        t * ay * az - s * ax],
        [t * ax * az - s * ay,    t * ay * az + s * ax,   t * az * az + c],
    ]
    # 合成: out = R @ base  (列向量约定: v_world = R @ v_local)
    out: List[List[float]] = [[0.0, 0.0, 0.0] for _ in range(3)]
    for i in range(3):
        for j in range(3):
            out[i][j] = sum(R[i][k] * base[k][j] for k in range(3))
    return origin, out


__all__ = [
    "CoordinateFrame",
    "FrameRegistry",
    "resolve_point",
    "resolve_placement",
]
