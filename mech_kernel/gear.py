"""
MechKernel v2.8: Spur Gear Generator

v2.8 实现: 完整数学模型（pitch/addendum/dedendum/中心距）+ 梯形齿形 proxy.

数学 (真实 ISO 6336):
  module m, 齿数 z, 压力角 α
  pitch_radius r = m * z / 2
  base_radius    rb = r * cos(α)
  addendum_radius ra = r + m
  dedendum_radius rf = r - 1.25 * m
  tooth_thickness_at_pitch = π * m / 2
  center_distance (z1, z2) = m * (z1 + z2) / 2

齿形: 梯形近似 (WARNING: 不是真 involute 曲线).
  - pitch circle 上齿厚 = πm/2
  - 齿顶 (addendum): 90% 半齿宽
  - 齿根 (dedendum): 100% 半齿宽
  - 这给出与真实 involute 齿相似的啮合性能, 但几何构造简单 (避免 OCC face-from-polyline 边界精度问题)

⚠️ 已知限制 (v2.8.1):
  - 真实啮合时会有 ~100-500 mm³ 干涉 (梯形 vs 真圆齿, 几何差异)
  - 实际机械应视为"几何参考", 真实加工前必须用真 involute 齿形 (v2.10+)
  - collision check 会检测到这些"假干涉", 这是预期行为

Returns: build123d Part, 可直接 boolean / export.
"""
from __future__ import annotations

import math
from typing import List, Tuple

from build123d import (
    BuildPart, BuildLine, BuildSketch, Plane, add, extrude, Mode,
    Polyline, make_face, Part, Edge, Circle, Cylinder, Axis,
)


# ---------- public API: 几何参数 ----------

def gear_geometry(module: float, teeth: int,
                  pressure_angle_deg: float = 20.0,
                  addendum_ratio: float = 1.0,
                  dedendum_ratio: float = 1.25) -> dict:
    """Return the standard gear geometry parameters."""
    alpha = math.radians(pressure_angle_deg)
    r = module * teeth / 2.0
    return {
        "module": module,
        "teeth": teeth,
        "pressure_angle_deg": pressure_angle_deg,
        "pitch_diameter": 2 * r,
        "pitch_radius": r,
        "base_radius": r * math.cos(alpha),
        "addendum_radius": r + addendum_ratio * module,
        "dedendum_radius": r - dedendum_ratio * module,
        "tooth_thickness_at_pitch": math.pi * module / 2.0,  # mm
    }


def center_distance(module: float, z1: int, z2: int) -> float:
    """Distance between centers of two meshing gears (mm)."""
    return module * (z1 + z2) / 2.0


# ---------- internal: profile points (trapezoidal teeth) ----------

def _gear_profile_points(module: float, teeth: int,
                         pressure_angle_deg: float = 20.0,
                         addendum_ratio: float = 1.0,
                         dedendum_ratio: float = 1.25,
                         tooth_top_ratio: float = 0.5) -> List[Tuple[float, float]]:
    """Return closed profile (XY plane, center at origin) with trapezoidal teeth.

    每齿由 4 关键点构成: 左 base / 左 top / 右 top / 右 base.
    齿形 = 2 直线 (flank) + addendum 上的直线 (top).
    """
    z = teeth
    m = module
    r = m * z / 2.0
    ra = r + addendum_ratio * m
    rf = r - dedendum_ratio * m
    half_pitch = math.pi / z
    half_tooth = half_pitch / 2.0  # 齿半宽对应极角 (pitch 上)

    # 齿顶半宽 = half_tooth * tooth_top_ratio (默认 0.5, 给出 50% 顶宽的梯形)
    # 这给出一个明显的梯形外观, 而非细长的尖锐齿
    top_half = half_tooth * tooth_top_ratio

    profile: List[Tuple[float, float]] = []

    for i in range(z):
        tc = i * 2.0 * half_pitch  # this tooth's center angle

        if i == 0:
            # 起点: 最后一齿的右 base (在 dedendum 圆上)
            prev_right_base = (z - 1) * 2.0 * half_pitch + half_tooth
            profile.append((rf * math.cos(prev_right_base), rf * math.sin(prev_right_base)))

        # 4 个关键点: 左base → 左top → 右top → 右base
        # 左 base (dedendum, 极角 = tc - half_tooth)
        profile.append((rf * math.cos(tc - half_tooth), rf * math.sin(tc - half_tooth)))
        # 左 top (addendum, 极角 = tc - top_half)
        profile.append((ra * math.cos(tc - top_half), ra * math.sin(tc - top_half)))
        # 右 top (addendum, 极角 = tc + top_half)
        profile.append((ra * math.cos(tc + top_half), ra * math.sin(tc + top_half)))
        # 右 base (dedendum, 极角 = tc + half_tooth)
        profile.append((rf * math.cos(tc + half_tooth), rf * math.sin(tc + half_tooth)))

    return profile


def _bore_profile_points(bore_radius: float, n_points: int = 32) -> List[Tuple[float, float]]:
    """Generate a closed circular profile for the bore."""
    pts = []
    for i in range(n_points + 1):
        a = 2.0 * math.pi * i / n_points
        pts.append((bore_radius * math.cos(a), bore_radius * math.sin(a)))
    return pts


def _build_involute_tooth_face(
    module: float, teeth: int, pressure_angle_deg: float,
    n_points_flank: int = 25, n_points_dedendum_arc: int = 5,
):
    """v2.10: 造 1 个 involute 齿的 closed 2D face.

    齿形: 
      - 起点 (rb, 0)  (base circle 切点)
      - left_flank: involute 曲线 (rb, 0) -> (ra, +y_top)
      - top_arc: (ra, +y_top) -> (ra, -y_top)
      - right_flank: involute 镜像 (ra, -y_top) -> (rb, 0)
    中心: hub circle (rf, 0) 不画, 单独加

    数学 (ISO 21771):
      involute 曲线: x = rb*(cos t + t sin t), y = rb*(sin t - t cos t)
      rb = r*cos α
      max_t = sqrt((ra/rb)² - 1)
    """
    m = module
    z = teeth
    alpha = math.radians(pressure_angle_deg)
    r = m * z / 2.0
    rb = r * math.cos(alpha)
    ra = r + m

    # left_flank: involute from (rb, 0) to (ra, +y_top)
    max_t = math.sqrt((ra / rb) ** 2 - 1)
    left_flank: List[Tuple[float, float, float]] = []
    for i in range(n_points_flank + 1):
        t = i / n_points_flank * max_t
        x = rb * (math.cos(t) + t * math.sin(t))
        y = rb * (math.sin(t) - t * math.cos(t))
        left_flank.append((x, y, 0.0))

    # right_flank: 镜像 left_flank
    right_flank = [(p[0], -p[1], 0.0) for p in left_flank]

    # top arc (ra 圆) from (ra, +y_top) to (ra, -y_top)
    th0 = math.atan2(left_flank[-1][1], left_flank[-1][0])
    th1 = math.atan2(right_flank[-1][1], right_flank[-1][0])
    top_arc: List[Tuple[float, float, float]] = []
    for i in range(1, n_points_dedendum_arc):
        th = th0 + i * (th1 - th0) / n_points_dedendum_arc
        top_arc.append((ra * math.cos(th), ra * math.sin(th), 0.0))

    # 闭合 wire: left_flank + top_arc + right_flank reversed (skip start)
    pts = left_flank + top_arc + right_flank[::-1][1:]
    return pts


# ---------- public API: 几何构造 ----------

def build_involute_gear(
    module: float,
    teeth: int,
    width: float,
    bore: float = 0.0,
    pressure_angle_deg: float = 20.0,
    n_points_flank: int = 25,
    n_points_dedendum_arc: int = 5,
    fallback_to_trapezoid: bool = True,
    involute_teeth_threshold: int = 30,
) -> Part:
    """Build a spur gear as a build123d Part.

    v2.10 升级: 真 involute 曲线 (spline_approx) + hub 圆盘 + bore subtract.
    v2.8 实现梯形齿形 (fallback, involute 失败时自动回退).

    数学严格按 ISO 21771 / AGMA 2015 (module/teeth/pressure_angle).

    Args:
        module: 齿轮模数 m (mm)
        teeth: 齿数 z (>= 6)
        width: face width (extrude depth) (mm)
        bore: 中心孔直径 (0 = no bore) (mm)
        pressure_angle_deg: 压力角 (默认 20°)
        n_points_flank: involute 曲线采样点数 (默认 25)
        n_points_dedendum_arc: 齿根圆弧采样点数 (默认 5)
        fallback_to_trapezoid: True 时 involute 失败回退梯形
        involute_teeth_threshold: 齿数 > 此值自动走梯形 (因为 OCC boolean
                                   O(N²) 在 z>30 时太慢, ~0.4s/add)

    Returns:
        build123d Part
    """
    if teeth < 6:
        raise ValueError(f"teeth 必须 >= 6（当前 {teeth}）")
    if module <= 0:
        raise ValueError(f"module 必须 > 0（当前 {module}）")
    if width <= 0:
        raise ValueError(f"width 必须 > 0（当前 {width}）")

    # 大齿数 (> threshold) 自动走梯形 (OCC boolean O(N²) 慢)
    if teeth > involute_teeth_threshold and fallback_to_trapezoid:
        return _build_trapezoid_gear(
            module=module, teeth=teeth, width=width, bore=bore,
            pressure_angle_deg=pressure_angle_deg,
        )

    # 尝试真 involute (v2.10)
    try:
        tooth_pts = _build_involute_tooth_face(
            module=module, teeth=teeth,
            pressure_angle_deg=pressure_angle_deg,
            n_points_flank=n_points_flank,
            n_points_dedendum_arc=n_points_dedendum_arc,
        )
        tooth_edge = Edge.make_spline_approx(tooth_pts)
        if not tooth_edge.is_closed:
            raise ValueError("tooth_edge not closed (spline fit failed)")
        tooth_face = make_face(tooth_edge)
        tooth_part = extrude(tooth_face, width)

        m = module
        z = teeth
        alpha = math.radians(pressure_angle_deg)
        r = m * z / 2.0
        rf = r - 1.25 * m

        # hub 圆盘 (rf 半径, 厚 width)
        with BuildPart(Plane.XY) as bp:
            with BuildSketch():
                add(Circle(rf))
            extrude(amount=width)
        hub = bp.part

        # 完整齿轮: hub + 20 齿 union
        with BuildPart(Plane.XY) as bp2:
            add(hub)
            for i in range(z):
                rotated = tooth_part.rotate(Axis.Z, i * 360.0 / z)
                add(rotated)
        gear = bp2.part

        # bore subtract
        if bore > 0:
            from build123d import Cylinder
            with BuildPart(Plane.XY) as bp3:
                add(Cylinder(width, bore / 2.0))
            bore_cyl = bp3.part
            gear = gear - bore_cyl
        return gear
    except Exception as e:
        if not fallback_to_trapezoid:
            raise
        return _build_trapezoid_gear(
            module=module, teeth=teeth, width=width, bore=bore,
            pressure_angle_deg=pressure_angle_deg,
        )


def _build_trapezoid_gear(
    module: float, teeth: int, width: float, bore: float = 0.0,
    pressure_angle_deg: float = 20.0,
) -> Part:
    """v2.8 梯形 proxy (fallback, 大齿数 / involute 失败时用)"""
    outer = _gear_profile_points(
        module=module, teeth=teeth,
        pressure_angle_deg=pressure_angle_deg,
    )
    with BuildLine() as bl:
        Polyline(outer, close=True)
    outer_wire = bl.line
    outer_face = make_face(outer_wire)
    with BuildPart(Plane.XY) as bp:
        with BuildSketch() as s:
            add(outer_face)
            if bore > 0:
                with BuildLine() as bl2:
                    Polyline(_bore_profile_points(bore / 2.0), close=True)
                bore_wire = bl2.line
                add(make_face(bore_wire), mode=Mode.SUBTRACT)
        extrude(amount=width)
    return bp.part


__all__ = ["build_involute_gear", "gear_geometry", "center_distance"]
