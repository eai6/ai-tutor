"""Tests for figure_facts (F1-F4 of memory/figure_facts_plan.md).

Covers:
  - Pydantic schema validation (FigureFacts, LabelledFeature, AngleRelationship)
  - Extractor with mocked Anthropic client (no live LLM calls)
  - Runtime injection in conversational_tutor._build_figure_facts_block
  - Anti-imagination rule presence
  - Anchor-prompt rendering
"""

from unittest.mock import MagicMock

from django.contrib.auth.models import User
from django.test import TestCase

from apps.accounts.models import Institution
from apps.curriculum.figure_facts_extractor import (
    _strip_code_fences,
    extract_figure_facts,
)
from apps.curriculum.figure_facts_schema import (
    AngleRelationship,
    FigureFacts,
    LabelledFeature,
    validate_figure_facts,
)
from apps.curriculum.models import Course, Lesson, LessonStep, Unit
from apps.media_library.models import MediaAsset
from apps.tutoring.models import TutorSession


# ============================================================================
# Schema
# ============================================================================


class FigureFactsSchemaTest(TestCase):
    def test_minimal_valid_facts(self):
        ff = FigureFacts(
            type="parallel_lines_with_transversal",
            scene_description="Two parallel lines cut by a transversal.",
        )
        self.assertEqual(ff.labelled_features, [])
        self.assertEqual(ff.angle_relationships, [])

    def test_scene_description_required_non_empty(self):
        v, err = validate_figure_facts({
            "type": "x", "scene_description": "",
        })
        self.assertIsNone(v)
        self.assertIn("scene_description", err)

    def test_full_facts_round_trip(self):
        data = {
            "type": "parallel_lines_with_transversal",
            "scene_description": "Two horizontal lines cut by a diagonal.",
            "labelled_features": [
                {"label": "1", "location": "top-left", "color": "blue"},
            ],
            "angle_relationships": [
                {"pair": [1, 5], "relationship": "corresponding", "equal": True},
                {"pair": [3, 5], "relationship": "co_interior", "sum": 180},
            ],
            "extra_facts": ["lines l and m are parallel"],
            "anchor_prompts": ["Look at angle 1 — what colour is it?"],
        }
        v, err = validate_figure_facts(data)
        self.assertIsNone(err)
        self.assertEqual(v.angle_relationships[0].pair, [1, 5])
        self.assertTrue(v.angle_relationships[0].equal)
        self.assertEqual(v.angle_relationships[1].sum, 180)

    def test_relationship_must_be_known_kind(self):
        v, err = validate_figure_facts({
            "type": "x",
            "scene_description": "ok",
            "angle_relationships": [
                {"pair": [1, 5], "relationship": "totally_made_up"},
            ],
        })
        self.assertIsNone(v)

    def test_pair_cannot_self_reference(self):
        v, err = validate_figure_facts({
            "type": "x",
            "scene_description": "ok",
            "angle_relationships": [
                {"pair": [3, 3], "relationship": "corresponding", "equal": True},
            ],
        })
        self.assertIsNone(v)

    def test_unstructured_figure_passes(self):
        # A photo-style figure without geometry — should validate cleanly
        # with type=unstructured and no relationships.
        v, err = validate_figure_facts({
            "type": "unstructured",
            "scene_description": "A photo of two roads in Victoria.",
            "extra_facts": ["the roads appear to run parallel"],
        })
        self.assertIsNone(err)
        self.assertEqual(v.angle_relationships, [])


# ============================================================================
# Extractor
# ============================================================================


class FigureFactsExtractorTest(TestCase):
    """Mocked-LLM tests for the extractor — no real vision calls."""

    def _mock_client(self, content: str):
        response = MagicMock()
        response.content = content
        client = MagicMock()
        client.generate.return_value = response
        return client

    def test_strip_code_fences_handles_json_fence(self):
        text = '```json\n{"a":1}\n```'
        self.assertEqual(_strip_code_fences(text), '{"a":1}')

    def test_strip_code_fences_no_fence(self):
        self.assertEqual(_strip_code_fences('{"a":1}'), '{"a":1}')

    def test_extractor_returns_facts_on_clean_json(self):
        client = self._mock_client('''
{
  "type": "parallel_lines_with_transversal",
  "scene_description": "Two parallel lines cut by a diagonal transversal.",
  "labelled_features": [
    {"label": "1", "location": "top-left of upper intersection", "color": "blue"}
  ],
  "angle_relationships": [
    {"pair": [1, 5], "relationship": "corresponding", "equal": true}
  ]
}
''')
        # 1x1 transparent PNG bytes
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
            "1f15c4890000000d49444154789c63000100000005000100"
            "5d0c2db40000000049454e44ae426082"
        )
        facts, err = extract_figure_facts(png, llm_client=client)
        self.assertIsNone(err)
        self.assertEqual(facts.type, "parallel_lines_with_transversal")
        self.assertEqual(facts.angle_relationships[0].pair, [1, 5])

    def test_extractor_strips_code_fences(self):
        client = self._mock_client(
            '```json\n{"type":"x","scene_description":"a figure"}\n```'
        )
        png = bytes.fromhex("89504e470d0a1a0a")
        facts, err = extract_figure_facts(png, llm_client=client)
        self.assertIsNone(err)

    def test_extractor_returns_error_on_bad_json(self):
        client = self._mock_client("not json at all")
        png = bytes.fromhex("89504e470d0a1a0a")
        facts, err = extract_figure_facts(png, llm_client=client)
        self.assertIsNone(facts)
        self.assertIn("JSON", err)

    def test_extractor_returns_error_on_schema_violation(self):
        client = self._mock_client(
            '{"type":"x","scene_description":""}'  # empty scene fails
        )
        png = bytes.fromhex("89504e470d0a1a0a")
        facts, err = extract_figure_facts(png, llm_client=client)
        self.assertIsNone(facts)
        self.assertIn("schema validation", err)

    def test_extractor_returns_error_when_llm_call_fails(self):
        client = MagicMock()
        client.generate.side_effect = RuntimeError("boom")
        png = bytes.fromhex("89504e470d0a1a0a")
        facts, err = extract_figure_facts(png, llm_client=client)
        self.assertIsNone(facts)
        self.assertIn("LLM call failed", err)


class ExtractAndSaveHelperTest(TestCase):
    """Tests for extract_and_save_for_asset — the helper called from
    every image-creation site (image_service, dashboard upload, KB
    ingestion) so every figure entering the system gets facts."""

    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(name="ES", slug="es")

    def test_skips_non_image_asset(self):
        from apps.curriculum.figure_facts_extractor import extract_and_save_for_asset
        asset = MediaAsset(
            institution=self.institution,
            title="not an image",
            asset_type="audio",
        )
        asset.file.name = "audio.mp3"
        asset.save()
        saved, err = extract_and_save_for_asset(asset)
        self.assertFalse(saved)
        self.assertEqual(err, "asset_not_image")

    def test_skips_when_facts_already_present(self):
        from apps.curriculum.figure_facts_extractor import extract_and_save_for_asset
        asset = MediaAsset(
            institution=self.institution,
            title="x",
            asset_type=MediaAsset.AssetType.IMAGE,
            figure_facts={"type": "x", "scene_description": "a"},
        )
        asset.file.name = "x.png"
        asset.save()
        saved, err = extract_and_save_for_asset(asset)
        self.assertFalse(saved)
        self.assertEqual(err, "already_has_facts")

    def test_skips_when_no_file(self):
        from apps.curriculum.figure_facts_extractor import extract_and_save_for_asset
        asset = MediaAsset(
            institution=self.institution,
            title="y",
            asset_type=MediaAsset.AssetType.IMAGE,
        )
        # No .file.name assigned — empty file field
        asset.save()
        saved, err = extract_and_save_for_asset(asset)
        self.assertFalse(saved)
        self.assertEqual(err, "asset_has_no_file")


# ============================================================================
# Runtime injection
# ============================================================================


class FigureFactsBlockTest(TestCase):
    """End-to-end tests for ConversationalTutor._build_figure_facts_block."""

    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(name="FF", slug="ff")
        cls.student = User.objects.create_user(username="ffstu", password="pw")
        cls.math_course = Course.objects.create(
            institution=cls.institution, title="Math S3",
            grade_level="S3", is_published=True, subject_type='math',
        )
        cls.geo_course = Course.objects.create(
            institution=cls.institution, title="Geo S3",
            grade_level="S3", is_published=True, subject_type='humanities',
        )
        cls.unit = Unit.objects.create(course=cls.math_course, title="U", order_index=0)
        cls.geo_unit = Unit.objects.create(course=cls.geo_course, title="G", order_index=0)
        cls.lesson = Lesson.objects.create(
            unit=cls.unit, title="L", objective="x",
            order_index=0, is_published=True,
        )
        cls.geo_lesson = Lesson.objects.create(
            unit=cls.geo_unit, title="G", objective="x",
            order_index=0, is_published=True,
        )
        cls.step = LessonStep.objects.create(
            lesson=cls.lesson, phase='explore', step_type='explore',
            order_index=0,
            teacher_script="Look at the figure showing parallel lines.",
            expected_answer="",
            concept_tag="angles_around_point",
            media={
                'images': [
                    {'url': '/media/test/parallel-lines.png',
                     'alt': 'Parallel lines diagram',
                     'caption': 'Three angle relationships'},
                ]
            },
        )
        # Stub MediaAsset whose .file.url matches the step.media URL.
        # We bypass Django's FileField storage by setting file.name —
        # the asset is never opened, only its .url is consulted.
        cls.asset = MediaAsset(
            institution=cls.institution,
            title='parallel-lines-diagram.png',
            asset_type=MediaAsset.AssetType.IMAGE,
            alt_text='Parallel lines diagram',
            figure_facts={
                'type': 'parallel_lines_with_transversal',
                'scene_description': (
                    'Two horizontal parallel lines, l (top) and m '
                    '(bottom), are cut by a diagonal transversal t.'
                ),
                'labelled_features': [
                    {'label': '1', 'location': 'top-left of upper intersection', 'color': 'blue'},
                    {'label': '5', 'location': 'top-left of lower intersection', 'color': 'blue'},
                ],
                'angle_relationships': [
                    {'pair': [1, 5], 'relationship': 'corresponding', 'equal': True},
                    {'pair': [3, 6], 'relationship': 'alternate_interior', 'equal': True},
                    {'pair': [3, 5], 'relationship': 'co_interior', 'sum': 180},
                ],
                'extra_facts': ['lines l and m are parallel'],
                'anchor_prompts': [
                    'Look at angles 1 and 5 — what do you notice about their position?',
                ],
            },
        )
        cls.asset.file.name = 'test/parallel-lines.png'  # no actual file write
        cls.asset.save()

    def _make_tutor(self, lesson, steps, current_topic_index=0):
        from apps.tutoring.conversational_tutor import ConversationalTutor
        session = TutorSession.objects.create(
            institution=self.institution,
            student=self.student,
            lesson=lesson,
            engine_state={},
        )
        tutor = ConversationalTutor.__new__(ConversationalTutor)
        tutor.session = session
        tutor.lesson = lesson
        tutor.student = self.student
        tutor.steps = steps
        tutor.current_topic_index = current_topic_index
        return tutor

    def test_block_empty_for_non_math_course(self):
        # Non-math lesson with the same step shape — should return ''
        geo_step = LessonStep.objects.create(
            lesson=self.geo_lesson, phase='explore', step_type='explore',
            order_index=0,
            teacher_script="t", expected_answer="",
            media={'images': [{'url': '/media/test/parallel-lines.png',
                               'alt': 'x', 'caption': 'x'}]},
        )
        tutor = self._make_tutor(self.geo_lesson, [geo_step])
        self.assertEqual(tutor._build_figure_facts_block(), '')

    def test_block_empty_when_step_has_no_media(self):
        bare_step = LessonStep.objects.create(
            lesson=self.lesson, phase='explore', step_type='explore',
            order_index=99, teacher_script="t", expected_answer="",
            media={},
        )
        tutor = self._make_tutor(self.lesson, [bare_step])
        self.assertEqual(tutor._build_figure_facts_block(), '')

    def test_block_renders_scene_description(self):
        tutor = self._make_tutor(self.lesson, [self.step])
        block = tutor._build_figure_facts_block()
        self.assertIn('<figure_facts', block)
        self.assertIn('Two horizontal parallel lines', block)

    def test_block_renders_labelled_features_with_colors(self):
        tutor = self._make_tutor(self.lesson, [self.step])
        block = tutor._build_figure_facts_block()
        self.assertIn('"1"', block)
        self.assertIn('top-left of upper intersection', block)
        self.assertIn('(blue)', block)

    def test_block_renders_relationships_with_correct_phrasing(self):
        tutor = self._make_tutor(self.lesson, [self.step])
        block = tutor._build_figure_facts_block()
        # Equal relationship phrased "are CORRESPONDING (equal)"
        self.assertIn('Angles 1 and 5 are CORRESPONDING (equal)', block)
        # Sum relationship phrased "are CO INTERIOR (sum to 180°)"
        self.assertIn('Angles 3 and 5 are CO INTERIOR (sum to 180°)', block)

    def test_block_renders_anchor_prompts_verbatim(self):
        tutor = self._make_tutor(self.lesson, [self.step])
        block = tutor._build_figure_facts_block()
        self.assertIn('Anchor prompts you may use VERBATIM', block)
        self.assertIn(
            'Look at angles 1 and 5 — what do you notice about their position?',
            block,
        )

    def test_block_includes_anti_imagination_rule(self):
        tutor = self._make_tutor(self.lesson, [self.step])
        block = tutor._build_figure_facts_block()
        self.assertIn('PROMPT VISUALISATION, NOT IMAGINATION', block)
        self.assertIn('NEVER', block)
        # Forbid the specific phrase patterns
        self.assertIn('imagine', block.lower())
        self.assertIn('picture this', block.lower())

    def test_block_includes_anchor_scaffolding_rule(self):
        tutor = self._make_tutor(self.lesson, [self.step])
        block = tutor._build_figure_facts_block()
        self.assertIn('ANCHOR YOUR SCAFFOLDING', block)
        self.assertIn('VERIFY CLAIMS AGAINST', block)
        self.assertIn('PREFER ANCHOR PROMPTS', block)
        self.assertIn('HONEST UNCERTAINTY', block)

    def test_block_skips_assets_without_figure_facts(self):
        # New asset, same URL, but figure_facts=None — no block
        no_facts_asset = MediaAsset(
            institution=self.institution,
            title='other.png',
            asset_type=MediaAsset.AssetType.IMAGE,
            figure_facts=None,
        )
        no_facts_asset.file.name = 'test/other.png'
        no_facts_asset.save()
        bare_step = LessonStep.objects.create(
            lesson=self.lesson, phase='explore', step_type='explore',
            order_index=88,
            teacher_script="t", expected_answer="",
            media={'images': [{'url': '/media/test/other.png',
                               'alt': 'x', 'caption': 'x'}]},
        )
        tutor = self._make_tutor(self.lesson, [bare_step])
        self.assertEqual(tutor._build_figure_facts_block(), '')
