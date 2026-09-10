"""v2.13 run_script 代码通道 tests.

覆盖:
- 成功脚本（循环打阵列孔）→ 单实体 + 体积对账 + stdout 捕获
- 可重放性（本次核心断言）: 代码件的 op 全进 _op_history，rebuild/update_feature 照常
- 失败脚本 → 原始 traceback 回传 + 状态整体回滚（DSH 检查点语义）
- AST 静态拒绝: import build123d / import os / 下划线属性 / 超长代码
- 运行时拒绝: k.execute("export") 黑名单
- query solid_count（finish_part 复检契约依赖）
"""
from __future__ import annotations
import math
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mech_kernel import MechKernel
from mech_kernel.script_sandbox import ScriptSandboxError, validate_script_code


PLATE_CODE = """
k.create_workplane(name="base", type="XY")
k.new_sketch(workplane_name="base", sketch_name="plate")
k.add_rectangle(sketch_name="plate", width=100, height=60, name="outline")
k.close_sketch(sketch_name="plate")
k.extrude(sketch_name="plate", depth=10)
for x in (-35, 35):
    for y in (-20, 20):
        r = k.hole(position=[x, y], diameter=8)
        if not r["success"]:
            raise RuntimeError("hole failed")
print("done")
"""


def test_run_script_success_single_solid_and_stdout():
    k = MechKernel()
    r = k.run_script(PLATE_CODE, name="plate4")
    assert r["success"] is True, r["error"]
    value = r.value
    assert value["ops_executed"] == 9
    assert value["solids"] == 1
    assert "done" in value["stdout"]
    # 体积对账: 100*60*10 - 4*π*4²*10 = 60000 - 2010.6
    expect = 100 * 60 * 10 - 4 * math.pi * 16 * 10
    assert abs(value["volume"] - expect) / expect < 0.01
    print("  ✓ test_run_script_success_single_solid_and_stdout")


def test_run_script_output_is_replayable():
    """核心断言: 代码件的 op 全部进 _op_history → rebuild/update_feature 照常可用。"""
    k = MechKernel()
    r = k.run_script(PLATE_CODE)
    assert r["success"] is True
    assert len(k._op_history) == 9
    assert k._has_non_replayable_op is False
    rb = k.rebuild()
    assert rb["success"] is True, rb["error"]
    v_rebuilt = float(k.execute("query", target="_current_geometry", what="volume")["value"])
    assert abs(v_rebuilt - r.value["volume"]) / r.value["volume"] < 1e-6
    # 参数化编辑: 把底板 extrude 深度 10→20 重放
    extrude_entry = [e for e in k._op_history if e["op"] == "extrude"][0]
    upd = k.update_feature(extrude_entry["feature_id"], {"depth": 20})
    assert upd["success"] is True, upd["error"]
    v2 = float(k.execute("query", target="_current_geometry", what="volume")["value"])
    assert v2 > r.value["volume"] * 1.5
    print("  ✓ test_run_script_output_is_replayable")


def test_run_script_failure_rolls_back_with_traceback():
    k = MechKernel()
    r0 = k.run_script(PLATE_CODE)
    v_before = r0.value["volume"]
    bad = """
k.new_sketch(workplane_name="base", sketch_name="bad")
k.add_rectangle(sketch_name="bad", width=200, height=200, name="big")
k.close_sketch(sketch_name="bad")
k.extrude(sketch_name="bad", depth=50, mode="new_body", confirm_replace=True)
raise ValueError("boom after building")
"""
    r = k.run_script(bad)
    assert r["success"] is False
    assert r["error_kind"] == "RECOVERABLE"
    assert "boom after building" in r["error"]  # 原始 traceback 回传（DSH 反馈通道）
    # 状态整体回滚: 几何与历史回到执行前
    v_after = float(k.execute("query", target="_current_geometry", what="volume")["value"])
    assert abs(v_after - v_before) < 1e-6
    assert len(k._op_history) == 9
    print("  ✓ test_run_script_failure_rolls_back_with_traceback")


def test_run_script_static_rejections():
    k = MechKernel()
    cases = [
        "import build123d\nresult = 1",                      # 裸几何库
        "from build123d import Box\nresult = 1",             # from-import 同样拒
        "import os\nprint(os.getcwd())",                     # 危险模块
        "k._current_geometry = None",                        # 下划线属性
        "x = k.__class__",                                   # dunder
        "result = eval('1+1')",                              # 危险调用名
        "x = getattr(k, '_op_history')",                     # 反射绕过
        "k.create_workplane" + "(name='a')\n" * 1 + "# pad" + "x" * 25000,  # 超长
    ]
    for code in cases:
        r = k.run_script(code)
        assert r["success"] is False, f"应拒绝: {code[:40]!r}"
        assert r["error_kind"] == "INVALID_REQUEST", (code[:40], r["error_kind"])
    print("  ✓ test_run_script_static_rejections")


def test_run_script_runtime_export_blacklist():
    """k.execute('export') 运行时被门面拒绝（导出归上层 finish_part），且回滚。"""
    k = MechKernel()
    r = k.run_script(
        "k.create_workplane(name='b', type='XY')\n"
        "k.execute('export', path='x.step')\n")
    assert r["success"] is False
    assert "export" in r["error"]
    # 回滚后无残留
    assert len(k._op_history) == 0
    print("  ✓ test_run_script_runtime_export_blacklist")


def test_query_solid_count():
    k = MechKernel()
    # 两个不接触方块经 add 模式会 fuse 成单实体；用悬浮双实体场景验证计数
    k.run_script(
        "k.create_workplane(name='b', type='XY')\n"
        "k.new_sketch(workplane_name='b', sketch_name='s1')\n"
        "k.add_rectangle(sketch_name='s1', width=10, height=10)\n"
        "k.close_sketch(sketch_name='s1')\n"
        "k.extrude(sketch_name='s1', depth=5)\n")
    r = k.execute("query", target="_current_geometry", what="solid_count")
    assert r["success"] is True
    assert r.value == 1
    print("  ✓ test_query_solid_count")


def test_run_script_math_namespace_available():
    k = MechKernel()
    code = """
import math
r = k.create_workplane(name="base", type="XY")
k.new_sketch(workplane_name="base", sketch_name="sk")
k.add_circle(sketch_name="sk", center=(0, 0), radius=math.sqrt(16.0))
k.close_sketch(sketch_name="sk")
k.extrude(sketch_name="sk", depth=5)
"""
    r = k.run_script(code)
    assert r["success"] is True, r["error"]
    bb = r.value["bounding_box"]
    assert abs(bb[3] - 4.0) < 0.01  # radius=4
    print("  ✓ test_run_script_math_namespace_available")


# ---------- entrypoint ----------




def _plane_bbox(wp_type: str):
    """在给定标准平面画 center=(30,50) r=5 的圆、depth=10 拉伸，返回 bbox。"""
    k = MechKernel()
    k.create_workplane(name="w", type=wp_type)
    k.new_sketch(workplane_name="w", sketch_name="s")
    k.add_circle(sketch_name="s", center=(30, 50), radius=5)
    k.close_sketch(sketch_name="s")
    k.extrude(sketch_name="s", depth=10)
    return k.execute("query", target="_current_geometry", what="bounding_box").value
def test_standard_plane_axes_match_declaration():
    """v2.13.2: XZ 曾因左手系声明被 build123d 重算，u/v 方向与声明相反，
    模型只能反复试探平面映射。此回归锁死三个标准平面的 (u,v,normal) 语义。"""
    cases = {
        # 平面: (期望 xmin,xmax,ymin,ymax,zmin,zmax)
        "XY": (25, 35, 45, 55, 0, 10),      # u=x, v=y, 法向 +z
        "YZ": (0, 10, 25, 35, 45, 55),      # u=y, v=z, 法向 +x
        "XZ": (25, 35, -10, 0, 45, 55),     # u=x, v=z, 法向 -y（右手系）
    }
    for wp_type, (x0, x1, y0, y1, z0, z1) in cases.items():
        bb = _plane_bbox(wp_type)
        got = (round(bb["xmin"]), round(bb["xmax"]), round(bb["ymin"]),
               round(bb["ymax"]), round(bb["zmin"]), round(bb["zmax"]))
        expect = (x0, x1, y0, y1, z0, z1)
        assert all(abs(a - b) <= 1 for a, b in zip(got, expect)), \
            f"{wp_type}: 实测 {got}，期望 {expect}"
    print("  ✓ test_standard_plane_axes_match_declaration")
def test_standard_planes_are_right_handed():
    """声明的 (x_dir, y_dir, normal) 必须构成右手系，否则几何构建必然走样。"""
    from mech_kernel.workplane import Workplane, WorkplaneType
    for t in (WorkplaneType.XY, WorkplaneType.YZ, WorkplaneType.XZ):
        wp = Workplane(id="x", name="x", type=t)
        xd, yd, nd = wp.x_dir, wp.y_dir, wp.normal
        cross = (xd[1] * yd[2] - xd[2] * yd[1],
                 xd[2] * yd[0] - xd[0] * yd[2],
                 xd[0] * yd[1] - xd[1] * yd[0])
        assert all(abs(cross[i] - nd[i]) < 1e-9 for i in range(3)), \
            f"{t}: x×y={cross} != normal={nd}（左手系）"
    print("  ✓ test_standard_planes_are_right_handed")

def main():
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    print(f"找到 {len(tests)} 个 v2.13 run_script 测试\n")
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
