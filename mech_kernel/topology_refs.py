"""
MechKernel 拓扑引用层 (v2.11)

打通 "select → fillet/chamfer/shell/面上草图" 的引用闭环：

- 按 geometry revision 缓存当前 B-Rep 的 face/edge 枚举
- 引用格式: "F03" (face #3) / "E12" (edge #12)，序号 0 起始
- 缓存附带几何摘要（类型/面积或长度/中心），LLM 不看渲染图也能理解拓扑
- revision 变化后旧引用自动失效 → resolve 抛 RefStaleError，op 层转 RECOVERABLE("re_select")

缓存对象是 build123d Face/Edge（包着 TopoDS），可直接回喂
Part.fillet/chamfer 的 edge_list 与 BRepOffsetAPI_MakeThickSolid。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


class RefStaleError(Exception):
    """引用对应的几何已被修改（revision 变化），需要重新 select。"""


class RefFormatError(ValueError):
    """引用格式非法或索引越界。"""


@dataclass
class TopoInfo:
    """单个 face/edge 的缓存条目"""
    ref: str
    kind: str                                    # "face" | "edge"
    shape: Any                                   # build123d Face / Edge
    geom_type: str = "unknown"                   # plane/cylinder/... | line/circle/...
    area: Optional[float] = None                 # face
    length: Optional[float] = None               # edge
    center: Optional[Tuple[float, float, float]] = None
    normal: Optional[Tuple[float, float, float]] = None   # plane face
    radius: Optional[float] = None               # cylinder/cone face, circle edge

    def summary(self) -> dict:
        """LLM 友好的摘要（不含 shape 对象）"""
        d: Dict[str, Any] = {"ref": self.ref, "type": self.geom_type}
        if self.kind == "face" and self.area is not None:
            d["area_mm2"] = round(self.area, 2)
        if self.kind == "edge" and self.length is not None:
            d["length_mm"] = round(self.length, 2)
        if self.center is not None:
            d["center"] = tuple(round(v, 2) for v in self.center)
        if self.normal is not None:
            d["normal"] = tuple(round(v, 3) for v in self.normal)
        if self.radius is not None:
            d["radius_mm"] = round(self.radius, 3)
        return d


_FACE_TYPE_NAMES = {
    0: "plane", 1: "cylinder", 2: "cone", 3: "sphere", 4: "torus",
}
_EDGE_TYPE_NAMES = {
    0: "line", 1: "circle", 2: "ellipse", 3: "hyperbola",
    4: "parabola", 5: "bezier", 6: "bspline", 7: "offset_curve", 8: "other",
}


def _vec3(v) -> Tuple[float, float, float]:
    return (float(v.X), float(v.Y), float(v.Z))


def _round_center(p) -> Tuple[float, float, float]:
    try:
        return (float(p.X), float(p.Y), float(p.Z))
    except AttributeError:
        return (float(p[0]), float(p[1]), float(p[2]))


def _make_ref(kind: str, index: int) -> str:
    return f"{kind[0].upper()}{index:02d}"


class TopologyCache:
    """按 revision 缓存当前几何的 face/edge 枚举"""

    def __init__(self):
        self._revision: Optional[int] = None
        self._faces: List[TopoInfo] = []
        self._edges: List[TopoInfo] = []

    def invalidate(self) -> None:
        self._revision = None
        self._faces = []
        self._edges = []

    def _ensure(self, revision: int, geometry: Any) -> None:
        if self._revision == revision:
            return
        if geometry is None:
            self.invalidate()
            return
        self._faces = self._enumerate_faces(geometry)
        self._edges = self._enumerate_edges(geometry)
        self._revision = revision

    def _enumerate_faces(self, geometry) -> List[TopoInfo]:
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Cone

        infos: List[TopoInfo] = []
        try:
            faces = list(geometry.faces())
        except Exception:
            return infos
        for i, face in enumerate(faces):
            info = TopoInfo(ref=_make_ref("face", i), kind="face", shape=face)
            try:
                adaptor = BRepAdaptor_Surface(face.wrapped)
                t = adaptor.GetType()
                info.geom_type = _FACE_TYPE_NAMES.get(int(t), "unknown")
                if info.geom_type == "cylinder":
                    info.radius = float(adaptor.Cylinder().Radius())
                elif info.geom_type == "cone":
                    info.radius = float(adaptor.Cone().RefRadius())
                elif info.geom_type == "sphere":
                    info.radius = float(adaptor.Sphere().Radius())
            except Exception:
                info.geom_type = "unknown"
            try:
                info.area = float(face.area)
            except Exception:
                pass
            try:
                info.center = _round_center(face.center())
            except Exception:
                pass
            try:
                if info.geom_type == "plane":
                    info.normal = _vec3(face.normal_at())
            except Exception:
                pass
            infos.append(info)
        return infos

    def _enumerate_edges(self, geometry) -> List[TopoInfo]:
        from OCP.BRepAdaptor import BRepAdaptor_Curve
        from OCP.GeomAbs import GeomAbs_Circle, GeomAbs_Cylinder

        infos: List[TopoInfo] = []
        try:
            edges = list(geometry.edges())
        except Exception:
            return infos
        for i, edge in enumerate(edges):
            info = TopoInfo(ref=_make_ref("edge", i), kind="edge", shape=edge)
            try:
                adaptor = BRepAdaptor_Curve(edge.wrapped)
                info.geom_type = _EDGE_TYPE_NAMES.get(int(adaptor.GetType()), "unknown")
                if info.geom_type == "circle":
                    circ = adaptor.Circle()
                    info.radius = float(circ.Radius())
                    info.center = _round_center(circ.Location())
            except Exception:
                info.geom_type = "unknown"
            try:
                info.length = float(edge.length)
            except Exception:
                pass
            if info.center is None:
                try:
                    info.center = _round_center(edge.center())
                except Exception:
                    pass
            infos.append(info)
        return infos

    def faces(self, revision: int, geometry: Any) -> List[TopoInfo]:
        self._ensure(revision, geometry)
        return list(self._faces)

    def edges(self, revision: int, geometry: Any) -> List[TopoInfo]:
        self._ensure(revision, geometry)
        return list(self._edges)

    def resolve(self, revision: int, geometry: Any, ref: str, kind: str) -> TopoInfo:
        """解析 "F03"/"E12" 引用。

        Raises:
            RefStaleError: 缓存 revision 与当前不一致（几何已被修改）
            RefFormatError: 格式非法 / 类型不符 / 索引越界
        """
        if not isinstance(ref, str) or len(ref) < 2 or ref[0].upper() not in ("F", "E"):
            raise RefFormatError(f"引用 {ref!r} 格式非法（应为 'F03'/'E12' 形式，来自 select 结果）")
        prefix = "face" if ref[0].upper() == "F" else "edge"
        if prefix != kind:
            raise RefFormatError(f"引用 {ref!r} 是 {prefix}，此操作需要 {kind} 引用")
        try:
            index = int(ref[1:])
        except ValueError:
            raise RefFormatError(f"引用 {ref!r} 序号非法")
        self._ensure(revision, geometry)
        items = self._faces if prefix == "face" else self._edges
        if not items:
            raise RefStaleError("拓扑缓存为空（几何可能已改变），请重新 select")
        if index < 0 or index >= len(items):
            raise RefFormatError(
                f"引用 {ref!r} 越界（当前 {prefix} 共 {len(items)} 个），请重新 select"
            )
        return items[index]


def resolve_refs(cache: "TopologyCache", revision: int, geometry: Any,
                 refs: List[str], kind: str) -> List[TopoInfo]:
    """批量解析引用，返回 shape 列表。任何失败抛 RefStaleError/RefFormatError。"""
    return [cache.resolve(revision, geometry, r, kind) for r in refs]
