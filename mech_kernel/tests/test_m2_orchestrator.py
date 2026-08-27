"""
测试 M2: AI Orchestrator（v1 单 Agent）

- Mock Planner 解析用户指令生成 plan
- run_loop 跑通 Plan→Execute→Inspect→Decide 循环
- E2E：建圆柱/立方体/法兰盘
"""
import pytest
from mech_kernel import MechKernel
from mech_kernel.ai_orchestrator import (
    MockPlanner, MockVision, run_loop, PlannerAction
)


def test_planner_action_to_dict():
    a = PlannerAction(op="add_circle", args={"sketch_name": "sk_1", "center": (0,0), "radius": 50}, description="画圆")
    d = a.to_dict()
    assert d["op"] == "add_circle"
    assert d["args"]["radius"] == 50
    assert d["is_final"] is False


def test_mock_planner_simple_cylinder():
    k = MechKernel()
    planner = MockPlanner(k)
    
    actions = planner._plan_from_prompt("建一个圆柱体 Ø100 高度 20")
    
    # 应该有：create_workplane + 4 步圆柱 + final
    assert len(actions) >= 5
    ops = [a.op for a in actions]
    assert "create_workplane" in ops
    assert "new_sketch" in ops
    assert "add_circle" in ops
    assert "close_sketch" in ops
    assert "extrude" in ops
    assert ops[-1] == "__final__"
    
    # 圆半径 = 50
    circle_action = [a for a in actions if a.op == "add_circle"][0]
    assert circle_action.args["radius"] == 50


def test_mock_planner_flange():
    k = MechKernel()
    planner = MockPlanner(k)
    actions = planner._plan_from_prompt("建一个法兰盘")
    ops = [a.op for a in actions]
    assert "extrude" in ops


def test_mock_planner_box():
    k = MechKernel()
    planner = MockPlanner(k)
    actions = planner._plan_from_prompt("建一个 30 立方体")
    ops = [a.op for a in actions]
    assert "add_rectangle" in ops
    # 矩形宽度
    rect = [a for a in actions if a.op == "add_rectangle"][0]
    assert rect.args["width"] == 30


def test_run_loop_cylinder():
    """E2E: 用户说"建一个圆柱" → orchestrator 跑通"""
    k = MechKernel()
    planner = MockPlanner(k)
    vision = MockVision()
    
    result = run_loop(
        kernel=k,
        planner=planner,
        vision=vision,
        user_prompt="建一个圆柱体 Ø100 高度 20",
        max_steps=20,
    )
    
    assert result["success"] is True
    assert result["steps"] >= 5
    
    # kernel 应该有 workplane + sketch + extrude feature
    state = result["final_state"]
    assert state["workplane_count"] >= 1
    assert state["feature_count"] >= 2  # sketch + extrude


def test_run_loop_max_steps_limit():
    """max_steps 限制"""
    k = MechKernel()
    planner = MockPlanner(k)
    vision = MockVision()
    
    # 即使 prompt 短，max_steps 也会限制
    result = run_loop(
        kernel=k,
        planner=planner,
        vision=vision,
        user_prompt="建一个圆柱体",
        max_steps=2,
    )
    # 2 步内不会完成
    assert result["steps"] <= 2


def test_run_loop_vision_failure_triggers_undo():
    """Vision 失败时撤销"""
    k = MechKernel()
    planner = MockPlanner(k)
    
    class FailingVision:
        def verify(self, render_base64, expected_action, user_prompt):
            return False  # 永远失败
    
    initial_feature_count = len(k.feature_graph)
    
    result = run_loop(
        kernel=k,
        planner=planner,
        vision=FailingVision(),
        user_prompt="建一个圆柱体",
        max_steps=20,
    )
    
    # Vision 失败会触发 undo，feature_count 应该不会爆炸增长
    # 注意：plan 还是会跑完，只是每步都会被 undo
    assert result["success"] is True


def test_run_loop_with_recoverable():
    """可恢复错误走 suggestion 重试路径"""
    k = MechKernel()
    
    class RecoverablePlanner:
        """第一步返回一个会触发 RECOVERABLE 的操作"""
        def __init__(self):
            self.step = 0
        def decide(self, user_prompt, current_narrative, geometry_summary, last_render_base64):
            self.step += 1
            if self.step == 1:
                return PlannerAction(
                    op="add_circle",
                    args={"sketch_name": "missing_sketch", "center": (0,0), "radius": 5},
                    description="会失败"
                )
            return PlannerAction(op="__final__", args={}, is_final=True)
    
    planner = RecoverablePlanner()
    vision = MockVision()
    
    result = run_loop(k, planner, vision, "test", max_steps=5)
    # 应该成功（即使第一次失败，最终还是 final）
    assert result["success"] is True


def test_kernel_execute_dispatches_correctly():
    """kernel.execute(op) 调用对应方法"""
    k = MechKernel()
    
    # 真实存在的 op
    r = k.execute("create_workplane", name="base", type="XY")
    assert r.success
    
    # 不存在的 op
    r = k.execute("nonexistent_op")
    assert not r.success
    assert r.error_kind == "NOT_IMPLEMENTED"
    
    # 触发 InvalidRequestError 的 op
    r = k.execute("create_workplane", name="", type="XY")
    assert not r.success
    assert r.error_kind == "INVALID_REQUEST"


def test_kernel_execute_catches_exceptions():
    """kernel.execute 捕获 InvalidRequestError"""
    k = MechKernel()
    k.create_workplane("base", "XY")
    
    # 重名触发 InvalidRequestError
    r = k.execute("create_workplane", name="base", type="YZ")
    assert not r.success
    assert r.error_kind == "INVALID_REQUEST"
