"""OpenAI-compatible LLM adapter and secret-handling tests."""
import os

import requests

from mech_kernel.llm import OpenAICompatibleClient, OpenAICompatiblePlannerLLM, OpenAICompatibleVisionLLM


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {"choices": [{"message": {"content": "{}"}}]}

    def json(self):
        return self._payload


def test_generic_client_reads_environment_without_exposing_key():
    names = ("MECHKERNEL_API_KEY", "MECHKERNEL_BASE_URL", "MECHKERNEL_MODEL")
    old = {name: os.environ.get(name) for name in names}
    try:
        os.environ.update({
            "MECHKERNEL_API_KEY": "private-test-key",
            "MECHKERNEL_BASE_URL": "https://example.invalid/v1/",
            "MECHKERNEL_MODEL": "test-model",
        })
        client = OpenAICompatibleClient()
        assert client.base_url == "https://example.invalid/v1"
        assert client.model == "test-model"
        assert "private-test-key" not in repr(client)
        assert not hasattr(client, "api_key")
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_generic_client_uses_openai_protocol():
    captured = {}
    original_post = requests.post
    try:
        def fake_post(url, headers, json, timeout):
            captured.update(url=url, headers=headers, body=json, timeout=timeout)
            return _Response(payload={"choices": [{"message": {"content": '{"ok": true}'}}]})

        requests.post = fake_post
        client = OpenAICompatibleClient(
            api_key="private-test-key", model="other-model", base_url="https://provider.example/v1",
        )
        assert client.chat_json([{"role": "user", "content": "hello"}]) == {"ok": True}
        assert captured["url"] == "https://provider.example/v1/chat/completions"
        assert captured["headers"]["Authorization"] == "Bearer private-test-key"
        assert captured["body"]["model"] == "other-model"
    finally:
        requests.post = original_post


def test_generic_vision_and_planner_accept_independent_models():
    vision = OpenAICompatibleVisionLLM(
        api_key="vision-key", model="vision-model", base_url="https://provider.example/v1",
    )
    planner = OpenAICompatiblePlannerLLM(
        api_key="planner-key", model="planner-model", base_url="https://provider.example/v1",
    )
    assert vision.model == "vision-model"
    assert planner.model == "planner-model"


def test_role_specific_environment_settings_override_shared_defaults():
    names = (
        "MECHKERNEL_API_KEY", "MECHKERNEL_BASE_URL", "MECHKERNEL_MODEL",
        "MECHKERNEL_PLANNER_API_KEY", "MECHKERNEL_PLANNER_MODEL",
        "MECHKERNEL_VISION_API_KEY", "MECHKERNEL_VISION_MODEL",
    )
    old = {name: os.environ.get(name) for name in names}
    try:
        os.environ.update({
            "MECHKERNEL_API_KEY": "shared-key",
            "MECHKERNEL_BASE_URL": "https://provider.example/v1",
            "MECHKERNEL_MODEL": "shared-model",
            "MECHKERNEL_PLANNER_API_KEY": "planner-key",
            "MECHKERNEL_PLANNER_MODEL": "planner-model",
            "MECHKERNEL_VISION_API_KEY": "vision-key",
            "MECHKERNEL_VISION_MODEL": "vision-model",
        })
        planner = OpenAICompatiblePlannerLLM()
        vision = OpenAICompatibleVisionLLM()
        assert planner.model == "planner-model"
        assert vision.model == "vision-model"
        assert planner._api_key == "planner-key"
        assert vision._api_key == "vision-key"
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_deepseek_compatibility_uses_dskey_without_public_secret_attribute():
    from mech_kernel.llm import DeepSeekPlannerLLM

    old = os.environ.get("DSKEY")
    try:
        os.environ["DSKEY"] = "private-deepseek-key"
        client = DeepSeekPlannerLLM()
        assert client.model == "deepseek-chat"
        assert client.base_url == "https://api.deepseek.com/v1"
        assert "private-deepseek-key" not in repr(client)
        assert not hasattr(client, "api_key")
    finally:
        if old is None:
            os.environ.pop("DSKEY", None)
        else:
            os.environ["DSKEY"] = old
