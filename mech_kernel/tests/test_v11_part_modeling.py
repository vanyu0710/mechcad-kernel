"""
v2.11 零件建模全流程 tests.

覆盖:
- WS1 安全基线: new_body 防护 / confirm_replace / sweep mode / 深度参数化
- WS2 选边选面闭环: select ref → fillet/chamfer/shell / 面上草图 / hole direction / countersink 真锥面
- WS3 真弧线剖面: 精确体积 + revolve / 分叉检测保留
- WS4 harness 接口: ID 实例隔离 / 结构化 suggestion / experimental 闸门 / reference frame 持久化
"""
from __future__ import annotations
import math
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mech_kernel import MechKernel
from mech_kernel.kernel import PUBLIC_OPS, EXPERIMENTAL_OPS
from mech_kernel.errors import InvalidRequestError, RecoverableError


def _make_plate(k, name="plate", w=40, h=30, depth=10):
    k.create_workplane("base", "XY")
    k.new_sketch("base", name)
    k.add_rectangle(name, w, h)
    k.close_sketch(name)
    return k.extrude(name, depth=depth)


# ============================================================
# WS1: 安全基线
# ============================================================

def test_new_body_guard_returns_recoverable_with_fix():
    """已有几何时 new_body → RECOVERABLE, suggestion.fix 指向 mode='add'"""
    k = MechKernel()
    _make_plate(k)
    k.new_sketch("base", "boss")
    k.add_circle("boss", (0, 0), 5)
    k.close_sketch("boss")
    r = k.execute("extrude", sketch_name="boss", depth=15, mode="new_body")
    assert not r.success
    assert r.error_kind == "RECOVERABLE"
    assert r.suggestion["fix"] == {"mode": "add"}
    assert r.suggestion["reason_code"] == "new_body_would_replace"
    assert r.suggestion["alternatives"][-1]["fix"] == {"confirm_replace": True}
    # 几何未被破坏
    assert abs(k.query("_current_geometry", "volume").value - 40 * 30 * 10) < 1e-6
    print("  ✓ test_new_body_guard_returns_recoverable_with_fix")


def test_new_body_guard_direct_call_raises_recoverable_error():
    """直调方法同样受防护（抛 RecoverableError）"""
    k = MechKernel()
    _make_plate(k)
    k.new_sketch("base", "boss")
    k.add_circle("boss", (0, 0), 5)
    k.close_sketch("boss")
    try:
        k.extrude("boss", depth=15, mode="new_body")
        assert False, "应抛 RecoverableError"
    except RecoverableError:
        pass
    print("  ✓ test_new_body_guard_direct_call_raises_recoverable_error")


def test_confirm_replace_escape_hatch():
    """confirm_replace=True 允许显式替换（旧行为逃生门）"""
    k = MechKernel()
    _make_plate(k)
    k.new_sketch("base", "block_sk")
    k.add_rectangle("block_sk", 20, 20)
    k.close_sketch("block_sk")
    r = k.execute("extrude", sketch_name="block_sk", depth=8, mode="new_body",
                  confirm_replace=True)
    assert r.success, r.error
    assert abs(k.query("_current_geometry", "volume").value - 20 * 20 * 8) < 1e-3
    print("  ✓ test_confirm_replace_escape_hatch")


def test_sweep_mode_add_and_cut():
    """sweep v2.11: mode add/cut 不再静默丢弃已有几何"""
    k = MechKernel()
    _make_plate(k, depth=10)
    k.new_sketch("base", "rod_prof")
    k.add_circle("rod_prof", (0, 0), 2)
    k.close_sketch("rod_prof")
    r = k.execute("sweep", profile_sketch="rod_prof", path="x_axis", length=50, mode="add")
    assert r.success, r.error
    vol_add = k.query("_current_geometry", "volume").value
    assert vol_add > 40 * 30 * 10 + 300, vol_add  # 板 + 至少大半根棒
    r2 = k.execute("sweep", profile_sketch="rod_prof", path="x_axis", length=50, mode="cut")
    assert r2.success, r2.error
    vol_cut = k.query("_current_geometry", "volume").value
    assert vol_cut < vol_add
    print("  ✓ test_sweep_mode_add_and_cut")


def test_sweep_new_body_guard():
    """sweep new_body 同样有防护"""
    k = MechKernel()
    _make_plate(k)
    k.new_sketch("base", "rod_prof")
    k.add_circle("rod_prof", (0, 0), 2)
    k.close_sketch("rod_prof")
    r = k.execute("sweep", profile_sketch="rod_prof", path="x_axis", length=50)
    assert not r.success and r.error_kind == "RECOVERABLE"
    assert r.suggestion["reason_code"] == "new_body_would_replace"
    print("  ✓ test_sweep_new_body_guard")


def test_linear_pattern_depth_derived_not_hardcoded_50():
    """linear_pattern depth 缺省按零件 Z 尺寸推导（不再硬编码 50）"""
    k = MechKernel()
    _make_plate(k, depth=10)
    k.new_sketch("base", "holes")
    k.add_circle("holes", (0, 0), 2)
    k.close_sketch("holes")
    r = k.execute("linear_pattern", sketch_name="holes", count=4,
                  direction=(1, 0), spacing=5, mode="cut")
    assert r.success, r.error
    assert r.warning is not None and "depth" in r.warning
    # 4 个 Ø4 通孔: 板 12000 - 4*π*4*10
    expected = 40 * 30 * 10 - 4 * math.pi * 4 * 10
    vol = k.query("_current_geometry", "volume").value
    assert abs(vol - expected) < 1.0, (vol, expected)
    print("  ✓ test_linear_pattern_depth_derived_not_hardcoded_50")


def test_mirror_explicit_depth():
    """mirror 支持显式 depth"""
    k = MechKernel()
    _make_plate(k, depth=12)
    k.new_sketch("base", "notch")
    k.add_rectangle("notch", 4, 6, center=(8, 0))
    k.close_sketch("notch")
    r = k.execute("mirror", sketch_name="notch", axis="X", mode="cut", depth=12)
    assert r.success, r.error
    expected = 40 * 30 * 12 - 2 * (4 * 6 * 12)
    vol = k.query("_current_geometry", "volume").value
    assert abs(vol - expected) < 1.0, (vol, expected)
    print("  ✓ test_mirror_explicit_depth")


# ============================================================
# WS2: 选边/选面闭环
# ============================================================

def _box_with_selectable_edges(k):
    _make_plate(k, name="box", w=20, h=20, depth=10)


def test_select_edge_returns_feedable_refs():
    """select element_type=edge 返回带 ref 的摘要"""
    k = MechKernel()
    _box_with_selectable_edges(k)
    r = k.execute("select", element_type="edge", filter_type="line")
    assert r.success, r.error
    edges = r.value["selected"]
    assert len(edges) > 0
    for e in edges:
        assert e["ref"].startswith("E")
        assert "length_mm" in e and "center" in e
    assert r.value["element_type"] == "edge"
    print("  ✓ test_select_edge_returns_feedable_refs")


def test_fillet_specific_edges_volume():
    """fillet 指定 4 条竖直边 → 体积精确可预测"""
    k = MechKernel()
    _box_with_selectable_edges(k)
    sel = k.execute("select", element_type="edge", filter_type="line")
    # 竖直边: length 10（Z 向棱边；水平边均为 20）
    v_refs = [e["ref"] for e in sel.value["selected"] if abs(e["length_mm"] - 10) < 1e-6]
    assert len(v_refs) == 4, v_refs
    r = k.execute("fillet", radius=2, edges=v_refs)
    assert r.success, r.error
    # 体积 = 盒 - 4 条竖棱的角柱亏量 (4-π)·r²·h
    expected = 20 * 20 * 10 - (4 - math.pi) * 4 * 10
    vol = k.query("_current_geometry", "volume").value
    assert abs(vol - expected) < 0.1, (vol, expected)
    print("  ✓ test_fillet_specific_edges_volume")


def test_fillet_stale_ref_is_recoverable():
    """几何修改后旧边引用 → RECOVERABLE(re_select)"""
    k = MechKernel()
    _box_with_selectable_edges(k)
    sel = k.execute("select", element_type="edge", filter_type="line")
    refs = [e["ref"] for e in sel.value["selected"]][:2]
    # 修改几何（打孔）→ revision 变化
    k.hole(position=(0, 0), diameter=5)
    r = k.execute("fillet", radius=1, edges=refs)
    assert not r.success and r.error_kind == "RECOVERABLE"
    assert r.suggestion["reason_code"] == "stale_topo_ref"
    assert "re-select" in r.suggestion["action"] or "重新 select" in r.suggestion["action"]
    print("  ✓ test_fillet_stale_ref_is_recoverable")


def test_chamfer_specific_edges():
    """chamfer 指定顶面 4 条边"""
    k = MechKernel()
    _box_with_selectable_edges(k)
    sel = k.execute("select", element_type="edge", filter_type="line")
    # 顶面边: 中心 z≈20
    top_refs = [e["ref"] for e in sel.value["selected"]
                if abs(e["center"][2] - 10) < 1e-6]
    assert len(top_refs) == 4, top_refs
    r = k.execute("chamfer", length=1.5, edges=top_refs)
    assert r.success, r.error
    print("  ✓ test_chamfer_specific_edges")


def test_shell_with_face_refs():
    """shell face_refs 直接指定开口面"""
    k = MechKernel()
    _box_with_selectable_edges(k)
    sel = k.execute("select", filter_type="plane")
    top = [f for f in sel.value["selected"]
           if f.get("normal") and abs(f["normal"][2] - 1) < 1e-6
           and abs(f["center"][2] - 10) < 1e-6]
    assert top
    r = k.execute("shell", thickness=2, face_refs=[top[0]["ref"]])
    assert r.success, r.error
    # 内腔 16x16x18; OCC GeomAbs_Arc join 使腔角圆化 → 余料略多于尖角理想值
    vol = k.query("_current_geometry", "volume").value
    sharp = 20 * 20 * 10 - 16 * 16 * 8
    assert sharp < vol < sharp + 1300, (vol, sharp)
    print("  ✓ test_shell_with_face_refs")


def test_hole_direction_side_entry():
    """hole 从侧面进入 (x-)"""
    k = MechKernel()
    _make_plate(k, name="box", w=40, h=30, depth=20)
    r = k.execute("hole", position=(10, 10), diameter=6, direction="x-")
    assert r.success, r.error
    expected = 40 * 30 * 20 - math.pi * 9 * 40  # 孔沿 X 穿透 40
    vol = k.query("_current_geometry", "volume").value
    assert abs(vol - expected) < 1.0, (vol, expected)
    print("  ✓ test_hole_direction_side_entry")


def test_hole_countersink_real_cone():
    """countersink 是真 90° 锥面（体积可精确预测）"""
    k = MechKernel()
    _make_plate(k, name="box", w=40, h=30, depth=20)
    d, cs_d, cs_depth = 6, 12, 3
    r = k.execute("hole", position=(0, 0), diameter=d, hole_type="countersink",
                  counterbore_diameter=cs_d, counterbore_depth=cs_depth)
    assert r.success, r.error
    # 方向默认 top: 沿 Z 穿透板厚 20
    bore = math.pi * (d / 2) ** 2 * 20
    frustum = math.pi * cs_depth / 3 * ((cs_d / 2) ** 2 + (d / 2) ** 2 + (cs_d / 2) * (d / 2))
    overlap = math.pi * (d / 2) ** 2 * cs_depth  # 锥体与孔柱重叠段
    expected = 40 * 30 * 20 - bore - (frustum - overlap)
    vol = k.query("_current_geometry", "volume").value
    assert abs(vol - expected) < 2.0, (vol, expected)
    print("  ✓ test_hole_countersink_real_cone")


def test_sketch_on_face_workflow():
    """select face → create_workplane face_ref → sketch → cut 全链路"""
    k = MechKernel()
    _make_plate(k, name="box", w=40, h=30, depth=10)
    sel = k.execute("select", filter_type="plane")
    top = [f for f in sel.value["selected"]
           if f.get("normal") and abs(f["normal"][2] - 1) < 1e-6
           and abs(f["center"][2] - 10) < 1e-6]
    assert top
    wp = k.execute("create_workplane", name="on_top", face_ref=top[0]["ref"])
    assert wp.success, wp.error
    k.new_sketch("on_top", "pocket")
    k.add_circle("pocket", (0, 0), 4)
    k.close_sketch("pocket")
    r = k.execute("extrude", sketch_name="pocket", depth=5, mode="cut", reverse=True)
    assert r.success, r.error
    expected = 40 * 30 * 10 - math.pi * 16 * 5
    vol = k.query("_current_geometry", "volume").value
    assert abs(vol - expected) < 1.0, (vol, expected)
    # 不带 reverse 的同款 cut → 静默无切除会被警告
    r2 = k.execute("extrude", sketch_name="pocket", depth=5, mode="cut")
    assert r2.success and r2.warning and "reverse" in r2.warning, (r2.success, r2.warning)
    print("  ✓ test_sketch_on_face_workflow")


def test_sketch_on_curved_face_rejected():
    """曲面引用建 workplane → RECOVERABLE(face_not_planar)"""
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "cyl_sk")
    k.add_circle("cyl_sk", (0, 0), 10)
    k.close_sketch("cyl_sk")
    k.extrude("cyl_sk", depth=20)
    sel = k.execute("select", filter_type="cylinder")
    assert sel.value["selected"], "应有圆柱面"
    r = k.execute("create_workplane", name="on_cyl", face_ref=sel.value["selected"][0]["ref"])
    assert not r.success and r.error_kind == "RECOVERABLE"
    assert r.suggestion["reason_code"] == "face_not_planar"
    print("  ✓ test_sketch_on_curved_face_rejected")


# ============================================================
# WS3: 真弧线剖面
# ============================================================

def test_mixed_profile_arc_wire_exact_volume():
    """line+arc 混合剖面 → 真弧线（体积 0 误差, 非采样近似）"""
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "d_sk")
    k.add_line("d_sk", start=(-10, 0), end=(10, 0))
    k.add_arc("d_sk", center=(0, 0), radius=10, start_angle=0, end_angle=180)
    k.close_sketch("d_sk")
    r = k.execute("extrude", sketch_name="d_sk", depth=5)
    assert r.success, (r.error, r.warning)
    assert r.warning is None  # 未走采样回退
    exact = math.pi * 100 / 2 * 5
    vol = k.query("_current_geometry", "volume").value
    assert abs(vol - exact) / exact < 1e-6, (vol, exact)
    print("  ✓ test_mixed_profile_arc_wire_exact_volume")


def test_revolve_arc_wire_exact():
    """revolve 半圆剖面 → 半角环面 Pappus 精确体积"""
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "prof")
    k.add_arc("prof", center=(5, 0), radius=5, start_angle=180, end_angle=360)
    k.add_line("prof", start=(10, 0), end=(0, 0))
    k.close_sketch("prof")
    r = k.execute("revolve", sketch_name="prof", angle=360)
    assert r.success, (r.error, r.warning)
    exact = 2 * math.pi * 5 * (math.pi * 25 / 2)  # Pappus: 2π·r̄·A
    vol = k.query("_current_geometry", "volume").value
    assert abs(vol - exact) / exact < 1e-6, (vol, exact)
    print("  ✓ test_revolve_arc_wire_exact")


def test_revolve_on_custom_workplane_honest_error():
    """revolve 拒绝非标准基准面上的草图（诚实报错）"""
    k = MechKernel()
    k.create_workplane("lift", "XY", offset=10)
    k.new_sketch("lift", "prof")
    k.add_rectangle("prof", 4, 4)
    k.close_sketch("prof")
    try:
        k.revolve("prof", angle=360)
        assert False, "应抛 InvalidRequestError"
    except InvalidRequestError as e:
        assert "revolve" in str(e)
    print("  ✓ test_revolve_on_custom_workplane_honest_error")


def test_custom_workplane_origin_respected():
    """custom workplane origin/normal 真实生效（不再被丢弃）"""
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "pad_sk")
    k.add_rectangle("pad_sk", 10, 10)
    k.close_sketch("pad_sk")
    k.extrude("pad_sk", depth=5)
    # 侧面偏置平面: x=20 处法向 -X
    r = k.execute("create_workplane", name="side", type="custom",
                  origin=[20, 0, 0], normal=[-1, 0, 0])
    assert r.success, r.error
    k.new_sketch("side", "boss_sk")
    k.add_circle("boss_sk", (0, 0), 3)
    k.close_sketch("boss_sk")
    r2 = k.execute("extrude", sketch_name="boss_sk", depth=8, mode="add")
    assert r2.success, r2.error
    # boss 在平面 origin (20,0,0) 处沿法向 (-1,0,0) 生长 → x∈[12,20], 与 pad 不相交
    expected = 10 * 10 * 5 + math.pi * 9 * 8
    vol = k.query("_current_geometry", "volume").value
    assert abs(vol - expected) < 1.0, (vol, expected)
    bb = k.query("_current_geometry", "bounding_box").value
    assert bb["xmax"] >= 20 - 1e-3 and bb["xmin"] < 5, bb
    print("  ✓ test_custom_workplane_origin_respected")


def test_offset_workplane():
    """基准面 offset 偏置"""
    k = MechKernel()
    r = k.execute("create_workplane", name="top_plane", type="XY", offset=25)
    assert r.success, r.error
    k.new_sketch("top_plane", "sk")
    k.add_circle("sk", (0, 0), 5)
    k.close_sketch("sk")
    r2 = k.execute("extrude", sketch_name="sk", depth=5, mode="new_body")
    assert r2.success, r2.error
    bb = k.query("_current_geometry", "bounding_box").value
    assert abs(bb["zmin"] - 25) < 1e-6 and abs(bb["zmax"] - 30) < 1e-6, bb
    print("  ✓ test_offset_workplane")


# ============================================================
# WS4: harness 接口
# ============================================================

def test_id_generators_are_instance_scoped():
    """两个 kernel 实例 ID 计数互不污染"""
    a, b = MechKernel(), MechKernel()
    _make_plate(a, name="p1")
    assert a._ids.feature.counter > 0
    assert b._ids.feature.counter == 0
    _make_plate(b, name="p2")
    assert b._ids.feature.counter == a._ids.feature.counter
    print("  ✓ test_id_generators_are_instance_scoped")


def test_unknown_field_suggestion_has_valid_fields():
    """unknown field 错误附 valid_fields"""
    k = MechKernel()
    r = k.execute("fillet", radius=1, bogus=2)
    assert not r.success
    assert r.suggestion["reason_code"] == "unknown_field"
    assert sorted(r.suggestion["valid_fields"]) == ["edges", "name", "radius"]
    print("  ✓ test_unknown_field_suggestion_has_valid_fields")


def test_experimental_gate():
    """装配 op 默认拒绝, allow_experimental=True 放行"""
    k = MechKernel()
    assert len(PUBLIC_OPS) == 33
    assert len(EXPERIMENTAL_OPS) == 10
    assert "assemble" in EXPERIMENTAL_OPS and "assemble" not in PUBLIC_OPS
    r = k.execute("query_assembly")
    assert not r.success and "experimental" in r.error
    r2 = k.execute("query_reference", allow_experimental=True)
    assert r2.success, r2.error
    llm_ops = {c["name"] for c in k.cap.list_public()}
    assert "assemble" not in llm_ops and "fillet" in llm_ops
    print("  ✓ test_experimental_gate")


def test_reference_frames_survive_save_load(tmp=None):
    """save/load 往返保留参考坐标系"""
    import tempfile, json
    from pathlib import Path
    k = MechKernel()
    _make_plate(k)
    k.execute("create_reference_plane", name="axis_a", origin=[1, 2, 3],
              normal=[0, 0, 1], allow_experimental=True)
    with tempfile.TemporaryDirectory() as tmp:
        paths = k.save_project(str(Path(tmp) / "proj"))
        with open(paths["json_path"], encoding="utf-8") as f:
            data = json.load(f)
        assert "axis_a" in data.get("_frame_registry", {})
        k2 = MechKernel()
        k2.load_project(str(Path(tmp) / "proj"))
        q = k2.execute("query_reference", allow_experimental=True)
        assert q.success
        names = [fr["name"] for fr in q.value["frames"]] if isinstance(q.value, dict) and "frames" in q.value else q.value
        assert any("axis_a" in str(n) for n in (names if isinstance(names, list) else [names])), q.value
    print("  ✓ test_reference_frames_survive_save_load")


def test_replay_works_with_edge_ref_fillet():
    """带边引用的 fillet 在参数化重放后仍正确（引用对重放拓扑确定性再解析）"""
    k = MechKernel()
    _make_plate(k, name="box", w=20, h=20, depth=20)
    sel = k.execute("select", element_type="edge", filter_type="line")
    v_refs = [e["ref"] for e in sel.value["selected"] if abs(e["length_mm"] - 20) < 1e-6]
    r = k.execute("fillet", radius=2, edges=v_refs)
    assert r.success, r.error
    vol_before = k.query("_current_geometry", "volume").value
    rb = k.execute("rebuild")
    assert rb.success, rb.error
    vol_after = k.query("_current_geometry", "volume").value
    assert abs(vol_before - vol_after) < 0.1, (vol_before, vol_after)
    print("  ✓ test_replay_works_with_edge_ref_fillet")


def test_fillet_too_large_structured_suggestion():
    """fillet 失败 → RECOVERABLE + 减半半径建议"""
    k = MechKernel()
    _make_plate(k, name="box", w=20, h=20, depth=20)
    r = k.execute("fillet", radius=15)  # 对 20 盒全部边过大
    assert not r.success and r.error_kind == "RECOVERABLE"
    assert r.suggestion["reason_code"] == "fillet_too_large"
    assert r.suggestion["fix"]["radius"] == 7.5
    print("  ✓ test_fillet_too_large_structured_suggestion")


if __name__ == "__main__":
    import mech_kernel._pytest_compat as mock
    sys.modules['pytest'] = mock
    sys.exit(mock.main([os.path.dirname(os.path.abspath(__file__))]))
