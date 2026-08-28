"""
MechKernel AI Orchestrator (v1 单 orchestrator)

P2-10 (v8 DeepSeek): 加结构化日志
"""
import logging

_logger = logging.getLogger("mech_kernel.orchestrator")
if not _logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s orchestrator: %(message)s",
        datefmt="%H:%M:%S"
    ))
    _logger.addHandler(_h)
    _logger.setLevel(logging.INFO)


from typing import Optional, Callable, List, Dict, Any, Protocol, Set
import time
import json
import hashlib

from .kernel import MechKernel
from .step_result import StepResult
from .errors import (
    InvalidRequestError, KernelBugError, StateCorruptionError,
)


class PlannerProtocol(Protocol):
    def decide(
        self,
        user_prompt: str,
        current_narrative: List[str],
        geometry_summary: Optional[dict],
        last_render_base64: Optional[str],
    ) -> "PlannerAction":
        ...


class VisionProtocol(Protocol):
    def verify(
        self,
        render_base64: str,
        expected_action: str,
        user_prompt: str,
    ) -> bool:
        ...


class PlannerAction:
    def __init__(
        self,
        op: str,
        args: Dict[str, Any],
        is_final: bool = False,
        description: str = "",
        is_unsupported: bool = False,
    ):
        self.op = op
        self.args = args
        self.is_final = is_final
        self.description = description
        self.is_unsupported = is_unsupported
    
    def to_dict(self) -> dict:
        return {
            "op": self.op,
            "args": self.args,
            "is_final": self.is_final,
            "description": self.description,
            "is_unsupported": self.is_unsupported,
        }
    
    def args_signature(self) -> str:
        try:
            args_str = json.dumps(self.args, sort_keys=True, default=str)
        except Exception:
            args_str = str(self.args)
        return f"{self.op}:{hashlib.md5(args_str.encode()).hexdigest()[:8]}"


class MockPlanner:
    """
    Mock Planner（启发式，P1-3 修复版）。
    
    P1-3 修复：无法识别时返回 PlannerAction(is_unsupported=True)，
    而不是静默造一个圆柱。
    """
    
    def __init__(self, kernel: MechKernel):
        self.kernel = kernel
        self.step_count = 0
        self.plan_steps: List[PlannerAction] = []
        self.plan_index = 0
    
    def decide(
        self,
        user_prompt: str,
        current_narrative: List[str],
        geometry_summary: Optional[dict],
        last_render_base64: Optional[str],
    ) -> PlannerAction:
        if self.step_count == 0:
            self.plan_steps = self._plan_from_prompt(user_prompt)
            self.plan_index = 0
        
        self.step_count += 1
        
        if self.plan_index < len(self.plan_steps):
            action = self.plan_steps[self.plan_index]
            self.plan_index += 1
            return action
        
        return PlannerAction(op="__final__", args={}, is_final=True, description="plan completed")
    
    def _plan_from_prompt(self, user_prompt: str) -> List[PlannerAction]:
        prompt_lower = user_prompt.lower()
        steps: List[PlannerAction] = []
        
        # 1. 撤销/重做先处理（独立指令）
        if "撤销" in user_prompt or "undo" in prompt_lower:
            steps.append(PlannerAction(op="undo", args={}, description="撤销"))
            steps.append(PlannerAction(op="__final__", args={}, is_final=True))
            return steps
        
        if "重做" in user_prompt or "redo" in prompt_lower:
            steps.append(PlannerAction(op="redo", args={}, description="重做"))
            steps.append(PlannerAction(op="__final__", args={}, is_final=True))
            return steps
        
        # 2. 创建工作平面（如果还没有）
        if not self.kernel.workplanes.has_name("base"):
            steps.append(PlannerAction(
                op="create_workplane",
                args={"name": "base", "type": "XY"},
                description="创建 XY 工作平面"
            ))
        
        # 3. 意图识别
        intent = self._recognize_intent(user_prompt, prompt_lower)
        
        if intent == "unsupported":
            # P1-3 修复：返回 UNSUPPORTED，不静默造圆柱
            steps.append(PlannerAction(
                op="__unsupported__", args={}, is_final=True, is_unsupported=True,
                description=f"无法识别指令: {user_prompt}"
            ))
            return steps
        
        if intent == "cylinder":
            diameter = self._extract_number(user_prompt, default=100)
            height = self._extract_height(user_prompt, default=20)
            radius = diameter / 2
            steps.extend([
                PlannerAction(op="new_sketch", args={"workplane_name": "base", "sketch_name": "sk_main"}, description="创建草图"),
                PlannerAction(op="add_circle", args={"sketch_name": "sk_main", "center": (0, 0), "radius": radius}, description=f"画圆 Ø{diameter}"),
                PlannerAction(op="close_sketch", args={"sketch_name": "sk_main"}, description="关闭草图"),
                PlannerAction(op="extrude", args={"sketch_name": "sk_main", "depth": height, "name": "main_body"}, description=f"拉伸 {height}mm"),
            ])
        elif intent == "flange":
            steps.extend([
                PlannerAction(op="new_sketch", args={"workplane_name": "base", "sketch_name": "sk_main"}, description="创建草图"),
                PlannerAction(op="add_circle", args={"sketch_name": "sk_main", "center": (0, 0), "radius": 50}, description="画外圆 Ø100"),
                PlannerAction(op="close_sketch", args={"sketch_name": "sk_main"}, description="关闭草图"),
                PlannerAction(op="extrude", args={"sketch_name": "sk_main", "depth": 20, "name": "main_body"}, description="拉伸主体"),
            ])
        elif intent == "box":
            size = self._extract_number(user_prompt, default=10)
            steps.extend([
                PlannerAction(op="new_sketch", args={"workplane_name": "base", "sketch_name": "sk_main"}, description="创建草图"),
                PlannerAction(op="add_rectangle", args={"sketch_name": "sk_main", "width": size, "height": size, "center": (0, 0)}, description=f"画矩形 {size}×{size}"),
                PlannerAction(op="close_sketch", args={"sketch_name": "sk_main"}, description="关闭草图"),
                PlannerAction(op="extrude", args={"sketch_name": "sk_main", "depth": size, "name": "main_body"}, description=f"拉伸 {size}mm"),
            ])
        elif intent == "shaft":
            size = self._extract_number(user_prompt, default=20)
            steps.extend([
                PlannerAction(op="new_sketch", args={"workplane_name": "base", "sketch_name": "sk_main"}, description="创建草图"),
                PlannerAction(op="add_rectangle", args={"sketch_name": "sk_main", "width": size, "height": size*2, "center": (0, 0)}, description=f"画矩形 {size}×{size*2}"),
                PlannerAction(op="close_sketch", args={"sketch_name": "sk_main"}, description="关闭草图"),
                PlannerAction(op="extrude", args={"sketch_name": "sk_main", "depth": size*3, "name": "main_body"}, description=f"拉伸 {size*3}mm"),
            ])
        
        # 4. 结束
        steps.append(PlannerAction(op="__final__", args={}, is_final=True, description="完成"))
        return steps
    
    def _recognize_intent(self, prompt: str, prompt_lower: str) -> str:
        """意图识别：无法识别返回 unsupported"""
        if "圆柱" in prompt or "cylinder" in prompt_lower:
            return "cylinder"
        if "法兰盘" in prompt or "flange" in prompt_lower:
            return "flange"
        if "轴" in prompt and "盘" not in prompt:
            return "shaft"
        if ("立方体" in prompt or "box" in prompt_lower 
            or "方块" in prompt or "正方体" in prompt):
            return "box"
        
        return "unsupported"
    
    def _extract_number(self, prompt: str, default: float = 50) -> float:
        import re
        numbers = re.findall(r'\d+(?:\.\d+)?', prompt)
        if numbers:
            return float(numbers[0])
        return default
    
    def _extract_height(self, prompt: str, default: float = 20) -> float:
        import re
        m = re.search(r'[高hH]\s*(\d+(?:\.\d+)?)', prompt)
        if m:
            return float(m.group(1))
        return default


class MockVision:
    def verify(self, render_base64, expected_action, user_prompt):
        return True


def run_loop(
    kernel: MechKernel,
    planner: PlannerProtocol,
    vision: VisionProtocol,
    user_prompt: str,
    max_steps: int = 20,
    max_retries_per_action: int = 3,
    max_time_seconds: float = 300.0,
    per_step_timeout_seconds: float = 60.0,
    on_step: Optional[Callable[[int, PlannerAction, StepResult], None]] = None,
) -> Dict[str, Any]:
    """CAD 建模循环（P0-2 修复版）"""
    start_time = time.time()
    history: List[Dict] = []
    attempted_signatures: Set[str] = set()
    
    def is_timeout() -> bool:
        return (time.time() - start_time) > max_time_seconds
    
    _logger.info(f"run_loop start: prompt='{user_prompt[:60]}{'...' if len(user_prompt) > 60 else ''}' max_steps={max_steps} max_time={max_time_seconds}s")
    
    for step in range(max_steps):
        # 0. 全局超时
        if is_timeout():
            return {
                "success": False,
                "error": f"全局超时（>{max_time_seconds}s）",
                "error_kind": "TIMEOUT",
                "steps": step,
                "history": history,
            }
        
        # 1. Planner 决策
        try:
            action = planner.decide(
                user_prompt=user_prompt,
                current_narrative=kernel.get_narrative(),
                geometry_summary=kernel.get_geometry_summary().to_dict() if kernel.get_geometry_summary() else None,
                last_render_base64=kernel.get_last_render_base64(),
            )
        except Exception as e:
            return {
                "success": False,
                "error": f"Planner failed: {e}",
                "error_kind": "PLANNER_ERROR",
                "steps": step,
                "history": history,
            }
        
        # P1-3：UNSUPPORTED 立即返回
        if action.is_unsupported:
            return {
                "success": False,
                "error": f"UNSUPPORTED: {action.description}",
                "error_kind": "UNSUPPORTED",
                "steps": step,
                "history": history,
            }
        
        if action.is_final:
            break
        
        # 2. 参数签名去重
        sig = action.args_signature()
        if sig in attempted_signatures:
            return {
                "success": False,
                "error": f"参数签名重复（无限重试保护）: {action.op} {action.args}",
                "error_kind": "STUCK_LOOP",
                "steps": step,
                "history": history,
            }
        attempted_signatures.add(sig)
        
        # 3. Execute（带 per_step timeout 监控）
        step_start = time.time()
        try:
            result = kernel.execute(action.op, **action.args)
        except (InvalidRequestError, KernelBugError, StateCorruptionError) as e:
            result = StepResult(
                success=False,
                error=str(e),
                error_kind=type(e).__name__,
            )
        except Exception as e:
            result = StepResult(
                success=False,
                error=f"{type(e).__name__}: {e}",
                error_kind="KERNEL_BUG",
            )
        step_elapsed = time.time() - step_start
        
        if step_elapsed > per_step_timeout_seconds:
            return {
                "success": False,
                "error": f"单步超时（>{per_step_timeout_seconds}s）: {action.op}",
                "error_kind": "TIMEOUT",
                "steps": step,
                "history": history,
            }
        
        # 4. Inspect + Recoverable 重试
        retry_count = 0
        while (not result.success 
               and result.error_kind == "RECOVERABLE" 
               and result.suggestion 
               and retry_count < max_retries_per_action):
            
            new_args = {**action.args, **result.suggestion}
            new_sig = f"{action.op}:{hashlib.md5(json.dumps(new_args, sort_keys=True, default=str).encode()).hexdigest()[:8]}"
            
            if new_sig in attempted_signatures:
                break
            attempted_signatures.add(new_sig)
            
            try:
                result = kernel.execute(action.op, **new_args)
            except Exception as e:
                result = StepResult(
                    success=False,
                    error=str(e),
                    error_kind="KERNEL_BUG",
                )
            retry_count += 1
        
        # 5. Vision 验证
        if result.success and result.has_render() and vision is not None:
            try:
                verified = vision.verify(
                    render_base64=result.render_base64 or "",
                    expected_action=action.description,
                    user_prompt=user_prompt,
                )
                if not verified:
                    kernel.undo()
                    continue
            except Exception:
                pass
        
        # 6. 记录
        history.append({
            "step": step,
            "action": action.to_dict(),
            "result": {
                "success": result.success,
                "error": result.error,
                "error_kind": result.error_kind,
                "render_level": result.render_level,
                "has_render": result.has_render(),
                "views": list(result.render_views.keys()) if result.render_views else [],
                "constraint_diagnostics": result.constraint_diagnostics,
                "dof": (
                    result.constraint_diagnostics.get("dof")
                    if isinstance(result.constraint_diagnostics, dict)
                    else None
                ),
                "solver_status": (
                    result.constraint_diagnostics.get("status")
                    if isinstance(result.constraint_diagnostics, dict)
                    else None
                ),
                "conflicting_constraints": (
                    result.constraint_diagnostics.get("conflicting_constraints", [])
                    if isinstance(result.constraint_diagnostics, dict)
                    else []
                ),
                "elapsed_ms": step_elapsed * 1000,
            },
        })
        
        # 7. 回调
        if on_step:
            try:
                on_step(step, action, result)
            except Exception:
                pass
    
    final_state = kernel.get_state()
    elapsed = time.time() - start_time
    _logger.info(f"run_loop done: success=True steps={len(history)} elapsed={elapsed:.2f}s")
    return {
        "success": True,
        "steps": len(history),
        "final_state": final_state,
        "history": history,
        "elapsed_seconds": elapsed,
    }
