"""
LLM app - PromptPack and ModelConfig models.

These models let institutions customize:
1. How the AI tutor behaves (prompts, persona, safety rules)
2. Which LLM provider/model to use
"""

import base64
import hashlib
import os

from cryptography.fernet import Fernet
from django.conf import settings
from django.db import models

from apps.accounts.models import Institution


class PromptPack(models.Model):
    """
    A collection of prompts that define the tutor's behavior.
    
    Institutions can create multiple packs (e.g., "Friendly Grade 2", 
    "Formal High School") and switch between them.
    
    The prompts are assembled in layers:
    - system_prompt: Core persona and boundaries
    - teaching_style_prompt: How to teach (Socratic, direct, etc.)
    - safety_prompt: Content restrictions
    - format_rules_prompt: Output formatting rules
    """
    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name='prompt_packs',
        null=True,
        blank=True,
        help_text="Null = platform-wide prompts"
    )
    name = models.CharField(max_length=100, help_text="e.g., 'Friendly K-5 Tutor'")
    
    # Prompt components - assembled into final system prompt
    system_prompt = models.TextField(
        help_text="Core persona and role definition"
    )
    teaching_style_prompt = models.TextField(
        blank=True,
        help_text="Teaching methodology (Socratic, direct instruction, etc.)"
    )
    safety_prompt = models.TextField(
        blank=True,
        help_text="Content restrictions and safety guidelines"
    )
    format_rules_prompt = models.TextField(
        blank=True,
        help_text="Output formatting rules (length, structure, etc.)"
    )

    # Extended prompts — empty means use built-in default
    tutor_system_prompt = models.TextField(
        blank=True, default='',
        help_text="Full tutor system prompt override. Supports {institution_name}, {locale_context}, {tutor_name}, {language}, {grade_level}, {safety_prompt} placeholders."
    )
    content_generation_prompt = models.TextField(
        blank=True, default='',
        help_text="System prompt for lesson content generation."
    )
    exit_ticket_prompt = models.TextField(
        blank=True, default='',
        help_text="System prompt for exit ticket generation."
    )
    grading_prompt = models.TextField(
        blank=True, default='',
        help_text="System prompt for answer grading."
    )
    image_generation_prompt = models.TextField(
        blank=True, default='',
        help_text="Prefix/context for image generation prompts (style, safety, educational context)."
    )

    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_active', '-updated_at']

    def __str__(self):
        scope = self.institution.slug if self.institution else 'platform'
        return f"{self.name} v{self.version} ({scope})"

    def get_full_system_prompt(self):
        """Assemble all prompt components into the final system prompt."""
        parts = [self.system_prompt]
        if self.teaching_style_prompt:
            parts.append(self.teaching_style_prompt)
        if self.safety_prompt:
            parts.append(self.safety_prompt)
        if self.format_rules_prompt:
            parts.append(self.format_rules_prompt)
        return "\n\n".join(parts)


class ModelConfig(models.Model):
    """
    Configuration for which LLM to use and how to call it.

    Design decision: API keys are stored as references (env var names),
    not actual secrets. The runtime looks up the actual key from env.
    """
    class Provider(models.TextChoices):
        ANTHROPIC = 'anthropic', 'Anthropic (Claude)'
        OPENAI = 'openai', 'OpenAI (GPT)'
        GOOGLE = 'google', 'Google (Gemini)'
        AZURE_OPENAI = 'azure_openai', 'Azure OpenAI'
        LOCAL_OLLAMA = 'local_ollama', 'Local (Ollama)'

    class Purpose(models.TextChoices):
        GENERATION = 'generation', 'Content Generation (Curriculum, Lessons)'
        TUTORING = 'tutoring', 'Student Tutoring'
        EXIT_TICKETS = 'exit_tickets', 'Exit Ticket Generation'
        SKILL_EXTRACTION = 'skill_extraction', 'Skill Extraction'
        IMAGE_GENERATION = 'image_generation', 'Image Generation'
        HELP_ASSISTANT = 'help_assistant', 'In-app Help Assistant'
        # Post-response sanity checker (combined_judge: arithmetic +
        # factual + rule_compliance). Lower-cost / faster model than
        # tutoring on purpose — judge calls run after every tutor turn,
        # so a 5x cheaper / 2x faster Sonnet model dramatically cuts
        # per-turn latency without compromising rule enforcement.
        JUDGE = 'judge', 'Post-response Judge'
        # Judge fallback tiers — get_judge_provider_chain walks
        # (judge, judge_fallback, judge_fallback_2, generation, tutoring,
        # exit_tickets) and picks DISTINCT providers. Tier 2 = cross-vendor
        # primary fallback (typically OpenAI). Tier 3 = last-resort
        # (typically Anthropic Haiku 4.5). See
        # apps/curriculum/content_judges/_providers.py.
        JUDGE_FALLBACK = 'judge_fallback', 'Post-response Judge — Tier 2 Fallback'
        JUDGE_FALLBACK_2 = 'judge_fallback_2', 'Post-response Judge — Tier 3 Fallback'
        # Regen ensemble — when the validator decides a tutor turn
        # needs to be rewritten, the engine fans out to N concurrent
        # `REGEN` configs (configurable: 1 / 2 / 3 models). Judges
        # then score each candidate and the engine picks the best
        # clean one. See apps/tutoring/regen/.
        REGEN = 'regen', 'Tutor Response Regeneration'
        # Synthetic student — drives end-to-end tutoring sessions for
        # the simulator (apps/tutoring/student_sim/). Uses a cheap,
        # fast model with higher temperature than tutoring (~0.7) to
        # produce believable persona variation. See
        # memory/llm_student_simulator_plan.md.
        STUDENT_SIM = 'student_sim', 'Synthetic Student (Simulator)'
        # Content quality judges — review GENERATED content (lesson
        # steps, exit-ticket items, image prompts, generated images)
        # rather than live tutor turns. Defaults to Gemini per the
        # locked plan in memory/content_quality_review_plan.md, with
        # cross-provider fallback through apps/curriculum/content_judges/
        # _providers.py. effective_temperature is forced to 0 (same as
        # JUDGE) for verdict consistency.
        CONTENT_JUDGE_IMAGE_PROMPT = 'content_judge_image_prompt', 'Content Judge — Image Prompt (PRE-gen)'
        CONTENT_JUDGE_FACTUAL_STEP = 'content_judge_factual_step', 'Content Judge — Factual (Lesson Step)'
        CONTENT_JUDGE_FIGURE_ALIGNMENT = 'content_judge_figure_alignment', 'Content Judge — Figure Alignment (POST-gen vision)'
        CONTENT_JUDGE_EXIT_QUESTION = 'content_judge_exit_question', 'Content Judge — Exit Ticket Question (MCQ)'
        CONTENT_JUDGE_PEDAGOGY_STEP = 'content_judge_pedagogy_step', 'Content Judge — Pedagogical Soundness (Lesson Step)'
        CONTENT_JUDGE_SAFETY_CONTENT = 'content_judge_safety_content', 'Content Judge — Safety + Cultural Fit (Content)'

        # v2 engine purposes (refactor-implementation-plan.md Phase 1
        # §7). Six new purposes for the grader / tutor / conformance /
        # profiler decomposition. GRADER_GROUNDED + TUTOR_CLAIM_ADJUDICATOR
        # MUST be Gemini (Google-grounding is provider-required). The
        # other four are provider-tuned during Phase 2 against the
        # benchmark (§7 sub-decision).
        GRADER_MATH = 'grader_math', 'v2 Grader — Math Path'
        GRADER_GROUNDED = 'grader_grounded', 'v2 Grader — Grounded (KB + Google) — Gemini-pinned'
        TUTOR_MOVE = 'tutor_move', 'v2 StudentTutor — Per-Move Response'
        CONFORMANCE_CLASSIFIER = 'conformance_classifier', 'v2 Conformance Classifier'
        TUTOR_CLAIM_ADJUDICATOR = 'tutor_claim_adjudicator', 'v2 Tutor-Claim Adjudicator — Gemini-pinned'
        PROFILER_SUMMARY = 'profiler_summary', 'v2 StudentProfiler — End-of-Session Summary'
        # Phase 4 — unverified-trap + open-question-pivot redesign
        # (memory/v2_unverified_trap_redesign.md).
        # GRADER_VERIFIER:    Haiku-backed "did the student claim the
        #                     same answer as the canonical?" check that
        #                     fires AFTER the grounded adjudicator when
        #                     confidence is low. Replaces the blanket
        #                     confidence-threshold downgrade.
        # QUESTION_EXTRACTOR: Haiku-backed post-render extractor that
        #                     counts action-prompts in a tutor turn so
        #                     conformance can reject stacked questions
        #                     and bind open_question to the actual
        #                     rendered question.
        GRADER_VERIFIER = 'grader_verifier', 'v2 Grader — Answer-Consistency Verifier'
        QUESTION_EXTRACTOR = 'question_extractor', 'v2 Post-render Question Extractor'

    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name='model_configs'
    )
    name = models.CharField(max_length=100, help_text="e.g., 'Default Claude'")
    
    provider = models.CharField(
        max_length=20,
        choices=Provider.choices,
        default=Provider.ANTHROPIC
    )
    model_name = models.CharField(
        max_length=100,
        default='claude-haiku-4-5-20251001',
        help_text="Model identifier (e.g., 'claude-haiku-4-5-20251001', 'claude-sonnet-4-20250514', 'gpt-4o')"
    )
    
    # Connection settings
    api_base = models.URLField(
        blank=True,
        null=True,
        help_text="Custom API endpoint (for Azure, Ollama, etc.)"
    )
    api_key_env_var = models.CharField(
        max_length=100,
        default='ANTHROPIC_API_KEY',
        help_text="Environment variable name containing the API key"
    )
    api_key_encrypted = models.TextField(
        blank=True,
        default='',
        help_text="Fernet-encrypted API key (overrides env var when set)"
    )

    # Generation parameters
    max_tokens = models.PositiveIntegerField(default=1024)
    temperature = models.FloatField(default=0.7)

    # Purpose-based temperature constraints — see effective_temperature
    # property. JUDGE is forced to 0 for evaluation consistency; TUTORING
    # is clamped to [0.1, 0.3] for controlled variability. Stored value
    # may differ; runtime always uses effective_temperature.
    # Lowered MIN from 0.1 → 0.0 on 2026-05-17 — pilot directive: try
    # temperature=0 on the tutor to improve tool-use instruction
    # following (LLM was emitting <tool_use> XML as prose under default
    # temp). Stored values still respected; the floor just allows 0.0
    # through.
    TUTORING_TEMP_MIN = 0.0
    TUTORING_TEMP_MAX = 0.3
    JUDGE_TEMP = 0.0

    purpose = models.CharField(
        max_length=40,
        choices=Purpose.choices,
        default=Purpose.GENERATION,
    )

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_active', '-updated_at']
        verbose_name = "Model Configuration"

    def __str__(self):
        scope = self.institution.slug if self.institution else 'platform'
        return f"{self.name} - {self.model_name} ({scope})"

    @property
    def effective_temperature(self) -> float:
        """Temperature to send to the API, applying purpose-based constraints.

        Hard runtime invariants (see CLAUDE.md, agentic_platform_architecture_plan.md):

        - **JUDGE**: always 0 — high consistency for evaluation. Even if the
          stored ``temperature`` field is non-zero, the runtime clamps it.
        - **TUTORING**: clamped to [0.1, 0.3] — controlled variability.
        - **Other purposes** (GENERATION, REGEN, EXIT_TICKETS, etc.): uses
          the raw stored value. REGEN is dynamically overridden per-cycle
          by ``apps.tutoring.regen`` via the explicit ``temperature``
          kwarg on ``BaseLLMClient.generate()``.

        Explicit ``temperature`` kwargs passed to ``generate()`` bypass
        this property entirely — that's the regen path.
        """
        purpose = (self.purpose or '').lower()
        temp = self.temperature if self.temperature is not None else 1.0
        if purpose == self.Purpose.JUDGE.value:
            return self.JUDGE_TEMP
        # Content judges (PRE/POST-gen reviewers in apps/curriculum/
        # content_judges/) follow the same verdict-consistency invariant
        # as the live JUDGE — temp must be 0. Match by purpose-prefix so
        # future content_judge_* purposes inherit the rule automatically.
        if purpose.startswith('content_judge'):
            return self.JUDGE_TEMP
        # v2 engine — JUDGE-class verification consistency (Phase 1 §7).
        # GRADER_MATH / GRADER_GROUNDED / CONFORMANCE_CLASSIFIER /
        # TUTOR_CLAIM_ADJUDICATOR all want deterministic verdicts.
        # PROFILER_SUMMARY is also 0 (memory artifact, not creative).
        if purpose in (
            self.Purpose.GRADER_MATH.value,
            self.Purpose.GRADER_GROUNDED.value,
            self.Purpose.CONFORMANCE_CLASSIFIER.value,
            self.Purpose.TUTOR_CLAIM_ADJUDICATOR.value,
            self.Purpose.PROFILER_SUMMARY.value,
            # Phase 4 — both new verifier/extractor steps are
            # adjudicator-class: structured-JSON output, determinism
            # required for replay + observability.
            self.Purpose.GRADER_VERIFIER.value,
            self.Purpose.QUESTION_EXTRACTOR.value,
        ):
            return self.JUDGE_TEMP
        # v2 TUTOR_MOVE — TUTORING-class clamp [0.1, 0.3].
        if purpose == self.Purpose.TUTOR_MOVE.value:
            return max(0.1, min(0.3, temp))
        if purpose == self.Purpose.TUTORING.value:
            return max(self.TUTORING_TEMP_MIN, min(self.TUTORING_TEMP_MAX, temp))
        return temp

    @staticmethod
    def _get_fernet() -> Fernet:
        """Derive a Fernet key from Django SECRET_KEY."""
        digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
        key = base64.urlsafe_b64encode(digest)
        return Fernet(key)

    def set_api_key(self, raw_key: str):
        """Encrypt and store an API key."""
        if not raw_key:
            self.api_key_encrypted = ''
            return
        f = self._get_fernet()
        self.api_key_encrypted = f.encrypt(raw_key.encode()).decode()

    def get_api_key(self) -> str:
        """Decrypt stored key, falling back to env var."""
        if self.api_key_encrypted:
            try:
                f = self._get_fernet()
                return f.decrypt(self.api_key_encrypted.encode()).decode()
            except Exception:
                pass
        # Fallback to environment variable
        if self.api_key_env_var:
            return os.getenv(self.api_key_env_var, '')
        return ''

    # Deploy-time tutoring-model overrides via env var. Format:
    # "provider/model_name" (e.g. "anthropic/claude-sonnet-4-6").
    # Empty / unset = use the DB-active config (mirrors the historical
    # TUTOR_MODEL_OVERRIDE pattern). Phase 3 §3.6.1 replaces the
    # single legacy env var with one per v2 ModelConfig.Purpose so
    # each dispatched call can be retargeted independently.
    _LEGACY_TUTORING_OVERRIDE_ENV = 'TUTOR_MODEL_OVERRIDE'
    _PURPOSE_OVERRIDE_ENVS = {
        'tutoring': _LEGACY_TUTORING_OVERRIDE_ENV,
        'tutor_move': 'TUTOR_MOVE_MODEL_OVERRIDE',
        'grader_math': 'GRADER_MATH_MODEL_OVERRIDE',
        'grader_grounded': 'GRADER_GROUNDED_MODEL_OVERRIDE',
        'conformance_classifier': 'CONFORMANCE_CLASSIFIER_MODEL_OVERRIDE',
        'tutor_claim_adjudicator': 'TUTOR_CLAIM_ADJUDICATOR_MODEL_OVERRIDE',
        'profiler_summary': 'PROFILER_SUMMARY_MODEL_OVERRIDE',
        'grader_verifier': 'GRADER_VERIFIER_MODEL_OVERRIDE',
        'question_extractor': 'QUESTION_EXTRACTOR_MODEL_OVERRIDE',
    }
    # Purposes where Google-grounding is the required runtime branch.
    # Non-Gemini overrides are refused at resolve-time (fail-soft to
    # DB-active) so a deploy typo can't silently disable grounding.
    _GROUNDING_REQUIRED_PURPOSES = frozenset({
        'grader_grounded', 'tutor_claim_adjudicator',
    })

    @classmethod
    def get_for(cls, purpose: str):
        """Get active config for a specific purpose, with fallback to any active config.

        Each purpose may be retargeted by its own env var (see
        ``_PURPOSE_OVERRIDE_ENVS``). On unknown provider/model the
        runtime logs a warning and falls through to the DB-active
        config — fail-soft so a typo doesn't break the path. The
        legacy ``TUTOR_MODEL_OVERRIDE`` env var is still honoured for
        ``purpose='tutoring'`` so in-flight legacy sessions resume
        with the same dispatch surface they were running on.
        """
        env_name = cls._PURPOSE_OVERRIDE_ENVS.get(purpose)
        if env_name:
            raw = (os.getenv(env_name, '') or '').strip()
            if raw:
                import logging
                log = logging.getLogger(__name__)
                if '/' not in raw:
                    log.warning(
                        "[ModelConfig] %s=%r is malformed (expected "
                        "'provider/model_name'); falling back to DB-active",
                        env_name, raw,
                    )
                else:
                    provider, model_name = raw.split('/', 1)
                    if (
                        purpose in cls._GROUNDING_REQUIRED_PURPOSES
                        and provider.lower() != 'google'
                    ):
                        log.warning(
                            "[ModelConfig] %s=%r non-Gemini provider on "
                            "grounding-required purpose; falling back to "
                            "DB-active to preserve Google-grounding",
                            env_name, raw,
                        )
                    else:
                        runtime = cls.resolve_runtime(provider, model_name)
                        if runtime is not None:
                            return runtime
                        log.warning(
                            "[ModelConfig] %s=%r did not resolve to a "
                            "ModelConfig (unknown provider or missing API "
                            "key env var); falling back to DB-active",
                            env_name, raw,
                        )

        config = cls.objects.filter(is_active=True, purpose=purpose).first()
        if not config:
            config = cls.objects.filter(is_active=True).first()
        return config

    # ------------------------------------------------------------------
    # Runtime override helper (task #216 — dashboard provider picker)
    # ------------------------------------------------------------------
    # Default env-var lookup table for each provider. Mirrors what's in
    # the seeded ModelConfig rows so an in-memory override can resolve
    # credentials without DB writes.
    _PROVIDER_API_KEY_ENV = {
        'anthropic': 'ANTHROPIC_API_KEY',
        'openai': 'OPENAI_API_KEY',
        'google': 'GOOGLE_API_KEY',
        'azure_openai': 'AZURE_OPENAI_API_KEY',
    }

    @classmethod
    def resolve_runtime(cls, provider: str, model_name: str):
        """Return a ModelConfig suitable for an ad-hoc run with a
        specific (provider, model_name) pair — without writing to the
        database.

        Lookup order:
          1. An existing active ModelConfig matching the exact pair
             (any institution / any purpose). Reuses its
             api_key_env_var + api_key_encrypted + max_tokens.
          2. An in-memory ModelConfig assembled from defaults, with
             api_key_env_var inferred from `_PROVIDER_API_KEY_ENV`.

        Used by the dashboard "Run AI review" + "Generate pending
        images" buttons so a teacher can pick a model per-run without
        promoting it to the active config.

        Returns None when `provider` is unknown or the inferred env
        var is missing. Callers fall back to the default chain.
        """
        provider = (provider or '').strip().lower()
        model_name = (model_name or '').strip()
        if not provider or not model_name:
            return None

        existing = (
            cls.objects.filter(
                is_active=True,
                provider=provider,
                model_name=model_name,
            ).first()
            or cls.objects.filter(
                provider=provider, model_name=model_name,
            ).first()
        )
        if existing is not None:
            return existing

        env_var = cls._PROVIDER_API_KEY_ENV.get(provider)
        if not env_var:
            return None

        # Build an unsaved instance — never call .save() on this.
        # institution is required at the FK level, so we attach the
        # global institution if available; that fact is purely for
        # field validity and isn't persisted.
        try:
            from apps.accounts.models import Institution as _Inst
            inst = _Inst.get_global()
        except Exception:
            inst = None
        return cls(
            institution=inst,
            name=f"Runtime override — {provider}/{model_name}",
            provider=provider,
            model_name=model_name,
            api_key_env_var=env_var,
            api_key_encrypted='',
            max_tokens=2048,
            temperature=0.0,
            purpose=cls.Purpose.JUDGE,
            is_active=False,
        )


class MobileInferenceModel(models.Model):
    """Catalog of on-device LLMs available to the React Native mobile app.

    Mirror of apps/llm/client.py BaseLLMClient pattern, but for the on-device
    inference backends (llama.rn / executorch / mlx). Admins add rows here;
    the mobile app's Model Store reads them via /api/v1/mobile/models/.

    See memory/mobile_rn_plan.md (three-layer inference abstraction).
    """

    class Family(models.TextChoices):
        GEMMA_3N = 'gemma_3n', 'Gemma 3n'
        QWEN_3_5 = 'qwen_3_5', 'Qwen 3.5'
        LLAMA_3_2 = 'llama_3_2', 'Llama 3.2'
        PHI_3_5 = 'phi_3_5', 'Phi 3.5'
        OTHER = 'other', 'Other'

    class Runtime(models.TextChoices):
        LLAMA_CPP = 'llama_cpp', 'llama.cpp (GGUF)'
        EXECUTORCH = 'executorch', 'ExecuTorch'
        MEDIAPIPE = 'mediapipe', 'MediaPipe LLM Inference'
        MLX = 'mlx', 'MLX (iOS)'

    class DeviceTier(models.TextChoices):
        FLAGSHIP = 'flagship', 'Flagship (iPhone 14+ / Pixel 7+)'
        MID = 'mid', 'Mid-range (~3yr-old phones)'
        LOW = 'low', 'Low-end (older / entry-level)'

    id = models.CharField(
        max_length=100, primary_key=True,
        help_text='Stable identifier, e.g. "qwen-3-5-2b-q4-k-m"',
    )
    display_name = models.CharField(max_length=200)
    family = models.CharField(max_length=30, choices=Family.choices)
    size_mb = models.PositiveIntegerField(help_text='Approximate on-disk size at the chosen quantization.')
    ram_required_mb = models.PositiveIntegerField(help_text='RAM headroom needed for inference.')
    download_url = models.URLField(max_length=500)
    checksum_sha256 = models.CharField(
        max_length=64, blank=True,
        help_text='SHA-256 of the model file. Verified by the app post-download.',
    )
    license = models.CharField(
        max_length=80, default='Apache 2.0',
        help_text='License the weights are distributed under (e.g. "Apache 2.0", "Gemma").',
    )
    runtime = models.CharField(max_length=30, choices=Runtime.choices)
    capabilities = models.JSONField(
        default=dict,
        help_text='{"text": true, "image": false, "audio": false, "video": false}',
    )
    chat_template = models.TextField(
        blank=True, default='',
        help_text='Jinja-like chat template the client renders before inference.',
    )
    system_prompt_style = models.CharField(
        max_length=30,
        choices=[
            ('system_role', 'system role message'),
            ('prepend_user', 'prepend to first user message'),
            ('none', 'no system prompt'),
        ],
        default='system_role',
    )
    recommended_tier = models.CharField(
        max_length=20, choices=DeviceTier.choices, default=DeviceTier.MID,
    )
    quality_scores = models.JSONField(
        default=dict, blank=True,
        help_text='{"math": 0.8, "science": 0.85, ...} from the M0 bake-off.',
    )
    is_active = models.BooleanField(default=True)
    min_app_version = models.CharField(
        max_length=20, blank=True,
        help_text='Hide this model from app builds older than this.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['recommended_tier', 'size_mb']
        verbose_name = 'Mobile Inference Model'

    def __str__(self):
        return f"{self.display_name} ({self.id})"
