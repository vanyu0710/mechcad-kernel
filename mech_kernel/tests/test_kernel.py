"""
测试 MechKernel 主类（M0 阶段：基础数据流）
"""
import pytest
from mech_kernel import (
    MechKernel, StepResult, FeatureType, Reference, InvalidRequestError
)


def test_kernel_creation():
    k = MechKernel()
    assert len(k.feature_graph) == 0
    assert len(k.workplanes.all()) == 0
    assert k.narrative == []


def test_create_workplane_xy():
    k = MechKernel()
    r = k.create_workplane("base", "XY")
    assert r.success
    assert r.feature_id is not None
    assert r.render_level == "none"  # 草图类不渲染
    assert k.workplanes.has_name("base")


def test_create_workplane_with_invalid_type():
    k = MechKernel()
    with pytest.raises(InvalidRequestError):
        k.create_workplane("base", "INVALID")


def test_create_workplane_with_empty_name():
    k = MechKernel()
    with pytest.raises(InvalidRequestError):
        k.create_workplane("", "XY")


def test_create_duplicate_workplane_name():
    k = MechKernel()
    k.create_workplane("base", "XY")
    with pytest.raises(InvalidRequestError):
        k.create_workplane("base", "YZ")


def test_new_sketch_basic_flow():
    k = MechKernel()
    k.create_workplane("base", "XY")
    r = k.new_sketch("base", "sk_1")
    assert r.success
    assert "sk_1" in k.sketches
    assert r.render_level == "none"


def test_new_sketch_with_missing_workplane():
    k = MechKernel()
    with pytest.raises(InvalidRequestError):
        k.new_sketch("nonexistent", "sk_1")


def test_add_circle_basic():
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "sk_1")
    r = k.add_circle("sk_1", (0, 0), 50, name="outer")
    assert r.success
    assert r.render_level == "none"
    assert "outer" in [e.name for e in k.sketches["sk_1"].entities]


def test_add_circle_invalid_radius():
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "sk_1")
    with pytest.raises(InvalidRequestError):
        k.add_circle("sk_1", (0, 0), -1)


def test_close_sketch():
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "sk_1")
    k.add_circle("sk_1", (0, 0), 50)
    r = k.close_sketch("sk_1")
    assert r.success
    assert k.sketches["sk_1"].closed is True


def test_close_empty_sketch_fails():
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "sk_1")
    with pytest.raises(InvalidRequestError):
        k.close_sketch("sk_1")


def test_extrude_requires_closed_sketch():
    """M0 阶段：extrude 校验依赖 closed"""
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "sk_1")
    k.add_circle("sk_1", (0, 0), 50)
    # 没 close，extrude 应该抛错
    with pytest.raises(InvalidRequestError):
        k.extrude("sk_1", depth=20)


def test_extrude_topology_change_triggers_render():
    """拓扑变化必渲染（专家 C 方案）"""
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "sk_1")
    k.add_circle("sk_1", (0, 0), 50)
    k.close_sketch("sk_1")
    r = k.extrude("sk_1", depth=20)
    assert r.success
    assert r.render_level == "iso_only"  # 拓扑变化


def test_narrative_accumulates():
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "sk_1")
    k.add_circle("sk_1", (0, 0), 50)
    k.close_sketch("sk_1")
    k.extrude("sk_1", depth=20)
    assert len(k.narrative) >= 5


def test_undo_basic():
    k = MechKernel()
    k.create_workplane("base", "XY")
    initial_narrative_len = len(k.narrative)
    k.create_workplane("top", "XY")
    assert len(k.narrative) == initial_narrative_len + 1
    r = k.undo()
    assert r.success
    assert len(k.narrative) == initial_narrative_len
    assert not k.workplanes.has_name("top")


def test_undo_empty_stack():
    k = MechKernel()
    r = k.undo()
    assert not r.success
    assert r.error_kind == "GEOMETRY_FAILURE"


def test_redo_after_undo():
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.create_workplane("top", "XY")
    k.undo()
    assert not k.workplanes.has_name("top")
    r = k.redo()
    assert r.success
    assert k.workplanes.has_name("top")


def test_get_state():
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "sk_1")
    state = k.get_state()
    assert state["workplane_count"] == 1
    assert state["sketch_count"] == 1
    assert len(state["narrative"]) >= 2


def test_hints_generated():
    """空 kernel 应该给提示"""
    k = MechKernel()
    r = k.create_workplane("base", "XY")
    assert any("草图" in h for h in r.next_hints) or any("create_workplane" not in h for h in r.next_hints)


def test_step_result_geometry_summary_present():
    """所有成功 StepResult 都应该含 geometry_summary"""
    k = MechKernel()
    r = k.create_workplane("base", "XY")
    assert r.geometry_summary is not None
    assert r.geometry_summary.feature_count == 0  # 还没 feature
