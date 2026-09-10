"""
v2.12 make_gear 公开 op tests.

覆盖:
- execute("make_gear") 走通（真渐开线齿轮，带孔）
- new_body 覆盖守护（RECOVERABLE + fix/alternatives，与 extrude 语义一致）
- involute_teeth_threshold 切换齿形（involute / trapezoid）
- 参数校验（teeth 类型与范围 / module / width / bore / mode / cut 无几何）
- add 模式并入已有零件
- 参数化重放: update_feature 改齿数 / delete_feature
- PUBLIC_OPS 成员与 cap 派生一致性
"""
from __future__ import annotations
import math
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mech_kernel import MechKernel
from mech_kernel.kernel import PUBLIC_OPS
from mech_kernel.errors import InvalidRequestError
from mech_kernel.feature_graph import FeatureGraph  # noqa: F401  (确认无 import 环)


def _gear(k, **kw):
    args = {"module": 2.0, "teeth": 20, "width": 18}
    args.update(kw)
    return k.execute("make_gear", **args)


def test_make_gear_in_public_ops_and_cap():
    """PUBLIC_OPS 与 capability registry 都含 make_gear（34 个公开 op）"""
    assert "make_gear" in PUBLIC_OPS
    k = MechKernel()
    names = [c["name"] for c in k.cap.list_public()]
    assert "make_gear" in names
    assert k.PUBLIC_OPS == frozenset(names)  # 实例派生集与 cap 一致
    cap = k.cap.get("make_gear")
    for field in ("module", "teeth", "width", "bore", "mode",
                  "confirm_replace", "involute_teeth_threshold", "fallback_to_trapezoid"):
        assert field in cap.input_schema, field


def test_make_gear_success_with_summary_and_validation():
    k = MechKernel()
    r = _gear(k, bore=12)
    assert r["success"] is True, r["error"]
    assert r["feature_id"]
    s = r["geometry_summary"]
    assert s is not None and s.volume > 0
    # m=2 z=20 b=18 减 Ø12 孔：体积在 dedendum/addendum 圆柱壳之间
    v_lo = math.pi * 17.5 ** 2 * 18 - math.pi * 6 ** 2 * 18
    v_hi = math.pi * 22 ** 2 * 18 - math.pi * 6 ** 2 * 18
    assert v_lo < s.volume < v_hi, s.volume
    # _wrap_step_result 附带几何健康度校验
    assert r["geometry_validation"] is not None
    assert "involute" in r["narrative"]


def test_make_gear_new_body_guard():
    """已有几何时 new_body → RECOVERABLE，fix 指向 add，alternatives 含 confirm_replace"""
    k = MechKernel()
    _gear(k)
    r = _gear(k, teeth=40)
    assert r["success"] is False
    assert r["error_kind"] == "RECOVERABLE"
    assert r["suggestion"]["reason_code"] == "new_body_would_replace"
    assert r["suggestion"]["fix"] == {"mode": "add"}
    alts = r["suggestion"]["alternatives"]
    assert {"fix": {"mode": "cut"}} in alts and {"fix": {"confirm_replace": True}} in alts
    # 失败不伤几何：当前零件仍是 z=20 的体积
    v_before = float(k.execute("query", target="_current_geometry", what="volume")["value"])
    # 显式确认后替换
    r2 = _gear(k, teeth=40, confirm_replace=True)
    assert r2["success"] is True, r2["error"]
    v_after = float(r2["geometry_summary"].volume)
    assert v_after > v_before * 3  # z=20→40 面积≈4 倍（孔差别小）


def test_make_gear_threshold_switches_profile():
    """z=60 默认走 trapezoid；threshold 提到 100 走强渐开线（narrative 标注齿形）"""
    k = MechKernel()
    r1 = _gear(k, teeth=60)
    assert r1["success"] is True
    assert "trapezoid" in r1["narrative"]
    k2 = MechKernel()
    r2 = _gear(k2, teeth=60, involute_teeth_threshold=100)
    assert r2["success"] is True
    assert "involute" in r2["narrative"]
    # 真渐开线齿形更"瘦"：同参数下 involute 体积 ≤ trapezoid 体积
    assert r2["geometry_summary"].volume <= r1["geometry_summary"].volume * 1.001


def test_make_gear_invalid_requests():
    k = MechKernel()
    bad_cases = [
        {"teeth": 5},                    # z<6
        {"teeth": 20.5},                 # 非整数
        {"teeth": True},                 # bool 伪装整数
        {"teeth": 401},                  # 上限
        {"module": 0},
        {"width": -3},
        {"bore": -1},
        {"mode": "union"},               # 非法 mode
    ]
    for kw in bad_cases:
        r = _gear(k, **kw)
        assert r["success"] is False, (kw, r["error_kind"])
        assert r["error_kind"] == "INVALID_REQUEST", (kw, r["error"])


def test_make_gear_cut_without_geometry():
    k = MechKernel()
    r = _gear(k, mode="cut")
    assert r["success"] is False
    assert r["error_kind"] == "INVALID_REQUEST"


def test_make_gear_add_mode_merges_into_part():
    """齿轮 add 进已有平板 → 体积为两者并集量级"""
    k = MechKernel()
    k.create_workplane("base", "XY")
    k.new_sketch("base", "plate")
    k.add_rectangle("plate", 80, 80)
    k.close_sketch("plate")
    r_plate = k.execute("extrude", sketch_name="plate", depth=5)
    assert r_plate["success"] is True
    v_plate = r_plate["geometry_summary"].volume
    # width=10 高出板面 5mm → 并集体积显著增加
    r = _gear(k, teeth=30, width=10, bore=0, mode="add")
    assert r["success"] is True, r["error"]
    assert r["geometry_summary"].volume > v_plate + 1000


def test_make_gear_update_feature_replay():
    """参数化重放：改齿数 → 全量重算，体积显著变化"""
    k = MechKernel()
    r = _gear(k, teeth=20, bore=12)
    fid = r["feature_id"]
    v20 = r["geometry_summary"].volume
    r2 = k.execute("update_feature", feature_id=fid, new_params={"teeth": 30})
    assert r2["success"] is True, r2["error"]
    s = k.execute("query", target="_current_geometry", what="volume")
    v30 = float(s["value"])
    assert v30 > v20 * 2  # 分度圆半径 1.5 倍 → 面积约 2.25 倍
    # 历史参数已更新（重放源正确）
    entry = k._op_history[0]
    assert entry["op"] == "make_gear" and entry["args"]["teeth"] == 30


def test_make_gear_delete_feature_clears():
    k = MechKernel()
    r = _gear(k)
    fid = r["feature_id"]
    r2 = k.execute("delete_feature", feature_id=fid)
    assert r2["success"] is True, r2["error"]
    assert k._current_geometry is None
    assert fid not in k.feature_graph.nodes


def test_make_gear_replay_is_deterministic():
    """改参数重放后改回 → 体积回到原值（重放确定性）"""
    k = MechKernel()
    r = _gear(k, teeth=20, bore=12)
    fid = r["feature_id"]
    v_before = float(k.execute("query", target="_current_geometry", what="volume")["value"])
    ru = k.execute("update_feature", feature_id=fid, new_params={"width": 24})
    assert ru["success"] is True, ru["error"]
    v_up = float(k.execute("query", target="_current_geometry", what="volume")["value"])
    assert v_up > v_before * 1.3  # 18→24 高 1/3
    k.execute("update_feature", feature_id=fid, new_params={"width": 18})
    v_back = float(k.execute("query", target="_current_geometry", what="volume")["value"])
    assert abs(v_back - v_before) / v_before < 1e-6


# ---------- entrypoint ----------

def main():
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    print(f"找到 {len(tests)} 个 v2.12 make_gear 测试\n")
    passed = 0
    failed = 0
    failures = []
    for fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            import traceback
            print(f"  ✗ {fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
            failures.append((fn.__name__, f"{type(e).__name__}: {e}"))
    print(f"\n通过 {passed}/{len(tests)}, 失败 {failed}")
    if failures:
        for n, e in failures:
            print(f"  - {n}: {e}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
