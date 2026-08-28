"""
MechKernel LLM 客户端（DeepSeek OpenAI 兼容）

专家 v8 审查后接真实 LLM（替换 MockPlanner / MockVision）。
DeepSeek 视觉模型：deepseek-v4-flash-vision-exp
DeepSeek 推理模型：deepseek-reasoner
DeepSeek 通用模型：deepseek-chat

环境变量：DSKEY（DeepSeek 官方 API key）
"""
from __future__ import annotations
import json
import base64
from typing import List, Dict, Any, Optional

from .openai_compatible import OpenAICompatibleClient


class DeepSeekClient(OpenAICompatibleClient):
    """DeepSeek OpenAI 兼容 HTTP 客户端"""
    
    BASE_URL = "https://api.deepseek.com/v1"
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "deepseek-chat",
        base_url: Optional[str] = None,
        timeout: float = 60.0,
    ):
        super().__init__(
            api_key=api_key,
            model=model,
            base_url=base_url or self.BASE_URL,
            timeout=timeout,
            api_key_env="DSKEY",
            provider_name="DeepSeek",
        )


# ====== 系统提示词 ======

VISION_SYSTEM_PROMPT = """你是机械工程师视觉分析助手。

输入图像可能是同一零件的多视图拼图（ISO/FRONT/TOP/SIDE 或转台视图），也可能包含截面。请综合所有视角判断几何，不要把每个格子当成不同零件。

输入：手绘草图（PNG）+ 文字描述
输出：结构化 JSON

JSON schema：
{
  "part_type": "圆盘" | "圆柱" | "立方体" | "环面" | "未知",
  "primary_features": [
    {"feature": "特征名（如 外圆、内孔、矩形、盲孔）", "estimated": {"param": "value mm"}, "confidence": 0.0-1.0}
  ],
  "dimensions": {
    "outer_diameter_mm": 数字 或 null,
    "height_mm": 数字 或 null,
    "inner_diameter_mm": 数字 或 null,
    "length_mm": 数字 或 null,
    "width_mm": 数字 或 null,
    "thickness_mm": 数字 或 null
  },
  "notes": "观察到的细节（不确定就标'无法确定'）",
  "confidence": 0.0-1.0
}

原则：
- 缺值必须 null，不能编造
- confidence 反映你的把握度
- 尺寸从草图比例估算，不确定就给 null
- notes 写"我看到 X，但 Y 看不清"
"""


PLANNER_SYSTEM_PROMPT = """你是机械 CAD Kernel 的 Planner。

输入：Vision LLM 输出的零件 JSON（包含 part_type / dimensions / features）
输出：可执行的 op 序列（JSON 数组）

可用 op（v1 支持）：
1. create_workplane(name: str, type: "XY")  - 必第一步
2. new_sketch(workplane_name: str, sketch_name: str)  - 必在草图前
3. add_circle(sketch_name: str, center: [x, y], radius: float)  - 圆（mm）
4. add_rectangle(sketch_name: str, width: float, height: float, center: [x, y])  - 矩形
5. close_sketch(sketch_name: str)  - ⚠ 必在 extrude 之前！
6. extrude(sketch_name: str, depth: float, mode: "new_body"|"add"|"cut", name: str)

JSON schema：
{
  "ops": [
    {"op": "create_workplane", "args": {...}},
    ...
  ],
  "rationale": "为什么这个序列",
  "estimated_volume_mm3": 数字 或 null
}

原则：
- 第一步必 create_workplane
- 草图加完所有 entity 后必 close_sketch，再 extrude
- 圆盘/圆柱 = create_workplane + new_sketch + add_circle + close_sketch + extrude
- 立方体 = + add_rectangle
- 环面 = + add_circle(外) + add_circle(内) + close_sketch + extrude(mode="new_body") + add_circle(内+外) ... 太复杂；用 cut 实现
- 半径 = 直径/2（注意：add_circle 的 radius 是半径不是直径）
- mode="new_body" 创建新实体，"cut" 切除
- 不确定就给出最简版本
- 输出必须 JSON，不要 markdown
"""


# ====== Vision LLM ======

class DeepSeekVisionLLM(DeepSeekClient):
    """视觉分析 LLM（deepseek-v4-flash-vision-exp）"""
    
    def __init__(self, api_key: Optional[str] = None, **kwargs):
        super().__init__(api_key=api_key, model="deepseek-v4-flash-vision-exp", **kwargs)
    
    def analyze(
        self,
        image_b64: str,
        user_prompt: str = "分析这个零件草图",
        system_prompt: Optional[str] = None,
    ) -> Dict[str, Any]:
        """视觉分析：base64 PNG + 文字 → JSON"""
        messages = [
            {"role": "system", "content": system_prompt or VISION_SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": "这是同一零件的多视图/截面拼图，请综合所有视角。\n" + user_prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
            ]},
        ]
        return self.chat_json(messages, max_tokens=2000)
    
    def analyze_file(self, image_path: str, user_prompt: str = "分析这个零件草图") -> Dict[str, Any]:
        """从文件分析"""
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return self.analyze(b64, user_prompt)


# ====== Planner LLM ======

class DeepSeekPlannerLLM(DeepSeekClient):
    """规划 LLM（deepseek-chat）"""
    
    def __init__(self, api_key: Optional[str] = None, **kwargs):
        super().__init__(api_key=api_key, model="deepseek-chat", **kwargs)
    
    def plan(
        self,
        vision_json: Dict[str, Any],
        user_intent: str = "",
        system_prompt: Optional[str] = None,
    ) -> Dict[str, Any]:
        """把 Vision JSON 拆成 op 序列"""
        messages = [
            {"role": "system", "content": system_prompt or PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({
                "user_intent": user_intent,
                "vision_result": vision_json,
            }, ensure_ascii=False)},
        ]
        return self.chat_json(messages, max_tokens=2000)
