"""v2.20 壳体设计分析 + 审计测试。

规范要点：壳体尺寸由内部件包络反推（禁止大盒子）；逐项审计齿顶/轴向间隙、
轴承座配对、螺栓边距、可拆性、单实体、布局一致性。
"""
import math

import pytest

from mech_kernel import MechKernel
from mech_kernel.housing_design import audit_housing, internals_design_brief


def _shaft(k, radius=15.0, length=200.0, z0=0.0, center=(0.0, 0.0)):
    k.create_workplane("wp", "XY", origin=(center[0], center[1], z0))
    k.new_sketch("wp", "s")
    k.add_circle("s", (0, 0), radius)
    k.close_sketch("s")
    k.extrude("s", length)
    return k


def _gears_and_shafts(tmp_path):
    """造一个最小内部件集合：两根轴 + 两个齿轮，导出 STEP。"""
    from build123d import export_step
    parts = []

    k1 = MechKernel()
    k1.make_gear(module=2.0, teeth=30, width=20.0, bore=20.0)
    p1 = tmp_path / "gear_a.step"
    export_step(k1._current_geometry, str(p1))
    parts.append({"path": str(p1), "name": "齿轮_A", "pose": {"position": [0.0, 0.0, 0.0]}})

    k2 = MechKernel()
    k2.make_gear(module=2.0, teeth=20, width=20.0, bore=20.0)
    p2 = tmp_path / "gear_b.step"
    export_step(k2._current_geometry, str(p2))
    parts.append({"path": str(p2), "name": "齿轮_B", "pose": {"position": [80.0, 0.0, 0.0]}})

    k3 = MechKernel()
    _shaft(k3, 12.0, 240.0, z0=-20.0, center=(0.0, 0.0))
    p3 = tmp_path / "shaft_a.step"
    export_step(k3._current_geometry, str(p3))
    parts.append({"path": str(p3), "name": "轴_A", "pose": {"position": [0.0, 0.0, 0.0]}})

    k4 = MechKernel()
    _shaft(k4, 12.0, 240.0, z0=-20.0, center=(80.0, 0.0))
    p4 = tmp_path / "shaft_b.step"
    export_step(k4._current_geometry, str(p4))
    parts.append({"path": str(p4), "name": "轴_B", "pose": {"position": [0.0, 0.0, 0.0]}})
    return parts


def test_brief_derives_cavity_from_envelope(tmp_path):
    parts = _gears_and_shafts(tmp_path)
    brief = internals_design_brief(parts, min_clearance=5.0)
    assert brief["ok"]
    env = brief["envelope"]
    cav = brief["required_cavity"]
    # 内腔 = 包络 + 最小间隙（各轴向外扩 5mm）
    for i in range(3):
        assert abs(cav["min"][i] - (env["min"][i] - 5.0)) < 1e-6
        assert abs(cav["max"][i] - (env["max"][i] + 5.0)) < 1e-6
    assert brief["count"]["gears"] == 2
    assert brief["count"]["shafts"] == 2
    # 轴心与轴颈
    ys = {s["name"]: s for s in brief["shafts"]}
    assert "轴_A" in ys and ys["轴_A"]["cyl_radii"]


def test_audit_flags_tiny_box_that_clips_rotating_parts(tmp_path):
    """内腔小于包络 → 旋转件与壳体接触 → FAIL。"""
    from build123d import Box, export_step
    parts = _gears_and_shafts(tmp_path)
    k = MechKernel()
    k.create_workplane("b", "XY")
    k.new_sketch("b", "s")
    # 一个明显小于内部件的"盒子"
    k.add_rectangle("s", 40, 40)
    k.close_sketch("s")
    k.extrude("s", 40)
    shell = tmp_path / "下壳体.step"
    export_step(k._current_geometry, str(shell))
    parts.append({"path": str(shell), "name": "下壳体", "pose": {"position": [0.0, 0.0, 0.0]}})
    rep = audit_housing(parts)
    ids = {c["id"]: c for c in rep["checks"]}
    assert ids["rotating_clearance"]["status"] == "FAIL"


def test_audit_flags_oversized_box_as_loose(tmp_path):
    """箱子远大于包络 → cavity_looseness WARN（抓"任意大盒子"）。"""
    from build123d import Box, export_step
    parts = _gears_and_shafts(tmp_path)
    k = MechKernel()
    k.create_workplane("b", "XY")
    k.new_sketch("b", "s")
    k.add_rectangle("s", 900, 900)
    k.close_sketch("s")
    k.extrude("s", 900)
    shell = tmp_path / "下壳体.step"
    export_step(k._current_geometry, str(shell))
    parts.append({"path": str(shell), "name": "下壳体", "pose": {"position": [0.0, 0.0, 0.0]}})
    # 需要简报做"必需内腔"基准，才能判定箱体过大
    brief = internals_design_brief(parts)
    rep = audit_housing(parts, brief=brief)
    ids = {c["id"]: c for c in rep["checks"]}
    assert ids["cavity_looseness"]["status"] == "WARN"


def test_audit_reports_layout_and_solids(tmp_path):
    parts = _gears_and_shafts(tmp_path)
    from build123d import export_step
    # 包围盒略大于包络的壳体
    k = MechKernel()
    k.create_workplane("b", "XY")
    k.new_sketch("b", "s")
    k.add_rectangle("s", 140, 80)
    k.close_sketch("s")
    k.extrude("s", 300)
    shell = tmp_path / "箱体.step"
    export_step(k._current_geometry, str(shell))
    parts.append({"path": str(shell), "name": "箱体", "pose": {"position": [0.0, 0.0, -30.0]}})
    rep = audit_housing(parts)
    ids = {c["id"]: c for c in rep["checks"]}
    assert ids["single_solid::箱体"]["status"] == "PASS"
    assert ids["layout_consistent"]["status"] == "PASS"   # 两轴都沿 Z 且共面


def test_audit_requires_housing_part(tmp_path):
    parts = _gears_and_shafts(tmp_path)
    rep = audit_housing(parts)
    assert not rep["ok"]
    assert "没有壳体零件" in rep["error"]
