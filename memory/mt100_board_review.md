# mt100 board — independent review (2026-08-12)

Review of the 18-arm multi-turn board in
`offline_eval/multi_turn_results/mt100/`. Every number below was recomputed
from the raw result JSONs, not read off the README. The recompute script is at
the bottom.

**Verdict**: the run is clean and the reported numbers are exact. The
*interpretation* needs revising — the board's pass column is close to a pure
pacing check, and no cloud arm is statistically separable from any other.

---

## 1. What reproduces

All 18 pass rates and all 18 rubric means match the README exactly.

- One engine (`simple_tutor`), one rubric judge (`claude-sonnet-4-6`), one
  student-sim (`claude-haiku-4-5`), 100 shared scenario ids across every arm.
- Header `passed`/`errored` counts agree with a recount of `results[]`.
- Rubric means recomputed from `rubric_result.mean_score` match to ±0.005.
- Two rubric-judge errors (one each on `claude-opus-4-7`, `claude-sonnet-4-6`)
  in addition to the single scenario error the README already notes.

**Git SHA split, and why it is benign.** The arms did not all run on one
revision: `qwen3.5-2b`, `qwen3-4b` and `qwen3-8b` ran on `8e849745d70d`, the
other 15 on `938e7045ecac`. The diff between them is
`infra/ollama/Modelfile.qwen3-30b-a3b-jetson`, `MT100_RUNBOOK.md` and a test
file — no engine code, and the 30B arm ran on the fixed revision. Not a
confound, but the README should say so rather than leave a reader to check.

---

## 2. The board's pass column measures pacing, not tutoring quality

This is the finding that changes how the table should be read.

Across all 18 arms there are **185 failed assertions, and 181 are
`max_turn_count`**:

| failed assertion | count |
|---|---|
| `max_turn_count` | 181 |
| `expected_reason` | 2 |
| `no_repeated_tutor_phrase_within_window` | 1 |
| `no_tool_syntax_in_any_turn` | 1 |

In the cloud tier, **all 105 turn-limit failures carry
`sim_reason=exit_ticket`** — every one reached the exit ticket, i.e. completed
the lesson. Their mean rubric is 0.871, against arm-level means of 0.86–0.94.
24% are over budget by exactly one turn; 34% by two or fewer.

Pass and rubric are near-independent in the cloud tier:

- point-biserial *r*(pass, rubric) = **0.223**, r² = 0.05
- mean rubric | passed = 0.899, | failed = 0.820
- **32% of failed sessions scored at or above the median rubric of passing ones**

So `pass` ≈ "finished inside the reference turn count" and `rubric` ≈ "taught
well". Presenting them as adjacent columns of one board invites reading rank as
quality.

**claude-opus-5 is the clean illustration** — and resolves the README's "worth a
second look". It has the board's best rubric (0.936) and ranks 10th on pass.
Eight of its nine failures are `max_turn_count`, all completing at the exit
ticket, with rubrics of 0.86, 0.90, 0.92, 0.94, 0.97, 0.98, 0.98. Only one
failure is a genuine quality miss (`short_session_probe_resistant_1143_16`,
rubric 0.51, stopped at `max_turns`). It is not teaching worse than opus-4-7; it
is teaching longer.

### The assertion itself

`evals/gen_multi_turn.py:195` emits `max_turn_count` from the same `max_turns`
value used for the scenario — the reference session's length. In practice the
two have drifted apart in the dataset: `straight_line_average_1142_06` declares
`max_turns: 20` but `max_turn_count: 6`, so the harness lets a session run to 20
turns and then fails it for exceeding 6.

Whether "same lesson, more turns" is worse teaching is a design question worth
answering explicitly. It is defensible for lab-time budgeting; it is not a
quality measure, and right now it decides 98% of pass/fail.

---

## 3. No cloud arm is distinguishable from any other

All arms ran the same 100 scenarios, so this is paired data and McNemar applies
(exact binomial on discordant pairs). Against the top arm, `claude-opus-4-7`
(94%):

| vs | top wins | other wins | p |
|---|---|---|---|
| qwen3.6-27b-instruct | 2 | 2 | 1.000 |
| claude-sonnet-4-6 | 4 | 3 | 1.000 |
| gemini-3.5-flash | 3 | 2 | 1.000 |
| gemini-2.5-flash | 3 | 1 | 0.625 |
| gemini-3.1-pro / gpt-5.4-mini / gpt-5.6-luna | 4 | 2 | 0.688 |
| gpt-5.6-sol | 5 | 3 | 0.727 |
| claude-opus-5 | 5 | 2 | 0.453 |
| claude-haiku-4-5 / claude-sonnet-5 | 8 | 3 | 0.227 |
| gpt-5.6-terra | 6 | 1 | 0.125 |
| gpt-5.4-nano | 8 | 2 | 0.109 |

**Not one comparison reaches p<0.05.** Even the widest gap on the board, 94% vs
88%, is p=0.109. Wilson intervals overlap heavily throughout: opus-4-7 is
[87.5, 97.2], gpt-5.4-nano [80.2, 93.0].

The ranking has little to work with. Only **27 of 100 scenarios discriminate
among the 13 cloud arms** — 71 pass on all of them, 2 fail on all of them.

### Ceiling effect

Excluding turn-limit-only failures, the cloud tier collapses to **98–100%**:

| arm | raw | excl. turn-limit |
|---|---|---|
| gemini-3.5-flash / gemini-2.5-flash / gemini-3.1-pro | 92–93% | 100% |
| claude-opus-4-7, qwen3.6-27b-instruct, gpt-5.4-mini, gpt-5.6-luna/sol, claude-opus-5, claude-sonnet-5 | 89–94% | 99% |
| claude-sonnet-4-6, claude-haiku-4-5, gpt-5.6-terra, gpt-5.4-nano | 88–93% | 98% |

The benchmark has no headroom left for cloud models on tutoring quality.

---

## 4. Two scenarios are dead weight

`straight_line_average_1142_06` and `straight_line_average_1145_12` both carry
`max_turn_count: 6` and fail **18/18 arms**. No model, cloud or local, completes
them in six turns. They carry zero discriminating information and subtract a
flat 2 points from every arm.

Next most-tripped: `error_prone_straight_line_math_001` and
`session_completion_struggler_1144_12` (15/18 each, limit 15).

The `max_turn_count` distribution across the 100 v2 scenarios is
{6: 12, 12: 17, 15: 32, 24: 20, 25: 1, 30: 18}. The twelve limit-6 items are
where the pressure concentrates.

---

## 5. Two README claims that need softening

Both are in "Two results worth a second look".

1. **"The 30B MoE lands below the dense 4B and 8B (61% vs 65%/74%)."**
   Below the 8B is real: 23-10 discordant, **p=0.035**.
   Below the 4B is **not**: 15-19 discordant, **p=0.61**. The 4B/30B gap is
   noise, so the puzzle is narrower than stated — the 30B trails the *8B*, and
   is indistinguishable from the 4B.

2. **The Qwen ladder ordering 8B > 4B** (74 vs 65) is also not significant:
   17-8 discordant, **p=0.108**. Directionally right, not established.

---

## 6. What holds up

- **qwen3.6-27b-instruct ties the best cloud arm.** 2-2 discordant against
  claude-opus-4-7, p=1.0. Genuinely indistinguishable, and on a lower rubric
  (0.862 vs 0.905) — clears the bar as often, less polished per turn, exactly as
  the README says. This is the result worth publishing.
- **Cloud ≫ best Jetson-viable (8B)**: 22-2 discordant, p<0.0001.
- **27B ≫ 8B**: 21-1, p<0.0001. **4B ≫ 2B**: 50-4, p<0.0001.
- The judge-misconfiguration catch, the restart, and the archived
  `mt100_localjudge_archive/` control (4B grader ~4 points more lenient) are
  exactly right, and the "no cloud judge available" log-line caveat is the kind
  of thing that saves a future reader a wasted afternoon.

---

## 7. Recommendations

1. **Split the board.** Report `pass` and `rubric` as two tables, or drop
   `max_turn_count` from the pass criterion and give it its own "within turn
   budget" column. As it stands one column is pacing and the other is quality,
   and the rank order comes from the pacing one.
2. **Band the cloud tier.** Add Wilson intervals or an explicit
   "statistically indistinguishable" grouping so 88–94% reads as one group
   rather than fourteen ranks.
3. **Fix or drop the two 18/18-fail items**, and revisit whether
   `max_turn_count` should equal the reference length or reference × a factor.
4. **The benchmark is saturated for cloud models.** More arms will not
   discriminate; harder scenarios will. The 27 discriminating items are the ones
   worth studying and multiplying.
5. Record the git-SHA split in the README so the next reader does not have to
   verify it themselves.

---

## Reproduction

```bash
./venv/bin/python - <<'PY'
import glob, json, os, math, statistics
from collections import Counter

arms, rub = {}, {}
for f in sorted(glob.glob('offline_eval/multi_turn_results/mt100/*.json')):
    n = os.path.basename(f)[:-5]
    res = json.load(open(f))['results']
    arms[n] = {r['scenario_id']: bool(r.get('passed')) for r in res}
    rub[n] = statistics.mean([r['rubric_result']['mean_score'] for r in res
                              if isinstance(r.get('rubric_result'), dict)])
ids = sorted(arms['claude-opus-5'])

def mcnemar(a, b):
    w = sum(1 for i in ids if arms[a][i] and not arms[b][i])
    l = sum(1 for i in ids if arms[b][i] and not arms[a][i])
    n = w + l
    if not n:
        return w, l, 1.0
    k = min(w, l)
    return w, l, min(1.0, 2*sum(math.comb(n, j) for j in range(k+1)) / 2**n)

top = max(arms, key=lambda a: sum(arms[a].values()))
for a in sorted(arms, key=lambda a: -sum(arms[a].values())):
    w, l, p = mcnemar(top, a)
    print(f'{a:26} {sum(arms[a].values()):>3}%  rubric {rub[a]:.3f}  p_vs_top={p:.3f}')

# what the failures actually are
names = Counter()
for f in glob.glob('offline_eval/multi_turn_results/mt100/*.json'):
    for r in json.load(open(f))['results']:
        if not r.get('passed'):
            for x in r['assertion_results']:
                if not x.get('passed'):
                    names[x.get('name')] += 1
print('\nfailed assertions by name:', dict(names))
PY
```

Data: `offline_eval/multi_turn_results/mt100/*.json` (18 arms × 100 scenarios,
run 2026-08-10 → 2026-08-12, git `938e7045ecac` / `8e849745d70d`).
Source README: `offline_eval/multi_turn_results/mt100/README.md`.
