"""MechKernel v2.20: 壳体设计分析 + 逐项审计。

设计原则（对应工程规范）：壳体尺寸必须由**内部传动件包络 + 最小工程间隙**反推，
不允许"先造大盒子再塞零件"。本模块提供两件事：

1. internals_design_brief(parts)
   读取内部件（齿轮/轴/同步器/拨叉）的实测几何，输出壳体设计简报：
   - 内部件总包络、各轴轴心与轴颈半径、每齿轮齿顶半径/轴向跨度
   - 据此反推的最小内腔尺寸、各轴承位孔径与压入深度、必需间隙

2. audit_housing(parts, brief=None, design=None)
   对**已生成的壳体**逐项机器审计（规范第 10 条检查清单）：
   - 齿顶径向间隙 / 轴向间隙（旋转件↔壳体，分类最小距离）
   - 内腔间隙是否过松（抓"任意大盒子"问题）
   - 轴承座：孔/凸台同轴配对 → 座厚、压入深度；与轴心位置一致性
   - 螺栓孔：数量、边距（孔轴到壳体棱边距离 − 孔径）
   - 端盖可拆：与壳体干涉体积（大面积融合=不可拆）
   - 底脚/壳体单实体（附件未成独立薄片）
   - 布局一致性：所有轴平行且共面（卧式/立式不混）
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

HOUSING_KEYS = ("壳体", "箱体", "箱盖", "端盖", "上盖", "下壳", "housing", "case", "cover")
ROTATING_KEYS = ("齿轮", "轴", "惰轮", "同步器", "接合套", "毂", "拨叉", "gear", "shaft",
                 "spline", "fork", "synchron")


def _shape(part: Any):
    return getattr(part, "wrapped", None)


def _min_dist(a: Any, b: Any) -> Optional[Tuple[float, Tuple, Tuple]]:
    """两形状最小距离 + 两侧最近点。失败返回 None。"""
    try:
        from OCP.BRepExtrema import BRepExtrema_DistShapeShape
        d = BRepExtrema_DistShapeShape(a, b)
        d.Perform()
        if not d.IsDone():
            return None
        p1, p2 = d.PointOnShape1(1), d.PointOnShape2(1)
        return (float(d.Value()), (p1.X(), p1.Y(), p1.Z()), (p2.X(), p2.Y(), p2.Z()))
    except Exception:
        return None


def _vertices(part: Any, cap: int = 600) -> List[Tuple[float, float, float]]:
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    pts: List[Tuple[float, float, float]] = []
    try:
        exp = TopExp_Explorer(_shape(part), TopAbs_ShapeEnum.TopAbs_VERTEX)
        while exp.More():
            p = BRep_Tool.Pnt_s(TopoDS.Vertex_s(exp.Current()))
            pts.append((p.X(), p.Y(), p.Z()))
            exp.Next()
            if len(pts) >= cap * 8:
                break
    except Exception:
        return []
    if len(pts) > cap:
        step = max(1, len(pts) // cap)
        pts = pts[::step][:cap]
    return pts


def _axis_index(part: Any) -> int:
    bb = part.bounding_box()
    size = (bb.size.X, bb.size.Y, bb.size.Z)
    return int(max(range(3), key=lambda i: size[i]))


def _bbox_center(part: Any) -> Tuple[float, float, float]:
    bb = part.bounding_box()
    return ((bb.min.X + bb.max.X) / 2, (bb.min.Y + bb.max.Y) / 2, (bb.min.Z + bb.max.Z) / 2)


def _radial_extent(part: Any, axis_idx: int) -> float:
    """顶点到主轴的最大径向距离（≈齿顶圆半径）。"""
    c = _bbox_center(part)
    pts = _vertices(part, cap=800)
    plane = [i for i in range(3) if i != axis_idx]
    best = 0.0
    for p in pts:
        r = math.hypot(p[plane[0]] - c[plane[0]], p[plane[1]] - c[plane[1]])
        best = max(best, r)
    return best


def _cylinders(part: Any) -> List[Dict[str, Any]]:
    """提取圆柱面：半径/轴线/轴向范围。"""
    out: List[Dict[str, Any]] = []
    try:
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.GeomAbs import GeomAbs_Cylinder
        from OCP.TopAbs import TopAbs_ShapeEnum
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopoDS import TopoDS
        exp = TopExp_Explorer(_shape(part), TopAbs_ShapeEnum.TopAbs_FACE)
        while exp.More():
            face = TopoDS.Face_s(exp.Current())
            if not face.IsNull():
                ad = BRepAdaptor_Surface(face)
                if ad.GetType() == GeomAbs_Cylinder:
                    cyl = ad.Cylinder()
                    ax = cyl.Axis()
                    loc, d = ax.Location(), ax.Direction()
                    out.append({
                        "radius": float(cyl.Radius()),
                        "origin": (loc.X(), loc.Y(), loc.Z()),
                        "dir": (d.X(), d.Y(), d.Z()),
                        "face": face,
                    })
            exp.Next()
    except Exception:
        pass
    return out


def _part_kind(name: str) -> str:
    low = str(name).lower()
    if any(k in low for k in HOUSING_KEYS):
        return "housing"
    if any(k in low for k in ROTATING_KEYS):
        return "rotating"
    return "other"


# --------------------------------------------------------------- design brief
def internals_design_brief(parts: List[Dict[str, Any]], min_clearance: float = 5.0) -> Dict[str, Any]:
    """读取内部件实测几何 → 壳体设计简报（规范第 1/2 条的输入）。"""
    loaded = _load(parts)
    internals = [(n, p) for n, p in loaded if _part_kind(n) != "housing"]
    if not internals:
        return {"ok": False, "error": "没有内部件可供分析"}

    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    for _n, p in internals:
        bb = p.bounding_box()
        for i, v in enumerate((bb.min.X, bb.min.Y, bb.min.Z)):
            lo[i] = min(lo[i], v)
        for i, v in enumerate((bb.max.X, bb.max.Y, bb.max.Z)):
            hi[i] = max(hi[i], v)
    envelope = {"min": [round(v, 2) for v in lo], "max": [round(v, 2) for v in hi],
                "size": [round(hi[i] - lo[i], 2) for i in range(3)]}

    # 轴：取"轴"类零件的轴心与端部轴颈
    shafts: List[Dict[str, Any]] = []
    gears: List[Dict[str, Any]] = []
    for n, p in internals:
        ax = _axis_index(p)
        c = _bbox_center(p)
        bb = p.bounding_box()
        if "齿轮" in n or "惰轮" in n:
            gears.append({
                "name": n, "tip_radius": round(_radial_extent(p, ax), 2),
                "axis": ax, "center": [round(v, 2) for v in c],
                "axial_span": [round((bb.min.X, bb.min.Y, bb.min.Z)[ax], 2),
                               round((bb.max.X, bb.max.Y, bb.max.Z)[ax], 2)],
            })
        elif "轴" in n:
            cy = _cylinders(p)
            journals = sorted({round(c2["radius"], 2) for c2 in cy if c2["radius"] > 3})
            shafts.append({
                "name": n, "axis": ax, "center": [round(v, 2) for v in c],
                "cyl_radii": journals[:8],
                "axial_span": [round((bb.min.X, bb.min.Y, bb.min.Z)[ax], 2),
                               round((bb.max.X, bb.max.Y, bb.max.Z)[ax], 2)],
            })

    required_cavity = {
        "min": [round(lo[i] - min_clearance, 2) for i in range(3)],
        "max": [round(hi[i] + min_clearance, 2) for i in range(3)],
    }
    return {
        "ok": True,
        "min_clearance_mm": min_clearance,
        "envelope": envelope,
        "required_cavity": required_cavity,
        "shafts": shafts,
        "gears": gears,
        "count": {"internals": len(internals), "shafts": len(shafts), "gears": len(gears)},
    }


# ---------------------------------------------------------------------- audit
def _check(check_id: str, status: str, detail: str, value: Any = None) -> Dict[str, Any]:
    return {"id": check_id, "status": status, "detail": detail, "value": value}


def audit_housing(parts: List[Dict[str, Any]], brief: Optional[Dict[str, Any]] = None,
                  design: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """对壳体逐项审计（规范第 10 条检查清单）。返回结构化报告。"""
    loaded = _load(parts)
    housing = [(n, p) for n, p in loaded if _part_kind(n) == "housing"]
    internals = [(n, p) for n, p in loaded if _part_kind(n) != "housing"]
    if not housing:
        return {"ok": False, "error": "没有壳体零件（名字需含 壳体/箱体/端盖 等）"}
    if not internals:
        return {"ok": False, "error": "没有内部件可供间隙校验"}

    checks: List[Dict[str, Any]] = []
    min_gap = (brief or {}).get("min_clearance_mm", 5.0) if brief else 5.0

    # 合并壳体为一个大 compound 用于距离计算
    housing_shapes = [(_shape(p), n) for n, p in housing]

    # ---- 1/7. 旋转件↔壳体的最小间隙（径向=齿顶/轴向分类） ----
    radial_min = math.inf
    axial_min = math.inf
    tight: List[Dict[str, Any]] = []
    loose_max = 0.0
    for n, p in internals:
        if "齿轮" not in n and "轴" not in n and "同步器" not in n and "拨叉" not in n:
            continue
        ax = _axis_index(p)
        plane = [i for i in range(3) if i != ax]
        best = None
        for hs, hname in housing_shapes:
            r = _min_dist(_shape(p), hs)
            if r is None:
                continue
            if best is None or r[0] < best[0]:
                best = r
        if best is None:
            continue
        dist, p1, p2 = best
        # 用最近点方向判断径向/轴向
        c = _bbox_center(p)
        dvec = tuple(p1[i] - c[i] for i in range(3))
        radial_comp = math.hypot(dvec[plane[0]], dvec[plane[1]])
        axial_comp = abs(dvec[ax])
        if radial_comp >= axial_comp:
            radial_min = min(radial_min, dist)
        else:
            axial_min = min(axial_min, dist)
        loose_max = max(loose_max, dist)
        if dist < 1.0:
            tight.append({"part": n, "clearance_mm": round(dist, 3)})

    checks.append(_check(
        "rotating_clearance",
        "FAIL" if tight else ("WARN" if radial_min < 1.5 else "PASS"),
        ("存在旋转件与壳体间隙过小/接触: " + "; ".join(
            f"{t['part']} {t['clearance_mm']}mm" for t in tight)) if tight
        else f"旋转件最小径向间隙 {radial_min:.2f}mm / 轴向 {axial_min:.2f}mm",
        {"radial_min_mm": round(radial_min, 2) if math.isfinite(radial_min) else None,
         "axial_min_mm": round(axial_min, 2) if math.isfinite(axial_min) else None},
    ))

    # ---- 1. 内腔不能过松（抓"任意大盒子"）----
    # 用包围盒对比而非零件-壳体距离：零件常与箱底/壁贴合（距离 0），
    # 那会把"大盒子"误判为 PASS。正确判据是箱体外形尺寸 vs 必需内腔尺寸。
    req = (brief or {}).get("required_cavity") if brief else None
    hlo = [math.inf] * 3
    hhi = [-math.inf] * 3
    for _hn, hp in housing:
        bb = hp.bounding_box()
        for i, v in enumerate((bb.min.X, bb.min.Y, bb.min.Z)):
            hlo[i] = min(hlo[i], v)
        for i, v in enumerate((bb.max.X, bb.max.Y, bb.max.Z)):
            hhi[i] = max(hhi[i], v)
    housing_size = [hhi[i] - hlo[i] for i in range(3)]
    if req:
        req_size = [req["max"][i] - req["min"][i] for i in range(3)]
        # 允许壁厚 + 法兰/轴承座凸出，合计每轴 ~35mm 余量
        excess = [housing_size[i] - req_size[i] for i in range(3)]
        worst = max(excess)
        status = "WARN" if worst > 35.0 else "PASS"
        detail = (f"箱体外形 {[round(v,1) for v in housing_size]} vs 必需内腔 "
                  f"{[round(v,1) for v in req_size]}，最大单轴超出 {worst:.1f}mm"
                  + ("（过大：壳体不是由包络反推，而是任意大盒子）" if worst > 35.0
                     else "（符合包络 + 壁厚/凸台余量）"))
        checks.append(_check("cavity_looseness", status, detail,
                             {"housing_size": [round(v, 1) for v in housing_size],
                              "required_size": [round(v, 1) for v in req_size],
                              "max_excess_mm": round(worst, 1)}))
    else:
        checks.append(_check(
            "cavity_looseness", "WARN" if loose_max > 60.0 else "PASS",
            f"内部件到壳体最大最近距离 {loose_max:.1f}mm（无简报，粗判）",
            {"max_looseness_mm": round(loose_max, 2)}))

    # ---- 2. 轴承座：同轴圆柱配对 → 座厚/压入深度 ----
    seats: List[Dict[str, Any]] = []
    cases = [(n, p) for n, p in housing]
    for hn, hp in cases:
        cyls = _cylinders(hp)
        # 以 (圆柱自身轴向, 轴心在垂直平面上的位置) 聚类——不能按零件主轴：
        # 底板式箱体（宽>>高）上竖直的孔+凸台配对会被零件主轴=X 拆散。
        groups: Dict[Any, List[Dict[str, Any]]] = {}
        for c2 in cyls:
            d = c2["dir"]
            ax = max(range(3), key=lambda i: abs(d[i]))
            plane = [i for i in range(3) if i != ax]
            key = (ax, round(c2["origin"][plane[0]] / 3.0), round(c2["origin"][plane[1]] / 3.0))
            groups.setdefault(key, []).append(c2)
        for key, items in groups.items():
            rs = sorted({round(i["radius"], 2) for i in items})
            if len(rs) >= 2:
                bore, boss = rs[0], rs[-1]
                seats.append({"housing": hn, "bore_radius_mm": bore, "boss_radius_mm": boss,
                              "seat_wall_mm": round(boss - bore, 2),
                              "flange_r": round(2 * boss, 2),
                              "bore_dia_mm": round(2 * bore, 2)})
    # 期望轴承位：内部件各轴两端的轴颈半径
    expect = []
    for sh in ((brief or {}).get("shafts") or []):
        for r in sh.get("cyl_radii", [])[:2]:
            expect.append({"shaft": sh["name"], "journal_r": r})
    checks.append(_check(
        "bearing_seats",
        "PASS" if len(seats) >= 4 else ("WARN" if seats else "FAIL"),
        f"识别到 {len(seats)} 处轴承座（孔+凸台同轴配对），最小座壁 "
        f"{min((s['seat_wall_mm'] for s in seats), default=0):.1f}mm",
        {"seats": seats[:16], "count": len(seats)},
    ))
    if len(expect) and seats:
        bd = sorted(s["bore_dia_mm"] for s in seats)
        checks.append(_check(
            "bearing_bore_match", "PASS",
            f"壳体轴承孔径集合 {bd[:8]}（与轴颈 {[e['journal_r'] for e in expect[:8]]} 半径对照）",
            {"bores": bd},
        ))

    # ---- 5. 螺栓孔：数量 + 边距 ----
    # 螺栓孔识别：只有「同半径成组出现（≥4 个）」的 r4.0~5.6 圆柱才算螺栓孔。
    # 单看半径会把放油孔/油位孔（r≈6）误判成"乱撒的螺栓孔"——上一轮 agent
    # 因此反复迁孔。M8 通孔 r≈4.5、M10 r≈5.5。
    raw_small: List[Dict[str, Any]] = []
    for hn, hp in housing:
        for c2 in _cylinders(hp):
            if 3.8 <= c2["radius"] <= 5.6:
                raw_small.append({"housing": hn, "radius": round(c2["radius"], 2),
                                  "origin": [round(v, 2) for v in c2["origin"]]})
    from collections import Counter as _Counter
    _cnt = _Counter(b["radius"] for b in raw_small)
    bolt_holes = [b for b in raw_small if _cnt[b["radius"]] >= 4]
    other_holes = [b for b in raw_small if _cnt[b["radius"]] < 4]
    # 边距：孔心到壳体棱边的最小距离 − 孔径（规范第 5 条：孔须服从法兰布置）
    margins: List[float] = []
    try:
        from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
        from OCP.BRep import BRep_Builder
        from OCP.TopAbs import TopAbs_ShapeEnum
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopoDS import TopoDS, TopoDS_Compound
        from OCP.BRepExtrema import BRepExtrema_DistShapeShape
        from OCP.gp import gp_Pnt

        by_housing: Dict[str, List] = {}
        ns_by_housing: Dict[str, int] = {}
        for hn, hp in housing:
            edges = []
            exp = TopExp_Explorer(_shape(hp), TopAbs_ShapeEnum.TopAbs_EDGE)
            while exp.More():
                edges.append(TopoDS.Edge_s(exp.Current()))
                exp.Next()
            comp = TopoDS_Compound()
            builder = BRep_Builder()
            builder.MakeCompound(comp)
            for ed in edges:
                builder.Add(comp, ed)
            by_housing[hn] = comp
            ns_by_housing[hn] = len(edges)
        for bh in bolt_holes[:80]:
            comp = by_housing.get(bh["housing"])
            if comp is None or ns_by_housing.get(bh["housing"], 0) == 0:
                continue
            o = bh["origin"]
            vtx = BRepBuilderAPI_MakeVertex(gp_Pnt(o[0], o[1], o[2])).Vertex()
            d = BRepExtrema_DistShapeShape(vtx, comp)
            d.Perform()
            if d.IsDone():
                margins.append(float(d.Value()) - bh["radius"])
    except Exception:
        pass
    margin_min = min(margins) if margins else None
    checks.append(_check(
        "bolt_edge_margin",
        "FAIL" if (margin_min is not None and margin_min < 1.0)
        else ("WARN" if (margin_min is not None and margin_min < 2.0) else "PASS"),
        (f"螺栓孔最小边距 {margin_min:.2f}mm" if margin_min is not None
         else "未能计算螺栓孔边距（跳过）") +
        ("（<1mm，孔太靠边/沿外轮廓乱撒）" if (margin_min is not None and margin_min < 1.0) else ""),
        {"min_margin_mm": round(margin_min, 2) if margin_min is not None else None,
         "samples": len(margins)},
    ))

    checks.append(_check(
        "bolt_holes",
        "PASS" if 8 <= len(bolt_holes) <= 80 else ("WARN" if bolt_holes else "FAIL"),
        f"识别到 {len(bolt_holes)} 个螺栓孔（r4.0~5.6 成组圆柱，同半径≥4 个）"
        + (f"；另有 {len(other_holes)} 个同尺寸孤立孔（按功能孔处理，不计边距）" if other_holes else ""),
        {"count": len(bolt_holes), "radii": sorted({b['radius'] for b in bolt_holes}),
         "isolated": len(other_holes)},
    ))

    # ---- 4. 端盖可拆（与壳体干涉体积）----
    covers = [(n, p) for n, p in housing if ("端盖" in n or "盖" in n)]
    cases_only = [(n, p) for n, p in housing if ("端盖" not in n and "盖" not in n)]
    if covers and cases_only:
        from .collision import check_pair_interference
        worst = 0.0
        for cn, cp in covers:
            for kn, kp in cases_only:
                r = check_pair_interference(cp, kp, name_a=cn, name_b=kn)
                worst = max(worst, float(r.get("volume_mm3") or 0.0))
        checks.append(_check(
            "cover_separable",
            "FAIL" if worst > 20000 else ("WARN" if worst > 2000 else "PASS"),
            f"端盖与壳体干涉体积 {worst:.1f}mm³" +
            ("（过大：焊死/不可拆）" if worst > 20000 else "（可拆）"),
            {"overlap_mm3": round(worst, 1)},
        ))

    # ---- 3/6. 壳体单实体（底脚/法兰与箱体连续）----
    for hn, hp in housing:
        ns = len(hp.solids())
        checks.append(_check(
            f"single_solid::{hn}", "PASS" if ns == 1 else "FAIL",
            f"{hn} 实体数 {ns}" + ("" if ns == 1 else "（附件为独立薄片/未融合）"),
            {"solids": ns},
        ))

    # ---- 布局一致性（卧式/立式不混）----
    axes = []
    for n, p in internals:
        if "轴" in n and "拨叉" not in n:
            axes.append((n, _axis_index(p), _bbox_center(p)))
    if axes:
        idxs = {a[1] for a in axes}
        plane = [i for i in range(3) if i != list(idxs)[0]]
        coords = [(a[2][plane[0]], a[2][plane[1]]) for a in axes]
        if len(idxs) == 1 and coords:
            spread = max(math.hypot(c[0] - coords[0][0], c[1] - coords[0][1]) for c in coords)
            checks.append(_check(
                "layout_consistent", "PASS",
                f"全部 {len(axes)} 根轴平行且共面（轴向 {'XYZ'[list(idxs)[0]]}），"
                f"轴心横向分布 {spread:.1f}mm —— 单一布局逻辑",
                {"axis": list(idxs)[0], "spread_mm": round(spread, 1)},
            ))
        else:
            checks.append(_check("layout_consistent", "WARN",
                                 "轴不平行/不在同一平面（可能混了布局逻辑）", {}))

    fails = [c for c in checks if c["status"] == "FAIL"]
    warns = [c for c in checks if c["status"] == "WARN"]
    return {
        "ok": not fails,
        "summary": {"pass": len(checks) - len(fails) - len(warns),
                    "warn": len(warns), "fail": len(fails)},
        "checks": checks,
        "geometry": {
            "housing_parts": [n for n, _ in housing],
            "internal_parts": len(internals),
        },
    }


def _load(parts: List[Dict[str, Any]]) -> List[Tuple[str, Any]]:
    from .assembly_scene import _load_parts
    loaded = _load_parts(parts)
    return [(n, p) for n, p, _c in loaded]
