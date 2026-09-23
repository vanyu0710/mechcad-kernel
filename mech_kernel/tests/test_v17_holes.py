"""v2.17 P1-5: 孔语义分析测试 + 盲孔深度回归。

审查场景：外部凸台圆柱面冒充通孔过契约——分类器必须把 boss 排除；
盲孔 depth 参数必须真实生效（v2.17 修复前 margin=5 使盲孔系统性超深）。
"""
import math

import pytest

from mech_kernel import MechKernel
from mech_kernel.errors import InvalidRequestError


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


def test_coaxial_convex_boss_is_not_a_counterbore():
    """v2.21.1 回归：同轴"凸"外圆（凸台外圆）不得把通孔误判成沉孔。

    旧逻辑只要存在同轴异径圆柱就判 counterbore_hole——凸台外圆是凸面，
    法向背离轴线，不构成沉孔台阶。凸台必须建在板顶面之上（offset workplane），
    否则拉伸落在板内部、并集体积为 0，用例会空转。"""
    k = MechKernel()
    _plate(k)
    k.create_workplane("top", "XY", offset=10)
    k.new_sketch("top", "boss")
    k.add_circle("boss", (0, 0), 7)  # Ø14 凸台，与孔同轴且不同径
    k.close_sketch("boss")
    k.extrude("boss", 6, mode="add")
    k.hole(position=(0, 0), diameter=6)
    holes = _holes(k)
    assert [h["kind"] for h in holes] == ["through_hole"], holes
    assert abs(holes[0]["diameter_mm"] - 6.0) < 0.01


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


def test_counterbore_reports_measurable_two_stage_semantics():
    """counterbore 的两个圆柱面都必须携带真实几何证据。

    小径 depth 是进入面到孔底的总孔深；segment_depth 单独暴露小圆柱段长度，
    避免把肩台下 2mm 误当成整个 5mm 盲孔。
    """
    k = MechKernel()
    _plate(k)
    k.hole(position=(0, 0), diameter=6, depth=5, hole_type="counterbore",
           counterbore_diameter=10, counterbore_depth=3)
    holes = _holes(k)
    assert len(holes) == 2
    small = next(h for h in holes if abs(h["diameter_mm"] - 6.0) < 0.01)
    large = next(h for h in holes if abs(h["diameter_mm"] - 10.0) < 0.01)

    assert small["kind"] == "counterbore_hole"
    assert small["through"] is False
    assert abs(small["depth_mm"] - 5.0) < 0.01
    assert abs(small["segment_depth_mm"] - 2.0) < 0.01
    assert abs(small["counterbore_diameter_mm"] - 10.0) < 0.01
    assert abs(small["counterbore_depth_mm"] - 3.0) < 0.01
    assert abs(small["bore_diameter_mm"] - 6.0) < 0.01

    assert large["kind"] == "counterbore_hole"
    assert large["through"] is True
    assert abs(large["depth_mm"] - 3.0) < 0.01
    assert abs(large["segment_depth_mm"] - 3.0) < 0.01
    assert abs(large["counterbore_diameter_mm"] - 10.0) < 0.01
    assert abs(large["counterbore_depth_mm"] - 3.0) < 0.01
    assert abs(large["bore_diameter_mm"] - 6.0) < 0.01


def test_counterbore_rejects_degenerate_or_nonfinite_parameters():
    """不能让单段圆柱/切穿肩台/NaN 参数伪装成 counterbore 成功。"""
    k = MechKernel()
    _plate(k)
    with pytest.raises(InvalidRequestError, match="大径"):
        k.hole(position=(0, 0), diameter=6, depth=5, hole_type="counterbore",
               counterbore_diameter=6, counterbore_depth=3)
    with pytest.raises(InvalidRequestError, match="深度"):
        k.hole(position=(0, 0), diameter=6, depth=5, hole_type="counterbore",
               counterbore_diameter=10, counterbore_depth=5)
    with pytest.raises(InvalidRequestError, match="材料厚度"):
        k.hole(position=(0, 0), diameter=6, depth=12, hole_type="counterbore",
               counterbore_diameter=10, counterbore_depth=3)
    with pytest.raises(InvalidRequestError, match="有限"):
        k.hole(position=(0, 0), diameter=float("nan"), hole_type="counterbore")
    with pytest.raises(InvalidRequestError, match="有限"):
        k.hole(position=(0, 0), diameter=6, hole_type="counterbore",
               counterbore_diameter=float("inf"))


def test_blind_depth_equal_to_material_is_flush_boundary():
    """深度恰好等于材料厚度是合法齐平边界：孔底与远端面重合，几何上可测为通孔。"""
    k = MechKernel()
    _plate(k)  # 10mm 板
    r = k.hole(position=(0, 0), diameter=6, depth=10)
    assert r.success, r.error
    holes = _holes(k)
    assert holes, "holes 查询不应为空"
    h = holes[0]
    assert h["diameter_mm"] == 6
    # 没有留下盲孔底面 → 几何证据表明它是通孔，而不是参数伪装的盲孔
    assert h["through"] is True


def test_stepped_hole_cuts_measurable_shoulders():
    """三段阶梯孔必须留下两级真实肩台，缺一段体积就不成立。"""
    k = MechKernel()
    _plate(k, size=(80, 50, 12))
    v0 = k.query(target="_current_geometry", what="volume").value
    result = k.hole(
        position=(0, 0), hole_type="stepped",
        stages=[
            {"diameter": 14, "depth": 3},
            {"diameter": 10, "depth": 4},
            {"diameter": 6, "depth": 5},
        ],
    )
    assert result.success, result.error
    v1 = k.query(target="_current_geometry", what="volume").value
    expected = math.pi * (7 * 7 * 3 + 5 * 5 * 4 + 3 * 3 * 5)
    assert abs((v0 - v1) - expected) < 0.8

    holes = _holes(k)
    assert len(holes) == 3
    assert {round(h["diameter_mm"], 1) for h in holes} == {14.0, 10.0, 6.0}
    assert all(h["kind"] == "stepped_hole" for h in holes)
    small = next(h for h in holes if abs(h["diameter_mm"] - 6.0) < 0.01)
    assert abs(small["depth_mm"] - 12.0) < 0.05
    assert [stage["diameter_mm"] for stage in small["stages"]] == [14.0, 10.0, 6.0]
    assert [stage["depth_mm"] for stage in small["stages"]] == [3.0, 4.0, 5.0]


def test_stepped_hole_rejects_illegal_stages():
    k = MechKernel()
    _plate(k)
    with pytest.raises(InvalidRequestError, match="逐段变小"):
        k.hole(position=(0, 0), hole_type="stepped", stages=[
            {"diameter": 6, "depth": 3}, {"diameter": 10, "depth": 4},
        ])
    with pytest.raises(InvalidRequestError, match="有限"):
        k.hole(position=(0, 0), hole_type="stepped", stages=[
            {"diameter": 10, "depth": float("nan")}, {"diameter": 6, "depth": 3},
        ])
    with pytest.raises(InvalidRequestError, match="材料厚度"):
        k.hole(position=(0, 0), hole_type="stepped", stages=[
            {"diameter": 10, "depth": 10}, {"diameter": 6},
        ])


def test_query_holes_schema_registered():
    k = MechKernel()
    assert "holes" in k.cap.get("query").input_schema["what"].enum
