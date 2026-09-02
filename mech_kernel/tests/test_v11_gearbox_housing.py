"""
Demo 15 单级减速器箱座 tests — 复杂零件精确体积 + 参数化重放 + 选边引用闭环

零件: 11 步特征（底板/内腔/选边倒圆/自定义工作平面凸台x2/侧面镗孔x2/
螺栓孔x4/排油孔/油标凸台+孔/顶缘倒角）
解析体积 = 407035.5 + 5892·π ≈ 425545.8 mm³，包围盒 200×130×72。
"""
from __future__ import annotations
import importlib.util
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mech_kernel import MechKernel
from mech_kernel.errors import RecoverableError

# 复用 demo 15 的建模序列（模块导入无副作用，仅在 __main__ 下执行 main）
_DEMO_PATH = Path(__file__).resolve().parents[1] / "examples" / "16_gearbox_housing.py"
_spec = importlib.util.spec_from_file_location("demo16_gearbox_housing", str(_DEMO_PATH))
_d15 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_d15)

# 解析体积（独立于 demo 常数，交叉验证）
ANALYTIC_TOTAL = 407035.5 + 5892 * math.pi
BBOX_EXPECT = (200.0, 130.0, 72.0)


def _build():
    return _d15.build_housing(verbose=False)


def _volume(k):
    return k.query("_current_geometry", "volume").value


def _bbox_sizes(k):
    bb = k.query("_current_geometry", "bounding_box").value
    return bb["size_x"], bb["size_y"], bb["size_z"]


def test_final_volume_matches_analytic():
    """11 步特征后的体积与解析值精确一致（每步增量已由 demo 逐步核对）"""
    k = _build()
    v = _volume(k)
    assert abs(v - ANALYTIC_TOTAL) < 1.0, f"体积 {v} vs 解析 {ANALYTIC_TOTAL}"
    # demo 常数与测试常数交叉一致
    assert abs(_d15.ANALYTIC_TOTAL - ANALYTIC_TOTAL) < 1e-6
    print("  ✓ test_final_volume_matches_analytic")


def test_bounding_box_exact():
    """包围盒 200×130×72：底板 200x130 决定 x/y，箱体高 72 决定 z"""
    k = _build()
    sx, sy, sz = _bbox_sizes(k)
    assert abs(sx - BBOX_EXPECT[0]) < 1e-3, sx
    assert abs(sy - BBOX_EXPECT[1]) < 1e-3, sy
    assert abs(sz - BBOX_EXPECT[2]) < 1e-3, sz
    vr = k.validate_geometry(level="standard")
    assert vr.success and vr.value["valid"] is True
    assert vr.value["solid_count"] == 1
    print("  ✓ test_bounding_box_exact")


def test_rebuild_preserves_volume():
    """全历史确定性重放：rebuild 后体积不变（无 import/assemble，可重放）"""
    k = _build()
    v0 = _volume(k)
    k.rebuild()
    v1 = _volume(k)
    assert abs(v1 - v0) < max(1e-6, v0 * 1e-9), f"rebuild 前后 {v0} vs {v1}"
    assert abs(v1 - ANALYTIC_TOTAL) < 1.0
    print("  ✓ test_rebuild_preserves_volume")


def test_update_bore_diameter_exact_delta():
    """update_feature 轴承孔 Ø36→Ø40：ΔV = π(20²−18²)·18 = 1368π 精确"""
    k = _build()
    v0 = _volume(k)
    fid = next(n.id for n in k.feature_graph.nodes.values()
               if n.name == "bearing_bore_ypos")
    r = k.update_feature(fid, {"diameter": 40.0})
    assert r.success, r.error
    v1 = _volume(k)
    expected_delta = math.pi * (20 ** 2 - 18 ** 2) * 18  # 1368π
    assert abs((v0 - v1) - expected_delta) < 1.0, f"ΔV={v0 - v1} vs {expected_delta}"
    assert abs(v1 - (ANALYTIC_TOTAL - expected_delta)) < 1.0
    print("  ✓ test_update_bore_diameter_exact_delta")


def test_stale_edge_ref_after_geometry_change():
    """select 发放的边引用在几何被修改后失效 → RecoverableError(stale_topo_ref)"""
    k = _build()
    sel = k.select(filter_type="line", element_type="edge")
    refs = [it["ref"] for it in sel.value["selected"]][:4]
    assert len(refs) == 4
    # 几何修改（底板打一个 Ø5 小孔）→ revision bump → 旧引用失效
    k.hole(position=(0.0, 0.0), diameter=5.0, depth=6.0, direction="bottom")
    try:
        k.fillet(2.0, edges=refs)
        assert False, "旧引用应触发 stale_topo_ref"
    except RecoverableError as e:
        assert "失效" in str(e)
        suggestion = getattr(e, "suggestion", None) or {}
        assert suggestion.get("reason_code") == "stale_topo_ref"
    # 重新 select 后引用可用：箱壁角竖棱（z12..70.5，下端 z6..12 被凸缘环包住、
    # 上端被顶缘倒角修掉；底板四角已在建模中倒圆不可复用）倒圆成功且体积下降
    sel2 = k.select(filter_type="line", element_type="edge")
    fresh = [it["ref"] for it in sel2.value["selected"]
             if abs(abs(it["center"][0]) - 80.0) < 0.5
             and abs(abs(it["center"][1]) - 50.0) < 0.5
             and 58.0 < it.get("length_mm", 0.0) < 59.0]
    assert len(fresh) == 4, fresh
    v0 = _volume(k)
    k.fillet(2.0, edges=fresh)
    assert _volume(k) < v0
    print("  ✓ test_stale_edge_ref_after_geometry_change")


if __name__ == "__main__":
    import mech_kernel._pytest_compat as mock
    sys.modules['pytest'] = mock
    sys.exit(mock.main([os.path.dirname(os.path.abspath(__file__))]))
