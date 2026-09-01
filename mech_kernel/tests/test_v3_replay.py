"""
v2.0 参数化重放引擎测试
1. op 历史记录（顺序 + 参数 + feature_id 回填）
2. rebuild 公共 op（确定性、可执行、含导入会话 RECOVERABLE）
3. delete_feature 真实重算（删几何特征/草图实体/基座特征）
4. update_feature 真实重算（几何参数/草图实体参数/非法参数拒绝）
5. undo 回退 delete/update；重放失败回滚
"""
import math

import pytest
from mech_kernel import MechKernel, InvalidRequestError
from mech_kernel.kernel import PUBLIC_OPS

try:
    import build123d  # noqa: F401
    import OCP  # noqa: F401
    HAS_OCC = True
except Exception:
    HAS_OCC = False


def _make_cylinder(k, sketch_name="sk", radius=10, depth=10, center=(0, 0)):
    k.create_workplane("base", "XY")
    k.new_sketch("base", sketch_name)
    k.add_circle(sketch_name, center=center, radius=radius)
    k.close_sketch(sketch_name)
    return k.extrude(sketch_name, depth=depth, mode="new_body")


# === 1. op 历史记录 ===

def test_history_records_ops_order_and_args():
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "sk")
    k.add_circle("sk", center=(0, 0), radius=5)
    k.close_sketch("sk")
    ops = [e["op"] for e in k._op_history]
    assert ops == ["create_workplane", "new_sketch", "add_circle", "close_sketch"]
    wp_entry = k._op_history[0]
    assert wp_entry["args"]["name"] == "base" and wp_entry["args"]["type"] == "XY"
    circle_entry = k._op_history[2]
    assert circle_entry["args"]["radius"] == 5
    assert circle_entry["feature_id"].startswith("E_")


def test_history_records_geometry_feature_ids():
    if not HAS_OCC:
        return
    k = MechKernel()
    r = _make_cylinder(k)
    fid = r.feature_id
    assert fid.startswith("F_")
    assert k._op_history[-1]["op"] == "extrude"
    assert k._op_history[-1]["feature_id"] == fid


def test_rebuild_is_public_op():
    assert "rebuild" in PUBLIC_OPS


# === 2. rebuild ===

def test_rebuild_empty_kernel_success():
    k = MechKernel()
    r = k.rebuild()
    assert r.success
    assert r.value["replayed_ops"] == 0
    assert r.value["volume"] == 0.0


def test_rebuild_recoverable_after_import():
    """含导入/加载的会话：rebuild/delete/update 返回 RECOVERABLE"""
    k = MechKernel()
    k._has_non_replayable_op = True
    r = k.rebuild()
    assert not r.success
    assert r.error_kind == "RECOVERABLE"

    k.create_workplane("base", "XY")
    k.new_sketch("base", "sk")
    rc = k.add_circle("sk", center=(0, 0), radius=5)
    rd = k.delete_feature(rc.feature_id)
    assert not rd.success and rd.error_kind == "RECOVERABLE"
    ru = k.update_feature(rc.feature_id, {"radius": 6})
    assert not ru.success and ru.error_kind == "RECOVERABLE"


# === 3/4. delete / update 无 OCP 边界 ===

def test_delete_unknown_id_raises():
    k = MechKernel()
    try:
        k.delete_feature("F_NONEXIST")
        assert False, "应抛 InvalidRequestError"
    except InvalidRequestError:
        pass


def test_update_rejects_unknown_param():
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "sk")
    r = k.add_circle("sk", center=(0, 0), radius=5)
    try:
        k.update_feature(r.feature_id, {"bogus": 1})
        assert False, "应抛 InvalidRequestError"
    except InvalidRequestError:
        pass


def test_update_rejects_invalid_type():
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "sk")
    r = k.add_circle("sk", center=(0, 0), radius=5)
    try:
        k.update_feature(r.feature_id, {"radius": "huge"})
        assert False, "应抛 InvalidRequestError"
    except InvalidRequestError:
        pass


def test_delete_sketch_entity_reduces_entities():
    """删除草图实体 E_xxxx → 重放后实体数减少"""
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "sk")
    r1 = k.add_circle("sk", center=(0, 0), radius=5)
    r2 = k.add_circle("sk", center=(20, 0), radius=3)
    assert len(k.sketches["sk"].entities) == 2
    r = k.delete_feature(r1.feature_id)
    assert r.success and r.value["geometry_recomputed"] is True
    assert len(k.sketches["sk"].entities) == 1
    assert k.sketches["sk"].entities[0].params["radius"] == 3

# === 5. OCC：真实重算 ===

def test_rebuild_reproduces_geometry():
    if not HAS_OCC:
        return
    k = MechKernel()
    r = _make_cylinder(k, radius=10, depth=10)
    fid = r.feature_id
    vol_before = k.query("_current_geometry", "volume").value

    rb = k.rebuild()
    assert rb.success
    assert rb.value["volume"] > 0
    assert abs(rb.value["volume"] - vol_before) < 1e-3
    # 确定性：feature id 与几何不变
    assert k.query(fid, "volume").success
    assert abs(k.query("_current_geometry", "volume").value - vol_before) < 1e-3


def test_execute_rebuild_via_execute():
    if not HAS_OCC:
        return
    k = MechKernel()
    _make_cylinder(k, radius=10, depth=10)
    r = k.execute("rebuild", name="r1")
    assert r.success, r.error
    assert r.value["replayed_ops"] == 5


def test_delete_hole_recomputes_volume():
    """删孔后体积回到无孔状态（真实重算）"""
    if not HAS_OCC:
        return
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "plate")
    k.add_rectangle("plate", 40, 30)
    k.close_sketch("plate")
    k.extrude("plate", depth=5, mode="new_body")
    vol_plate = k.query("_current_geometry", "volume").value
    rh = k.hole(position=(0, 0), diameter=10)
    hole_fid = rh.feature_id
    vol_with_hole = k.query("_current_geometry", "volume").value
    assert vol_with_hole < vol_plate

    rd = k.delete_feature(hole_fid)
    assert rd.success and rd.value["geometry_recomputed"] is True
    vol_after = k.query("_current_geometry", "volume").value
    assert abs(vol_after - vol_plate) < 1e-3, f"{vol_after} != {vol_plate}"


def test_delete_base_feature_clears_geometry():
    if not HAS_OCC:
        return
    k = MechKernel()
    r = _make_cylinder(k)
    fid = r.feature_id
    assert k._current_geometry is not None
    rd = k.delete_feature(fid)
    assert rd.success
    assert k._current_geometry is None
    assert len(k._op_history) == 4  # 只剩草图/工作平面


def test_update_extrude_depth_scales_volume():
    if not HAS_OCC:
        return
    k = MechKernel()
    r = _make_cylinder(k, radius=10, depth=10)
    fid = r.feature_id
    vol1 = k.query("_current_geometry", "volume").value
    ru = k.update_feature(fid, {"depth": 20})
    assert ru.success and ru.value["geometry_recomputed"] is True
    vol2 = k.query("_current_geometry", "volume").value
    assert abs(vol2 - 2 * vol1) < 1e-3


def test_update_circle_radius_scales_volume_squared():
    if not HAS_OCC:
        return
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "sk")
    rc = k.add_circle("sk", center=(0, 0), radius=5)
    eid = rc.feature_id
    k.close_sketch("sk")
    k.extrude("sk", depth=10, mode="new_body")
    vol1 = k.query("_current_geometry", "volume").value
    ru = k.update_feature(eid, {"radius": 10})
    assert ru.success and ru.value["geometry_recomputed"] is True
    vol2 = k.query("_current_geometry", "volume").value
    assert abs(vol2 - 4 * vol1) < 1e-3


def test_undo_after_delete_restores():
    if not HAS_OCC:
        return
    k = MechKernel()
    _make_cylinder(k, radius=10, depth=10)
    vol1 = k.query("_current_geometry", "volume").value
    # 再打一个孔
    rh = k.hole(position=(0, 0), diameter=5)
    vol2 = k.query("_current_geometry", "volume").value
    assert vol2 < vol1
    # 删除孔
    rd = k.delete_feature(rh.feature_id)
    assert rd.success
    vol3 = k.query("_current_geometry", "volume").value
    assert abs(vol3 - vol1) < 1e-3
    # undo delete → 恢复带孔
    ru = k.undo()
    assert ru.success
    vol4 = k.query("_current_geometry", "volume").value
    assert abs(vol4 - vol2) < 1e-3


def test_update_replay_failure_rolls_back():
    """重放中途失败（fillet 半径过大）→ 状态回滚到 update 前"""
    if not HAS_OCC:
        return
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "sk")
    k.add_rectangle("sk", 20, 20)
    k.close_sketch("sk")
    k.extrude("sk", depth=10, mode="new_body")
    rf = k.fillet(1.0, edges="all")
    fid = rf.feature_id
    vol_before = k.query("_current_geometry", "volume").value
    try:
        k.update_feature(fid, {"radius": 100.0})
        assert False, "更新为超大圆角应导致重放失败"
    except Exception:
        pass
    vol_after = k.query("_current_geometry", "volume").value
    assert abs(vol_after - vol_before) < 1e-3, "失败后状态应回滚"
    assert k._op_history[-1]["args"]["radius"] == 1.0, "失败后历史参数应保持原值"
