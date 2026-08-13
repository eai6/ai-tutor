# Vertex Model Garden (DeepSeek/Kimi) Eval — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add DeepSeek + Kimi (Vertex AI Model Garden MaaS) as tutor-under-test rows in the `offline_eval` cloud benchmark, scored on the existing 60-scenario harness with the Anthropic judge + student-sim.

**Architecture:** A new `VertexModelGardenClient` subclasses `OpenAIClient` (these models speak OpenAI Chat Completions over the Vertex MaaS `endpoints/openapi` endpoint). It overrides only auth (ADC / hourly OAuth token, per-region base URL) and the two generate methods — which parse from the **raw JSON body** and **retry on empty `choices`** (a verified intermittent DeepSeek failure mode). A `Provider` enum value + factory branch route to it; the benchmark harness gains a per-model region column.

**Tech Stack:** Django 5, Python 3.11, `openai==2.20.0` (base_url + `with_raw_response`), `google-auth==2.48.0` (ADC + token refresh). No new dependencies.

**Design spec:** `docs/superpowers/specs/2026-06-17-vertex-model-garden-eval-design.md` (read it; this plan implements it).

## Global Constraints

- **Benchmark-only.** No production provider selection / dashboard wiring.
- **Cross-family grader preserved.** Judge + student-sim stay Anthropic; only the tutor (`TUTOR_MODEL_OVERRIDE`) is swapped. Do not touch judge/sim config.
- **Per-model region.** Regions differ: `deepseek-v3.2-maas`→`global`, `deepseek-v3.1-maas`→`us-west2`, `deepseek-r1-0528-maas`→`us-central1`, `kimi-k2-thinking-maas`→`global`. Region is a 3rd column in `cloud_models.txt`, exported per run.
- **Raw-parse + retry.** The OpenAI SDK's parsed `ChatCompletion.choices` is intermittently `None` for DeepSeek MaaS while the raw body is valid — always parse `json.loads(raw.text)` and retry on empty `choices`.
- **Rule of Three (project convention).** The OpenAI tool-schema + `tool_choice` mapping is duplicated into the subclass (2nd use) rather than extracted — do **not** refactor `OpenAIClient` to share it.
- **Auth isolation (already set up).** ADC lives in `~/.config/gcloud-pixeldesignlabs` (`CLOUDSDK_CONFIG`); `.env` carries `CLOUDSDK_CONFIG` + `GOOGLE_CLOUD_PROJECT=ai-tutor-499714`. The client reads `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION` from the environment.
- **Test runner:** `python manage.py test apps.llm.tests -v 2` (tests subclass `django.test`; settings module `config.settings`). New tests go in the existing `apps/llm/tests.py`.
- **Migrations:** one logical change per file; latest is `0032`, so the new one is `0033`.

---

## File Structure

- `apps/llm/models.py` — add `Provider.VERTEX_MODEL_GARDEN` enum value + `_PROVIDER_API_KEY_ENV` entry. *(Task 1)*
- `apps/llm/migrations/0033_add_vertex_model_garden_provider.py` — generated choices-only `AlterField`. *(Task 1)*
- `apps/llm/client.py` — add module-level `_adapt_openai_dict()` helper *(Task 2)*; add `VertexModelGardenClient` class *(Task 3)*; add factory branch in `get_llm_client` *(Task 4)*.
- `apps/llm/tests.py` — new test classes (adapter, client, factory, resolve_runtime, enum). *(Tasks 1-4)*
- `offline_eval/cloud_models.txt` — Vertex MaaS rows with region column. *(Task 5)*
- `offline_eval/run_cloud.sh` — export `GOOGLE_CLOUD_LOCATION` per model from the region column. *(Task 5)*
- `offline_eval/_probe_cloud_models.py` — add Vertex candidates with per-candidate region. *(Task 5)*

---

### Task 1: Provider enum + resolve_runtime wiring + migration

**Files:**
- Modify: `apps/llm/models.py` (Provider enum ~line 113; `_PROVIDER_API_KEY_ENV` ~line 351)
- Create: `apps/llm/migrations/0033_add_vertex_model_garden_provider.py` (generated)
- Test: `apps/llm/tests.py`

**Interfaces:**
- Produces: `ModelConfig.Provider.VERTEX_MODEL_GARDEN == 'vertex_model_garden'`; `ModelConfig.resolve_runtime('vertex_model_garden', '<model>')` returns a non-saved `ModelConfig` with `provider='vertex_model_garden'`, `api_key_env_var='GOOGLE_CLOUD_PROJECT'`.

- [ ] **Step 1: Write the failing tests**

In `apps/llm/tests.py`, replace the stub body with:

```python
from django.test import SimpleTestCase, TestCase
from ai_tutor.apps.llm.models import ModelConfig


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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python manage.py test apps.llm.tests.VertexProviderEnumTests apps.llm.tests.VertexResolveRuntimeTests -v 2`
Expected: FAIL — `AttributeError: VERTEX_MODEL_GARDEN` / `resolve_runtime` returns `None`.

- [ ] **Step 3: Add the enum value**

In `apps/llm/models.py`, in `class Provider(models.TextChoices)`, after the `LOCAL_OLLAMA` line:

```python
        LOCAL_OLLAMA = 'local_ollama', 'Local (Ollama)'
        VERTEX_MODEL_GARDEN = 'vertex_model_garden', 'Vertex Model Garden (MaaS)'
```

- [ ] **Step 4: Add the credential-env mapping**

In `apps/llm/models.py`, in the `_PROVIDER_API_KEY_ENV` dict, add the entry:

```python
    _PROVIDER_API_KEY_ENV = {
        'anthropic': 'ANTHROPIC_API_KEY',
        'openai': 'OPENAI_API_KEY',
        'google': 'GOOGLE_API_KEY',
        'azure_openai': 'AZURE_OPENAI_API_KEY',
        # Vertex MaaS uses ADC (google.auth), not a static key — this entry only
        # lets resolve_runtime build an in-memory config; the client reads
        # GOOGLE_CLOUD_PROJECT/LOCATION + ADC at call time.
        'vertex_model_garden': 'GOOGLE_CLOUD_PROJECT',
    }
```

- [ ] **Step 5: Generate the migration**

Run: `python manage.py makemigrations llm --name add_vertex_model_garden_provider`
Expected: creates `apps/llm/migrations/0033_add_vertex_model_garden_provider.py` with an `AlterField` on `provider` (choices change only).

- [ ] **Step 6: Run tests to verify they pass**

Run: `python manage.py test apps.llm.tests.VertexProviderEnumTests apps.llm.tests.VertexResolveRuntimeTests -v 2`
Expected: PASS (2 tests).

- [ ] **Step 7: Commit**

```bash
git add apps/llm/models.py apps/llm/migrations/0033_add_vertex_model_garden_provider.py apps/llm/tests.py
git commit -m "llm: add vertex_model_garden provider enum + resolve_runtime wiring"
```

---

### Task 2: `_adapt_openai_dict` raw-JSON adapter

**Files:**
- Modify: `apps/llm/client.py` (add module-level function after `_adapt_openai_response`, ~line 244)
- Test: `apps/llm/tests.py`

**Interfaces:**
- Consumes: existing `AdaptedMessage`, `AdaptedTextBlock`, `AdaptedToolUseBlock`, `AdaptedUsage` (already in `client.py`).
- Produces: `_adapt_openai_dict(data: dict, *, model_name: str = '') -> AdaptedMessage` — builds blocks from an OpenAI-shaped response **dict** (`data['choices'][0]['message']` with `content` / `tool_calls`), ignoring `reasoning_content`. Maps `finish_reason` → `stop_reason` (`length`→`max_tokens`, `tool_calls`→`tool_use`, `content_filter`→`stop_sequence`, else `end_turn`).

- [ ] **Step 1: Write the failing tests**

Append to `apps/llm/tests.py`:

```python
from ai_tutor.apps.llm.client import _adapt_openai_dict

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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python manage.py test apps.llm.tests.AdaptOpenAIDictTests -v 2`
Expected: FAIL — `ImportError: cannot import name '_adapt_openai_dict'`.

- [ ] **Step 3: Implement the adapter**

In `apps/llm/client.py`, immediately after the `_adapt_openai_response` function (before `_extract_balanced`), add:

```python
def _adapt_openai_dict(data: dict, *, model_name: str = '') -> AdaptedMessage:
    """Adapt a RAW OpenAI-shaped response *dict* → AdaptedMessage.

    Sibling of `_adapt_openai_response` (which reads the SDK's parsed object).
    Used by `VertexModelGardenClient`, which parses `json.loads(raw.text)`
    because the SDK's parsed `ChatCompletion.choices` is intermittently None
    for DeepSeek MaaS even when the body is valid (verified 2026-06-17).
    Reads `data['choices'][0]['message']` ('content' / 'tool_calls'); ignores
    'reasoning_content' (thinking models populate it).
    """
    blocks: List[Union[AdaptedTextBlock, AdaptedToolUseBlock]] = []
    stop_reason = 'end_turn'
    choices = (data or {}).get('choices') or []
    if choices:
        choice = choices[0] or {}
        finish = choice.get('finish_reason')
        if finish == 'length':
            stop_reason = 'max_tokens'
        elif finish == 'tool_calls':
            stop_reason = 'tool_use'
        elif finish == 'content_filter':
            stop_reason = 'stop_sequence'
        msg = choice.get('message') or {}
        text = msg.get('content')  # NB: ignore reasoning_content
        if text:
            blocks.append(AdaptedTextBlock(text=text))
        for i, tc in enumerate(msg.get('tool_calls') or []):
            fn = (tc or {}).get('function') or {}
            raw_args = fn.get('arguments') or ''
            try:
                args = json.loads(raw_args) if raw_args else {}
            except (ValueError, TypeError):
                args = {}
            blocks.append(AdaptedToolUseBlock(
                id=(tc or {}).get('id') or f'vertex_tool_{i}',
                name=fn.get('name') or '',
                input=args,
            ))
    usage_obj = (data or {}).get('usage') or {}
    usage = AdaptedUsage(
        input_tokens=usage_obj.get('prompt_tokens', 0) or 0,
        output_tokens=usage_obj.get('completion_tokens', 0) or 0,
    )
    return AdaptedMessage(
        content=blocks, stop_reason=stop_reason, usage=usage,
        model=(data or {}).get('model') or model_name or '',
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python manage.py test apps.llm.tests.AdaptOpenAIDictTests -v 2`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/llm/client.py apps/llm/tests.py
git commit -m "llm: add _adapt_openai_dict raw-JSON response adapter"
```

---

### Task 3: `VertexModelGardenClient`

**Files:**
- Modify: `apps/llm/client.py` (add class after `OpenAIClient`, before `GeminiClient`)
- Test: `apps/llm/tests.py`

**Interfaces:**
- Consumes: `OpenAIClient` (parent — `_translate_messages_for_openai`, `_build_completion_kwargs`), `_adapt_openai_dict` (Task 2), `BaseLLMClient`, `LLMResponse`.
- Produces: `VertexModelGardenClient(config)` with `_base_url` (str), `client` (property → `openai.OpenAI`), `_create_raw(**kwargs) -> dict`, `_generate_impl(...) -> LLMResponse`, `generate_with_tools(...) -> AdaptedMessage`. Raises `ValueError` if `GOOGLE_CLOUD_PROJECT` unset.

- [ ] **Step 1: Write the failing tests**

Append to `apps/llm/tests.py`:

```python
import json
import os
from unittest.mock import patch, PropertyMock, MagicMock
from ai_tutor.apps.llm.client import VertexModelGardenClient, LLMResponse


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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python manage.py test apps.llm.tests.VertexClientInitTests apps.llm.tests.VertexCreateRawTests apps.llm.tests.VertexGenerateTests -v 2`
Expected: FAIL — `ImportError: cannot import name 'VertexModelGardenClient'`.

- [ ] **Step 3: Implement the client**

In `apps/llm/client.py`, after the `OpenAIClient` class (ends ~line 1346) and before `class GeminiClient`, add:

```python
class VertexModelGardenClient(OpenAIClient):
    """DeepSeek / Kimi / etc. served by Vertex AI Model Garden (MaaS) over the
    OpenAI-compatible endpoint.

    Auth is Application Default Credentials (`google.auth`) with an hourly
    OAuth token; the base URL is per-region. Overrides the two generate methods
    to parse from the RAW JSON body and RETRY on empty `choices` — the OpenAI
    SDK's parsed `ChatCompletion.choices` is intermittently None for DeepSeek
    MaaS even when the body is valid (verified 2026-06-17). Reuses the parent's
    message translation + tool-schema/tool_choice handling.
    """

    _SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
    EMPTY_MAX_RETRIES = 3
    EMPTY_RETRY_BACKOFF = [1, 2, 4]  # seconds

    def __init__(self, config: ModelConfig):
        # Skip OpenAIClient.__init__ (it builds a static-key client + raises on
        # empty key). Grandparent sets config + api_key('').
        BaseLLMClient.__init__(self, config)
        project = (os.environ.get('GOOGLE_CLOUD_PROJECT', '') or '').strip()
        if not project:
            raise ValueError(
                "GOOGLE_CLOUD_PROJECT must be set for the vertex_model_garden "
                "provider (Vertex AI Model Garden MaaS)."
            )
        self._project = project
        self._location = (
            (os.environ.get('GOOGLE_CLOUD_LOCATION', '') or '').strip()
            or 'us-central1'
        )
        import google.auth
        self._creds, _ = google.auth.default(scopes=self._SCOPES)
        host = ('aiplatform.googleapis.com' if self._location == 'global'
                else f'{self._location}-aiplatform.googleapis.com')
        self._base_url = (
            f"https://{host}/v1/projects/{self._project}"
            f"/locations/{self._location}/endpoints/openapi"
        )

    def _get_api_key(self) -> str:
        # Credential is the OAuth token (see `client`), not a static env key.
        return ''

    @property
    def client(self):
        """Fresh OpenAI client pointed at the Vertex MaaS endpoint, with a
        refreshed OAuth token (tokens expire ~hourly — refresh when invalid)."""
        import google.auth.transport.requests
        import openai
        if not self._creds.valid:
            self._creds.refresh(google.auth.transport.requests.Request())
        return openai.OpenAI(base_url=self._base_url, api_key=self._creds.token)

    def _create_raw(self, **kwargs) -> dict:
        """POST chat/completions via `with_raw_response`, return the parsed JSON
        dict. Retry when `choices` is empty/None (intermittent DeepSeek MaaS
        failure). Raise after the budget."""
        client = self.client
        for attempt in range(self.EMPTY_MAX_RETRIES + 1):
            raw = client.chat.completions.with_raw_response.create(**kwargs)
            data = json.loads(raw.text)
            if data.get('choices'):
                return data
            logger.warning(
                "[VertexMaaS] empty choices attempt=%d/%d model=%s body=%s",
                attempt + 1, self.EMPTY_MAX_RETRIES + 1,
                self.config.model_name, (raw.text or '')[:200],
            )
            if attempt < self.EMPTY_MAX_RETRIES:
                time.sleep(self.EMPTY_RETRY_BACKOFF[attempt])
        raise RuntimeError(
            "Vertex MaaS returned empty choices after "
            f"{self.EMPTY_MAX_RETRIES + 1} attempts: {self.config.model_name}"
        )

    def _generate_impl(
        self,
        messages: list[dict],
        system_prompt: str,
        max_tokens: int | None = None,
        *,
        temperature: float | None = None,
    ) -> LLMResponse:
        openai_messages = [{"role": "system", "content": system_prompt}]
        openai_messages.extend(self._translate_messages_for_openai(messages))
        completion_kwargs = self._build_completion_kwargs(
            max_tokens=max_tokens, temperature=temperature,
        )
        data = self._create_raw(
            model=self.config.model_name,
            messages=openai_messages,
            **completion_kwargs,
        )
        choice = data["choices"][0]
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}
        return LLMResponse(
            content=msg.get("content") or "",
            tokens_in=usage.get("prompt_tokens", 0) or 0,
            tokens_out=usage.get("completion_tokens", 0) or 0,
            model=data.get("model") or self.config.model_name,
            stop_reason=choice.get("finish_reason"),
        )

    def generate_with_tools(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict],
        max_tokens: int | None = None,
        *,
        tool_choice: dict | str | None = None,
    ):
        # Anthropic-style tool schema -> OpenAI function schema. Duplicated from
        # OpenAIClient per the project's Rule of Three (2nd use; don't extract).
        openai_tools = [{
            "type": "function",
            "function": {
                "name": t.get("name"),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        } for t in (tools or [])]

        openai_messages = [{"role": "system", "content": system_prompt}]
        openai_messages.extend(self._translate_messages_for_openai(messages))
        kwargs = dict(
            model=self.config.model_name,
            messages=openai_messages,
            tools=openai_tools,
        )
        if isinstance(tool_choice, dict):
            if tool_choice.get("type") == "tool" and tool_choice.get("name"):
                kwargs["tool_choice"] = {
                    "type": "function",
                    "function": {"name": tool_choice["name"]},
                }
            elif tool_choice.get("type") in ("any", "required"):
                kwargs["tool_choice"] = "required"
            elif tool_choice.get("type") == "none":
                kwargs["tool_choice"] = "none"
        elif isinstance(tool_choice, str):
            low = tool_choice.strip().lower()
            if low == "any":
                kwargs["tool_choice"] = "required"
            elif low in ("required", "none"):
                kwargs["tool_choice"] = low

        completion_kwargs = self._build_completion_kwargs(
            max_tokens=max_tokens, temperature=None,
        )
        data = self._create_raw(**kwargs, **completion_kwargs)
        return _adapt_openai_dict(data, model_name=self.config.model_name)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python manage.py test apps.llm.tests.VertexClientInitTests apps.llm.tests.VertexCreateRawTests apps.llm.tests.VertexGenerateTests -v 2`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/llm/client.py apps/llm/tests.py
git commit -m "llm: add VertexModelGardenClient (raw-JSON parse + empty-choices retry)"
```

---

### Task 4: Factory branch

**Files:**
- Modify: `apps/llm/client.py` (`get_llm_client`, ~line 1725)
- Test: `apps/llm/tests.py`

**Interfaces:**
- Consumes: `VertexModelGardenClient` (Task 3), `ModelConfig.Provider.VERTEX_MODEL_GARDEN` (Task 1).
- Produces: `get_llm_client(cfg)` returns a `VertexModelGardenClient` when `cfg.provider == 'vertex_model_garden'`.

- [ ] **Step 1: Write the failing test**

Append to `apps/llm/tests.py`:

```python
from ai_tutor.apps.llm.client import get_llm_client


class VertexFactoryTests(SimpleTestCase):
    def test_factory_returns_vertex_client(self):
        with patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "test-proj",
                                     "GOOGLE_CLOUD_LOCATION": "global"}), \
             patch("google.auth.default", return_value=(_FakeCreds(), "test-proj")):
            cfg = ModelConfig(provider="vertex_model_garden",
                              model_name="deepseek-ai/deepseek-v3.2-maas")
            client = get_llm_client(cfg)
        assert isinstance(client, VertexModelGardenClient)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python manage.py test apps.llm.tests.VertexFactoryTests -v 2`
Expected: FAIL — `ValueError: Unsupported provider: vertex_model_garden`.

- [ ] **Step 3: Add the factory branch**

In `apps/llm/client.py`, in `get_llm_client`, after the `LOCAL_OLLAMA` branch and before the final `raise ValueError`:

```python
    elif config.provider == ModelConfig.Provider.LOCAL_OLLAMA:
        return OllamaClient(config)
    elif config.provider == ModelConfig.Provider.VERTEX_MODEL_GARDEN:
        return VertexModelGardenClient(config)

    raise ValueError(f"Unsupported provider: {config.provider}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python manage.py test apps.llm.tests.VertexFactoryTests -v 2`
Expected: PASS.

- [ ] **Step 5: Run the full llm test module**

Run: `python manage.py test apps.llm.tests -v 2`
Expected: PASS (all Vertex test classes — Tasks 1-4).

- [ ] **Step 6: Commit**

```bash
git add apps/llm/client.py apps/llm/tests.py
git commit -m "llm: route vertex_model_garden provider in get_llm_client factory"
```

---

### Task 5: Benchmark harness — model rows, per-model region, probe

**Files:**
- Modify: `offline_eval/cloud_models.txt`
- Modify: `offline_eval/run_cloud.sh` (the `while read` loop)
- Modify: `offline_eval/_probe_cloud_models.py` (`CANDIDATES` + the probe loop)

**Interfaces:**
- Consumes: `TUTOR_MODEL_OVERRIDE` (splits on first `/`), `GOOGLE_CLOUD_LOCATION` (read by `VertexModelGardenClient.__init__`), `ModelConfig.resolve_runtime` (Task 1), `get_llm_client` (Task 4).
- Produces: a live, validated probe + sweep-ready rows.

- [ ] **Step 1: Add the Vertex MaaS rows to `cloud_models.txt`**

Append to `offline_eval/cloud_models.txt` (3rd column = region):

```
# --- Vertex Model Garden (MaaS) — provider/model  safe_name  region ---
vertex_model_garden/deepseek-ai/deepseek-v3.2-maas      deepseek-v3.2     global
vertex_model_garden/deepseek-ai/deepseek-v3.1-maas      deepseek-v3.1     us-west2
vertex_model_garden/moonshotai/kimi-k2-thinking-maas    kimi-k2-thinking  global
# Follow-up (reasoning model — run after the above pass cleanly):
vertex_model_garden/deepseek-ai/deepseek-r1-0528-maas   deepseek-r1       us-central1
```

- [ ] **Step 2: Wire the region column in `run_cloud.sh`**

In `offline_eval/run_cloud.sh`, change the loop header and the `run_eval` invocation. Replace:

```bash
while read -r spec safe _rest; do
  [[ -z "${spec:-}" || "$spec" == \#* ]] && continue
  if [[ -f "$RESULTS/${safe}.json" && "${FORCE:-0}" != "1" ]]; then
    echo "==================== $spec — already done, skipping ===================="
    echo; continue
  fi
  echo "==================== $spec ===================="
  start=$(date +%s)
  log="$RESULTS/${safe}.log"
  TUTOR_MODEL_OVERRIDE="$spec" "$PY" manage.py run_eval $MODE >"$log" 2>&1
```

with:

```bash
while read -r spec safe region _rest; do
  [[ -z "${spec:-}" || "$spec" == \#* ]] && continue
  if [[ -f "$RESULTS/${safe}.json" && "${FORCE:-0}" != "1" ]]; then
    echo "==================== $spec — already done, skipping ===================="
    echo; continue
  fi
  echo "==================== $spec ${region:+(region=$region)} ===================="
  start=$(date +%s)
  log="$RESULTS/${safe}.log"
  # Vertex MaaS models live in per-model regions; export GOOGLE_CLOUD_LOCATION
  # for those rows (non-override .env default applies to rows without a region).
  if [[ -n "${region:-}" ]]; then
    GOOGLE_CLOUD_LOCATION="$region" TUTOR_MODEL_OVERRIDE="$spec" "$PY" manage.py run_eval $MODE >"$log" 2>&1
  else
    TUTOR_MODEL_OVERRIDE="$spec" "$PY" manage.py run_eval $MODE >"$log" 2>&1
  fi
```

- [ ] **Step 3: Add Vertex candidates to the probe**

In `offline_eval/_probe_cloud_models.py`, append to the `CANDIDATES` list (note the optional 3rd region element):

```python
    # Vertex Model Garden MaaS — (provider, model, region). Regions differ.
    ('vertex_model_garden', 'deepseek-ai/deepseek-v3.2-maas', 'global'),
    ('vertex_model_garden', 'deepseek-ai/deepseek-v3.1-maas', 'us-west2'),
    ('vertex_model_garden', 'moonshotai/kimi-k2-thinking-maas', 'global'),
    ('vertex_model_garden', 'deepseek-ai/deepseek-r1-0528-maas', 'us-central1'),
```

Then update `main()` to set the region per candidate (it currently unpacks 2-tuples). Replace:

```python
def main():
    ok = []
    for provider, model in CANDIDATES:
        good, detail = probe(provider, model)
        print(f"  [{'OK ' if good else 'XX '}] {provider}/{model:32} {detail}")
        if good:
            ok.append(f"{provider}/{model}")
```

with:

```python
def main():
    ok = []
    for cand in CANDIDATES:
        provider, model = cand[0], cand[1]
        region = cand[2] if len(cand) > 2 else None
        if region:
            os.environ['GOOGLE_CLOUD_LOCATION'] = region
        good, detail = probe(provider, model)
        label = f"{provider}/{model}" + (f" @{region}" if region else "")
        print(f"  [{'OK ' if good else 'XX '}] {label:48} {detail}")
        if good:
            ok.append(f"{provider}/{model}")
```

Also bump the probe call's `max_tokens` from `5` to `32` (thinking models can emit reasoning before any visible content) — in `probe()`:

```python
        resp = client.generate(messages=[{'role': 'user', 'content': 'hi'}],
                               system_prompt='Reply with one word.', max_tokens=32)
```

- [ ] **Step 4: Run the probe (live verification)**

Run: `CLOUDSDK_CONFIG="$HOME/.config/gcloud-pixeldesignlabs" venv/bin/python offline_eval/_probe_cloud_models.py`
Expected: each `vertex_model_garden/...` line prints `[OK ]` with a short reply. (Cheap — a few tokens per model.) If a model shows `XX`, read the detail — usually a wrong region or not-enabled model.

- [ ] **Step 5: Commit**

```bash
git add offline_eval/cloud_models.txt offline_eval/run_cloud.sh offline_eval/_probe_cloud_models.py
git commit -m "offline-eval: add Vertex MaaS rows + per-model region + probe candidates"
```

---

### Task 6: Smoke sweep + leaderboard (live verification, small spend)

**Files:** none (run-only). This is the verification ladder step 3-5 from the spec.

**Interfaces:** consumes everything above.

- [ ] **Step 1: Single-model smoke via a curated models file**

```bash
cd /home/daniel/Documents/work/Nyansapo/web/ai-tutor
printf 'vertex_model_garden/deepseek-ai/deepseek-v3.2-maas  deepseek-v3.2  global\n' > offline_eval/_smoke.txt
CLOUDSDK_CONFIG="$HOME/.config/gcloud-pixeldesignlabs" \
  CLOUD_MODELS_FILE=offline_eval/_smoke.txt bash offline_eval/run_cloud.sh
```
Expected: `>> saved results/deepseek-v3.2.json … Result: …` printed; `offline_eval/results/deepseek-v3.2.json` exists; the `.log` shows tool calls landing (not reasoning-only/empty turns) and a non-zero pass count. Watch for `[VertexMaaS] empty choices` warnings — occasional is fine (retry handles it); persistent means investigate.

- [ ] **Step 2: Inspect the smoke result**

Run: `CLOUDSDK_CONFIG="$HOME/.config/gcloud-pixeldesignlabs" venv/bin/python offline_eval/aggregate.py`
Expected: `deepseek-v3.2` appears on the leaderboard with a pass rate + rubric, 0 harness errors. If errors > 0, read `offline_eval/results/deepseek-v3.2.log`.

- [ ] **Step 3: Full sweep (instruct/non-thinking first, then R1)**

```bash
rm -f offline_eval/_smoke.txt
CLOUDSDK_CONFIG="$HOME/.config/gcloud-pixeldesignlabs" bash offline_eval/run_cloud.sh
```
Expected: one `results/<safe>.json` per Vertex row (existing Claude/Gemini rows skip if already present). Leaderboard prints at the end.

- [ ] **Step 4: Fold results into FINDINGS**

Add the new DeepSeek/Kimi rows to the proprietary-ceiling table in `offline_eval/FINDINGS_offline_model_eval.md`, noting thinking vs non-thinking and any empty-choices/truncation observations. Commit:

```bash
git add offline_eval/FINDINGS_offline_model_eval.md offline_eval/results/deepseek-v3.2.json offline_eval/results/deepseek-v3.1.json offline_eval/results/kimi-k2-thinking.json
git commit -m "offline-eval: Vertex MaaS (DeepSeek/Kimi) benchmark results"
```
(Result JSONs are force-added past the `results/` gitignore, per the offline_eval convention — use `git add -f` if needed.)

---

## Self-Review

**Spec coverage:**
- VertexModelGardenClient (subclass, auth, base_url, override generate methods, raw-parse+retry, ignore reasoning_content) → Task 3. ✅
- `_adapt_openai_dict` → Task 2. ✅
- Provider enum + `_PROVIDER_API_KEY_ENV` + migration → Task 1. ✅
- Factory branch → Task 4. ✅
- Per-model region (cloud_models.txt column + run_cloud.sh export + probe) → Task 5. ✅
- Verification ladder (probe → smoke → sweep → FINDINGS) → Tasks 5-6. ✅
- Cross-family grader untouched (only `TUTOR_MODEL_OVERRIDE` swapped) → no judge/sim changes in any task. ✅

**Placeholder scan:** No TBD/TODO; every code step has complete code; commands have expected output. ✅

**Type consistency:** `_adapt_openai_dict(data, *, model_name)` defined in Task 2, used in Task 3. `_create_raw(**kwargs) -> dict` defined + used in Task 3. `client` property used by `_create_raw`. `VertexModelGardenClient` defined in Task 3, referenced by factory (Task 4) and tests. `_FakeCreds` / `_make_vertex_client` / `_raw` defined in Task 3's test block, reused in Task 4's test. Enum `VERTEX_MODEL_GARDEN` defined Task 1, used Tasks 3-4. ✅

**Note on test ordering:** Task 4's test reuses helpers (`_FakeCreds`, `_make_vertex_client`) defined in Task 3's appended test block — tasks are appended in order to the same `apps/llm/tests.py`, so run them in sequence.
