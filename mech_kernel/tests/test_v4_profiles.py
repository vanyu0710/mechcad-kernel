"""
v2.1: 剖面/装配能力测试
1. add_polyline / add_arc 草图实体（记录 + 校验）
2. revolve 支持 line/polyline/arc 闭合剖面（CD 喷口）
3. extrude 支持闭合 polyline 剖面
4. assemble 装配（多 STEP 零件定位融合）
"""
import math
import tempfile
from pathlib import Path

import pytest
from mech_kernel import MechKernel, InvalidRequestError
from mech_kernel.kernel import PUBLIC_OPS

try:
    import build123d  # noqa: F401
    import OCP  # noqa: F401
    HAS_OCC = True
except Exception:
    HAS_OCC = False


def _workplane_sketch(k, name="sk"):
    k.create_workplane("XY", "XY")
    k.new_sketch("XY", name)
    return k


# === 1. add_polyline / add_arc 实体与历史 ===

def test_add_polyline_arc_record_entities():
    k = MechKernel()
    _workplane_sketch(k)
    rp = k.add_polyline("sk", [[0, 0], [10, 0], [10, 5], [0, 5]])
    ra = k.add_arc("sk", center=(0, 0), radius=5, start_angle=0, end_angle=180)
    types = [e.type for e in k.sketches["sk"].entities]
    assert types == ["polyline", "arc"]
    assert rp.feature_id.startswith("E_") and ra.feature_id.startswith("E_")
    ops = [e["op"] for e in k._op_history]
    assert "add_polyline" in ops and "add_arc" in ops
    assert k._op_history[-2]["feature_id"] == rp.feature_id
    assert k._op_history[-1]["feature_id"] == ra.feature_id


def test_add_polyline_requires_3_points():
    k = MechKernel()
    _workplane_sketch(k)
    try:
        k.add_polyline("sk", [[0, 0], [10, 0]])
        assert False, "应抛 InvalidRequestError"
    except InvalidRequestError:
        pass


def test_add_arc_rejects_bad_radius():
    k = MechKernel()
    _workplane_sketch(k)
    try:
        k.add_arc("sk", center=(0, 0), radius=-1, start_angle=0, end_angle=90)
        assert False, "应抛 InvalidRequestError"
    except InvalidRequestError:
        pass


def test_new_ops_are_public():
    assert {"add_polyline", "add_arc", "assemble"} <= set(PUBLIC_OPS)


# === 2. revolve 闭合剖面 ===

def test_revolve_cd_nozzle_lines():
    """CD 喷口：line 闭合半剖面绕 Y 轴旋转（收敛-喉部-扩张）"""
    if not HAS_OCC:
        return
    k = MechKernel()
    _workplane_sketch(k, "cd")
    for seg in [((0, 0), (22, 0)), ((22, 0), (8, 20)), ((8, 20), (8, 30)),
                ((8, 30), (16, 50)), ((16, 50), (0, 50)), ((0, 50), (0, 0))]:
        k.add_line("cd", start=seg[0], end=seg[1])
    k.close_sketch("cd")
    r = k.revolve("cd", axis=[0, 0, 0, 0, 1, 0], angle=360, mode="new_body")
    assert r.success, r.error
    vol = k.query("_current_geometry", "volume").value
    assert vol > 1000
    bb = k.query("_current_geometry", "bounding_box").value
    assert abs(bb["size_x"] - 44) < 0.1  # 最大半径 22 → 直径 44
    assert abs(bb["size_z"] - 44) < 0.1
    assert abs(bb["size_y"] - 50) < 0.1  # 高度 50


def test_revolve_polyline_profile():
    if not HAS_OCC:
        return
    k = MechKernel()
    _workplane_sketch(k, "pl")
    k.add_polyline("pl", [[0, 0], [20, 0], [8, 25], [0, 25]])
    k.close_sketch("pl")
    r = k.revolve("pl", axis=[0, 0, 0, 0, 1, 0], angle=360, mode="new_body")
    assert r.success, r.error
    assert k.query("_current_geometry", "volume").value > 0


def test_revolve_arc_profile():
    if not HAS_OCC:
        return
    k = MechKernel()
    _workplane_sketch(k, "arc")
    k.add_line("arc", start=(0, 0), end=(18, 0))
    k.add_arc("arc", center=(0, 0), radius=18, start_angle=0, end_angle=90)
    k.add_line("arc", start=(0, 18), end=(0, 0))
    k.close_sketch("arc")
    r = k.revolve("arc", axis=[0, 0, 0, 0, 1, 0], angle=360, mode="new_body")
    assert r.success, r.error
    assert k.query("_current_geometry", "volume").value > 0


def test_revolve_rejects_open_profile():
    if not HAS_OCC:
        return
    k = MechKernel()
    _workplane_sketch(k, "bad")
    k.add_line("bad", start=(0, 0), end=(10, 0))
    k.close_sketch("bad")
    try:
        k.revolve("bad", axis=[0, 0, 0, 0, 1, 0], angle=360, mode="new_body")
        assert False, "未闭合剖面应报错"
    except InvalidRequestError as e:
        assert "未闭合" in str(e) or "不连续" in str(e)


# === 3. extrude 闭合 polyline ===

def test_extrude_polyline_profile():
    if not HAS_OCC:
        return
    k = MechKernel()
    _workplane_sketch(k, "pl")
    k.add_polyline("pl", [[0, 0], [10, 0], [10, 5], [0, 5]])
    k.close_sketch("pl")
    r = k.extrude("pl", depth=20, mode="new_body")
    assert r.success, r.error
    vol = k.query("_current_geometry", "volume").value
    assert abs(vol - 1000.0) < 1e-3  # 10x5x20


# === 4. assemble ===

def _make_step_cylinder(r, h, name):
    k = MechKernel()
    _workplane_sketch(k, "s")
    k.add_circle("s", center=(0, 0), radius=r)
    k.close_sketch("s")
    k.extrude("s", depth=h, mode="new_body")
    path = Path(tempfile.gettempdir()) / name
    k.export(str(path), format="step")
    return str(path)


def test_assemble_fuses_parts():
    if not HAS_OCC:
        return
    pa = _make_step_cylinder(10, 20, "a.step")
    pb = _make_step_cylinder(5, 10, "b.step")
    k = MechKernel()
    r = k.assemble([{"path": pa, "position": [0, 0, 0]},
                    {"path": pb, "position": [0, 0, 25]}])
    assert r.success, r.error
    vol = k.query("_current_geometry", "volume").value
    assert abs(vol - (math.pi * 100 * 20 + math.pi * 25 * 10)) < 1.0
    assert k._has_non_replayable_op is True  # 装配不可重放


def test_assemble_blocks_delete():
    if not HAS_OCC:
        return
    pa = _make_step_cylinder(10, 20, "a2.step")
    k = MechKernel()
    r = k.assemble([{"path": pa, "position": [0, 0, 0]}])
    assert r.success
    fid = r.feature_id
    rd = k.delete_feature(fid)
    assert not rd.success and rd.error_kind == "RECOVERABLE"

# === 5. 完善项回归（v2.1 polish）===

def test_extrude_add_polyline_protrudes():
    """add 模式支持 polyline：伸出底板的剖面才增加体积"""
    if not HAS_OCC:
        return
    k = MechKernel()
    k.create_workplane("XY", "XY")
    k.new_sketch("XY", "base")
    k.add_rectangle("base", 40, 30)
    k.close_sketch("base")
    k.extrude("base", depth=10, mode="new_body")
    v0 = k.query("_current_geometry", "volume").value
    k.new_sketch("XY", "rib")
    k.add_polyline("rib", [[30, 0], [45, 0], [45, 5], [30, 5]])  # 板 x -20..20，完全伸出
    k.close_sketch("rib")
    r = k.extrude("rib", depth=10, mode="add")
    assert r.success, r.error
    dv = k.query("_current_geometry", "volume").value - v0
    assert abs(dv - 750.0) < 1.0  # 15x5x10


def test_boolean_polyline_tool():
    """boolean subtract 支持 polyline 工具草图"""
    if not HAS_OCC:
        return
    k = MechKernel()
    k.create_workplane("XY", "XY")
    k.new_sketch("XY", "a")
    k.add_rectangle("a", 40, 30)
    k.close_sketch("a")
    k.new_sketch("XY", "b")
    k.add_polyline("b", [[0, 0], [10, 0], [10, 5], [0, 5]])
    k.close_sketch("b")
    r = k.boolean("a", tools=["b"], operation="subtract")
    assert r.success, r.error
    vol = k.query("_current_geometry", "volume").value
    assert abs(vol - (40 * 30 * 50 - 10 * 5 * 50)) < 1.0


def test_assembly_bbox_covers_all_solids():
    """query bbox 取全部 SOLID 并集（装配体包围盒覆盖整机）"""
    if not HAS_OCC:
        return
    pa = _make_step_cylinder(10, 20, "aa.step")
    pb = _make_step_cylinder(5, 10, "bb.step")
    k = MechKernel()
    k.assemble([{"path": pa, "position": [0, 0, 0]},
                {"path": pb, "position": [0, 0, 25]}])
    bb = k.query("_current_geometry", "bounding_box").value
    assert abs(bb["zmin"]) < 0.1
    assert abs(bb["zmax"] - 35.0) < 0.1


def test_pattern_rejects_polyline():
    """pattern/mirror 对 polyline/arc 明确报错（不静默出错）"""
    if not HAS_OCC:
        return
    k = MechKernel()
    k.create_workplane("XY", "XY")
    k.new_sketch("XY", "pl")
    k.add_polyline("pl", [[0, 0], [10, 0], [10, 5], [0, 5]])
    k.close_sketch("pl")
    k.extrude("pl", depth=5, mode="new_body")
    k.new_sketch("XY", "cut")
    k.add_polyline("cut", [[0, 0], [5, 0], [5, 3], [0, 3]])
    k.close_sketch("cut")
    try:
        k.linear_pattern("cut", count=3, direction=(1, 0), spacing=10, mode="cut")
        assert False, "应抛 NotImplementedError"
    except NotImplementedError:
        pass


def test_profile_branch_detection():
    """剖面分叉（一个点引出多条线）明确报错"""
    if not HAS_OCC:
        return
    k = MechKernel()
    k.create_workplane("XY", "XY")
    k.new_sketch("XY", "bad")
    k.add_line("bad", start=(0, 0), end=(10, 0))
    k.add_line("bad", start=(0, 0), end=(5, 5))
    k.close_sketch("bad")
    try:
        k.revolve("bad", axis=[0, 0, 0, 0, 1, 0], angle=360, mode="new_body")
        assert False, "应抛 InvalidRequestError"
    except InvalidRequestError as e:
        assert "分叉" in str(e)


def test_extrude_offset_circle_single_cylinder():
    """偏移圆 extrude 不产生幻影原点圆柱（bbox 覆盖 15..25 而非 -5..25）"""
    if not HAS_OCC:
        return
    k = MechKernel()
    k.create_workplane("XY", "XY")
    k.new_sketch("XY", "sk")
    k.add_circle("sk", center=(20, 0), radius=5)
    k.close_sketch("sk")
    k.extrude("sk", depth=10, mode="new_body")
    bb = k.query("_current_geometry", "bounding_box").value
    assert abs(bb["xmin"] - 15) < 1e-3
    assert abs(bb["xmax"] - 25) < 1e-3
