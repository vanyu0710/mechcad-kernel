"""MechKernel v2.17 P1-5: 孔语义分析。

"数圆柱面半径"分不清通孔与外凸台——两者都是半径相同的圆柱面。本模块按
实体拓扑判定每个圆柱面的真实语义：

- 凹凸：面心沿径向两侧采样，BRepClass3d 分类——轴向侧为空隙且背轴侧为
  材料 → 孔（凹）；反之 → 外凸台（凸）。
- 贯通/盲：圆柱面沿轴向的跨度对比实体同轴总厚度（容差 0.05mm）。
- 沉孔/锪孔：与孔同轴的大半径圆柱（counterbore）/ 锥面（countersink）。

输出面向 feature_contract 验证：直径、位置、轴向、深度、through、kind。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def dot(a: Tuple[float, float, float], b: Tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _solid_classifier(solid):
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    return BRepClass3d_SolidClassifier(solid)


def _classify(classifier, x: float, y: float, z: float) -> str:
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_State
    classifier.Perform(gp_Pnt(x, y, z), 1e-6)
    state = classifier.State()
    if state == TopAbs_State.TopAbs_IN:
        return "in"
    if state == TopAbs_State.TopAbs_OUT:
        return "out"
    return "on"


def _face_points(face) -> List[Tuple[float, float, float]]:
    """面顶点（无顶点时退化为面的 bbox 角点投影近似）。"""
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopoDS import TopoDS
    pts: List[Tuple[float, float, float]] = []
    exp = TopExp_Explorer(face, TopAbs_ShapeEnum.TopAbs_VERTEX)
    while exp.More():
        v = TopoDS.Vertex_s(exp.Current())
        if not v.IsNull():
            from OCP.BRep import BRep_Tool
            p = BRep_Tool.Pnt_s(v)
            pts.append((p.X(), p.Y(), p.Z()))
        exp.Next()
    if pts:
        return pts
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    box = Bnd_Box()
    BRepBndLib.Add_s(face, box)
    lo, hi = box.CornerMin(), box.CornerMax()
    return [(lo.X(), lo.Y(), lo.Z()), (hi.X(), hi.Y(), hi.Z())]


def analyze_holes(geometry: Any) -> Optional[List[Dict]]:
    """返回 [{diameter_mm, center, axis, depth_mm, through, kind}]；失败 None。

    center 为孔轴与入口端交点；axis 为单位方向向量。
    """
    try:
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.GeomAbs import GeomAbs_Cone, GeomAbs_Cylinder
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopAbs import TopAbs_ShapeEnum
        from OCP.TopoDS import TopoDS

        wrapped = getattr(geometry, "wrapped", None)
        if wrapped is None:
            return None

        solids: List = []
        exp = TopExp_Explorer(wrapped, TopAbs_ShapeEnum.TopAbs_SOLID)
        while exp.More():
            solids.append(TopoDS.Solid_s(exp.Current()))
            exp.Next()
        if not solids:
            return []

        holes: List[Dict] = []
        for solid in solids:
            classifier = _solid_classifier(solid)

            cyl_faces = []
            fexp = TopExp_Explorer(solid, TopAbs_ShapeEnum.TopAbs_FACE)
            while fexp.More():
                face = TopoDS.Face_s(fexp.Current())
                if not face.IsNull():
                    adaptor = BRepAdaptor_Surface(face)
                    if adaptor.GetType() == GeomAbs_Cylinder:
                        cyl_faces.append((face, adaptor.Cylinder()))
                fexp.Next()

            for face, cylinder in cyl_faces:
                try:
                    axis = cylinder.Axis()
                    radius = float(cylinder.Radius())
                    loc = axis.Location()
                    d = axis.Direction()
                    ax = (float(d.X()), float(d.Y()), float(d.Z()))
                    # 面心与径向法向
                    from build123d import Face as BFace
                    bface = BFace(face)
                    center = bface.center()
                    p = (float(center.X), float(center.Y), float(center.Z))
                    normal = bface.normal_at()
                    n = (float(normal.X), float(normal.Y), float(normal.Z))
                    nlen = (n[0] ** 2 + n[1] ** 2 + n[2] ** 2) ** 0.5 or 1.0
                    n = tuple(v / nlen for v in n)
                    # 径向朝外向量（轴 → 面心，垂直于轴）
                    v0 = (p[0] - loc.X(), p[1] - loc.Y(), p[2] - loc.Z())
                    t_along = v0[0] * ax[0] + v0[1] * ax[1] + v0[2] * ax[2]
                    radial = (v0[0] - t_along * ax[0], v0[1] - t_along * ax[1], v0[2] - t_along * ax[2])
                    rlen = (radial[0] ** 2 + radial[1] ** 2 + radial[2] ** 2) ** 0.5
                    if rlen < 1e-9:
                        continue
                    radial = tuple(v / rlen for v in radial)
                    # 凹凸判据：孔的面法向指向轴线（dot<0），凸台指向外（dot>0）；
                    # 再用分类器双保险：法向侧为空腔、反侧为材料
                    eps = max(min(0.05, radius / 4.0), 1e-3)
                    toward_axis = dot(n, radial) < -0.5
                    void_side = _classify(classifier, *(tuple(p[i] + n[i] * eps for i in range(3)))) == "out"
                    material_side = _classify(classifier, *(tuple(p[i] - n[i] * eps for i in range(3)))) == "in"
                    is_hole = toward_axis and void_side and material_side
                    if not is_hole:
                        continue
                    # 轴向跨度
                    pts = _face_points(face)
                    zs = [ax[0] * (x - loc.X()) + ax[1] * (y - loc.Y()) + ax[2] * (z - loc.Z())
                          for x, y, z in pts]
                    z1, z2 = min(zs), max(zs)
                    depth = z2 - z1
                    # 贯通判定（局部）：孔两端各探出 eps → 两侧都见空腔才 through。
                    # 不用 bbox：凸台/其他特征会拉长材料范围导致误判。
                    eps2 = max(min(0.05, depth / 4.0), 1e-3)
                    e1 = (loc.X() + ax[0] * (z1 - eps2), loc.Y() + ax[1] * (z1 - eps2),
                          loc.Z() + ax[2] * (z1 - eps2))
                    e2 = (loc.X() + ax[0] * (z2 + eps2), loc.Y() + ax[1] * (z2 + eps2),
                          loc.Z() + ax[2] * (z2 + eps2))
                    through = _classify(classifier, *e1) == "out" and _classify(classifier, *e2) == "out"
                    entry = (loc.X() + ax[0] * z1, loc.Y() + ax[1] * z1, loc.Z() + ax[2] * z1)
                    kind = "through_hole" if through else "blind_hole"
                    # 沉孔/锪孔：同轴大半径圆柱 / 锥面
                    for other_face, other_cyl in cyl_faces:
                        if other_face is face:
                            continue
                        o_axis = other_cyl.Axis()
                        if abs(float(other_cyl.Radius()) - radius) <= 0.05:
                            continue
                        if _coaxial(axis, o_axis):
                            kind = "counterbore_hole"
                            break
                    else:
                        kexp = TopExp_Explorer(solid, TopAbs_ShapeEnum.TopAbs_FACE)
                        while kexp.More():
                            kface = TopoDS.Face_s(kexp.Current())
                            if not kface.IsNull():
                                kad = BRepAdaptor_Surface(kface)
                                if kad.GetType() == GeomAbs_Cone and _coaxial(axis, kad.Cone().Axis()):
                                    kind = "countersink_hole"
                                    break
                            kexp.Next()
                    holes.append({
                        "diameter_mm": round(2 * radius, 3),
                        "center": [round(v, 2) for v in entry],
                        "axis": [round(v, 3) for v in ax],
                        "depth_mm": round(depth, 3),
                        "through": bool(through),
                        "kind": kind,
                    })
                except Exception:
                    continue
        return holes
    except Exception:
        return None


def _coaxial(a, b) -> bool:
    """两轴是否同线：方向平行（同向或反向）且 b 原点到 a 轴距离≈0。"""
    try:
        da = a.Direction()
        db = b.Direction()
        cross = (da.Y() * db.Z() - da.Z() * db.Y(),
                 da.Z() * db.X() - da.X() * db.Z(),
                 da.X() * db.Y() - da.Y() * db.X())
        if (cross[0] ** 2 + cross[1] ** 2 + cross[2] ** 2) ** 0.5 > 1e-4:
            return False
        lb = b.Location()
        la = a.Location()
        v = (lb.X() - la.X(), lb.Y() - la.Y(), lb.Z() - la.Z())
        dist2 = (v[0] ** 2 + v[1] ** 2 + v[2] ** 2
                 - (v[0] * da.X() + v[1] * da.Y() + v[2] * da.Z()) ** 2)
        return dist2 ** 0.5 < 1e-3
    except Exception:
        return False
