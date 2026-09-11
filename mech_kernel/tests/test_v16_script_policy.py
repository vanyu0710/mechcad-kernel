"""v2.16 run_script 失败策略测试（P0-3：脚本内 op 失败绝不静默交付半成品）。

审查测试 4：script 内部 op 失败必须回滚 + SCRIPT_OP_FAILED + failed_op 结构。
"""
import sys

from mech_kernel import MechKernel


def _box_script():
    return """
k.create_workplane("base", "XY")
k.new_sketch("base", "s")
k.add_rectangle("s", 40, 40)
k.close_sketch("s")
k.extrude("s", 10)
"""


def test_abort_policy_rolls_back_and_reports_failed_op():
    k = MechKernel()
    r = k.run_script(_box_script() + "k.undo()\nk.undo()\nk.undo()\nk.undo()\nk.undo()\nk.undo()\n",
                     name="t")
    # 6 次 undo 必然有失败（历史只有 5 个 op）→ abort 整体回滚
    assert r.success is False
    assert r.error_kind == "SCRIPT_OP_FAILED"
    failed = (r.suggestion or {}).get("failed_op") or {}
    assert failed.get("name") == "undo"
    assert (r.suggestion or {}).get("rollback") is True
    assert k._current_geometry is None  # 整体回滚，半成品不残留


def test_raised_error_still_rolls_back_as_recoverable():
    k = MechKernel()
    r = k.run_script(_box_script() + 'k.hole(position=(0, 0), diameter=-1)\n', name="t")
    assert r.success is False
    assert r.error_kind == "RECOVERABLE"  # 入参校验异常走通用路径，同样回滚
    assert k._current_geometry is None


def test_best_effort_collects_failures_but_never_silent_success():
    k = MechKernel()
    r = k.run_script("k.undo()\nk.undo()\n", name="t", failure_policy="best_effort")
    assert r.success is False
    assert r.error_kind == "SCRIPT_OP_FAILED"
    failed = (r.suggestion or {}).get("failed_ops")
    assert failed and failed[0]["op"] == "undo"
    assert (r.suggestion or {}).get("rollback") is False


def test_best_effort_keeps_state_and_never_rolls_back():
    k = MechKernel()
    r = k.run_script(_box_script() + "k.undo()\n" * 6, name="t", failure_policy="best_effort")
    # 前 5 次 undo 成功、第 6 次失败被收集；best_effort 绝不整体回滚
    assert r.success is False
    assert r.error_kind == "SCRIPT_OP_FAILED"
    assert (r.suggestion or {}).get("failed_ops")
    assert (r.suggestion or {}).get("rollback") is False


def test_script_can_try_except_around_risky_ops():
    k = MechKernel()
    r = k.run_script(_box_script() + """
try:
    k.undo()
    k.undo()
    k.undo()
    k.undo()
    k.undo()
    k.undo()
except Exception:
    pass
print("recovered")
""", name="t")
    assert r.success is True
    assert "recovered" in (r.value or {}).get("stdout", "")


def test_invalid_failure_policy_rejected():
    k = MechKernel()
    r = k.run_script("k.query(target='_current_geometry')", failure_policy="yolo")
    assert r.success is False
    assert r.error_kind == "INVALID_REQUEST"


def test_success_path_unaffected():
    k = MechKernel()
    r = k.run_script(_box_script(), name="t")
    assert r.success is True
    assert (r.value or {}).get("solids") == 1
    assert (r.value or {}).get("ops_executed") >= 4
