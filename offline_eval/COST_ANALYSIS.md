# Cost analysis — offline vs cloud tutoring

**Geography only.** The maths boards are excluded: two of their lessons hold
fewer bank questions than a session consumes, so sessions there end on the turn
cap rather than on the tutor's behaviour. Every arm is depressed by the same
content gap, and including them would price a content defect as a model
difference.

Every number below is computed from files in this repository by
`offline_eval/cost_audit.py`, which prints the full derivation and runs
internal consistency checks. Nothing is a remembered figure.

---

## 1. What was measured

34 scenarios per arm, same tutor build, same simulated students.

| arm | sessions | tutor turns | turns/session | sec/turn (median) |
|---|---:|---:|---:|---:|
| qwen3-4B (on-device) | 34 | 313 | 7.0 | 5.09 |
| qwen3.8-27B (on-device) | 34 | 294 | 7.5 | 19.62 |
| claude-opus-4-7 | 34 | 314 | 9.0 | 4.27 |
| gemini-3.5-flash | 34 | 310 | 8.0 | 10.55 |
| gpt-5.4-mini | 34 | 295 | 7.5 | 1.65 |

*Source: `multi_turn_results/<board>/<arm>.json`, `transcript[].latency_ms`.*

---

## 2. The unit: one student-tutoring-hour

Cost per *session* is not comparable across models, because sessions differ in
length. Cost per *student* hides how much tutoring each one gets. So the
denominator is one **student-tutoring-hour** — one student receiving one hour
of tutoring.

Session length is **not** the time the test harness took: the simulated student
answers instantly, a real one reads and types. So

```
session minutes = turns × (tutor seconds per turn + student think time)
```

with 30 seconds of student time per turn.

| arm | turns | tutor sec | think sec | **minutes/session** |
|---|---:|---:|---:|---:|
| qwen3-4B | 7.0 | 36 | 210 | **4.1** |
| qwen3.8-27B | 7.5 | 147 | 225 | **6.2** |
| claude-opus-4-7 | 9.0 | 38 | 270 | **5.1** |
| gemini-3.5-flash | 8.0 | 84 | 240 | **5.4** |
| gpt-5.4-mini | 7.5 | 12 | 225 | **4.0** |

---

## 3. Cloud cost — metered, from the token counts

Cloud providers bill per token, and the tutor's prompt is cached, so three
input buckets are billed at different rates. They are **disjoint**: a
provider's `input_tokens` excludes cached tokens, so the prompt's true size is
their sum.

```
bill = fresh×rate + cached×rate×0.10 + written×rate×1.25 + output×output_rate
```

| arm | fresh in | cached in | cache writes | input $ | output $ | **total** |
|---|---:|---:|---:|---:|---:|---:|
| claude-opus-4-7 | 1,504,857 | 4,124,127 | 415,837 | 12.19 | 1.05 | **13.23** |
| gemini-3.5-flash | 5,557,243 | 244,212 | 0 | 1.67 | 0.09 | **1.76** |
| gpt-5.4-mini | 3,613,357 | 2,209,408 | 0 | 0.96 | 0.06 | **1.02** |

*Source: `multi_turn_results/geo_cloud/trace/<arm>.jsonl`, `tok_*` fields.
Output tokens are not recorded by the tracer and are estimated from reply
length; they are 6–8% of each bill.*

That is **34 sessions** per arm, so:

| arm | $/session | $/tutoring-hour |
|---|---:|---:|
| claude-opus-4-7 | 0.389 | **4.543** |
| gemini-3.5-flash | 0.052 | **0.575** |
| gpt-5.4-mini | 0.030 | **0.455** |

---

## 4. On-device cost — a card bought once

| | |
|---|---:|
| capital (RTX 3090) | $2,500 |
| useful life | 3 years |
| discount rate | 5% |
| **annualised capital** | **$918/yr** |
| electricity (350 W × 6 h × 190 days × $0.20/kWh) | $80/yr |
| **annual cost of ownership** | **$998/yr** |

Capital is spread over the card's life using a *capital recovery factor* rather
than simply divided by three, because money tied up in the card has an
opportunity cost. The difference is $918 against $833 — small, but it is the
standard treatment.

**How much tutoring does that buy?** At 300 students × 200 sessions/year:

| arm | tutoring hours/yr | avg students online | capacity ceiling | cards needed | **$/hour** |
|---|---:|---:|---:|---:|---:|
| qwen3-4B | 4,094 | 3.6 | 48 | 1 | **0.244** |
| qwen3.8-27B | 6,202 | 5.4 | 4 | 2 | **0.322** |

The roll is never online together: about **4 students of 300** are tutoring at
any instant. Demand above one card's capacity is met by buying another card,
not by serving fewer students — which is why the 27B needs two.

---

## 5. The comparison

| option | $/tutoring-hour |
|---|---:|
| human teacher, 1:40 class | 0.037 |
| **on-device qwen3-4B** | **0.244** |
| on-device qwen3.8-27B | 0.322 |
| cloud gpt-5.4-mini | 0.455 |
| cloud gemini-3.5-flash | 0.575 |
| **human tutor, 1:1** | **1.500** |
| cloud claude-opus-4-7 | 4.543 |

*Human cost uses $1.50/hour, the mid-point of published Tanzanian secondary
teacher pay ($0.79–$2.30/hour, WageIndicator 2025, 45-hour week).*

**Read this against 1:1 tutoring, not against the classroom.** A teacher with
40 pupils is cheaper per student-hour than any machine and always will be —
that is what a large class buys. But a class of 40 is not individual tutoring.
The service being delivered here is one-to-one, and against one-to-one human
tutoring the on-device 4B is **$0.244 against $1.50 — six times cheaper**, and
the 27B, at near-frontier teaching quality, is nearly five times cheaper.

Individual tutoring is scarce not because it fails but because its cost rises
in direct proportion to the hours delivered. A bought card does not.

---

## 6. What a school pays

| option | 150 pupils: total | per pupil | 300 pupils: total | per pupil |
|---|---:|---:|---:|---:|
| **on-device qwen3-4B** | **$998** | $6.65 | **$998** | **$3.33** |
| on-device qwen3.8-27B | $998 | $6.65 | $1,996 | $6.65 |
| cloud gpt-5.4-mini | $901 | $6.01 | $1,802 | $6.01 |
| cloud gemini-3.5-flash | $1,555 | $10.36 | $3,109 | $10.36 |
| cloud claude-opus-4-7 | $11,676 | $77.84 | $23,351 | $77.84 |

**This table is the cost argument.** On-device totals are flat as the roll
grows — the same card serves 300 pupils as serves 150 — so cost per pupil
halves. Cloud totals rise in direct proportion, so cost per pupil never
falls. At 150 pupils the cheapest cloud option is competitive; at 300 it is
not; and the gap widens with every additional pupil and every additional year
the card keeps working.

---

## 7. Cost against teaching quality

Quality is the human-graded pass rate over 169 hand-graded sessions.

| option | $/hour | graded | pass rate | $/quality-adjusted hour |
|---|---:|---:|---:|---:|
| on-device qwen3-4B | 0.244 | 34 | 68% | **0.360** |
| on-device qwen3.8-27B | 0.322 | 34 | 97% | **0.332** |
| cloud gpt-5.4-mini | 0.455 | 33 | 88% | 0.518 |
| cloud gemini-3.5-flash | 0.575 | 34 | 71% | 0.815 |
| cloud claude-opus-4-7 | 4.543 | 34 | 100% | 4.543 |

The two on-device options are the most cost-effective, and the 27B is the
better of them once quality is counted: it costs 32% more per hour than the 4B
and delivers a 29-point higher pass rate.

**This is a measure of tutoring quality, not of learning.** It records whether
experts judged the tutoring sound, not whether students learned more. A
learning outcome would require a controlled trial, which this work does not
have.

---

## 8. What is and is not counted

| | included | excluded |
|---|---|---|
| on-device | GPU capital (annualised), electricity | host machine, networking, premises, staff time |
| cloud | metered tokens | internet connection, egress |
| human | wage cost to the school | recruitment, training, absence cover |

Excluded from every option: premises, administration, curriculum development,
and the engineering already spent building the system. This is the cost of
*delivering tutoring by each route*, not a full budget for running a school.

---

## 9. What would change the answer

Ranked by influence:

1. **Sessions per student per year.** Scales the cloud column and leaves the
   card untouched. At low usage the cheapest cloud tier wins; at 200/year it
   does not.
2. **Useful life of the card.** 2 to 5 years moves annualised capital from
   $1,345 to $577.
3. **Pupil–teacher ratio** in the human comparison — 1:1 to 1:40 is a factor
   of 40, and decides whether the machine looks cheap or expensive.
4. **Utilisation.** Geography alone uses roughly 8% of the 4B's capacity. The
   card's cost per hour falls as more subjects and more pupils use it; a
   single-subject deployment is the weakest case for the hardware.
5. **Wage level** — $0.79 to $2.30/hour across the published range.

```bash
python offline_eval/cost_audit.py --wage 1.50 --ratio 40 \
    --students 300 --sessions 200 --life 3 --discount 0.05
```
