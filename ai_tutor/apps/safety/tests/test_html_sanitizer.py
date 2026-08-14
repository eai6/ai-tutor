"""Finding F-07 — LLM-generated figure markup is rendered, not trusted.

Two halves, and both matter equally:

* the attack payloads must not survive, and
* legitimate figures must come through intact. A sanitiser that blanks every
  chart is a worse outage than the bug it fixes, and it is the failure mode
  nobody notices until a class sits an exam.
"""
from __future__ import annotations

import re
from pathlib import Path

from django.template import Context, Template
from django.test import SimpleTestCase

from ai_tutor.apps.safety.html_sanitizer import (
    sanitize_answer_data,
    sanitize_figure_html,
)

BASE_DIR = Path(__file__).resolve().parents[4]
TEMPLATE_DIR = BASE_DIR / 'ai_tutor' / 'templates'


class ScriptExecutionIsRemovedTests(SimpleTestCase):
    """Nothing that can run code survives."""

    PAYLOADS = [
        ('inline script', '<div>chart<script>alert(1)</script></div>', 'alert'),
        ('svg script', '<svg><script>alert(1)</script><path d="M0 0"/></svg>', 'alert'),
        ('img onerror', '<img src=x onerror="alert(1)">', 'onerror'),
        ('body onload', '<div onmouseover="alert(1)">hover</div>', 'onmouseover'),
        ('javascript url', '<img src="javascript:alert(1)">', 'javascript:'),
        ('svg foreignObject', '<svg><foreignObject><img src=x onerror=alert(1)>'
                              '</foreignObject></svg>', 'onerror'),
        ('svg use', '<svg><use xlink:href="data:image/svg+xml;base64,AAA"/></svg>', 'use'),
        ('svg animate href', '<svg><animate attributeName="href" '
                             'values="javascript:alert(1)"/></svg>', 'javascript'),
        ('iframe', '<iframe src="https://evil.example"></iframe>', 'iframe'),
        ('object', '<object data="evil.swf"></object>', 'object'),
        ('anchor js', '<a href="javascript:alert(1)">click</a>', 'javascript:'),
        ('form', '<form action="https://evil.example"><input name="p"></form>', 'form'),
        ('style element', '<style>body{display:none}</style>', 'display:none'),
        ('meta refresh', '<meta http-equiv="refresh" content="0;url=https://evil">', 'refresh'),
        ('base tag', '<base href="https://evil.example/">', 'base'),
    ]

    def test_payloads_do_not_survive(self):
        for label, payload, forbidden in self.PAYLOADS:
            with self.subTest(payload=label):
                cleaned = sanitize_figure_html(payload)
                self.assertNotIn(forbidden, cleaned,
                                 f'{label}: {forbidden!r} survived in {cleaned!r}')

    def test_script_body_is_removed_not_left_as_text(self):
        """Dropping only the tag would leave the source visible on the page."""
        cleaned = sanitize_figure_html('<div><script>var secret=1;</script>ok</div>')
        self.assertNotIn('secret', cleaned)
        self.assertIn('ok', cleaned)

    def test_no_event_handler_attribute_survives_any_element(self):
        for handler in ('onclick', 'onerror', 'onload', 'onmouseover', 'onfocus'):
            with self.subTest(handler=handler):
                cleaned = sanitize_figure_html(
                    f'<rect {handler}="alert(1)" width="10" height="10"/>')
                self.assertNotIn(handler, cleaned)

    def test_style_attribute_cannot_carry_a_url(self):
        """Not script execution, but it turns a figure into a tracking beacon."""
        cleaned = sanitize_figure_html(
            '<div style="max-width:100%;background:url(https://evil.example/x)">c</div>')
        self.assertNotIn('evil.example', cleaned)
        self.assertIn('max-width', cleaned)

    def test_style_attribute_cannot_overlay_the_page(self):
        cleaned = sanitize_figure_html(
            '<div style="position:fixed;top:0;left:0;width:100vw;height:100vh">x</div>')
        self.assertNotIn('position', cleaned)
        self.assertNotIn('fixed', cleaned)


class LegitimateFiguresSurviveTests(SimpleTestCase):
    """The half that protects the product rather than the student."""

    def test_the_generators_own_figure_html_is_preserved(self):
        """Exactly what content_generator.py builds for a matched KB figure."""
        source = (
            "<div style='text-align:center;margin-bottom:8px;'>"
            "<img src='/media/figures/graph.png' style='max-width:100%;border-radius:4px;' "
            "alt='Rainfall by month'>"
            "<div style='font-size:11px;color:#71717a;margin-top:4px;'>"
            "Source: geography_f3.pdf</div>"
            "</div>"
            "<div style='font-size:13px;'>Study the graph and answer.</div>"
        )
        cleaned = sanitize_figure_html(source)
        self.assertIn('/media/figures/graph.png', cleaned)
        self.assertIn('Rainfall by month', cleaned)
        self.assertIn('geography_f3.pdf', cleaned)
        self.assertIn('max-width', cleaned)
        self.assertIn('text-align', cleaned)

    def test_a_data_table_is_preserved(self):
        source = (
            '<table style="border-collapse:collapse;width:100%">'
            '<caption>Population by district</caption>'
            '<thead><tr><th scope="col">District</th><th scope="col">2020</th></tr></thead>'
            '<tbody><tr><td>Victoria</td><td colspan="1">26,450</td></tr></tbody>'
            '</table>'
        )
        cleaned = sanitize_figure_html(source)
        for fragment in ('<table', '<caption', '<thead', '<th', 'scope="col"',
                         '<tbody', 'colspan="1"', 'Victoria', '26,450',
                         'border-collapse'):
            self.assertIn(fragment, cleaned)

    def test_an_inline_svg_chart_is_preserved(self):
        source = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 120" width="200">'
            '<defs><linearGradient id="g"><stop offset="0%" stop-color="#fff"/>'
            '</linearGradient></defs>'
            '<g transform="translate(10,10)">'
            '<rect x="0" y="0" width="30" height="80" fill="#4ECDC4" stroke="#333" '
            'stroke-width="1"/>'
            '<line x1="0" y1="90" x2="180" y2="90" stroke="#333"/>'
            '<polyline points="0,80 30,60 60,20" fill="none" stroke="#FF6B35"/>'
            '<text x="5" y="100" font-size="10" text-anchor="middle">Jan</text>'
            '</g></svg>'
        )
        cleaned = sanitize_figure_html(source)
        for fragment in ('<svg', 'viewBox="0 0 200 120"', '<linearGradient',
                         'stop-color', '<g', 'transform="translate(10,10)"',
                         '<rect', 'fill="#4ECDC4"', 'stroke-width="1"',
                         '<polyline', 'points=', '<text', 'text-anchor',
                         'Jan'):
            self.assertIn(fragment, cleaned, f'{fragment!r} lost from SVG')

    def test_relative_media_urls_survive(self):
        """Figures are served from our own domain; denying these blanks them all."""
        cleaned = sanitize_figure_html('<img src="/media/platform_logos/x.png">')
        self.assertIn('/media/platform_logos/x.png', cleaned)

    def test_data_uri_images_survive(self):
        cleaned = sanitize_figure_html(
            '<img src="data:image/png;base64,iVBORw0KGgo=">')
        self.assertIn('data:image/png;base64', cleaned)

    def test_plain_text_is_untouched(self):
        self.assertEqual(sanitize_figure_html('Study the table below.'),
                         'Study the table below.')


class EdgeCaseTests(SimpleTestCase):

    def test_non_string_input_is_empty(self):
        for value in (None, 0, [], {}, {'a': 1}):
            with self.subTest(value=value):
                self.assertEqual(sanitize_figure_html(value), '')

    def test_answer_data_helper_cleans_only_the_markup_fields(self):
        original = {
            'data_description': '<div onclick="alert(1)">Table</div>',
            'figure_svg': '<svg><script>alert(1)</script></svg>',
            'source': '<p onmouseover="alert(1)">Extract</p>',
            'model_answer': 'The answer is 42 <not markup>',
            'keywords': ['a', 'b'],
        }
        cleaned = sanitize_answer_data(original)

        self.assertNotIn('onclick', cleaned['data_description'])
        self.assertNotIn('alert', cleaned['figure_svg'])
        self.assertNotIn('onmouseover', cleaned['source'])
        # Non-markup fields pass through untouched — they are escaped by the
        # frontend, and rewriting them here would corrupt answer keys.
        self.assertEqual(cleaned['model_answer'], original['model_answer'])
        self.assertEqual(cleaned['keywords'], original['keywords'])

    def test_answer_data_helper_does_not_mutate_its_input(self):
        original = {'source': '<p onclick="alert(1)">x</p>'}
        sanitize_answer_data(original)
        self.assertIn('onclick', original['source'],
                      'the stored row must not be rewritten as a side effect')

    def test_answer_data_helper_tolerates_junk(self):
        for value in (None, '', [], 'string'):
            with self.subTest(value=value):
                self.assertEqual(sanitize_answer_data(value), {})


class TemplateFilterTests(SimpleTestCase):

    def test_filter_sanitises_and_marks_safe(self):
        rendered = Template(
            '{% load safe_html %}{{ value|sanitized }}'
        ).render(Context({'value': '<div onclick="alert(1)">Chart</div>'}))
        self.assertIn('Chart', rendered)
        self.assertNotIn('onclick', rendered)
        # Marked safe, so the surviving markup is not double-escaped.
        self.assertIn('<div>', rendered)

    def test_filter_does_not_escape_legitimate_markup(self):
        rendered = Template(
            '{% load safe_html %}{{ value|sanitized }}'
        ).render(Context({'value': '<table><tr><td>1</td></tr></table>'}))
        self.assertIn('<table>', rendered)
        self.assertNotIn('&lt;table&gt;', rendered)


class NoUnsanitisedFigureRenderTests(SimpleTestCase):
    """The three fields may never be rendered with |safe again."""

    FIELDS = ('figure_svg', 'data_description', 'source')

    def test_no_template_renders_a_figure_field_with_safe(self):
        pattern = re.compile(
            r'\{\{\s*[\w.]*\b(?:' + '|'.join(self.FIELDS) + r')\s*\|\s*safe\s*\}\}')
        offenders = []
        for path in TEMPLATE_DIR.rglob('*.html'):
            text = path.read_text()
            for match in pattern.finditer(text):
                line = text[:match.start()].count('\n') + 1
                offenders.append(f'{path.relative_to(BASE_DIR)}:{line}')
        self.assertEqual(offenders, [], (
            'These render generated markup raw. Use |sanitized (finding F-07).'
        ))

    def test_every_template_using_the_filter_loads_the_library(self):
        offenders = []
        for path in TEMPLATE_DIR.rglob('*.html'):
            text = path.read_text()
            if '|sanitized' not in text:
                continue
            if not re.search(r'\{%\s*load\b[^%]*\bsafe_html\b', text):
                offenders.append(str(path.relative_to(BASE_DIR)))
        self.assertEqual(offenders, [], 'missing {% load safe_html %}')


class ClientSideFailClosedTests(SimpleTestCase):
    """The exit-ticket modal must not fall back to raw innerHTML.

    Server-side sanitising makes this defence in depth rather than the only
    control, but the pattern is wrong regardless — chat_tutor.html carries a
    comment explaining why it was removed there, and the same fallback survived
    in the modal.
    """

    MODAL = TEMPLATE_DIR / 'tutoring' / '_partials' / 'exit_modal.html'

    def test_no_raw_interpolation_of_generated_markup(self):
        text = self.MODAL.read_text()
        for field in ('q.source', 'ad.data_description'):
            with self.subTest(field=field):
                # Bare `${q.source}` inside a template literal is the bug; the
                # escaped form is what should appear.
                self.assertNotIn('${' + field + '}', text,
                                 f'{field} is interpolated without escapeHtml')

    def test_dompurify_absence_is_handled_explicitly(self):
        text = self.MODAL.read_text()
        self.assertIn('!window.DOMPurify', text,
                      'the modal must check for DOMPurify and fail closed')
