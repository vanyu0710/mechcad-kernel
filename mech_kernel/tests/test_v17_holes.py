"""v2.17 P1-5: 孔语义分析测试 + 盲孔深度回归。

审查场景：外部凸台圆柱面冒充通孔过契约——分类器必须把 boss 排除；
盲孔 depth 参数必须真实生效（v2.17 修复前 margin=5 使盲孔系统性超深）。
"""
import math

from mech_kernel import MechKernel


def _plate(k, size=(60, 40, 10)):
    k.create_workplane("base", "XY")
    k.new_sketch("base", "p")
    k.add_rectangle("p", size[0], size[1])
    k.close_sketch("p")
    k.extrude("p", size[2])


def _holes(k):
    r = k.query(target="_current_geometry", what="holes")
    assert r.success, r.error
    return r.value["holes"]


def test_blind_hole_depth_is_real():
    """回归：depth=6 在 10mm 板上不得打穿（旧 bug：margin 加进钻深 → 意外贯通）。"""
    k = MechKernel()
    _plate(k)
    v0 = k.query(target="_current_geometry", what="volume").value
    k.hole(position=(15, -10), diameter=8, depth=6)
    v1 = k.query(target="_current_geometry", what="volume").value
    assert abs((v0 - v1) - math.pi * 16 * 6) < 0.5  # 探针二分 ±0.005mm 面积化容差
    holes = _holes(k)
    assert len(holes) == 1
    assert holes[0]["kind"] == "blind_hole"
    assert holes[0]["through"] is False
    assert abs(holes[0]["depth_mm"] - 6.0) < 0.05


def test_through_hole_detected():
    k = MechKernel()
    _plate(k)
    k.hole(position=(15, 10), diameter=9)
    holes = _holes(k)
    assert len(holes) == 1
    assert holes[0]["kind"] == "through_hole"
    assert holes[0]["through"] is True
    assert abs(holes[0]["diameter_mm"] - 9.0) < 0.01
    assert holes[0]["center"][:2] == [15.0, 10.0]


def test_external_boss_is_not_a_hole():
    """审查测试 6 的内核半：外凸台圆柱（半径与孔相同）不得进孔清单。"""
    k = MechKernel()
    _plate(k)
    k.hole(position=(15, 10), diameter=9)
    k.new_sketch("base", "b")
    k.add_circle("b", (-15, -10), 4.5)  # 与孔同半径的 boss
    k.close_sketch("b")
    k.extrude("b", 12, mode="add")
    holes = _holes(k)
    assert len(holes) == 1
    assert holes[0]["center"][:2] == [15.0, 10.0]


def test_counterbore_detected():
    k = MechKernel()
    _plate(k)
    k.hole(position=(0, 0), diameter=6, depth=5, hole_type="counterbore",
           counterbore_diameter=10, counterbore_depth=3)
    holes = _holes(k)
    assert holes and all(h["kind"] == "counterbore_hole" for h in holes)
    diameters = sorted(h["diameter_mm"] for h in holes)
    assert diameters == [6.0, 10.0]


def test_counterbore_depths_correct():
    k = MechKernel()
    _plate(k)
    v0 = k.query(target="_current_geometry", what="volume").value
    k.hole(position=(0, 0), diameter=6, depth=5, hole_type="counterbore",
           counterbore_diameter=10, counterbore_depth=3)
    v1 = k.query(target="_current_geometry", what="volume").value
    # 沉孔环 π(25-9)*3 + 主孔 π*9*5（重叠只算一次）
    expected = math.pi * 16 * 3 + math.pi * 9 * 5
    assert abs((v0 - v1) - expected) < 0.5


def test_query_holes_schema_registered():
    k = MechKernel()
    assert "holes" in k.cap.get("query").input_schema["what"].enum
