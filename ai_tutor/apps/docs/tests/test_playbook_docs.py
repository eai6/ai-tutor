"""The public documentation site.

Two things break here without anyone noticing, and both are covered below.

The first is a link that leads nowhere. The section index is hand-written and
the prose is generated from a Word file by a script nobody runs on every
change, so the index and the partials drift apart the moment a heading is
renamed in the .docx. A page that 404s from the front door is worse than a
missing page, because the front door is what a ministry was sent.

The second is the header link disappearing. It exists on the landing page only
because both public pages now share one include; a future edit that inlines
the header again takes the link with it.
"""
import re
from pathlib import Path

import pytest
from django.template import TemplateDoesNotExist, loader
from django.test import Client
from django.urls import reverse

from ai_tutor.apps.docs import playbook


@pytest.fixture
def client():
    return Client()


class TestSectionIndex:
    """The hand-written index against the generated prose."""

    def test_every_section_has_generated_prose(self):
        missing = []
        for section in playbook.SECTIONS:
            try:
                loader.get_template(f'docs/sections/{section.slug}.html')
            except TemplateDoesNotExist:
                missing.append(section.slug)
        assert not missing, (
            f'no generated partial for {missing} — rerun '
            'scripts/build_playbook_docs.py against the .docx'
        )

    def test_every_generated_partial_is_reachable(self):
        """The other direction: a section converted but never indexed is a
        page that exists and cannot be found."""
        generated = {
            p.stem for p in
            (Path(__file__).resolve().parents[3] / 'templates' / 'docs' / 'sections')
            .glob('*.html')
        }
        assert generated - set(playbook.BY_SLUG) == set()

    def test_slugs_and_labels_are_unique(self):
        slugs = [s.slug for s in playbook.SECTIONS]
        labels = [s.label for s in playbook.SECTIONS]
        assert len(set(slugs)) == len(slugs)
        assert len(set(labels)) == len(labels)

    def test_every_card_link_resolves(self):
        """Card membership is written by slug string, so a rename here is a
        KeyError at import — but the footer link is resolved lazily."""
        for card in playbook.CARDS:
            assert card.sections, f'{card.title} has no sections'
            if not card.more_url_name:
                assert card.more_section is not None, (
                    f'{card.title} points at an unknown section')

    def test_reading_order_has_no_gaps_at_the_ends(self):
        first, last = playbook.SECTIONS[0], playbook.SECTIONS[-1]
        assert playbook.neighbours(first)[0] is None
        assert playbook.neighbours(last)[1] is None
        # And the chain is continuous in between.
        for i, section in enumerate(playbook.SECTIONS[1:-1], start=1):
            previous, following = playbook.neighbours(section)
            assert previous is playbook.SECTIONS[i - 1]
            assert following is playbook.SECTIONS[i + 1]


@pytest.mark.django_db
class TestPages:

    def test_index_is_public(self, client):
        response = client.get(reverse('docs:index'))
        assert response.status_code == 200
        body = response.content.decode()
        assert 'Country Adoption' in body
        # Every section is listed somewhere on the page, card or contents.
        for section in playbook.SECTIONS:
            assert reverse('docs:section', args=[section.slug]) in body

    def test_every_section_page_renders(self, client):
        for section in playbook.SECTIONS:
            response = client.get(reverse('docs:section', args=[section.slug]))
            assert response.status_code == 200, section.slug
            assert section.title in response.content.decode(), section.slug

    def test_unknown_section_is_a_404_not_a_template_error(self, client):
        assert client.get('/docs/not-a-section/').status_code == 404

    def test_head_is_answered(self, client):
        """Link previewers and uptime checks probe with HEAD; require_GET
        answers those with 405."""
        assert client.head(reverse('docs:index')).status_code == 200
        assert client.head(reverse('docs:section', args=['costs'])).status_code == 200

    def test_search_index_covers_every_section(self, client):
        response = client.get(reverse('docs:search_index'))
        assert response.status_code == 200
        data = response.json()
        assert set(data) == set(playbook.BY_SLUG)
        # The point of the index: prose that appears on no card or heading.
        assert 'pgvector' in data['hosting']
        assert 'jetson' in data['adoption-models']

    def test_a_section_carries_the_documents_own_content(self, client):
        """A spot check that the conversion produced the document, not an
        empty shell: the cost table, an aside and a shell block."""
        body = client.get(reverse('docs:section', args=['hosting'])).content.decode()
        assert 'docker compose pull' in body
        assert 'doc-note' in body
        assert 'doc-table' in body


@pytest.mark.django_db
class TestHeaderLink:

    def test_landing_page_links_to_the_playbook(self, client):
        body = client.get(reverse('accounts:landing')).content.decode()
        assert reverse('docs:index') in body
        assert 'Country Adoption' in body

    def test_playbook_marks_itself_current(self, client):
        body = client.get(reverse('docs:index')).content.decode()
        link = re.search(r'<a href="/docs/" class="lp-header__link"[^>]*>', body)
        assert link and 'aria-current="page"' in link.group(0)

    def test_both_public_pages_carry_the_home_link(self, client):
        # The header is shared so that neither page can quietly lose a link.
        # This is the assertion that makes that true rather than intended.
        landing = reverse('accounts:landing')
        for url in (landing, reverse('docs:index')):
            body = client.get(url).content.decode()
            assert re.search(
                r'<a href="%s" class="lp-header__link"[^>]*>\s*Home' % re.escape(landing),
                body), url

    def test_home_marks_itself_current_on_the_landing_page(self, client):
        landing = reverse('accounts:landing')
        body = client.get(landing).content.decode()
        link = re.search(
            r'<a href="%s" class="lp-header__link"[^>]*>' % re.escape(landing), body)
        assert link and 'aria-current="page"' in link.group(0)


class TestGeneratedProse:
    """Guards on the .docx conversion itself, checked against the output.

    These caught two real defects. Word writes bold-off as ``<w:b w:val="0"/>``
    — an element, not an absence — so reading presence alone marked the whole
    document bold. And every shell block is a single paragraph whose lines are
    ``<w:br/>``, so dropping them concatenates two commands into one that
    cannot be run.
    """

    @pytest.fixture(scope='class')
    @staticmethod
    def prose():
        root = Path(__file__).resolve().parents[3] / 'templates' / 'docs' / 'sections'
        return {p.stem: p.read_text() for p in root.glob('*.html')}

    def test_emphasis_marks_phrases_not_the_whole_document(self, prose):
        """Measured as a share of the text, not as a shape.

        The defect wrapped every run in both <strong> and <em>, and an earlier
        version of this test looked for `<p><strong>` — which the broken
        output never produced, because the italic wrapped the bold. Emphasis
        is a proportion of the prose; assert on the proportion.
        """
        joined = ''.join(prose.values())
        total = len(re.sub(r'<[^>]+>', '', joined))
        emphasised = {
            tag: sum(len(m) for m in
                     re.findall(rf'<{tag}>(.*?)</{tag}>', joined, re.S))
            for tag in ('strong', 'em')
        }
        assert '<strong>' in joined, 'the document does bold key terms'
        for tag, length in emphasised.items():
            assert length < total * 0.25, (
                f'{length / total:.0%} of the prose is <{tag}> — Word writes '
                'bold-off as an element, so presence is not the on state'
            )

    def test_shell_blocks_keep_their_line_breaks(self, prose):
        block = re.search(r'<pre class="doc-code"><code>(.*?)</code></pre>',
                          prose['hosting'], re.S)
        assert block and '\n' in block.group(1)
        assert 'ai-tutordocker' not in prose['hosting']

    def test_tables_have_headers(self, prose):
        for slug, markup in prose.items():
            for table in re.findall(r'<table class="doc-table">.*?</table>',
                                    markup, re.S):
                assert '<thead>' in table, slug
                assert 'scope="row"' in table, slug

    def test_headings_carry_ids_the_contents_rail_can_link_to(self, prose):
        for section in playbook.SECTIONS:
            for anchor, _title in section.headings:
                assert f'id="{anchor}"' in prose[section.slug], \
                    f'{section.slug}#{anchor}'
