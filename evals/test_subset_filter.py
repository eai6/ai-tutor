"""`--subset` tag filtering: single tag unchanged, comma-separated ANDs.

The comma form exists so a board can be sliced without minting a new dataset
tag for every slice. `v2` itself had to be minted (evals/select_representative.py)
because the flag could only match one tag; a 30-scenario geography cut of that
same board should not need `v2_geo30` scattered across 30 YAML files.
"""
import pathlib
import re
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_filter():
    """Import filter_by_subset without booting Django.

    run_eval imports BaseCommand at module scope; the helper under test is pure
    and has no Django dependency, so the module source is executed with a stub
    in place rather than requiring settings for a list-comprehension test.
    """
    src = (ROOT / 'apps' / 'tutoring' / 'management' / 'commands'
           / 'run_eval.py').read_text()
    body = src.split('def filter_by_subset', 1)
    assert len(body) == 2, 'filter_by_subset no longer exists in run_eval.py'
    fn_src = 'def filter_by_subset' + body[1].split('\nclass Command', 1)[0]
    ns: dict = {}
    exec(fn_src, ns)
    return ns['filter_by_subset']


filter_by_subset = _load_filter()


def _s(name, tags):
    return types.SimpleNamespace(id=name, tags=tags)


SCENARIOS = [
    _s('geo_a', ['multi_turn', 'geography', 'v2']),
    _s('geo_b', ['multi_turn', 'geography']),
    _s('math_a', ['multi_turn', 'math', 'v2']),
    _s('math_b', ['multi_turn', 'math']),
]


def test_single_tag_behaves_exactly_as_before():
    assert [s.id for s in filter_by_subset(SCENARIOS, 'v2')] == ['geo_a', 'math_a']
    assert [s.id for s in filter_by_subset(SCENARIOS, 'geography')] == ['geo_a', 'geo_b']


def test_comma_separated_tags_and_together():
    assert [s.id for s in filter_by_subset(SCENARIOS, 'v2,geography')] == ['geo_a']
    assert [s.id for s in filter_by_subset(SCENARIOS, 'v2,math')] == ['math_a']


def test_and_is_not_or():
    """The failure that would matter: OR silently doubles the scenario count,
    so a '30 geography scenarios' run quietly becomes a mixed-subject run."""
    assert len(filter_by_subset(SCENARIOS, 'v2,geography')) == 1


def test_whitespace_and_empty_segments_are_tolerated():
    assert [s.id for s in filter_by_subset(SCENARIOS, ' v2 , geography ')] == ['geo_a']
    assert [s.id for s in filter_by_subset(SCENARIOS, 'v2,')] == ['geo_a', 'math_a']


def test_unknown_tag_selects_nothing_rather_than_everything():
    assert filter_by_subset(SCENARIOS, 'v2,typo_tag') == []


# --- against the real dataset, so a retagging that empties a slice is caught --

def _real_multi_turn():
    out = []
    for f in (ROOT / 'evals' / 'dataset').rglob('*.yaml'):
        t = f.read_text()
        if 'mode: multi_turn' not in t:
            continue
        m = re.search(r'(?m)^tags: \[(.*)\]', t)
        out.append(_s(f.stem, [x.strip() for x in m.group(1).split(',')] if m else []))
    return out


@pytest.mark.parametrize('spec,expected', [
    ('v2', 100),
    ('v2,geography', 44),
    ('v2,math', 47),
])
def test_real_dataset_slice_sizes(spec, expected):
    """Pins the slices the mt100 runbook and the geo30/math30 notebooks name.

    v2,geography is 44 and not 48: four geography-SUBJECT scenarios carry no
    `geography` TAG, and --subset matches tags.
    """
    got = len(filter_by_subset(_real_multi_turn(), spec))
    assert got == expected, f'--subset {spec} selects {got}, expected {expected}'


def test_the_two_subject_slices_do_not_overlap():
    """Disjoint, but NOT a partition: 44 + 47 = 91 of the 100 v2 scenarios.
    Nine carry neither subject tag, so "geography + math" is not the whole
    board and a slice-based run leaves those nine unmeasured."""
    real = _real_multi_turn()
    geo = {s.id for s in filter_by_subset(real, 'v2,geography')}
    math = {s.id for s in filter_by_subset(real, 'v2,math')}
    assert not (geo & math), f'a scenario is tagged both subjects: {geo & math}'
    assert len(geo) + len(math) == 91
