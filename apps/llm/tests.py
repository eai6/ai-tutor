from django.test import SimpleTestCase, TestCase
from apps.llm.models import ModelConfig
from apps.llm.client import _adapt_openai_dict

# Real shapes captured from the live Vertex MaaS endpoint (2026-06-17).
DEEPSEEK_TEXT = {
    "choices": [{"finish_reason": "stop", "index": 0, "matched_stop": 1,
                 "message": {"content": "OK", "reasoning_content": None,
                             "role": "assistant", "tool_calls": None}}],
    "model": "deepseek-ai/deepseek-v3.2-maas",
    "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11},
}
DEEPSEEK_TOOL = {
    "choices": [{"finish_reason": "tool_calls", "index": 0,
                 "message": {"content": None, "role": "assistant", "tool_calls": [
                     {"function": {"arguments": "{\"question\": \"What is 3/4 + 2/3?\"}",
                                   "name": "pose_question"},
                      "id": "call_abc", "index": 0, "type": "function"}]}}],
    "model": "deepseek-ai/deepseek-v3.2-maas",
    "usage": {"prompt_tokens": 306, "completion_tokens": 34},
}
KIMI_TOOL_WITH_REASONING = {
    "choices": [{"finish_reason": "tool_calls", "index": 0,
                 "message": {"content": "", "reasoning_content": "let me think...",
                             "role": "assistant", "tool_calls": [
                     {"function": {"arguments": "{\"question\": \"Name a fraction equal to 1/2.\"}",
                                   "name": "pose_question"},
                      "id": "call_xyz", "index": 0, "type": "function"}]}}],
    "model": "moonshotai/kimi-k2-thinking-maas",
    "usage": {"prompt_tokens": 69, "completion_tokens": 309},
}


class VertexProviderEnumTests(SimpleTestCase):
    def test_enum_value_present(self):
        assert ModelConfig.Provider.VERTEX_MODEL_GARDEN == "vertex_model_garden"
        assert "vertex_model_garden" in ModelConfig.Provider.values


class VertexResolveRuntimeTests(TestCase):
    def test_resolve_runtime_builds_in_memory_config(self):
        cfg = ModelConfig.resolve_runtime(
            "vertex_model_garden", "deepseek-ai/deepseek-v3.2-maas"
        )
        assert cfg is not None
        assert cfg.provider == "vertex_model_garden"
        assert cfg.model_name == "deepseek-ai/deepseek-v3.2-maas"
        assert cfg.api_key_env_var == "GOOGLE_CLOUD_PROJECT"
        assert cfg.pk is None  # never persisted


class AdaptOpenAIDictTests(SimpleTestCase):
    def test_text_response(self):
        msg = _adapt_openai_dict(DEEPSEEK_TEXT, model_name="x")
        assert msg.stop_reason == "end_turn"
        assert [b.type for b in msg.content] == ["text"]
        assert msg.content[0].text == "OK"
        assert msg.usage.input_tokens == 9
        assert msg.usage.output_tokens == 2

    def test_tool_call_response(self):
        msg = _adapt_openai_dict(DEEPSEEK_TOOL)
        assert msg.stop_reason == "tool_use"
        tool_blocks = [b for b in msg.content if b.type == "tool_use"]
        assert len(tool_blocks) == 1
        assert tool_blocks[0].name == "pose_question"
        assert tool_blocks[0].input == {"question": "What is 3/4 + 2/3?"}

    def test_reasoning_content_is_ignored(self):
        msg = _adapt_openai_dict(KIMI_TOOL_WITH_REASONING)
        # reasoning text must NOT appear as a content block
        texts = [b.text for b in msg.content if b.type == "text"]
        assert "let me think..." not in "".join(texts)
        assert any(b.type == "tool_use" and b.name == "pose_question"
                   for b in msg.content)

    def test_empty_choices(self):
        msg = _adapt_openai_dict({"choices": None})
        assert msg.content == []
        assert msg.stop_reason == "end_turn"


import json
import os
from unittest.mock import patch, PropertyMock, MagicMock
from apps.llm.client import VertexModelGardenClient, LLMResponse


class _FakeCreds:
    valid = True
    token = "fake-token"
    def refresh(self, request):  # noqa: D401
        self.token = "refreshed"


def _make_vertex_client(location="global", project="test-proj"):
    """Build a VertexModelGardenClient without real ADC."""
    env = {"GOOGLE_CLOUD_PROJECT": project, "GOOGLE_CLOUD_LOCATION": location}
    with patch.dict(os.environ, env), \
         patch("google.auth.default", return_value=(_FakeCreds(), project)):
        cfg = ModelConfig(provider="vertex_model_garden",
                          model_name="deepseek-ai/deepseek-v3.2-maas",
                          max_tokens=256, temperature=0.2, purpose="tutoring")
        return VertexModelGardenClient(cfg)


class VertexClientInitTests(SimpleTestCase):
    def test_base_url_regional(self):
        c = _make_vertex_client(location="us-west2")
        assert c._base_url == (
            "https://us-west2-aiplatform.googleapis.com/v1/projects/test-proj"
            "/locations/us-west2/endpoints/openapi")

    def test_base_url_global(self):
        c = _make_vertex_client(location="global")
        assert c._base_url == (
            "https://aiplatform.googleapis.com/v1/projects/test-proj"
            "/locations/global/endpoints/openapi")

    def test_missing_project_raises(self):
        with patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": ""}), \
             patch("google.auth.default", return_value=(_FakeCreds(), None)):
            cfg = ModelConfig(provider="vertex_model_garden",
                              model_name="deepseek-ai/deepseek-v3.2-maas")
            with self.assertRaises(ValueError):
                VertexModelGardenClient(cfg)


def _raw(text):
    m = MagicMock()
    m.text = text
    return m


class VertexCreateRawTests(SimpleTestCase):
    def test_retries_on_empty_choices_then_succeeds(self):
        c = _make_vertex_client()
        fake = MagicMock()
        fake.chat.completions.with_raw_response.create.side_effect = [
            _raw('{"choices": null}'),
            _raw('{"choices": []}'),
            _raw('{"choices": [{"finish_reason": "stop", "message": '
                 '{"content": "hi"}}], "usage": {"prompt_tokens": 1, '
                 '"completion_tokens": 1}}'),
        ]
        with patch.object(VertexModelGardenClient, "client",
                          new_callable=PropertyMock, return_value=fake), \
             patch("time.sleep"):
            data = c._create_raw(model="m", messages=[])
        assert data["choices"][0]["message"]["content"] == "hi"
        assert fake.chat.completions.with_raw_response.create.call_count == 3

    def test_raises_after_retry_budget(self):
        c = _make_vertex_client()
        fake = MagicMock()
        fake.chat.completions.with_raw_response.create.return_value = _raw('{"choices": []}')
        with patch.object(VertexModelGardenClient, "client",
                          new_callable=PropertyMock, return_value=fake), \
             patch("time.sleep"):
            with self.assertRaises(RuntimeError):
                c._create_raw(model="m", messages=[])


class VertexGenerateTests(SimpleTestCase):
    def test_generate_with_tools_returns_adapted_tool_call(self):
        c = _make_vertex_client()
        body = ('{"choices": [{"finish_reason": "tool_calls", "message": '
                '{"content": null, "tool_calls": [{"id": "call_1", "type": '
                '"function", "function": {"name": "pose_question", "arguments": '
                '"{\\"question\\": \\"Q?\\"}"}}]}}], "usage": {"prompt_tokens": '
                '5, "completion_tokens": 3}, "model": "deepseek-ai/deepseek-v3.2-maas"}')
        fake = MagicMock()
        fake.chat.completions.with_raw_response.create.return_value = _raw(body)
        tools = [{"name": "pose_question", "description": "ask",
                  "input_schema": {"type": "object",
                                   "properties": {"question": {"type": "string"}}}}]
        with patch.object(VertexModelGardenClient, "client",
                          new_callable=PropertyMock, return_value=fake):
            msg = c.generate_with_tools([{"role": "user", "content": "go"}],
                                        "sys", tools, tool_choice="any")
        tool_blocks = [b for b in msg.content if b.type == "tool_use"]
        assert tool_blocks and tool_blocks[0].name == "pose_question"
        assert tool_blocks[0].input == {"question": "Q?"}
        # tool_choice "any" → OpenAI "required"
        _, kwargs = fake.chat.completions.with_raw_response.create.call_args
        assert kwargs["tool_choice"] == "required"

    def test_generate_impl_returns_llm_response(self):
        c = _make_vertex_client()
        body = ('{"choices": [{"finish_reason": "stop", "message": '
                '{"content": "answer"}}], "usage": {"prompt_tokens": 7, '
                '"completion_tokens": 4}, "model": "deepseek-ai/deepseek-v3.2-maas"}')
        fake = MagicMock()
        fake.chat.completions.with_raw_response.create.return_value = _raw(body)
        with patch.object(VertexModelGardenClient, "client",
                          new_callable=PropertyMock, return_value=fake):
            resp = c._generate_impl([{"role": "user", "content": "q"}], "sys",
                                    max_tokens=50, temperature=0.2)
        assert isinstance(resp, LLMResponse)
        assert resp.content == "answer"
        assert resp.tokens_in == 7 and resp.tokens_out == 4
        assert resp.stop_reason == "stop"


from apps.llm.client import get_llm_client


class VertexFactoryTests(SimpleTestCase):
    def test_factory_returns_vertex_client(self):
        with patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "test-proj",
                                     "GOOGLE_CLOUD_LOCATION": "global"}), \
             patch("google.auth.default", return_value=(_FakeCreds(), "test-proj")):
            cfg = ModelConfig(provider="vertex_model_garden",
                              model_name="deepseek-ai/deepseek-v3.2-maas")
            client = get_llm_client(cfg)
        assert isinstance(client, VertexModelGardenClient)
