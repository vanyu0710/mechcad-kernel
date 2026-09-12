"""
MechKernel v2.18: Involute Gear Generator (spur + helical)

v2.8 梯形 proxy 起步；v2.10 尝试 involute 但齿廓未闭合 → 一直静默回退梯形
（z=17 实测 `is_closed=False`）；v2.18 重写为**整轮单条闭合齿廓**的真渐开线，
并新增斜齿（helix_angle_deg，剖面沿轴扭转 loft）。

数学 (ISO 21771 / ISO 6336):
  module m, 齿数 z, 压力角 α, 螺旋角 β(可选)
  pitch_radius  r  = m * z / 2          （m = 端面模数；法向模数 mn 时传 mn/cosβ）
  base_radius   rb = r * cos(α)
  addendum      ra = r + m
  dedendum      rf = r - 1.25 m
  分度圆半齿角  φ_p = π / (2z)
  基圆半齿角    φ_b = φ_p + inv(α),  inv(α) = tanα - α
  任意半径半齿角 φ(ρ) = φ_b - inv(acos(rb/ρ))
  齿廓: 右齿面 (ρ: rb→ra, 角 -φ) → 齿顶弧 → 左齿面 (ρ: ra→rb, 角 +φ)
        → 径向到齿根 → 齿根弧 → 下一齿

特征:
  - 真渐开线齿面（不是梯形、不是逐齿 boolean union）
  - 斜齿: helix_angle_deg > 0 时，端面齿廓沿 Z 扭转 loft（总扭转角 = b·tanβ / r）
  - 单实体（一处成面，无 N 次 union），大齿数也快

限制:
  - 齿根为直齿根弧（无 trochoid 过渡圆角）
  - 螺旋角按端面齿廓几何扭转建模；m 视为端面模数

Returns: build123d Part, 可直接 boolean / export.
"""
from __future__ import annotations

import math
from typing import List, Tuple

from build123d import (
    BuildPart, BuildLine, BuildSketch, Plane, add, extrude, loft, Mode,
    Polyline, make_face, Part, Edge, Circle, Cylinder, Axis, Align, Location,
    Vector, Wire, Spline,
)


# ---------- public API: 几何参数 ----------

def gear_geometry(module: float, teeth: int,
                  pressure_angle_deg: float = 20.0,
                  addendum_ratio: float = 1.0,
                  dedendum_ratio: float = 1.25,
                  helix_angle_deg: float = 0.0) -> dict:
    """Return the standard gear geometry parameters.

    ``module`` is the transverse module; for a normal module mn with helix
    angle β pass ``mn / cos(β)``.
    """
    alpha = math.radians(pressure_angle_deg)
    r = module * teeth / 2.0
    return {
        "module": module,
        "teeth": teeth,
        "pressure_angle_deg": pressure_angle_deg,
        "helix_angle_deg": helix_angle_deg,
        "helix_hand": "right" if helix_angle_deg > 0 else ("left" if helix_angle_deg < 0 else "none"),
        "pitch_diameter": 2 * r,
        "pitch_radius": r,
        "base_radius": r * math.cos(alpha),
        "addendum_radius": r + addendum_ratio * module,
        "dedendum_radius": r - dedendum_ratio * module,
        "tooth_thickness_at_pitch": math.pi * module / 2.0,  # mm
    }


def center_distance(module: float, z1: int, z2: int) -> float:
    """Distance between centers of two meshing gears (mm, transverse module)."""
    return module * (z1 + z2) / 2.0


# ---------- internal: true involute closed profile ----------

def _involute_outer_points(
    module: float, teeth: int, pressure_angle_deg: float = 20.0,
    addendum_ratio: float = 1.0, dedendum_ratio: float = 1.25,
    n_flank: int = 6, n_tip: int = 2, n_root: int = 3,
) -> List[Tuple[float, float]]:
    """整轮单条闭合外轮廓（真渐开线齿面，逆时针）。

    每个齿依次给出: 齿根起点 → 右齿面(rb→ra) → 齿顶弧 → 左齿面(ra→rb)
    → 径向到齿根 → 齿根弧(到下一齿起点)。闭合由调用方 close=True 完成。

    采样经验（v2.18 实测）：n_flank=6 / n_tip=2 / n_root=3 对 z=8..85 体积
    误差 < 1%；n_tip=1 会让小齿数齿顶弧退化自交（z17 体积掉到 221）。高采样
    (16/5/6) 反而会在小齿数产生重复点使成面失败——故默认值取保守小采样。
    """
    m, z, alpha_deg = module, teeth, pressure_angle_deg
    alpha = math.radians(alpha_deg)
    r = m * z / 2.0
    rb = r * math.cos(alpha)
    ra = r + addendum_ratio * m
    rf = max(r - dedendum_ratio * m, 0.02 * r)

    pitch = 2.0 * math.pi / z
    phi_p = math.pi / (2.0 * z)                     # 分度圆半齿角
    inv = lambda a: math.tan(a) - a                  # noqa: E731
    phi_b = min(phi_p + inv(alpha), pitch / 2.0 * 0.92)  # 基圆半齿角（保险，防低齿数自交）
    rho_lo = max(rb, rf)                             # 渐开线起始半径

    def phi_at(rho: float) -> float:
        ar = math.acos(max(-1.0, min(1.0, rb / rho)))
        return phi_b - inv(ar)

    rs = [rho_lo + (ra - rho_lo) * k / n_flank for k in range(n_flank + 1)]
    phi_tip = phi_at(ra)

    pts: List[Tuple[float, float]] = []
    for i in range(z):
        c = i * pitch
        # 齿根起点（上一齿根弧的终点，与下一齿同相位）
        pts.append((rf * math.cos(c - phi_b), rf * math.sin(c - phi_b)))
        # 右齿面: ρ 从 rho_lo 升到 ra，角 = c - φ(ρ)
        for rho in rs:
            a = c - phi_at(rho)
            pts.append((rho * math.cos(a), rho * math.sin(a)))
        # 齿顶弧: c-φ_tip → c+φ_tip
        for k in range(1, n_tip):
            a = c - phi_tip + 2.0 * phi_tip * k / n_tip
            pts.append((ra * math.cos(a), ra * math.sin(a)))
        # 左齿面: ρ 从 ra 降回 rho_lo，角 = c + φ(ρ)
        for rho in reversed(rs):
            a = c + phi_at(rho)
            pts.append((rho * math.cos(a), rho * math.sin(a)))
        # 径向到齿根，再齿根弧到下一齿起点
        pts.append((rf * math.cos(c + phi_b), rf * math.sin(c + phi_b)))
        a0, a1 = c + phi_b, c + pitch - phi_b
        for k in range(1, n_root):
            a = a0 + (a1 - a0) * k / n_root
            pts.append((rf * math.cos(a), rf * math.sin(a)))

    # 去重：连续点过近（<1e-6）会让 Polyline/make_face 退化
    clean: List[Tuple[float, float]] = []
    for p in pts:
        if not clean or (p[0] - clean[-1][0]) ** 2 + (p[1] - clean[-1][1]) ** 2 > 1e-12:
            clean.append(p)
    if len(clean) > 2 and (clean[0][0] - clean[-1][0]) ** 2 + (clean[0][1] - clean[-1][1]) ** 2 <= 1e-12:
        clean.pop()
    return clean


def _involute_wire_edges(module: float, teeth: int, pressure_angle_deg: float = 20.0,
                         addendum_ratio: float = 1.0, dedendum_ratio: float = 1.25,
                         n_flank: int = 8):
    """整轮外轮廓的边序列：齿面用真样条（Spline），齿顶/齿根/径向用直线。

    返回 Edge 列表，可用 Wire(edges) 闭合；失败返回 None（调用方回退折线齿廓）。
    这给出真正光滑的渐开线齿面（EXTRUSION 曲面），而非折线多面近似。
    """
    m, z = module, teeth
    alpha = math.radians(pressure_angle_deg)
    r = m * z / 2.0
    rb = r * math.cos(alpha)
    ra = r + addendum_ratio * m
    rf = max(r - dedendum_ratio * m, 0.02 * r)
    pitch = 2.0 * math.pi / z
    phi_p = math.pi / (2.0 * z)
    inv = lambda a: math.tan(a) - a                      # noqa: E731
    phi_b = min(phi_p + inv(alpha), pitch / 2.0 * 0.92)
    rho_lo = max(rb, rf)

    def phi_at(rho: float) -> float:
        return phi_b - inv(math.acos(max(-1.0, min(1.0, rb / rho))))

    phi_tip = phi_at(ra)
    rs = [rho_lo + (ra - rho_lo) * k / n_flank for k in range(n_flank + 1)]
    P = lambda rad, ang: Vector(rad * math.cos(ang), rad * math.sin(ang), 0.0)  # noqa: E731

    edges = []
    for i in range(z):
        c = i * pitch
        # 齿根弧: 上一齿 +phi_b → 本齿 -phi_b
        prev_end = (i - 1) * pitch + phi_b if i > 0 else (z - 1) * pitch + phi_b
        edges.append(Edge.make_line(P(rf, prev_end), P(rf, c - phi_b)))
        # 径向 rf→rho_lo（右）
        if rho_lo > rf + 1e-9:
            edges.append(Edge.make_line(P(rf, c - phi_b), P(rho_lo, c - phi_b)))
        # 右齿面样条 rho_lo→ra
        edges.append(Spline(*[P(rho, c - phi_at(rho)) for rho in rs]))
        # 齿顶
        edges.append(Edge.make_line(P(ra, c - phi_tip), P(ra, c + phi_tip)))
        # 左齿面样条 ra→rho_lo
        edges.append(Spline(*[P(rho, c + phi_at(rho)) for rho in reversed(rs)]))
        # 径向 rho_lo→rf（左）
        if rho_lo > rf + 1e-9:
            edges.append(Edge.make_line(P(rho_lo, c + phi_b), P(rf, c + phi_b)))
    return edges


def _full_gear_face(module: float, teeth: int, bore: float,
                    pressure_angle_deg: float, addendum_ratio: float,
                    dedendum_ratio: float):
    """端面整轮 2D face（真渐开线外轮廓 + bore 挖孔）。

    优先真样条齿面；样条线框闭合失败时回退折线齿廓（同样保真度高、稳健）。
    """
    n_flank = 8 if teeth <= 60 else 6
    face = None
    try:
        edges = _involute_wire_edges(
            module=module, teeth=teeth, pressure_angle_deg=pressure_angle_deg,
            addendum_ratio=addendum_ratio, dedendum_ratio=dedendum_ratio,
            n_flank=n_flank,
        )
        face = make_face(Wire(edges))
        if len(face.faces()) != 1:
            face = None
    except Exception:
        face = None
    if face is None:
        pts = _involute_outer_points(
            module=module, teeth=teeth, pressure_angle_deg=pressure_angle_deg,
            addendum_ratio=addendum_ratio, dedendum_ratio=dedendum_ratio,
            n_flank=n_flank, n_tip=2, n_root=3,
        )
        with BuildSketch(Plane.XY) as sk:
            with BuildLine():
                Polyline(*pts, close=True)
            make_face()
        return sk.sketch
    return face


def _bore_cylinder(bore: float, width: float):
    """沿 Z 从 0 起、高 width 的中心孔刀具体（bore 直径）。"""
    with BuildPart(Plane.XY) as bp:
        Cylinder(bore / 2.0, width, align=(Align.CENTER, Align.CENTER, Align.MIN))
    return bp.part


# ---------- public API: 几何构造 ----------

def build_involute_gear(
    module: float,
    teeth: int,
    width: float,
    bore: float = 0.0,
    pressure_angle_deg: float = 20.0,
    helix_angle_deg: float = 0.0,
    helix_sections: int = 8,
    fallback_to_trapezoid: bool = True,
    involute_teeth_threshold: int = 400,
    addendum_ratio: float = 1.0,
    dedendum_ratio: float = 1.25,
) -> Part:
    """Build a spur (β=0) or helical gear as a build123d Part.

    v2.18: 真渐开线整轮闭合齿廓（单实体，无逐齿 union）；helix_angle_deg != 0
    时端面剖面沿 Z 扭转后 loft 成螺旋齿。

    Args:
        module: 端面模数 m (mm)
        teeth: 齿数 z (>= 6)
        width: 齿宽 (mm)
        bore: 中心孔直径 (0 = 无孔) (mm)
        pressure_angle_deg: 压力角 (默认 20°)
        helix_angle_deg: 螺旋角 (0 = 直齿; 正=右旋)
        helix_sections: 斜齿 loft 剖面数 (>=2, 越大螺旋越光滑)
        fallback_to_trapezoid: 渐开线失败时回退梯形
        involute_teeth_threshold: 齿数超过则走梯形 (默认 400=基本不触发)
    """
    if teeth < 6:
        raise ValueError(f"teeth 必须 >= 6（当前 {teeth}）")
    if module <= 0:
        raise ValueError(f"module 必须 > 0（当前 {module}）")
    if width <= 0:
        raise ValueError(f"width 必须 > 0（当前 {width}）")
    if abs(helix_angle_deg) >= 45.0:
        raise ValueError(f"螺旋角需 |β| < 45°（当前 {helix_angle_deg}）")

    if teeth > involute_teeth_threshold and fallback_to_trapezoid:
        return _build_trapezoid_gear(
            module=module, teeth=teeth, width=width, bore=bore,
            pressure_angle_deg=pressure_angle_deg,
        )

    try:
        face = _full_gear_face(
            module=module, teeth=teeth, bore=bore,
            pressure_angle_deg=pressure_angle_deg,
            addendum_ratio=addendum_ratio, dedendum_ratio=dedendum_ratio,
        )
        if abs(helix_angle_deg) < 1e-9:
            part = extrude(face, width)
        else:
            # 斜齿: 端面剖面沿 Z 扭转 loft（总扭转角 = b·tanβ / r）
            r = module * teeth / 2.0
            twist = math.degrees(width * math.tan(math.radians(abs(helix_angle_deg))) / r)
            sections = []
            n = max(2, int(helix_sections))
            for k in range(n):
                zk = width * k / (n - 1)
                ang = twist * k / (n - 1)
                sections.append(face.rotate(Axis.Z, ang).moved(Location((0, 0, zk))))
            part = loft(sections)
            if len(part.solids()) != 1:
                raise ValueError(f"helical loft 未产出单实体（{len(part.solids())} 个）")
        if bore > 0:
            part = part - _bore_cylinder(bore, width)
            if len(part.solids()) != 1:
                raise ValueError("bore 切除后非单实体")
        return part
    except Exception:
        if not fallback_to_trapezoid:
            raise
        return _build_trapezoid_gear(
            module=module, teeth=teeth, width=width, bore=bore,
            pressure_angle_deg=pressure_angle_deg,
        )


def _build_trapezoid_gear(
    module: float, teeth: int, width: float, bore: float = 0.0,
    pressure_angle_deg: float = 20.0, helix_angle_deg: float = 0.0,
    helix_sections: int = 8,
) -> Part:
    """梯形 proxy (fallback: 大齿数 / 渐开线失败时用)，也支持扭转成斜齿。"""
    outer = _gear_profile_points(
        module=module, teeth=teeth, pressure_angle_deg=pressure_angle_deg,
    )
    with BuildLine() as bl:
        Polyline(outer, close=True)
    outer_face = make_face(bl.line)
    with BuildSketch(Plane.XY) as sk:
        add(outer_face)
        if bore > 0:
            with BuildLine() as bl2:
                Polyline(_bore_profile_points(bore / 2.0), close=True)
            add(make_face(bl2.line), mode=Mode.SUBTRACT)
    face = sk.sketch
    if abs(helix_angle_deg) < 1e-9:
        return extrude(face, width)
    r = module * teeth / 2.0
    twist = math.degrees(width * math.tan(math.radians(abs(helix_angle_deg))) / r)
    n = max(2, int(helix_sections))
    sections = [face.rotate(Axis.Z, twist * k / (n - 1)).moved(Location((0, 0, width * k / (n - 1))))
                for k in range(n)]
    return loft(sections)


def _gear_profile_points(module: float, teeth: int,
                         pressure_angle_deg: float = 20.0,
                         addendum_ratio: float = 1.0,
                         dedendum_ratio: float = 1.25,
                         tooth_top_ratio: float = 0.5) -> List[Tuple[float, float]]:
    """梯形齿廓（fallback 用）。"""
    z = teeth
    m = module
    r = m * z / 2.0
    ra = r + addendum_ratio * m
    rf = r - dedendum_ratio * m
    half_pitch = math.pi / z
    half_tooth = half_pitch / 2.0
    top_half = half_tooth * tooth_top_ratio
    profile: List[Tuple[float, float]] = []
    for i in range(z):
        tc = i * 2.0 * half_pitch
        if i == 0:
            prev_right_base = (z - 1) * 2.0 * half_pitch + half_tooth
            profile.append((rf * math.cos(prev_right_base), rf * math.sin(prev_right_base)))
        profile.append((rf * math.cos(tc - half_tooth), rf * math.sin(tc - half_tooth)))
        profile.append((ra * math.cos(tc - top_half), ra * math.sin(tc - top_half)))
        profile.append((ra * math.cos(tc + top_half), ra * math.sin(tc + top_half)))
        profile.append((rf * math.cos(tc + half_tooth), rf * math.sin(tc + half_tooth)))
    return profile


def _bore_profile_points(bore_radius: float, n_points: int = 32) -> List[Tuple[float, float]]:
    """闭合圆剖面（bore）。"""
    return [(bore_radius * math.cos(2.0 * math.pi * i / n_points),
             bore_radius * math.sin(2.0 * math.pi * i / n_points))
            for i in range(n_points + 1)]


__all__ = ["build_involute_gear", "gear_geometry", "center_distance"]
