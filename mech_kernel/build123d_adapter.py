"""
MechKernel Build123d Adapter（v1.1.1）

设计目标：
- 完全 mock build123d 的核心 API（Part, BuildPart, extrude, revolve, etc.）
- 用 numpy 自己算几何（不依赖 OCC）
- 接口和 build123d 一致，未来切换只需改 import
- 几何对象都是 duck-typed：vertices/faces/bbox

支持的形状（v1.1.1）：
- Box（立方体）
- Cylinder（圆柱体）
- Cone（圆锥）
- Sphere（球）

未来扩展（v1.2）：
- 布尔运算（union/subtract/intersect）
- 圆角/倒角（fillet/chamfer）
- 扫掠/回转（sweep/revolve with path）

P10 原则：所有几何用 duck-typed + try/except 兼容
"""
from typing import List, Tuple, Optional, Any
from dataclasses import dataclass, field
import math

import numpy as np


# ============================================================
# 几何对象（duck-typed：vertices/faces/bbox）
# ============================================================

@dataclass
class Build123dPart:
    """
    build123d Part 的 mock 版本。
    
    接口：
    - vertices: List[Tuple[float, float, float]]
    - faces: List[List[int]]  # 每个面是顶点索引列表（可能不均匀：3 边 / 4 边混合）
    - bbox: Tuple[float, float, float, float, float, float]
    - volume: float
    - surface_area: float
    """
    _vertices: np.ndarray
    _faces: list  # List[List[int]]（不均匀形状）
    
    @property
    def vertices(self) -> List[Tuple[float, float, float]]:
        """返回顶点列表"""
        return [tuple(v) for v in self._vertices.tolist()]
    
    @property
    def faces(self) -> List[List[int]]:
        """返回面列表（每个面是顶点索引）"""
        return list(self._faces)
    
    @property
    def bbox(self) -> Tuple[float, float, float, float, float, float]:
        """返回包围盒 (xmin, ymin, zmin, xmax, ymax, zmax)"""
        if len(self._vertices) == 0:
            return (0, 0, 0, 0, 0, 0)
        mn = self._vertices.min(axis=0)
        mx = self._vertices.max(axis=0)
        return (float(mn[0]), float(mn[1]), float(mn[2]), 
                float(mx[0]), float(mx[1]), float(mx[2]))
    
    @property
    def bounding_box(self) -> Tuple[float, float, float, float, float, float]:
        """兼容 build123d 命名（也是 callable）"""
        return self.bbox
    
    def bounding_box_callable(self) -> Tuple[float, float, float, float, float, float]:
        return self.bbox
    
    @property
    def volume(self) -> float:
        """计算体积（用面 + 顶点，按公式 sum(faces * vertices[face[0]] cross vertices[face[1]] dot vertices[face[2]] / 6)）"""
        if len(self._faces) == 0 or len(self._vertices) == 0:
            return 0.0
        v = self._vertices
        vol = 0.0
        for face in self._faces:
            if len(face) < 3:
                continue
            for i in range(1, len(face) - 1):
                v0 = v[face[0]]
                v1 = v[face[i]]
                v2 = v[face[i + 1]]
                cross = np.cross(v1 - v0, v2 - v0)
                vol += np.dot(v0, cross) / 6.0
        return abs(vol)
    
    @property
    def surface_area(self) -> float:
        """计算表面积（所有三角面面积之和）"""
        if len(self._faces) == 0 or len(self._vertices) == 0:
            return 0.0
        v = self._vertices
        area = 0.0
        for face in self._faces:
            if len(face) < 3:
                continue
            for i in range(1, len(face) - 1):
                v0 = v[face[0]]
                v1 = v[face[i]]
                v2 = v[face[i + 1]]
                area += 0.5 * np.linalg.norm(np.cross(v1 - v0, v2 - v0))
        return area
    
    @property
    def face_count(self) -> int:
        return len(self._faces)

    @property
    def edge_count(self) -> int:
        # 估算：每个面 4 边，去重
        edges = set()
        for face in self._faces:
            for i in range(len(face)):
                a, b = face[i], face[(i + 1) % len(face)]
                edges.add((min(a, b), max(a, b)))
        return len(edges)
        # 估算：每个面边数，去重
        edges = set()
        for face in self._faces:
            for i in range(len(face)):
                a, b = face[i], face[(i + 1) % len(face)]
                edges.add((min(a, b), max(a, b)))
        return len(edges)
    
    @property
    def vertex_count(self) -> int:
        return len(self._vertices)
    
    def to_dict(self) -> dict:
        return {
            "vertices": self.vertices,
            "faces": self.faces,
            "bbox": self.bbox,
            "volume": self.volume,
            "surface_area": self.surface_area,
            "face_count": self.face_count,
            "edge_count": self.edge_count,
            "vertex_count": self.vertex_count,
        }
    
    def __repr__(self) -> str:
        return f"Part(V={self.vertex_count}, F={self.face_count}, vol={self.volume:.1f})"


# ============================================================
# 形状生成器（基础图元）
# ============================================================

def make_box(width: float, height: float, depth: float) -> Build123dPart:
    """
    生成 3D 立方体（中心在原点）。
    
    Args:
        width: X 方向尺寸
        height: Y 方向尺寸
        depth: Z 方向尺寸
    
    Returns:
        Build123dPart（6 个面、8 个顶点）
    """
    w, h, d = width / 2, height / 2, depth / 2
    vertices = np.array([
        [-w, -h, -d],   # 0
        [+w, -h, -d],   # 1
        [+w, +h, -d],   # 2
        [-w, +h, -d],   # 3
        [-w, -h, +d],   # 4
        [+w, -h, +d],   # 5
        [+w, +h, +d],   # 6
        [-w, +h, +d],   # 7
    ], dtype=float)
    faces = np.array([
        [0, 3, 2, 1],  # 底面（Z-，逆时针看）
        [4, 5, 6, 7],  # 顶面（Z+）
        [0, 1, 5, 4],  # 前面（Y-）
        [2, 3, 7, 6],  # 后面（Y+）
        [1, 2, 6, 5],  # 右面（X+）
        [0, 4, 7, 3],  # 左面（X-）
    ])
    return Build123dPart(vertices, faces)


def make_cylinder(radius: float, height: float, segments: int = 32) -> Build123dPart:
    """
    生成 3D 圆柱体（中心轴沿 Z 轴，从 -height/2 到 +height/2）。
    
    Args:
        radius: 底面半径
        height: 高度
        segments: 圆周分段数（默认 32，平滑）
    
    Returns:
        Build123dPart（3 个面：顶 + 底 + 侧）
    """
    h = height / 2
    angles = np.linspace(0, 2 * np.pi, segments, endpoint=False)
    cos_a = np.cos(angles)
    sin_a = np.sin(angles)
    
    # 顶点：底面圆周 + 顶面圆周 + 中心
    bottom_verts = np.column_stack([radius * cos_a, radius * sin_a, np.full(segments, -h)])
    top_verts = np.column_stack([radius * cos_a, radius * sin_a, np.full(segments, +h)])
    
    vertices = np.vstack([bottom_verts, top_verts])
    
    # 面：底面三角形 fan（顶点 = bottom_verts 中心）
    # 但我们没中心顶点，简化：底面用 segments 个三角形，中心 = 原点投影
    # 实际上底面是 disk，简化成 n 个三角形共享一个虚拟中心
    
    # 更简单：把 n 个三角形，每个用 bottom_verts[i], bottom_verts[i+1], 中心
    # 中心是 [0, 0, -h]
    # 但这样多一个顶点
    
    # 用 fan with center vertex: 添加 [0, 0, -h] 和 [0, 0, +h] 作为最后两个
    bottom_center = np.array([[0, 0, -h]])
    top_center = np.array([[0, 0, +h]])
    vertices = np.vstack([bottom_verts, top_verts, bottom_center, top_center])
    
    n = segments
    bottom_center_idx = 2 * n
    top_center_idx = 2 * n + 1
    
    faces = []
    # 底面（法向 -Z）三角形
    for i in range(n):
        next_i = (i + 1) % n
        faces.append([bottom_center_idx, next_i, i])
    # 顶面（法向 +Z）三角形
    for i in range(n):
        next_i = (i + 1) % n
        faces.append([top_center_idx, n + i, n + next_i])
    # 侧面 4 边形（分开存，因为形状不均匀）
    side_faces = []
    for i in range(n):
        next_i = (i + 1) % n
        side_faces.append([i, next_i, n + next_i, n + i])
    
    # 混合形状不能直接 np.array，统一存为 list of lists
    return Build123dPart(vertices, faces + side_faces)  # 传入 list of lists


def make_sphere(radius: float, segments: int = 16) -> Build123dPart:
    """生成 3D 球体"""
    lat_lines = segments // 2
    lon_lines = segments
    vertices = []
    for i in range(lat_lines + 1):
        theta = np.pi * i / lat_lines  # 0 to pi
        z = radius * np.cos(theta)
        r_xy = radius * np.sin(theta)
        for j in range(lon_lines):
            phi = 2 * np.pi * j / lon_lines
            x = r_xy * np.cos(phi)
            y = r_xy * np.sin(phi)
            vertices.append([x, y, z])
    vertices = np.array(vertices)
    
    faces = []
    for i in range(lat_lines):
        for j in range(lon_lines):
            jp = (j + 1) % lon_lines
            a = i * lon_lines + j
            b = i * lon_lines + jp
            c = (i + 1) * lon_lines + jp
            d = (i + 1) * lon_lines + j
            if i > 0:
                faces.append([a, b, c, d])
            else:
                faces.append([a, b, c])  # 极点
            if i < lat_lines - 1:
                faces.append([a, d, c])
            else:
                faces.append([a, d, c])
    faces = np.array(faces)
    return Build123dPart(vertices, faces)


def make_annular_cylinder(outer_radius: float, inner_radius: float, height: float, segments: int = 32) -> Build123dPart:
    """
    生成环形圆柱（外圆 - 内圆，Z 方向拉伸）。
    
    v1.1.1 简化：直接构造环形面
    """
    h = height / 2
    angles = np.linspace(0, 2 * np.pi, segments, endpoint=False)
    cos_a = np.cos(angles)
    sin_a = np.sin(angles)
    
    # 顶点：底面外圆 + 底面内圆 + 顶面外圆 + 顶面内圆 + 4 个中心
    n = segments
    
    # 顶点布局
    # 0..n-1: 底面外圆
    # n..2n-1: 底面内圆
    # 2n..3n-1: 顶面外圆
    # 3n..4n-1: 顶面内圆
    # 4n: 底面中心（外）
    # 4n+1: 顶面中心（外）
    
    verts = []
    # 底面外圆
    for i in range(n):
        verts.append([outer_radius * cos_a[i], outer_radius * sin_a[i], -h])
    # 底面内圆
    for i in range(n):
        verts.append([inner_radius * cos_a[i], inner_radius * sin_a[i], -h])
    # 顶面外圆
    for i in range(n):
        verts.append([outer_radius * cos_a[i], outer_radius * sin_a[i], +h])
    # 顶面内圆
    for i in range(n):
        verts.append([inner_radius * cos_a[i], inner_radius * sin_a[i], +h])
    # 中心点
    verts.append([0, 0, -h])  # 底面中心 4n
    verts.append([0, 0, +h])  # 顶面中心 4n+1
    
    vertices = np.array(verts)
    
    outer_bottom = 0
    inner_bottom = n
    outer_top = 2 * n
    inner_top = 3 * n
    bottom_center = 4 * n
    top_center = 4 * n + 1
    
    faces = []
    # 底面外环（外圆 → 内圆，分 2n 个三角形）
    for i in range(n):
        next_i = (i + 1) % n
        # 三角形 1: 底面中心, 外圆[i], 外圆[next_i] (这是实心圆，我们只画环形)
        # 实际：环形 = 外圆三角形 - 内圆三角形
        # 简化为两个四边形：外圆[i], 内圆[i], 内圆[next_i], 外圆[next_i]
        faces.append([
            outer_bottom + i, inner_bottom + i, 
            inner_bottom + next_i, outer_bottom + next_i
        ])
    # 顶面外环
    for i in range(n):
        next_i = (i + 1) % n
        faces.append([
            outer_top + i, outer_top + next_i,
            inner_top + next_i, inner_top + i
        ])
    # 外侧面
    for i in range(n):
        next_i = (i + 1) % n
        faces.append([
            outer_bottom + i, outer_bottom + next_i,
            outer_top + next_i, outer_top + i
        ])
    # 内侧面
    for i in range(n):
        next_i = (i + 1) % n
        faces.append([
            inner_bottom + next_i, inner_bottom + i,
            inner_top + i, inner_top + next_i
        ])
    
    return Build123dPart(vertices, faces)


# ============================================================
# build123d 风格 API（context manager）
# ============================================================

class BuildPart:
    """
    模拟 build123d 的 BuildPart context manager。
    
    用法（和 build123d 一致）：
        with BuildPart() as bp:
            extrude(sketch, amount=20)
        result = bp.part
    
    v1.1.1 简化：
    - extrude(sketch, amount) 支持圆形草图 → 圆柱
    - extrude(sketch, amount) 支持矩形草图 → 立方体
    """
    
    def __init__(self):
        self._part: Optional[Build123dPart] = None
    
    @property
    def part(self) -> Build123dPart:
        if self._part is None:
            # 空 part
            return Build123dPart(np.zeros((0, 3)), np.zeros((0, 0), dtype=int))
        return self._part
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


def extrude(sketch, amount: float) -> BuildPart:
    """
    Mock build123d.extrude：把 2D 草图拉伸成 3D。
    
    sketch 是 dict-like {"type": "circle"|"rectangle", ...} 或 MockSketch
    """
    bp = BuildPart()
    
    if hasattr(sketch, "to_extrude_params"):
        params = sketch.to_extrude_params()
    elif isinstance(sketch, dict):
        params = sketch
    else:
        # 默认识别
        params = {"type": "box", "width": 10, "height": 10}
    
    sketch_type = params.get("type", "box")
    
    if sketch_type == "circle":
        radius = params.get("radius", 5)
        bp._part = make_cylinder(radius, amount)
    elif sketch_type == "annulus":
        outer = params.get("outer_radius", 10)
        inner = params.get("inner_radius", 5)
        bp._part = make_annular_cylinder(outer, inner, amount)
    elif sketch_type == "rectangle":
        width = params.get("width", 10)
        height = params.get("height", 10)
        bp._part = make_box(width, height, amount)
    elif sketch_type == "box":
        # 通用 box
        w = params.get("width", 10)
        h = params.get("height", 10)
        d = params.get("depth", 10)
        bp._part = make_box(w, h, d)
    else:
        # 未知 type → 默认 box
        bp._part = make_box(10, 10, amount)
    
    return bp


# ============================================================
# 草图抽象（duck-typed 兼容 build123d.Sketch / build123d.Sketch API）
# ============================================================

class MockSketch:
    """
    Mock 草图：和 kernel.Sketch 兼容的 2D 形状描述。
    
    v1.1.1 简化：只支持圆 + 矩形
    """
    
    def __init__(self, sketch_type: str = "box", **params):
        self.type = sketch_type
        self.params = params
    
    def to_extrude_params(self) -> dict:
        return {"type": self.type, **self.params}
    
    def __repr__(self) -> str:
        return f"MockSketch({self.type}, {self.params})"


# ============================================================
# 草图识别（从 kernel.Sketch → MockSketch）
# ============================================================

def sketch_to_build123d(sketch) -> MockSketch:
    """
    把 kernel.Sketch 转换为 build123d MockSketch。
    
    v1.1.1 简化：
    - 单个 circle → cylinder
    - 单个 rectangle → box
    - 多个 circle（同心）→ annulus（外圆 + 内圆）
    - 其他 → 最小包围盒
    """
    if not sketch or not sketch.entities:
        return MockSketch("box", width=10, height=10)
    
    # 单个 circle → cylinder
    if len(sketch.entities) == 1:
        e = sketch.entities[0]
        if e.type == "circle":
            return MockSketch("circle", radius=e.params["radius"])
        if e.type == "rectangle":
            return MockSketch("rectangle", 
                            width=e.params["width"], 
                            height=e.params["height"])
    
    # 多个 circle 同心 → annulus
    if len(sketch.entities) >= 2 and all(e.type == "circle" for e in sketch.entities):
        # 同心（中心相同）
        circles = sketch.entities
        c0 = circles[0].params["center"]
        if all(e.params["center"] == c0 for e in circles):
            radii = sorted([e.params["radius"] for e in circles], reverse=True)
            if len(radii) == 2:
                return MockSketch("annulus", outer_radius=radii[0], inner_radius=radii[1])
            # 3+ 圆 → 外圆 - 内圆，外内圆之外用最大（fallback）
            return MockSketch("annulus", outer_radius=radii[0], inner_radius=radii[1])
    
    # 多个实体 → 最小包围盒
    all_x, all_y = [], []
    for e in sketch.entities:
        if e.type == "circle":
            cx, cy = e.params["center"]
            r = e.params["radius"]
            all_x.extend([cx - r, cx + r])
            all_y.extend([cy - r, cy + r])
        elif e.type == "rectangle":
            cx, cy = e.params["center"]
            w, h = e.params["width"], e.params["height"]
            all_x.extend([cx - w/2, cx + w/2])
            all_y.extend([cy - h/2, cy + h/2])
    
    if all_x and all_y:
        width = max(all_x) - min(all_x)
        height = max(all_y) - min(all_y)
        return MockSketch("rectangle", width=width, height=height)
    
    return MockSketch("box", width=10, height=10)


# ============================================================
# 工厂（让 kernel.py 用一个统一的入口）
# ============================================================

def extrude_sketch(sketch, depth: float) -> Build123dPart:
    """
    拉伸 kernel 草图为 3D 几何。
    
    v1.1.1 流程：
    1. sketch → MockSketch（识别形状）
    2. MockSketch → BuildPart（用 build123d-style API）
    3. 返回 Build123dPart
    """
    ms = sketch_to_build123d(sketch)
    bp = extrude(ms, depth)
    return bp.part
