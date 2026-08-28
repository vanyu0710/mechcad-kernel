"""OpenAI-compatible LLM clients with environment-only secret handling.

The module intentionally uses ``requests`` rather than a provider SDK.  Any
endpoint implementing ``POST /chat/completions`` can therefore be used without
putting provider-specific credentials in the project.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Dict, List, Optional

import requests

_logger = logging.getLogger("mech_kernel.llm")


VISION_SYSTEM_PROMPT = """You are a mechanical-engineering vision assistant.

The input can be a collage of ISO/FRONT/TOP/SIDE or section views of the same
part. Combine every view; do not treat the cells as separate parts. Return a
JSON object. State uncertainty explicitly and never invent dimensions.
"""


PLANNER_SYSTEM_PROMPT = """You are a mechanical CAD Kernel planner.

Return a JSON object with an ``ops`` array. Each item must contain an allowed
MechKernel operation name and keyword arguments. Build sketches before solid
features, close sketches before extrusion, and prefer the simplest valid model.
"""


class OpenAICompatibleClient:
    """Small client for providers exposing the OpenAI chat-completions API.

    ``api_key`` is accepted for service processes, but local users should set
    the configured environment variable instead. The key is never logged,
    serialized, or included in raised error text.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 60.0,
        api_key_env: str = "MECHKERNEL_API_KEY",
        base_url_env: str = "MECHKERNEL_BASE_URL",
        model_env: str = "MECHKERNEL_MODEL",
        provider_name: str = "OpenAI-compatible",
    ):
        self._api_key = api_key or os.environ.get(api_key_env)
        if not self._api_key:
            raise ValueError(f"API key 未配置（设环境变量 {api_key_env}）")
        self.model = model or os.environ.get(model_env)
        if not self.model:
            raise ValueError(f"模型未配置（传 model 或设环境变量 {model_env}）")
        configured_url = base_url or os.environ.get(base_url_env)
        if not configured_url:
            raise ValueError(f"API 地址未配置（传 base_url 或设环境变量 {base_url_env}）")
        self.base_url = configured_url.rstrip("/")
        self.timeout = timeout
        self.provider_name = provider_name

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(model={self.model!r}, base_url={self.base_url!r}, "
            "api_key=<redacted>)"
        )

    def chat(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: int = 2000,
        response_format: Optional[Dict[str, str]] = None,
        temperature: float = 0.0,
    ) -> Dict[str, Any]:
        """Call ``/chat/completions`` without logging request or response data."""
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if response_format:
            body["response_format"] = response_format

        _logger.info("LLM call: provider=%s model=%s max_tokens=%s", self.provider_name, self.model, max_tokens)
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=self.timeout,
        )
        if response.status_code != 200:
            # Gateways sometimes echo headers or request payloads in their
            # response body. Do not surface it because it may contain secrets.
            _logger.error("LLM request failed: provider=%s status=%s", self.provider_name, response.status_code)
            raise requests.HTTPError(f"{self.provider_name} request failed with HTTP {response.status_code}")
        data = response.json()
        try:
            return data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"{self.provider_name} returned no chat message") from exc

    def chat_json(self, messages: List[Dict[str, Any]], max_tokens: int = 2000) -> Dict[str, Any]:
        """Call the model and decode either raw JSON or a fenced JSON response."""
        content = self.chat(
            messages, max_tokens=max_tokens, response_format={"type": "json_object"},
        ).get("content", "{}")
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            if "```json" in content:
                start = content.index("```json") + len("```json")
                end = content.find("```", start)
                if end >= 0:
                    return json.loads(content[start:end].strip())
            raise


class OpenAICompatibleVisionLLM(OpenAICompatibleClient):
    """Vision adapter for an OpenAI-compatible multimodal model."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, **kwargs: Any):
        super().__init__(
            api_key=api_key or os.environ.get("MECHKERNEL_VISION_API_KEY"),
            model=model or os.environ.get("MECHKERNEL_VISION_MODEL") or os.environ.get("MECHKERNEL_MODEL"),
            model_env="MECHKERNEL_VISION_MODEL",
            api_key_env="MECHKERNEL_API_KEY",
            **kwargs,
        )

    def analyze(
        self,
        image_b64: str,
        user_prompt: str = "分析这个零件草图",
        system_prompt: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.chat_json([
            {"role": "system", "content": system_prompt or VISION_SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": "这是同一零件的多视图/截面拼图，请综合所有视角。\n" + user_prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
            ]},
        ])

    def analyze_file(self, image_path: str, user_prompt: str = "分析这个零件草图") -> Dict[str, Any]:
        with open(image_path, "rb") as image_file:
            image_b64 = base64.b64encode(image_file.read()).decode("ascii")
        return self.analyze(image_b64, user_prompt)


class OpenAICompatiblePlannerLLM(OpenAICompatibleClient):
    """Planning adapter for any OpenAI-compatible text or reasoning model."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, **kwargs: Any):
        super().__init__(
            api_key=api_key or os.environ.get("MECHKERNEL_PLANNER_API_KEY"),
            model=model or os.environ.get("MECHKERNEL_PLANNER_MODEL") or os.environ.get("MECHKERNEL_MODEL"),
            model_env="MECHKERNEL_PLANNER_MODEL",
            api_key_env="MECHKERNEL_API_KEY",
            **kwargs,
        )

    def plan(
        self,
        vision_json: Dict[str, Any],
        user_intent: str = "",
        system_prompt: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.chat_json([
            {"role": "system", "content": system_prompt or PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({
                "user_intent": user_intent,
                "vision_result": vision_json,
            }, ensure_ascii=False)},
        ])
