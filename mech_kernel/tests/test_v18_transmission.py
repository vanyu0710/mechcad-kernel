"""v2.18 传动件原语测试：真渐开线齿轮（直/斜）+ 键槽 + 花键。

背景：v2.10 的 involute 齿廓未闭合（z=17 实测 is_closed=False），所有齿轮一直
静默回退梯形近似——"真渐开线"从未生效。v2.18 重写为整轮单条闭合齿廓，并新增
斜齿（helix_angle_deg）、create_keyway（GB/T 1096 键槽）、create_spline（花键）。
"""
import math
import time

from build123d import GeomType

from mech_kernel import MechKernel
from mech_kernel.gear import build_involute_gear, gear_geometry
from mech_kernel.kernel import PUBLIC_OPS


# ---------------- 真渐开线几何 ----------------

def test_involute_profile_is_closed_and_single_solid():
    """回归：齿廓必须闭合，且整轮为单实体 + 真曲面齿面（旧实现静默回退梯形）。"""
    g = build_involute_gear(module=2.0, teeth=17, width=20.0, bore=0.0)
    assert len(g.solids()) == 1
    curved = sum(1 for f in g.faces()
                 if f.geom_type in (GeomType.BSPLINE, GeomType.CYLINDER,
                                    GeomType.EXTRUSION, GeomType.CONE))
    assert curved >= 17, f"每个齿应有曲面齿面（实测 {curved} 个曲面）"


def test_involute_volume_tracks_analytic_range():
    """体积应落在齿根圆柱与齿顶圆柱之间（且接近解析估计）。"""
    m, z, b = 2.0, 17, 20.0
    g = build_involute_gear(module=m, teeth=z, width=b)
    r = m * z / 2.0
    rf, ra = r - 1.25 * m, r + m
    v_root = math.pi * rf ** 2 * b
    v_tip = math.pi * ra ** 2 * b
    assert v_root < g.volume < v_tip
    # 真实渐开线齿轮体积约在 (rf..ra) 中间偏下
    assert 0.45 * (v_tip - v_root) + v_root < g.volume < 0.85 * (v_tip - v_root) + v_root


def test_gear_geometry_reports_helix():
    geo = gear_geometry(2.0, 17, helix_angle_deg=15.0)
    assert geo["helix_angle_deg"] == 15.0
    assert geo["helix_hand"] == "right"
    assert gear_geometry(2.0, 17)["helix_hand"] == "none"


def test_spur_gear_scales_across_tooth_counts():
    """z=8..100 都应产出单实体真渐开线（体积单调增长）。"""
    vols = []
    for z in (8, 17, 40, 85, 100):
        g = build_involute_gear(module=2.0, teeth=z, width=20.0)
        assert len(g.solids()) == 1, f"z={z} 非单实体"
        vols.append(g.volume)
    assert vols == sorted(vols)


# ---------------- 斜齿 ----------------

def test_helical_gear_single_solid_and_twisted():
    g = build_involute_gear(module=2.0, teeth=17, width=20.0, helix_angle_deg=15.0)
    assert len(g.solids()) == 1
    # 斜齿端面轮廓旋转 → bbox 略大于同参数直齿（扭转使齿顶扫过更大圆）
    spur = build_involute_gear(module=2.0, teeth=17, width=20.0)
    hb, sb = g.bounding_box(), spur.bounding_box()
    assert hb.max.Z - hb.min.Z > sb.max.Z - sb.min.Z - 1e-6
    assert (hb.max.X - hb.min.X) >= (sb.max.X - sb.min.X) - 1e-6


def test_helical_via_op_and_replay():
    k = MechKernel()
    r = k.make_gear(module=2.0, teeth=17, width=20.0, bore=15.0, helix_angle_deg=15.0)
    assert r.success
    assert len(k._current_geometry.solids()) == 1
    # 参数重放：改螺旋角仍可重建
    r2 = k.update_feature(r.feature_id, {"helix_angle_deg": 20.0})
    assert r2.success
    assert len(k._current_geometry.solids()) == 1


def test_helix_angle_bounds():
    k = MechKernel()
    r = k.execute("make_gear", module=2.0, teeth=17, width=10.0, helix_angle_deg=60.0)
    assert not r.success


# ---------------- 键槽 ----------------

def test_keyway_depth_is_exact():
    """切深语义：depth 从外圆面起算，不得因 margin 超切。"""
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "s")
    k.add_circle("s", (0, 0), 20)
    k.close_sketch("s")
    k.extrude("s", 60)
    v0 = k.query("_current_geometry", "volume").value
    k.execute("create_keyway", width=12, depth=5, length=40, direction="x+")
    v1 = k.query("_current_geometry", "volume").value
    # 单侧圆外角 = ∫0..6 (20 - sqrt(400-y²)) dy
    integral = 0.5 * 6 * math.sqrt(400 - 36) + 200 * math.asin(6 / 20)
    corner = 6 * 20 - integral
    expected = 40 * (12 * 5 - 2 * corner)
    assert abs((v0 - v1) - expected) < 1.0, f"removed {v0-v1} vs analytic {expected}"


def test_keyway_requires_perpendicular_direction():
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "s")
    k.add_circle("s", (0, 0), 15)
    k.close_sketch("s")
    k.extrude("s", 50)
    # 轴沿 Z，top 与之同向 → 拒绝
    r = k.execute("create_keyway", width=8, depth=3, length=10, direction="top")
    assert not r.success


def test_keyway_stays_single_solid():
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "s")
    k.add_circle("s", (0, 0), 20)
    k.close_sketch("s")
    k.extrude("s", 60)
    k.execute("create_keyway", width=12, depth=5, length=40, direction="x+")
    assert k.query("_current_geometry", "solid_count").value == 1


# ---------------- 花键 ----------------

def test_spline_adds_teeth_and_stays_single_solid():
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "s")
    k.add_circle("s", (0, 0), 15)
    k.close_sketch("s")
    k.extrude("s", 50)
    v0 = k.query("_current_geometry", "volume").value
    r = k.execute("create_spline", teeth=20, module=1.5, length=15)
    assert r.success, r.error
    v1 = k.query("_current_geometry", "volume").value
    assert v1 > v0, "外花键应增加体积"
    assert k.query("_current_geometry", "solid_count").value == 1


def test_internal_spline_cut_reduces_volume():
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "s")
    k.add_circle("s", (0, 0), 30)
    k.close_sketch("s")
    k.extrude("s", 40)
    v0 = k.query("_current_geometry", "volume").value
    r = k.execute("create_spline", teeth=24, module=1.5, length=20, cut=True)
    assert r.success, r.error
    v1 = k.query("_current_geometry", "volume").value
    assert v1 <= v0 + 1e-6


# ---------------- op 注册 ----------------

def test_new_ops_are_public_and_registered():
    assert "create_keyway" in PUBLIC_OPS
    assert "create_spline" in PUBLIC_OPS
    k = MechKernel()
    names = {c["name"] for c in k.cap.list_public()}
    assert {"create_keyway", "create_spline"} <= names
    mf = k.cap.get("make_gear").input_schema
    assert "helix_angle_deg" in mf


def test_helical_gear_build_time_reasonable():
    t0 = time.time()
    build_involute_gear(module=2.0, teeth=17, width=20.0, helix_angle_deg=15.0)
    assert time.time() - t0 < 30.0
