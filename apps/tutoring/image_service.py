"""Image Generation Service — OpenAI gpt-image-2 (primary) + Gemini (fallback).

Provider routing:

  - ModelConfig(purpose='image_generation') determines the configured
    primary. provider='openai' → gpt-image-2; provider='google' →
    gemini-3.1-flash-image-preview. The OTHER provider is used as a
    cross-provider fallback if the primary 503s or returns no image.
  - Callers can force a specific provider per-call via
    `get_or_generate_image(..., model_override='openai'|'gemini')`.
    The teacher's per-image Regenerate UI uses this so they can flip
    providers when one model produces a bad result.

Set DISABLE_IMAGE_GEN=1 in .env to disable both providers.
"""

import os
import logging
import hashlib
import mimetypes
import base64
from typing import Optional, Dict
from django.conf import settings
from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)

# OpenAI
DEFAULT_OPENAI_MODEL = 'gpt-image-2'

# Gemini
DEFAULT_GEMINI_MODEL = 'gemini-3.1-flash-image-preview'
GEMINI_FALLBACK_MODEL = 'gemini-3-pro-image-preview'

FACTUAL_CATEGORIES = {'diagram', 'map', 'chart', 'infographic', 'flowchart'}


class ImageGenerationService:
    """
    Generate images via OpenAI gpt-image-2 or Gemini.

    Usage:
        service = ImageGenerationService(lesson)
        result = service.get_or_generate_image(
            prompt="Diagram showing relief rainfall on a mountain",
            category="diagram",
            model_override="openai",  # optional; force a provider
        )
        # Returns: {'url': '/media/...', 'title': '...', 'generated': True/False}
    """

    def __init__(self, lesson=None, institution=None):
        self.lesson = lesson
        self.institution = institution
        self._model_config = None
        self._load_model_config()
        self.available = self._check_available()
        # Last error message — surfaced to the teacher Regenerate UI
        # when get_or_generate_image returns None so they know WHY.
        self.last_error: Optional[str] = None

    def _load_model_config(self):
        """Load image generation config from ModelConfig if available."""
        try:
            from apps.llm.models import ModelConfig
            self._model_config = ModelConfig.objects.filter(
                is_active=True, purpose='image_generation'
            ).first()
        except Exception:
            pass

    def _configured_provider(self) -> str:
        """Return 'openai' or 'gemini' from ModelConfig, defaulting to openai
        when both keys are available, gemini otherwise."""
        if self._model_config:
            provider = (self._model_config.provider or '').lower()
            if provider == 'openai':
                return 'openai'
            if provider in ('google', 'gemini'):
                return 'gemini'
        # No ModelConfig → prefer OpenAI when both keys are present.
        if self._get_openai_key():
            return 'openai'
        return 'gemini'

    def _get_openai_key(self) -> Optional[str]:
        """OpenAI API key from ModelConfig (when provider matches) or env."""
        if self._model_config and (self._model_config.provider or '').lower() == 'openai':
            key = self._model_config.get_api_key()
            if key:
                return key
        return os.environ.get('OPENAI_API_KEY')

    def _get_gemini_key(self) -> Optional[str]:
        """Gemini API key from ModelConfig (when provider matches) or env."""
        if self._model_config and (self._model_config.provider or '').lower() in ('google', 'gemini'):
            key = self._model_config.get_api_key()
            if key:
                return key
        return os.environ.get('GOOGLE_API_KEY') or os.environ.get('GEMINI_API_KEY')

    def _openai_model(self) -> str:
        """OpenAI model name from ModelConfig (if provider matches) or default."""
        if self._model_config and (self._model_config.provider or '').lower() == 'openai':
            return self._model_config.model_name or DEFAULT_OPENAI_MODEL
        return DEFAULT_OPENAI_MODEL

    def _gemini_model(self) -> str:
        """Gemini primary model from ModelConfig (if provider matches) or default."""
        if self._model_config and (self._model_config.provider or '').lower() in ('google', 'gemini'):
            return self._model_config.model_name or DEFAULT_GEMINI_MODEL
        return DEFAULT_GEMINI_MODEL

    def _check_available(self) -> bool:
        """Available if disabled flag isn't set AND at least one provider has a key."""
        if os.environ.get('DISABLE_IMAGE_GEN'):
            return False
        if not (self._get_openai_key() or self._get_gemini_key()):
            logger.info("Image generation not available: No API key configured")
            return False
        return True

    def get_or_generate_image(
        self,
        prompt: str,
        category: str = "general",
        textbook_context: str = "",
        include_bytes: bool = False,
        model_override: Optional[str] = None,
        current_image_url: Optional[str] = None,
        **kwargs
    ) -> Optional[Dict]:
        """
        Generate a new image — or EDIT an existing one when
        `current_image_url` is supplied.

        Args:
            prompt: Description of the image needed (or the edit
                    instruction when current_image_url is set).
            category: Type of image (diagram, photo, illustration, etc.)
            textbook_context: Optional description of the textbook figure style
            include_bytes: If True, include '_raw_bytes' key with raw image data
            model_override: 'openai' or 'gemini' — force a specific provider
                            and skip cross-provider fallback. Used by the
                            teacher Regenerate UI.
            current_image_url: When set, the provider EDITS this existing
                               image rather than generating from scratch.
                               OpenAI uses images.edit; Gemini sends the
                               image as multimodal context. Falls back to
                               full generation if the provider can't load
                               the existing image.

        Returns:
            Dict with 'url', 'title', 'caption', 'generated', 'model' keys,
            or None on failure.
        """
        self.last_error = None  # reset between calls
        # Stash category + textbook_context so the post-gen figure
        # regen path can re-issue gen calls with the same shape.
        # Lives on `self` instead of a thread-local because the
        # ImageService instance is created per-request.
        self._last_category = category
        self._last_textbook_context = textbook_context
        if not self.available:
            logger.warning(f"Image generation unavailable for: {prompt[:50]}...")
            self.last_error = "Image generation is disabled or no API keys configured."
            return None

        override = (model_override or '').lower().strip() or None

        # ─── PRE-gen image-prompt judge ────────────────────────────
        # Cheap text-LLM review of the candidate prompt BEFORE we pay
        # ~$0.04 + 8-15s for an image gen call that's destined to
        # produce garbage. The judge runs on a different provider than
        # the gen model (cross-provider review). Editing existing
        # images skips the judge — `current_image_url` means the prior
        # image was already accepted and the prompt is an EDIT
        # instruction, not a fresh generation prompt.
        if (
            current_image_url is None
            and getattr(settings, 'CONTENT_JUDGE_IMAGE_PROMPT_ENABLED', True)
        ):
            judged_prompt = self._review_image_prompt(
                prompt, category=category, textbook_context=textbook_context,
                gen_provider=override or self._configured_provider(),
            )
            if judged_prompt is not None:
                # Judge returned a revised prompt — use it.
                prompt = judged_prompt

        if override == 'openai':
            result = self._generate_with_openai(
                prompt, category, textbook_context, include_bytes,
                current_image_url=current_image_url,
            )
            return self._post_gen_judge(
                result, prompt=prompt, gen_provider='openai',
                current_image_url=current_image_url,
            )
        if override in ('gemini', 'google'):
            result = self._generate_with_gemini(
                prompt, category, textbook_context, include_bytes,
                current_image_url=current_image_url,
            )
            return self._post_gen_judge(
                result, prompt=prompt, gen_provider='google',
                current_image_url=current_image_url,
            )

        # No override — primary by config, with cross-provider fallback.
        primary = self._configured_provider()
        order = (
            ((self._generate_with_openai, 'openai'),
             (self._generate_with_gemini, 'google'))
            if primary == 'openai'
            else ((self._generate_with_gemini, 'google'),
                  (self._generate_with_openai, 'openai'))
        )
        for fn, gen_provider in order:
            result = fn(
                prompt, category, textbook_context, include_bytes,
                current_image_url=current_image_url,
            )
            if result:
                return self._post_gen_judge(
                    result, prompt=prompt, gen_provider=gen_provider,
                    current_image_url=current_image_url,
                )

        logger.warning("Both providers failed to generate image")
        return None

    # ─── Image-prompt judge (PRE-gen) ───────────────────────────────

    def _lesson_context_for_judge(self) -> Dict[str, str]:
        """Pull the four lesson facts the judge consumes.

        Returns blanks when self.lesson is None — judge degrades to
        category + style-hint only review (still useful for catching
        VAGUE / TEXT-IN-IMAGE / WRONG-VISUAL-TYPE).
        """
        lesson = self.lesson
        if lesson is None:
            return {"subject": "", "grade": "", "title": "", "objective": ""}
        ctx = {
            "title": str(getattr(lesson, 'title', '') or ''),
            "objective": str(getattr(lesson, 'objective', '') or ''),
            "subject": "",
            "grade": "",
        }
        # Walk lesson → unit → course for subject + grade. Course has
        # both legacy `grade_level` (str) and `grade_levels` (list);
        # prefer the list when populated.
        try:
            unit = getattr(lesson, 'unit', None)
            course = getattr(unit, 'course', None) if unit else None
            if course is not None:
                # Subject — prefer subject_type (canonical) over name.
                subj_type = getattr(course, 'subject_type', '') or ''
                course_name = getattr(course, 'name', '') or ''
                ctx["subject"] = str(subj_type or course_name)
                # Grade — prefer the list, fall back to scalar.
                grades = getattr(course, 'grade_levels', None) or []
                if isinstance(grades, list) and grades:
                    ctx["grade"] = ", ".join(str(g) for g in grades[:3])
                else:
                    ctx["grade"] = str(getattr(course, 'grade_level', '') or '')
        except Exception:
            pass
        return ctx

    def _review_image_prompt(
        self,
        prompt: str,
        *,
        category: str,
        textbook_context: str,
        gen_provider: str,
    ) -> Optional[str]:
        """Run the PRE-gen judge. Return a REVISED prompt or None.

        Returns:
            None when the judge approved the prompt OR couldn't run
            (no providers, judge errored, infrastructure failure). The
            caller proceeds with the ORIGINAL prompt.
            A revised prompt string when the judge rejected and offered
            a usable rewrite. The caller swaps in the revision and
            proceeds with image gen — single-shot revision, no loop.
        """
        try:
            from apps.curriculum.content_judges.image_prompt import (
                run_image_prompt_judge,
            )
        except Exception as exc:
            logger.warning(f"[ImagePromptJudge] import failed: {exc}")
            return None

        # Map gen-side provider ('gemini' is the user-facing label, but
        # ModelConfig stores the canonical 'google').
        exclude = (gen_provider or '').lower().strip()
        if exclude in ('gemini', 'google'):
            exclude = 'google'
        elif exclude == 'openai':
            exclude = 'openai'
        else:
            exclude = None

        ctx = self._lesson_context_for_judge()
        try:
            verdict = run_image_prompt_judge(
                prompt,
                category=category,
                lesson_subject=ctx["subject"],
                lesson_grade=ctx["grade"],
                lesson_title=ctx["title"],
                lesson_objective=ctx["objective"],
                textbook_context=textbook_context,
                exclude_provider=exclude,
            )
        except Exception as exc:
            logger.warning(f"[ImagePromptJudge] call raised: {exc}")
            return None

        # Telemetry — always log a single line per judge call so the
        # benchmark sampler can slice by skip_reason / violations.
        if verdict.skipped:
            logger.info(
                f"[ImagePromptJudge] skipped reason={verdict.skip_reason} "
                f"prompt_chars={len(prompt)}"
            )
            return None

        if verdict.passed:
            logger.info(
                f"[ImagePromptJudge] PASS via {verdict.provider}/"
                f"{verdict.model_name} prompt_chars={len(prompt)}"
            )
            return None

        # Rejected. Log the violations and the fix decision.
        logger.warning(
            f"[ImagePromptJudge] REJECT via {verdict.provider}/"
            f"{verdict.model_name} violations={verdict.violations} "
            f"reasoning={verdict.reasoning[:120]!r}"
        )
        if not verdict.recommended_fix:
            # Rejected with no rewrite — fall through with the original
            # prompt. We don't block image gen on judge dissent today;
            # the rejection is logged for benchmark mining.
            logger.warning(
                "[ImagePromptJudge] no recommended_fix supplied — "
                "proceeding with original prompt"
            )
            return None

        logger.info(
            f"[ImagePromptJudge] applying revised prompt "
            f"(orig_chars={len(prompt)} new_chars={len(verdict.recommended_fix)})"
        )
        return verdict.recommended_fix

    # ─── Figure alignment judge (POST-gen) ──────────────────────────

    def _post_gen_judge(
        self,
        result: Optional[Dict],
        *,
        prompt: str,
        gen_provider: str,
        current_image_url: Optional[str],
    ) -> Optional[Dict]:
        """Run the figure_alignment vision judge on the just-generated
        image. Persists verdict to MediaAsset.judge_outputs and adds it
        to the returned dict under `judge_outputs.figure_alignment`.

        Returns the (possibly enriched) result dict unchanged on
        infrastructure skip — the image pipeline never blocks on a
        judge failure. When the judge REJECTS, the result still flows
        through; today we only flag. The Q2 regen ensemble (when it
        lands) will consume passed=False to drive a rewrite.

        Skips:
          - result is None (gen failed) — nothing to judge
          - current_image_url is set — edits skip the judge same way
            PRE-gen prompt review skips for edits
          - settings.CONTENT_JUDGE_FIGURE_ALIGNMENT_ENABLED is False
        """
        if result is None:
            return None
        if current_image_url is not None:
            return result
        if not getattr(
            settings, 'CONTENT_JUDGE_FIGURE_ALIGNMENT_ENABLED', True,
        ):
            return result

        # Need bytes — prefer the in-memory copy if include_bytes was
        # set, otherwise re-read from the saved URL on disk.
        image_bytes = result.get('_raw_bytes')
        url = result.get('url') or ''
        if not image_bytes and url:
            image_bytes = self._read_image_bytes(url)
        if not image_bytes:
            logger.info(
                "[FigureAlignmentJudge] no bytes available for "
                f"{url[-60:]} — skipping post-gen review"
            )
            return result

        media_type = self._guess_mime(url) if url else 'image/png'

        try:
            from apps.curriculum.content_judges.figure_alignment import (
                run_figure_alignment_judge,
            )
        except Exception as exc:
            logger.warning(f"[FigureAlignmentJudge] import failed: {exc}")
            return result

        # Lesson context — same helper the PRE-gen judge uses.
        ctx = self._lesson_context_for_judge()
        # Step-level objective is harder to derive from this call site
        # because image_service doesn't take a step argument. Fall back
        # to lesson_objective; the caller can pass step context via
        # textbook_context if it has it. Improvement opportunity:
        # thread step_objective through get_or_generate_image kwargs.

        try:
            verdict = run_figure_alignment_judge(
                image_bytes=image_bytes,
                image_media_type=media_type,
                image_prompt=prompt,
                lesson_subject=ctx['subject'],
                lesson_grade=ctx['grade'],
                lesson_title=ctx['title'],
                lesson_objective=ctx['objective'],
                step_objective=ctx['objective'],
                exclude_provider=gen_provider,
            )
        except Exception as exc:
            logger.warning(f"[FigureAlignmentJudge] call raised: {exc}")
            return result

        verdict_dict = {
            'passed': verdict.passed,
            'violations': list(verdict.violations or []),
            'reasoning': verdict.reasoning or '',
            'recommended_fix': verdict.recommended_fix or '',
            'provider': verdict.provider or '',
            'model_name': verdict.model_name or '',
            'skipped': verdict.skipped,
            'skip_reason': verdict.skip_reason or '',
        }

        # Track the result fields locally so the regen branch (below)
        # can swap in new image bytes / new url when it produces a
        # clean candidate, without re-running anything.
        regen_audit_blob: Optional[Dict] = None

        if verdict.skipped:
            logger.info(
                f"[FigureAlignmentJudge] skipped "
                f"reason={verdict.skip_reason}"
            )
        elif verdict.passed:
            logger.info(
                f"[FigureAlignmentJudge] PASS via {verdict.provider}/"
                f"{verdict.model_name}"
            )
        else:
            logger.warning(
                f"[FigureAlignmentJudge] REJECT via {verdict.provider}/"
                f"{verdict.model_name} violations={verdict.violations} "
                f"reasoning={verdict.reasoning[:120]!r}"
            )

            # Auto-regen on REJECT — mirror the text-rewrite flow.
            # Re-uses the same gen provider as the original image
            # generation, calling back into _generate_with_<provider>
            # via a closure so the orchestrator stays free of
            # image-API dependencies.
            if getattr(settings, 'CONTENT_REGEN_ENABLED', True):
                regen_audit_blob = self._run_figure_regen(
                    result=result,
                    verdict_dict=verdict_dict,
                    original_bytes=image_bytes,
                    original_prompt=prompt,
                    gen_provider=gen_provider,
                    category=getattr(self, '_last_category', 'general'),
                    textbook_context=getattr(self, '_last_textbook_context', ''),
                    ctx=ctx,
                )
                if regen_audit_blob and regen_audit_blob.get('clean'):
                    # Regen produced a clean candidate. Refresh the
                    # local verdict_dict so the downstream persistence
                    # path picks up the post-regen verdict.
                    final_jr = regen_audit_blob.get('final_judge_result') or {}
                    if final_jr:
                        verdict_dict = dict(final_jr)

        # Persist to MediaAsset.judge_outputs by file URL lookup.
        # Best-effort — a missing asset just means the verdict only
        # rides along on the result dict.
        if url:
            try:
                from apps.media_library.models import MediaAsset
                # MediaAsset.file URL ends with the saved filename;
                # match by the relative path the URL contains.
                from django.conf import settings as _s
                rel = url
                if rel.startswith('/media/'):
                    rel = rel[len('/media/'):]
                asset = MediaAsset.objects.filter(
                    file=rel,
                ).first()
                if asset:
                    outputs = dict(asset.judge_outputs or {})
                    outputs['figure_alignment'] = verdict_dict
                    if regen_audit_blob is not None:
                        outputs['regen_audit'] = regen_audit_blob
                    asset.judge_outputs = outputs
                    asset.save(update_fields=['judge_outputs', 'updated_at'])
                    logger.debug(
                        f"[FigureAlignmentJudge] persisted to MediaAsset "
                        f"#{asset.id}"
                    )
            except Exception as exc:
                logger.warning(
                    f"[FigureAlignmentJudge] MediaAsset persist failed: "
                    f"{type(exc).__name__}: {exc}"
                )

        # Always ride the verdict back on the result dict so callers
        # that don't query MediaAsset can still see it.
        outputs = result.get('judge_outputs') or {}
        outputs['figure_alignment'] = verdict_dict
        if regen_audit_blob is not None:
            outputs['regen_audit'] = regen_audit_blob
        result['judge_outputs'] = outputs

        return result

    def _run_figure_regen(
        self,
        *,
        result: Dict,
        verdict_dict: Dict,
        original_bytes: bytes,
        original_prompt: str,
        gen_provider: str,
        category: str,
        textbook_context: str,
        ctx: Dict,
    ) -> Optional[Dict]:
        """Drive `run_figure_regen` against the flagged image and, on
        clean candidate, replace the saved image bytes + URL in
        `result`. Returns the audit blob (for persistence to
        MediaAsset.judge_outputs['regen_audit']) or None on infra
        failure.

        Mirrors the text-rewrite flow: the orchestrator owns the
        cycle loop + re-judging; this wrapper provides the image-gen
        callback closure + handles disk I/O when a clean image lands.
        """
        try:
            from apps.curriculum.content_regen import run_figure_regen
        except Exception as exc:
            logger.warning(
                f"[FigureRegen] import failed: {type(exc).__name__}: {exc}"
            )
            return None

        original_media_type = self._guess_mime(result.get('url') or '') or 'image/png'

        # Resolve the lesson instance for re-judge context derivation.
        # _lesson_context_for_judge gave us the strings; the
        # orchestrator can use them directly so we don't need the
        # Lesson object here.
        def _gen_image(prompt: str):
            """Per-cycle image-gen callback. Returns (bytes, media_type)
            or (None, None) on gen failure."""
            try:
                if gen_provider == 'openai':
                    res = self._generate_with_openai(
                        prompt, category, textbook_context,
                        include_bytes=True, current_image_url=None,
                    )
                elif gen_provider in ('google', 'gemini'):
                    res = self._generate_with_gemini(
                        prompt, category, textbook_context,
                        include_bytes=True, current_image_url=None,
                    )
                else:
                    logger.warning(
                        f"[FigureRegen] unknown gen_provider={gen_provider!r}"
                    )
                    return None, None
            except Exception as exc:
                logger.warning(
                    f"[FigureRegen] gen call raised: "
                    f"{type(exc).__name__}: {exc}"
                )
                return None, None
            if not res:
                return None, None
            new_bytes = res.get('_raw_bytes')
            new_url = res.get('url') or ''
            new_mt = self._guess_mime(new_url) if new_url else 'image/png'
            if not new_bytes and new_url:
                new_bytes = self._read_image_bytes(new_url)
            if not new_bytes:
                return None, None
            return new_bytes, new_mt

        try:
            regen_result = run_figure_regen(
                original_bytes=original_bytes,
                original_media_type=original_media_type,
                original_prompt=original_prompt,
                judge_result=verdict_dict,
                lesson=None,
                lesson_subject=ctx.get('subject', ''),
                lesson_grade=ctx.get('grade', ''),
                lesson_title=ctx.get('title', ''),
                lesson_objective=ctx.get('objective', ''),
                step_objective=ctx.get('objective', ''),
                image_gen_fn=_gen_image,
                exclude_provider=gen_provider,
            )
        except Exception as exc:
            logger.warning(
                f"[FigureRegen] orchestrator raised: "
                f"{type(exc).__name__}: {exc}"
            )
            return None

        audit_blob = {
            'cycles_run': regen_result.cycles_run,
            'clean': regen_result.clean,
            'picked_model': regen_result.picked_model,
            'elapsed_seconds': round(regen_result.elapsed_seconds, 2),
            'final_violations': list(regen_result.final_violations),
            'final_judge_result': dict(regen_result.final_judge_result or {}),
            'prompt_used': regen_result.prompt_used[:300],
            'cycles': regen_result.audit,
        }

        # On clean candidate: save new image bytes + update the
        # result dict to point at the new URL so callers see the
        # rewritten image, not the flagged original.
        if regen_result.clean and regen_result.image_bytes:
            ext = (
                '.jpg' if (regen_result.image_media_type or '').endswith('jpeg')
                else '.png'
            )
            new_url = self._save_generated_image_bytes(
                regen_result.image_bytes,
                regen_result.prompt_used,
                ext,
            )
            if new_url:
                result['url'] = new_url
                if '_raw_bytes' in result:
                    result['_raw_bytes'] = regen_result.image_bytes
                logger.info(
                    f"[FigureRegen] CLEAN after {regen_result.cycles_run} "
                    f"cycle(s) — replaced image with {new_url[-60:]}"
                )

        return audit_blob

    # ─── OpenAI gpt-image-2 ─────────────────────────────────────────

    def _generate_with_openai(
        self, prompt: str, category: str, textbook_context: str = "",
        include_bytes: bool = False,
        current_image_url: Optional[str] = None,
    ) -> Optional[Dict]:
        """Generate image via OpenAI Images API (gpt-image-2).

        When `current_image_url` is supplied, calls images.edit so the
        model EDITS the existing image rather than generating from
        scratch. Falls back to full generation if the existing image
        can't be loaded.
        """
        api_key = self._get_openai_key()
        if not api_key:
            return None

        try:
            from openai import OpenAI
        except Exception as e:
            logger.warning(f"openai SDK not installed: {e}")
            return None

        enhanced_prompt = self._enhance_prompt(prompt, category, textbook_context)
        model = self._openai_model()
        # Use landscape for factual/spatial categories, square for the rest.
        size = "1536x1024" if category in FACTUAL_CATEGORIES else "1024x1024"

        # Resolve the existing image bytes for edit-mode (if any).
        existing_bytes = None
        if current_image_url:
            existing_bytes = self._read_image_bytes(current_image_url)
            if existing_bytes is None:
                logger.warning(
                    "[ImageEdit] could not load current_image_url=%s — "
                    "falling back to text-only generation",
                    current_image_url,
                )

        try:
            client = OpenAI(api_key=api_key)
            if existing_bytes:
                # images.edit takes the prior image + a prompt that
                # describes the edit. The model preserves what's good
                # and changes what the prompt describes — much more
                # stable than re-generating from scratch on every regen.
                logger.info(
                    f"Editing image with {model} ({len(existing_bytes)} bytes): "
                    f"{prompt[:80]}..."
                )
                from io import BytesIO
                buf = BytesIO(existing_bytes)
                buf.name = "current.png"  # OpenAI SDK reads .name for MIME
                response = client.images.edit(
                    model=model,
                    image=buf,
                    prompt=enhanced_prompt,
                    size=size,
                    n=1,
                )
            else:
                logger.info(f"Generating image with {model}: {prompt[:80]}...")
                response = client.images.generate(
                    model=model,
                    prompt=enhanced_prompt,
                    size=size,
                    n=1,
                )
            data = response.data[0] if response.data else None
            if not data:
                logger.warning(f"{model}: empty response.data")
                return None

            image_bytes = None
            if getattr(data, 'b64_json', None):
                image_bytes = base64.b64decode(data.b64_json)
            elif getattr(data, 'url', None):
                # Fall back to fetching the URL if SDK returned a URL instead.
                import urllib.request
                with urllib.request.urlopen(data.url, timeout=30) as r:
                    image_bytes = r.read()

            if not image_bytes:
                logger.warning(f"{model}: no image bytes in response")
                return None

            saved_url = self._save_generated_image_bytes(image_bytes, prompt, '.png')
            if not saved_url:
                return None

            logger.info(f"Image generated successfully with {model}")
            result = {
                'url': saved_url,
                'title': prompt[:100],
                'caption': f"AI-generated: {prompt[:200]}",
                'alt_text': prompt,
                'generated': True,
                'model': model,
                'provider': 'openai',
            }
            if include_bytes:
                result['_raw_bytes'] = image_bytes
            return result

        except Exception as e:
            err = str(e)
            logger.error(f"{model} failed: {err[:300]}")
            # Detect the verification gate so the UI can show a clear
            # action ("verify your OpenAI org") instead of a generic
            # "no result" message.
            if 'verified' in err.lower() or 'must be verified' in err.lower():
                self.last_error = (
                    "OpenAI organization is not verified for gpt-image-2. "
                    "Verify at platform.openai.com/settings/organization/general, "
                    "wait ~15 min for propagation, then retry."
                )
            elif '403' in err or 'forbidden' in err.lower():
                self.last_error = f"OpenAI {model}: 403 Forbidden — check API key permissions."
            else:
                self.last_error = f"OpenAI {model} error: {err[:200]}"
            return None

    # ─── Gemini ─────────────────────────────────────────────────────

    def _generate_with_gemini(
        self, prompt: str, category: str, textbook_context: str = "",
        include_bytes: bool = False,
        current_image_url: Optional[str] = None,
    ) -> Optional[Dict]:
        """Generate image — tries configured Gemini model, falls back
        to GEMINI_FALLBACK_MODEL on 503. When `current_image_url` is
        supplied, sends the existing image as multimodal context so
        the model edits it. Falls back to text-only if the image
        can't be loaded."""
        api_key = self._get_gemini_key()
        if not api_key:
            return None

        try:
            from google import genai
            from google.genai import types
        except Exception as e:
            logger.warning(f"google-genai SDK not installed: {e}")
            return None

        client = genai.Client(api_key=api_key)
        enhanced_prompt = self._enhance_prompt(prompt, category, textbook_context)

        # Build the user-message parts. When current_image_url is
        # supplied, prepend the existing image as multimodal context
        # so Gemini edits it instead of generating from scratch.
        user_parts = []
        if current_image_url:
            existing_bytes = self._read_image_bytes(current_image_url)
            if existing_bytes:
                mime = self._guess_mime(current_image_url)
                try:
                    user_parts.append(types.Part(
                        inline_data=types.Blob(
                            mime_type=mime, data=existing_bytes,
                        )
                    ))
                except Exception as e:
                    logger.warning(
                        "[ImageEdit] Gemini inline_data failed: %s — "
                        "falling back to text-only", e,
                    )
            else:
                logger.warning(
                    "[ImageEdit] could not load current_image_url=%s "
                    "for Gemini — falling back to text-only",
                    current_image_url,
                )
        user_parts.append(types.Part.from_text(text=enhanced_prompt))

        contents = [
            types.Content(role="user", parts=user_parts),
        ]

        aspect = "16:9" if category in FACTUAL_CATEGORIES else "1:1"

        config = types.GenerateContentConfig(
            image_config=types.ImageConfig(
                aspect_ratio=aspect,
                image_size="1K",
            ),
            response_modalities=["IMAGE", "TEXT"],
        )

        # Always enable web + image search grounding for factual accuracy.
        tools = None
        try:
            tools = [types.Tool(google_search=types.GoogleSearch(
                search_types=types.SearchTypes(
                    web_search=types.WebSearch(),
                    image_search=types.ImageSearch(),
                )
            ))]
            logger.info(f"Search grounding (web+image) enabled for category: {category}")
        except Exception as e:
            logger.warning(f"Could not create search grounding tool: {e}")

        for model in [self._gemini_model(), GEMINI_FALLBACK_MODEL]:
            result = self._call_gemini(
                client, model, contents, config, prompt,
                tools=tools, include_bytes=include_bytes,
            )
            if result is not None:
                return result

        logger.warning("Both primary and fallback Gemini models failed")
        return None

    def _call_gemini(
        self, client, model: str, contents, config, prompt: str,
        tools=None, include_bytes: bool = False,
    ) -> Optional[Dict]:
        """Generate image from a single Gemini model. Returns result dict or None."""
        try:
            logger.info(f"Generating image with {model}: {prompt[:80]}...")

            kwargs = dict(model=model, contents=contents, config=config)
            if tools:
                kwargs['config'] = type(config)(
                    image_config=config.image_config,
                    response_modalities=config.response_modalities,
                    tools=tools,
                )

            try:
                response = client.models.generate_content(**kwargs)
            except Exception as tool_err:
                if tools:
                    logger.warning(f"{model}: tools param failed ({tool_err}), retrying without")
                    response = client.models.generate_content(
                        model=model, contents=contents, config=config
                    )
                else:
                    raise

            image_bytes = None
            mime_type = None

            if response.candidates and response.candidates[0].content:
                for part in response.candidates[0].content.parts:
                    if part.inline_data and part.inline_data.data:
                        image_bytes = part.inline_data.data
                        mime_type = part.inline_data.mime_type

            if not image_bytes:
                logger.warning(f"{model}: No image data in response")
                return None

            ext = mimetypes.guess_extension(mime_type) if mime_type else '.png'
            if ext is None:
                ext = '.png'

            saved_url = self._save_generated_image_bytes(image_bytes, prompt, ext)

            logger.info(f"Image generated successfully with {model}")
            result = {
                'url': saved_url,
                'title': prompt[:100],
                'caption': f"AI-generated: {prompt[:200]}",
                'alt_text': prompt,
                'generated': True,
                'model': model,
                'provider': 'gemini',
            }

            if include_bytes:
                result['_raw_bytes'] = image_bytes

            return result

        except Exception as e:
            err = str(e)
            if '503' in err or 'UNAVAILABLE' in err.upper() or 'overloaded' in err.lower():
                logger.warning(f"{model} unavailable (503), trying fallback...")
                self.last_error = f"{model} unavailable (503)"
                return None
            logger.error(f"{model} failed: {err[:300]}")
            self.last_error = f"Gemini {model} error: {err[:200]}"
            return None

    # ─── Prompt enhancement ─────────────────────────────────────────

    def _enhance_prompt(self, prompt: str, category: str, textbook_context: str = "") -> str:
        """Enhance prompt for better image results (provider-agnostic)."""
        from apps.llm.prompts import get_prompt_or_default
        institution_id = self.institution.id if self.institution else None
        context = get_prompt_or_default(
            institution_id, 'image_generation_prompt',
            "Educational visual for secondary school students. "
        )

        lesson_context = ""
        if self.lesson:
            try:
                course = self.lesson.unit.course
                lesson_context = (
                    f"Subject: {course.title}. "
                    f"Grade: {course.grade_level}. "
                    f"Lesson: {self.lesson.title}. "
                )
            except Exception:
                pass

        style_map = {
            'diagram': (
                "Create a detailed educational diagram with clear labels, "
                "arrows, and annotations on a clean white background. "
                "Make it precise and suitable for a textbook. "
            ),
            'photo': (
                "Create a high-quality photorealistic image, sharp focus, "
                "natural lighting. "
            ),
            'illustration': (
                "Create a colorful digital educational illustration with "
                "clean lines, vibrant colours, suitable for a "
                "secondary school textbook. "
            ),
            'map': (
                "Create a SCHEMATIC geographic map (NOT a satellite photo). "
                "Professional cartographic style with clean outlines, "
                "a legend, compass rose, and distinct colour-coded regions. "
            ),
            'chart': (
                "Create a professional chart with clear axis labels, "
                "a title, distinct colours, and a clean white background. "
            ),
            'flowchart': (
                "Create a flowchart with clear boxes, directional arrows, "
                "and concise labels on a clean white background. "
                "Make it easy to follow. "
            ),
            'infographic': (
                "Create a detailed educational infographic with icons, "
                "short text labels, vibrant colours, and a clear visual hierarchy. "
            ),
        }

        style = style_map.get(category, (
            "Create a detailed educational visual, clear and professional, "
            "suitable for a secondary school textbook. "
        ))

        textbook_style = ""
        if textbook_context:
            textbook_style = f"Match this style: {textbook_context}. "

        anti_hallucination = (
            "\n\nRULES:\n"
            "1. Do NOT include text labels with made-up or misspelled words. "
            "If unsure of spelling, omit the label.\n"
            "2. Do NOT fabricate geographic features, place names, or scientific data.\n"
            "3. Keep text labels minimal — prefer arrows and colour-coding.\n"
            "4. If the figure depicts a problem with an unknown "
            "(a 'find x' style question), mark the unknown with '?' or "
            "'x = ?'. NEVER write the computed answer in place of the "
            "unknown. The figure helps the student SEE the problem; "
            "the answer must come from the student's working, not "
            "from reading the image.\n"
        )

        return f"{style}{context}{lesson_context}{textbook_style}{prompt}{anti_hallucination}"

    # ─── Image-edit helpers ─────────────────────────────────────────

    def _read_image_bytes(self, url: str) -> Optional[bytes]:
        """Resolve a stored image URL (local /media/... path or absolute
        http(s) URL) to raw bytes for use as multimodal input to the
        edit-mode image regen path. Returns None on any failure so the
        caller can fall back to text-only generation."""
        if not url:
            return None
        try:
            from django.conf import settings
            import os
            if url.startswith('/media/') or not url.startswith(('http://', 'https://')):
                # Local file under MEDIA_ROOT
                relative = url[len('/media/'):] if url.startswith('/media/') else url
                local_path = os.path.join(settings.MEDIA_ROOT, relative.lstrip('/'))
                if not os.path.exists(local_path):
                    return None
                with open(local_path, 'rb') as f:
                    return f.read()
            # Remote URL — short timeout so a stuck CDN doesn't block regen.
            import requests
            resp = requests.get(url, timeout=6)
            if resp.status_code != 200:
                return None
            return resp.content
        except Exception as e:
            logger.warning("[ImageEdit] _read_image_bytes(%s) failed: %s", url[-60:], e)
            return None

    @staticmethod
    def _guess_mime(url: str) -> str:
        u = (url or '').lower()
        if u.endswith(('.jpg', '.jpeg')):
            return 'image/jpeg'
        if u.endswith('.gif'):
            return 'image/gif'
        if u.endswith('.webp'):
            return 'image/webp'
        return 'image/png'

    # ─── Save ───────────────────────────────────────────────────────

    def _save_generated_image_bytes(
        self, image_bytes: bytes, prompt: str, ext: str = '.png'
    ) -> Optional[str]:
        """Save generated image bytes to media library."""
        try:
            from apps.media_library.models import MediaAsset
            from apps.accounts.models import Institution

            prompt_hash = hashlib.md5(prompt.encode()).hexdigest()[:8]
            filename = f"generated_{prompt_hash}{ext}"

            # MediaAsset.institution is NOT NULL. For platform-wide
            # lessons (institution=None) fall back to the Global
            # institution so the save doesn't violate the FK.
            asset_institution = self.institution or Institution.get_global()

            asset = MediaAsset.objects.create(
                institution=asset_institution,
                title=prompt[:100],
                asset_type='image',
                caption=f"AI-generated image for: {prompt[:200]}",
                alt_text=prompt[:200],
            )

            asset.file.save(filename, ContentFile(image_bytes))
            asset.save()

            logger.info(f"Saved generated image: {asset.file.url}")

            # Best-effort: extract figure_facts so the runtime tutor can
            # anchor scaffolding in real labelled features instead of
            # asking the student to imagine. Non-fatal — if extraction
            # fails, the figure still serves; the runtime falls through
            # to text-only behaviour. See memory/figure_facts_plan.md.
            try:
                from apps.curriculum.figure_facts_extractor import (
                    extract_and_save_for_asset,
                )
                saved, ff_err = extract_and_save_for_asset(
                    asset,
                    generation_prompt=prompt,
                )
                if not saved and ff_err and ff_err != "already_has_facts":
                    logger.info(
                        f"[FigureFacts] skip asset #{asset.id}: {ff_err}"
                    )
            except Exception as ff_err:
                logger.warning(
                    f"[FigureFacts] extraction crashed for asset "
                    f"#{asset.id}: {ff_err}"
                )

            return asset.file.url

        except Exception as e:
            logger.error(f"Failed to save generated image: {e}")
            self.last_error = f"Saved image, but DB write failed: {str(e)[:200]}"
            return None


def get_image_for_lesson(lesson, prompt: str, category: str = "diagram") -> Optional[Dict]:
    """
    Convenience function to get/generate image for a lesson.

    Usage in engine:
        image = get_image_for_lesson(self.lesson, "relief rainfall diagram", "diagram")
        if image:
            commands.append({'type': 'show_media', 'data': image})
    """
    service = ImageGenerationService(
        lesson=lesson,
        institution=lesson.institution if hasattr(lesson, 'institution') else None
    )
    return service.get_or_generate_image(prompt, category)
