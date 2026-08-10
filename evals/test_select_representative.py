import collections
from evals.select_representative import load_scenarios, select

AXES = ('persona', 'subject', 'kind')


def test_selects_exactly_100_and_is_deterministic():
    rows = load_scenarios()
    assert len(rows) == 200
    a = [r['id'] for r in select(rows)]
    b = [r['id'] for r in select(rows)]
    assert len(a) == 100
    assert a == b, "same seed must give the same draw"


def test_retains_every_v1_scenario():
    rows = load_scenarios()
    chosen = {r['id'] for r in select(rows)}
    v1 = {r['id'] for r in rows if r['v1']}
    assert len(v1) == 30
    assert v1 <= chosen, "the mt30 board must stay a sub-board of mt100"


def test_every_axis_within_two_of_proportional_ideal():
    rows = load_scenarios()
    chosen = select(rows)
    for axis in AXES:
        pop = collections.Counter(r[axis] for r in rows)
        got = collections.Counter(r[axis] for r in chosen)
        for level, n in pop.items():
            ideal = n * len(chosen) / len(rows)
            assert abs(got[level] - ideal) <= 2, (
                f"{axis}={level}: {got[level]} vs ideal {ideal:.1f}")


def test_covers_every_lesson():
    rows = load_scenarios()
    chosen = select(rows)
    assert {r['lesson'] for r in chosen} == {r['lesson'] for r in rows}
