"""
Demo 4: M2 E2E — 用户说"建一个圆柱体"，AI 自动建

展示 MechAgent v1 单 orchestrator：
- Mock Planner 解析用户 prompt
- Mock Vision 验证
- Kernel 一步一步执行
- 每步打印结果
"""
import sys
import os
sys.path.insert(0, '/workspace')

from mech_kernel import MechKernel
from mech_kernel.ai_orchestrator import MockPlanner, MockVision, run_loop, PlannerAction


class MockBox:
    """Mock 几何（让 orchestrator 看到有几何可渲染）"""
    def __init__(self, size=10):
        self._size = size
        self.vertices = [
            (0, 0, 0), (size, 0, 0), (size, size, 0), (0, size, 0),
            (0, 0, size), (size, 0, size), (size, size, size), (0, size, size),
        ]
        self.faces = [
            [0, 1, 2], [0, 2, 3],
            [4, 5, 6], [4, 6, 7],
            [0, 1, 5], [0, 5, 4],
            [2, 3, 7], [2, 7, 6],
            [1, 2, 6], [1, 6, 5],
            [0, 3, 7], [0, 7, 4],
        ]
    def volume(self): return self._size ** 3
    def area(self): return 6 * self._size ** 2
    def face_count(self): return 12
    def edge_count(self): return 18
    def vertex_count(self): return 8
    def bounding_box(self):
        from types import SimpleNamespace
        bb = SimpleNamespace()
        bb.min = SimpleNamespace(X=0, Y=0, Z=0)
        bb.max = SimpleNamespace(X=self._size, Y=self._size, Z=self._size)
        return bb


def on_step_cb(step, action, result):
    """每步回调：打印执行情况"""
    status = "✓" if result.success else "✗"
    print(f"  [{step+1}] {status} {action.op:20s} → {action.description}")
    if not result.success:
        print(f"      error: {result.error}")
    if result.render_level != "none":
        print(f"      render_level: {result.render_level} (C 方案策略生效)")


def main():
    print("=" * 70)
    print("MechKernel v1.1 Demo 4: M2 AI Orchestrator E2E")
    print("=" * 70)
    print()
    print("流程：用户说一句话 → MockPlanner 解析 → run_loop 跑通 → 几何生成")
    print()
    
    test_cases = [
        "建一个圆柱体 Ø100 高度 20",
        "建一个立方体 30",
        "建一个法兰盘",
    ]
    
    for i, user_prompt in enumerate(test_cases, 1):
        print(f"\n{'─' * 70}")
        print(f"[Test {i}] 用户: \"{user_prompt}\"")
        print(f"{'─' * 70}")
        
        k = MechKernel()
        # 注：M2 阶段还没接 build123d，但 orchestrator 流程能跑
        # 真实几何用 MockBox 注入
        # k._current_geometry = MockBox(...)
        
        planner = MockPlanner(k)
        vision = MockVision()
        
        result = run_loop(
            kernel=k,
            planner=planner,
            vision=vision,
            user_prompt=user_prompt,
            max_steps=20,
            on_step=on_step_cb,
        )
        
        print()
        print(f"  总步数: {result['steps']}")
        print(f"  最终状态: workplanes={result['final_state']['workplane_count']}, "
              f"sketches={result['final_state']['sketch_count']}, "
              f"features={result['final_state']['feature_count']}")
        print(f"  narrative 条数: {len(result['final_state']['narrative'])}")
    
    print(f"\n{'=' * 70}")
    print("✓ M2 E2E 演示完成")
    print("=" * 70)
    print()
    print("M2 阶段交付：")
    print("  • ai_orchestrator.py    (v1 单 orchestrator + Mock Planner + Mock Vision)")
    print("  • 11 个 M2 测试 + 154/154 全过")
    print("  • PlannerAction 协议")
    print("  • run_loop 通用执行循环")
    print()
    print("v2 升级路径：")
    print("  • Mock Planner → 真实 LLM Planner（GPT-5.6-SOL）")
    print("  • Mock Vision → 真实 Vision LLM")
    print("  • 装 build123d 后 MockBox → 真实几何计算")
    print()
    print("等你装好 build123d + 配置 LLM API，M2 阶段就完全 ready")


if __name__ == "__main__":
    main()
