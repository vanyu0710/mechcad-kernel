"""
v1.16 修复回归测试
1. Capability Registry schema <-> 真实方法签名一致性（防漂移）
2. execute() LLM 调用路径可用（草图级 + 真实几何级）
3. delete_feature / update_feature 诚实行为（真实变更 + warning）
4. undo/redo 恢复几何
5. query/measure feature 目标解析 + 负坐标
6. sweep 方向 + extrude 偏移圆
"""
import inspect
import math

import pytest
from mech_kernel import MechKernel, InvalidRequestError
from mech_kernel.features import FeatureNode, FeatureType, FeatureState
from mech_kernel.kernel import PUBLIC_OPS

try:
    import build123d  # noqa: F401
    import OCP  # noqa: F401
    HAS_OCC = True
except Exception:
    HAS_OCC = False


def _method_params(method):
    """返回 (参数名列表, 有默认值的参数集合)，跳过 self 和 *args/**kwargs"""
    sig = inspect.signature(method)
    params = []
    has_default = set()
    for name, p in sig.parameters.items():
        if name == "self":
            continue
        if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        params.append(name)
        if p.default is not inspect.Parameter.empty:
            has_default.add(name)
    return params, has_default


# === 1. Capability Registry schema 一致性 ===

def test_capability_schema_matches_signatures():
    """每个 PUBLIC_OP 的注册 schema 必须与真实方法签名一致（键集合 + 必填）"""
    k = MechKernel()
    reg_names = set(k.cap._caps.keys())
    from mech_kernel.kernel import EXPERIMENTAL_OPS
    assert reg_names == set(PUBLIC_OPS) | set(EXPERIMENTAL_OPS), (
        f"registry 与 PUBLIC_OPS+EXPERIMENTAL_OPS 不一致: {sorted(reg_names ^ (set(PUBLIC_OPS) | set(EXPERIMENTAL_OPS)))}"
    )
    for op in reg_names:
        method = getattr(k, op, None)
        assert method is not None and callable(method), f"{op} 没有对应方法"
        schema = k.cap.get(op).input_schema
        params, has_default = _method_params(method)
        assert set(schema.keys()) == set(params), (
            f"{op}: schema 键 {sorted(schema.keys())} != 方法参数 {sorted(params)}"
        )
        for pname in params:
            if pname in has_default:
                assert not schema[pname].required, f"{op}.{pname} 有默认值但 schema 标 required"
            else:
                assert schema[pname].required, f"{op}.{pname} 无默认值但 schema 未标 required"


def test_execute_rejects_bogus_kwargs():
    """execute() 校验仍工作：未知字段拒绝"""
    k = MechKernel()
    r = k.execute("add_circle", sketch_name="sk", center=(0, 0), radius=5, bogus=1)
    assert not r.success
    assert r.error_kind == "INVALID_REQUEST"


def test_execute_export_rejects_iges_via_schema():
    """export 的 format 枚举已收敛为 step，iges 应在 schema 层被拒"""
    k = MechKernel()
    r = k.execute("export", path="x.iges", format="iges")
    assert not r.success
    assert r.error_kind == "INVALID_REQUEST"


# === 2. execute() 草图级 LLM 路径 ===

def test_execute_sketch_ops_via_execute():
    k = MechKernel()
    for op, kw in [
        ("create_workplane", {"name": "base", "type": "XY"}),
        ("new_sketch", {"workplane_name": "base", "sketch_name": "sk"}),
        ("add_circle", {"sketch_name": "sk", "center": (0, 0), "radius": 5}),
        ("add_rectangle", {"sketch_name": "sk", "width": 10, "height": 5, "center": (1, 1)}),
        ("add_line", {"sketch_name": "sk", "start": (0, 0), "end": (5, 5)}),
        ("close_sketch", {"sketch_name": "sk"}),
        ("undo", {"steps": 1}),
        ("redo", {"steps": 1}),
    ]:
        r = k.execute(op, **kw)
        assert r.success, f"{op} via execute 失败: {r.error}"


# === 3. delete_feature / update_feature 诚实化 ===

def test_delete_feature_removes_history_and_replays():
    """v2.0：delete_feature 删除历史目标及后续 → 重放（真实重算）"""
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "sk")
    r1 = k.add_circle("sk", center=(0, 0), radius=5)
    r2 = k.add_rectangle("sk", width=10, height=5)
    e1, e2 = r1.feature_id, r2.feature_id
    assert e1 in [e.get("feature_id") for e in k._op_history]

    r = k.delete_feature(e1)
    assert r.success
    assert r.value["geometry_recomputed"] is True
    assert [e.type for e in k.sketches["sk"].entities] == ["rectangle"]
    assert e1 not in [e.get("feature_id") for e in k._op_history]


def test_delete_feature_unknown_raises():
    k = MechKernel()
    try:
        k.delete_feature("F_NONEXIST")
        assert False, "应抛 InvalidRequestError"
    except InvalidRequestError:
        pass


def test_update_feature_updates_params_and_replays():
    """v2.0：update_feature 改历史参数 → 重放（草图实体半径更新）"""
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "sk")
    r = k.add_circle("sk", center=(0, 0), radius=5)
    eid = r.feature_id

    r2 = k.update_feature(eid, {"radius": 8})
    assert r2.success
    assert r2.value["geometry_recomputed"] is True
    assert k.sketches["sk"].entities[0].params["radius"] == 8


# === 4/5. measure 负坐标 + query 未知 feature（无 OCC 也测）===

def test_measure_negative_coordinates():
    """负坐标正则：(-10,-5,0) 到 (10,5,0) 距离 = sqrt(500)（measure 无条件导入 OCP，需 OCC 环境）"""
    if not HAS_OCC:
        return
    k = MechKernel()
    r = k.measure("(-10, -5, 0)", "(10, 5, 0)", metric="distance")
    assert r.success, r.error
    assert abs(r.value["distance"] - math.sqrt(500)) < 1e-6


def test_measure_rejects_unknown_metric():
    k = MechKernel()
    try:
        k.measure("(0,0,0)", "(1,1,1)", metric="angle")
        assert False, "angle 已移除，应抛 InvalidRequestError"
    except InvalidRequestError:
        pass


def test_query_rejects_unknown_feature():
    k = MechKernel()
    try:
        k.query("F_9999", "volume")
        assert False, "应抛 InvalidRequestError"
    except InvalidRequestError as e:
        assert "F_9999" in str(e)


def test_export_rejects_non_step():
    k = MechKernel()
    k._geometry_internal = object()  # 绕过几何检查，测 format 校验
    try:
        k.export("x.iges", format="iges")
        assert False, "应抛 InvalidRequestError"
    except InvalidRequestError as e:
        assert "step" in str(e)


def test_step_result_warning_field_in_summary():
    """StepResult.warning 进入 to_summary_dict"""
    from mech_kernel.step_result import make_success
    r = make_success(feature_id="F_001", narrative="n", warning="注意：几何未重算")
    assert r.warning == "注意：几何未重算"
    assert r.to_summary_dict()["warning"] == "注意：几何未重算"

# === 6. OCC 依赖：execute() 全 op 真实几何 ===

def _make_workplane(k, name="base"):
    k.create_workplane(name, "XY")


def _make_closed_circle_sketch(k, sketch_name, center=(0, 0), radius=10):
    k.new_sketch("base", sketch_name)
    k.add_circle(sketch_name, center=center, radius=radius)
    k.close_sketch(sketch_name)


def test_execute_real_ops_workflow():
    """execute() 完整 LLM 路径：底板→hole→fillet→query→select→measure→export"""
    if not HAS_OCC:
        return
    import os
    import tempfile
    k = MechKernel()
    for op, kw in [
        ("create_workplane", {"name": "base", "type": "XY"}),
        ("new_sketch", {"workplane_name": "base", "sketch_name": "base_sk"}),
        ("add_rectangle", {"sketch_name": "base_sk", "width": 40, "height": 30}),
        ("close_sketch", {"sketch_name": "base_sk"}),
        ("extrude", {"sketch_name": "base_sk", "depth": 5, "mode": "new_body", "name": "base_body"}),
    ]:
        r = k.execute(op, **kw)
        assert r.success, f"{op} failed: {r.error}"

    r = k.execute("fillet", radius=0.5, edges="all")
    assert r.success, r.error
    r = k.execute("hole", position=(0, 0), diameter=6, hole_type="simple")
    assert r.success, r.error
    r = k.execute("query", target="_current_geometry", what="volume")
    assert r.success and r.value > 0
    r = k.execute("select", filter_type="cylinder")
    assert r.success
    assert r.value["by_type"]["cylinder"] >= 1
    r = k.execute("measure", target1="(-10, -10, 0)", target2="(10, 10, 0)", metric="distance")
    assert r.success, r.error
    assert abs(r.value["distance"] - math.hypot(20, 20)) < 1e-6

    tmp = tempfile.mktemp(suffix=".step")
    r = k.execute("export", path=tmp, format="step")
    assert r.success, r.error
    assert os.path.exists(tmp)


def test_execute_boolean_via_execute():
    """boolean subtract：40x30x50 板 - r5 圆柱"""
    if not HAS_OCC:
        return
    k = MechKernel()
    for op, kw in [
        ("create_workplane", {"name": "base", "type": "XY"}),
        ("new_sketch", {"workplane_name": "base", "sketch_name": "a"}),
        ("add_rectangle", {"sketch_name": "a", "width": 40, "height": 30}),
        ("close_sketch", {"sketch_name": "a"}),
        ("new_sketch", {"workplane_name": "base", "sketch_name": "b"}),
        ("add_circle", {"sketch_name": "b", "center": (0, 0), "radius": 5}),
        ("close_sketch", {"sketch_name": "b"}),
    ]:
        r = k.execute(op, **kw)
        assert r.success, f"{op}: {r.error}"
    r = k.execute("boolean", target_sketch="a", tools=["b"], operation="subtract", depth=50)
    assert r.success, r.error
    vol = k.query("_current_geometry", "volume").value
    expected = 40 * 30 * 50 - math.pi * 25 * 50
    assert abs(vol - expected) < expected * 0.02


# === 7. undo/redo 恢复几何 ===

def test_undo_restores_geometry():
    if not HAS_OCC:
        return
    k = MechKernel()
    _make_workplane(k)
    _make_closed_circle_sketch(k, "sk1", radius=10)
    r1 = k.extrude("sk1", depth=10, mode="new_body", name="cyl1")
    assert r1.success
    vol1 = k.query("_current_geometry", "volume").value

    _make_closed_circle_sketch(k, "sk2", center=(30, 0), radius=5)
    r2 = k.extrude("sk2", depth=10, mode="add", name="cyl2")
    assert r2.success
    vol2 = k.query("_current_geometry", "volume").value
    assert vol2 > vol1

    r3 = k.undo()
    assert r3.success, r3.error
    vol_after = k.query("_current_geometry", "volume").value
    assert abs(vol_after - vol1) < 1e-3, f"undo 后几何未恢复: {vol_after} != {vol1}"

    r4 = k.redo()
    assert r4.success, r4.error
    vol_redo = k.query("_current_geometry", "volume").value
    assert abs(vol_redo - vol2) < 1e-3, f"redo 后几何未恢复: {vol_redo} != {vol2}"


# === 8. query(feature_id) 返回该 feature 当时的几何 ===

def test_query_feature_target_returns_feature_geometry():
    if not HAS_OCC:
        return
    k = MechKernel()
    _make_workplane(k)
    _make_closed_circle_sketch(k, "sk1", radius=10)
    r1 = k.extrude("sk1", depth=10, mode="new_body", name="cyl1")
    fid1 = r1.feature_id
    vol1 = k.query(fid1, "volume").value

    _make_closed_circle_sketch(k, "sk2", center=(30, 0), radius=5)
    r2 = k.extrude("sk2", depth=10, mode="add", name="cyl2")
    fid2 = r2.feature_id
    vol2 = k.query(fid2, "volume").value
    assert vol2 > vol1

    vol1_again = k.query(fid1, "volume").value
    assert abs(vol1_again - vol1) < 1e-6, "feature1 的几何应保持为第一体"


# === 9. sweep 方向生效 ===

def test_sweep_path_direction_differs():
    if not HAS_OCC:
        return

    def sweep_along(path):
        k = MechKernel()
        _make_workplane(k)
        _make_closed_circle_sketch(k, "prof", radius=5)
        r = k.sweep("prof", path=path, length=30)
        assert r.success, r.error
        return k.query("_current_geometry", "bounding_box").value

    bb_x = sweep_along("x_axis")
    bb_z = sweep_along("z_axis")
    assert bb_x["size_x"] > bb_z["size_x"] + 1, f"x_axis 应沿 X 拉长: {bb_x} vs {bb_z}"
    assert bb_z["size_z"] > bb_x["size_z"] + 1, f"z_axis 应沿 Z 拉长: {bb_x} vs {bb_z}"


# === 10. extrude new_body 偏移圆位置 ===

def test_extrude_offset_circle_position():
    if not HAS_OCC:
        return
    k = MechKernel()
    _make_workplane(k)
    _make_closed_circle_sketch(k, "sk", center=(20, 0), radius=5)
    r = k.extrude("sk", depth=10, mode="new_body")
    assert r.success, r.error
    bb = k.query("_current_geometry", "bounding_box").value
    assert abs(bb["xmin"] - 15) < 1e-6, bb
    assert abs(bb["xmax"] - 25) < 1e-6, bb
    assert abs(bb["ymin"] + 5) < 1e-6, bb
    assert abs(bb["ymax"] - 5) < 1e-6, bb
