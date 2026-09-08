"""The header and footer shared by the landing page and the documentation site.

Both are includes precisely because a link that exists on one public page and
not the other is invisible until someone reports it, so these assertions run
against both pages rather than one.
"""

import pytest
from django.urls import reverse

PUBLIC_PAGES = ['accounts:landing', 'docs:index']


def _split_at_footer(body):
    """(everything above the footer, the footer). The header and the footer
    are the two halves this file keeps apart, and `<footer` is the only
    boundary between them that does not depend on a class name."""
    head, sep, foot = body.partition('<footer')
    assert sep, 'no <footer> on the page — the split below would be meaningless'
    return head, foot


@pytest.mark.django_db
@pytest.mark.parametrize('page', PUBLIC_PAGES)
def test_the_header_carries_all_three_sign_in_doors(client, page):
    head, _ = _split_at_footer(client.get(reverse(page)).content.decode())

    for door in ('accounts:student_login', 'accounts:staff_login',
                 'accounts:country_login'):
        assert reverse(door) in head, f'{door} missing from the header of {page}'
    assert 'Enterprise sign in' in head


@pytest.mark.django_db
@pytest.mark.parametrize('page', PUBLIC_PAGES)
def test_the_language_picker_is_in_the_header_not_the_footer(client, page):
    """It moved out of the footer: choosing what to read in is a decision
    someone makes on arrival, not one they scroll to the bottom for."""
    head, foot = _split_at_footer(client.get(reverse(page)).content.decode())

    assert 'data-lang-switch' in head, f'no language picker in the header of {page}'
    assert 'data-lang-switch' not in foot, f'a second picker is still in the footer of {page}'


@pytest.mark.django_db
@pytest.mark.parametrize('page', PUBLIC_PAGES)
def test_the_picker_still_works_without_javascript(client, page):
    """static/js/lang-switch.js hides the submit button and submits on change.
    The button has to be in the HTML for the offline classroom build, where a
    blocked or slow script would otherwise leave a select nobody can act on."""
    head, _ = _split_at_footer(client.get(reverse(page)).content.decode())

    form = head[head.index('data-lang-switch'):]
    form = form[:form.index('</form>')]
    assert reverse('set_language') in head
    assert 'data-lang-go' in form, 'the no-JS submit button is gone'
    assert 'type="submit"' in form


@pytest.mark.django_db
def test_the_hero_carries_the_enterprise_door_too(client):
    """Above the fold as well as in the header — the enterprise reader is the
    one least likely to scroll to the cards to find out this is for them."""
    body = client.get(reverse('accounts:landing')).content.decode()
    hero = body[body.index('id="start"'):]
    hero = hero[:hero.index('</section>')]

    assert reverse('accounts:country_login') in hero
    assert 'Enterprise' in hero
