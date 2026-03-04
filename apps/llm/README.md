# llm

LLM provider abstraction layer and customizable prompt management.

This app decouples the rest of the system from specific LLM providers, allowing institutions to configure different models for different purposes and customize the tutor's behavior through prompt packs.

---

## Models

### PromptPack

A collection of prompts that define the tutor's behavior. Institutions can create multiple packs and switch between them.

| Field | Type | Description |
|-------|------|-------------|
| `institution` | ForeignKey(Institution, nullable) | `null` = platform-wide prompts |
| `name` | CharField(100) | e.g., "Friendly K-5 Tutor" |
| `system_prompt` | TextField | Core persona and role definition |
| `teaching_style_prompt` | TextField | Methodology (Socratic, direct instruction, etc.) |
| `safety_prompt` | TextField | Content restrictions and safety guidelines |
| `format_rules_prompt` | TextField | Output formatting rules (length, structure) |
| `version` | PositiveIntegerField | Version tracking |
| `is_active` | BooleanField | Only active packs are used |

#### Extended Prompt Fields

These override the built-in defaults when non-empty:

| Field | Description |
|-------|-------------|
| `tutor_system_prompt` | Full tutor system prompt. Supports `{institution_name}`, `{locale_context}`, `{tutor_name}`, `{language}`, `{grade_level}`, `{safety_prompt}` placeholders. |
| `content_generation_prompt` | System prompt for lesson content generation |
| `exit_ticket_prompt` | System prompt for exit ticket generation |
| `grading_prompt` | System prompt for answer grading |
| `image_generation_prompt` | Prefix/context for image generation prompts |

**`get_full_system_prompt()`** -- Assembles all components:
```
system_prompt + teaching_style_prompt + safety_prompt + format_rules_prompt
```

### ModelConfig

LLM provider configuration. Supports multiple providers and per-purpose model selection.

| Field | Type | Description |
|-------|------|-------------|
| `institution` | ForeignKey(Institution) | Scoped to school |
| `name` | CharField(100) | e.g., "Default Claude" |
| `provider` | CharField | `anthropic`, `openai`, `google`, `azure_openai`, `local_ollama` |
| `model_name` | CharField(100) | e.g., `claude-haiku-4-5-20251001`, `gpt-4o` |
| `purpose` | CharField | `generation`, `tutoring`, `exit_tickets`, `skill_extraction`, `image_generation` |
| `api_base` | URLField (nullable) | Custom endpoint (for Azure, Ollama) |
| `api_key_env_var` | CharField(100) | Environment variable name (default: `ANTHROPIC_API_KEY`) |
| `api_key_encrypted` | TextField | Fernet-encrypted API key (overrides env var when set) |
| `max_tokens` | PositiveIntegerField | Default: 1024 |
| `temperature` | FloatField | Default: 0.7 |
| `is_active` | BooleanField | Only active configs are used |

**API Key Resolution:**
1. Try decrypting `api_key_encrypted` (Fernet, key derived from Django `SECRET_KEY` via SHA-256)
2. Fall back to environment variable named in `api_key_env_var`

**Model Selection:**
```python
config = ModelConfig.get_for('tutoring')  # Returns active config for purpose
# Falls back to any active config if no purpose-specific one exists
```

---

## LLM Client Abstraction

### `client.py`

Provider-agnostic interface for LLM interactions.

#### LLMResponse

```python
@dataclass
class LLMResponse:
    content: str              # Generated text
    tokens_in: int            # Input tokens consumed
    tokens_out: int           # Output tokens generated
    model: str                # Model identifier
    stop_reason: Optional[str]  # Why generation stopped
```

#### BaseLLMClient (ABC)

```python
class BaseLLMClient:
    def generate(messages, system_prompt, max_tokens) -> LLMResponse
    def generate_stream(messages, system_prompt) -> Generator[str]  # yields chunks
```

#### Provider Implementations

| Client | Provider | Key Details |
|--------|----------|-------------|
| `AnthropicClient` | Anthropic Claude | Retry logic (4 retries, exponential backoff 15-120s). Uses streaming internally to avoid 10-min timeout. Max tokens clamped to model limits (Haiku/Sonnet: 64K, Opus: 32K). |
| `OpenAIClient` | OpenAI GPT | Standard `chat.completions.create()` with system message. |
| `GeminiClient` | Google Gemini | Maps chat roles to Gemini Content objects. Enables Google Search grounding via `types.GoogleSearch()`. Streaming with `usage_metadata` extraction. |
| `OllamaClient` | Local Ollama | REST API at configurable `api_base` (default: `http://localhost:11434`). No API key required. Graceful `ConnectionError`/`TimeoutError` handling. |
| `MockLLMClient` | Testing | Echoes back a mock response based on last message content. |

#### Factory

```python
client = get_llm_client(config: ModelConfig, use_mock: bool = False) -> BaseLLMClient
```

Dispatches to the correct client class based on `config.provider`.

---

## Prompt Assembly

### `prompts.py`

#### `get_active_prompt_pack(institution_id)`

Resolution order:
1. Active `PromptPack` for the specific institution
2. Active platform-wide `PromptPack` (institution=null)
3. `None` (use built-in defaults)

#### `get_prompt_or_default(institution_id, field_name, default, json_required)`

Resolves a specific prompt field:
1. Check active `PromptPack` for the field value
2. If empty, use the `default` parameter
3. If `json_required=True`, append "Return ONLY valid JSON" instruction

#### Built-in Defaults

| Prompt | Description |
|--------|-------------|
| `DEFAULT_SAFETY_PROMPT` | Content restrictions, age-appropriate language, PII prevention |
| `DEFAULT_CONTENT_GENERATION_PROMPT` | Lesson step generation instructions |
| `DEFAULT_EXIT_TICKET_PROMPT` | MCQ generation instructions |
| `DEFAULT_GRADING_PROMPT` | Answer evaluation criteria |
| `DEFAULT_IMAGE_GENERATION_PROMPT` | Educational diagram style guidelines |

#### `assemble_system_prompt(prompt_pack, lesson)`

Combines all prompt components with lesson context:
1. Base system prompt (persona)
2. Teaching style
3. Safety guidelines
4. Format rules
5. Lesson context (title, objective, unit, course)
6. Available media catalog with `[SHOW_MEDIA:title]` marker examples

#### `build_step_instruction(step, attempt_number, previous_answer, hint_level)`

Generates the step-specific instruction injected into the conversation:
- Step type instructions (TEACH/WORKED_EXAMPLE/PRACTICE/QUIZ/SUMMARY)
- Teacher script ("SAY THIS")
- Question + answer choices (if applicable)
- Retry context + progressive hint ladder (if attempt > 1)
- Correct answer + grading rubric (for tutor reference, not shown to student)

#### `build_tutor_message(prompt_pack, lesson, step, conversation_history, ...)`

Returns `(system_prompt, messages)` tuple ready for LLM call. Injects step instruction as a `[STEP CONTEXT]...[/STEP CONTEXT]` user message.

#### `get_lesson_media(lesson)`

Returns a list of media assets attached to the lesson's steps:
```python
[{"title": "...", "type": "diagram", "url": "/media/...", "caption": "...", "alt_text": "..."}]
```

---

## Purpose-Based Model Selection

Different tasks can use different models optimized for their needs:

| Purpose | Recommended Model | Rationale |
|---------|-------------------|-----------|
| `tutoring` | Claude Haiku | Fast, low-cost for real-time conversation |
| `generation` | Claude Sonnet | Higher quality for curriculum content |
| `exit_tickets` | Claude Haiku | Structured MCQ generation (speed over quality) |
| `skill_extraction` | Claude Haiku | Structured analysis of lesson content |
| `image_generation` | Gemini Flash | Native image generation capability |

---

## Architecture Decisions

- **Env var reference, not raw keys** -- `ModelConfig` stores the name of an environment variable (e.g., `ANTHROPIC_API_KEY`), not the key itself. For cases where env vars aren't available, keys can be encrypted via Fernet.
- **Fernet encryption derived from SECRET_KEY** -- API keys stored in DB are encrypted using a key derived from Django's `SECRET_KEY` via SHA-256. No separate encryption key to manage.
- **Per-purpose model selection** -- Allows optimizing cost/quality trade-offs per task type (e.g., cheap Haiku for tutoring, capable Sonnet for generation).
- **Prompt layering** -- System prompts are assembled from composable components (persona + teaching style + safety + format), allowing institutions to customize individual layers.
- **Streaming by default for Anthropic** -- Even `generate()` uses streaming internally to avoid Anthropic's 10-minute server-side timeout on long-running requests.
