# Economic method for the cost analysis

Written for an economics readership. The costing follows the **ingredients
method** (Levin & McEwan), the standard in education cost-effectiveness
analysis, and reports results as **unit costs** on a denominator that makes
three structurally different technologies comparable.

---

## 1. Unit of output

The denominator is the **student-tutoring-hour (STH)**: one student receiving
one hour of tutoring. A per-session denominator would not travel, because
session length differs by model and subject (9–14 minutes here); a per-student
denominator hides intensity of use, which is the variable that drives the whole
comparison.

Two derived units are reported alongside it:

- **$ per student per year** — the budgeting unit a school actually faces.
- **$ per quality-adjusted STH** — unit cost divided by the human-graded pass
  rate, the cost-effectiveness ratio.

---

## 2. Cost structure — the analytical core

The three delivery modes have genuinely different cost functions, and that
difference, not the headline prices, is the finding.

| mode | cost function | marginal cost | returns to scale |
|---|---|---|---|
| **Human labour** | ATC = w / r | w / r, constant | none; scale only by raising the pupil–teacher ratio *r* |
| **Cloud API** | ATC = p·q | p·q, constant | **none to the buyer** — the thousandth session costs what the first did |
| **On-device** | ATC = (K·CRF + E) / Q | ≈ 0 up to capacity | declining ATC in Q, until a hard capacity ceiling |

Where *w* is the hourly wage, *r* the pupil–teacher ratio, *p* the token price,
*q* tokens per session, *K* capital, *E* annual energy, *Q* annual STH.

The on-device case is the classic declining-average-cost problem: a large fixed
cost, near-zero marginal cost, and a **capacity constraint** that stops ATC
falling indefinitely. Utilisation, not the sticker price, decides whether the
hardware is worth buying.

---

## 3. Annualisation of capital

Durable goods are annualised rather than expensed in the year of purchase,
using the **capital recovery factor**:

$$ \text{CRF}(r,n) = \frac{r(1+r)^n}{(1+r)^n - 1} $$

with a **social discount rate of 5%** and a **3-year useful life** for computer
hardware. Both are conventional: education CEA commonly uses 3–5%, and 3 years
is the standard depreciation life for computing equipment.

| | value |
|---|---|
| capital *K* | $2,500 |
| CRF(0.05, 3) | 0.3672 |
| annualised capital | **$918/yr** |
| energy *E* (350 W × 6 h × 190 d × $0.20/kWh) | $80/yr |
| **annual cost of ownership** | **$998/yr** |

Dividing capital by life — the naive first pass — gives $833 and omits the
opportunity cost of the funds tied up in the asset. The gap is small against
the uncertainty in usage, but it is not defensible to leave out of a paper
claiming an economic method.

**Sensitivity.** Annualised capital ranges $884 (r=3%, n=3) to $1,345 (r=5%,
n=2) to $577 (r=5%, n=5). The result is not sensitive to the discount rate
within the conventional range; it *is* sensitive to useful life, which is the
parameter worth arguing about for a card under sustained inference load.

---

## 4. Scope — what is and is not in the ingredients list

Included, because they vary with the choice:

| mode | included |
|---|---|
| on-device | GPU capital (annualised), electricity |
| cloud | metered token spend, measured from the traces |
| human | teacher wage cost to the school |

Excluded on **every** option, so the comparison stays like-for-like: premises,
lighting and general utilities, administration, curriculum development, and
the sunk engineering cost of building the system. The human column further
excludes recruitment, training and absence cover; the cloud column excludes the
internet connection that on-device provision exists precisely to avoid
requiring.

**This is not a total cost of ownership.** It is the incremental cost of
delivering tutoring by each mode, which is the object of a cost-effectiveness
comparison. Stating the exclusions matters more than eliminating them: a
reviewer's first question is what was left out.

---

## 5. Wage parameters

The labour comparison is anchored on published Tanzanian secondary teacher
pay (WageIndicator, 2025), converted at ~TSh 2,600/USD over a 45-hour week:

| | monthly | hourly |
|---|---|---|
| new teacher, low | TSh 401,469 | **$0.79** |
| new teacher, high | TSh 772,735 | $1.53 |
| 5 years' service, low | TSh 492,530 | $0.97 |
| 5 years' service, high | TSh 1,163,986 | **$2.30** |

The analysis uses **$1.50/h** as a central case and reports the range. Note
this is the wage bill, not the full economic cost of employing a teacher.

---

## 6. Results — average total cost per STH

*(300-student roll, 200 sessions/student/year, 30s think time, r=5%, n=3)*

| option | $/STH | structure |
|---|---:|---|
| human teacher, 1:40 class | **0.037** | pure variable |
| **on-device qwen3-4B** | **0.089** | fixed + ~0 marginal |
| on-device qwen3.8-27B (3 cards) | 0.222 | fixed + ~0 marginal |
| cloud gpt-5.4-mini | 0.254 | pure variable |
| cloud gemini-3.5-flash | 0.315 | pure variable |
| **human tutor, 1:1** | **1.500** | pure variable |
| cloud claude-opus-4-7 | 2.697 | pure variable |

### The comparison that is economically meaningful

**A 1:40 classroom is cheaper per student-hour than any machine, and always
will be** — that is what a high pupil–teacher ratio buys. But a 1:40 classroom
is not individualised instruction, and the intervention here is not a
substitute for the classroom teacher.

The like-for-like comparison is against **1:1 human tutoring**, which is the
service actually being delivered. There the on-device 4B is **$0.089 against
$1.50 — a factor of 17** — and the 27B, at near-frontier teaching quality, is
still a factor of 7 cheaper.

Framed as an economist would: individualised tutoring is known to be highly
effective and is not provided at scale because its **cost function does not
permit it** — it is pure variable cost in a labour input whose price cannot
fall. Substituting a technology with near-zero marginal cost changes the cost
function, not merely the price.

---

## 7. Affordability against the actual budget constraint

Unit costs mean little without the envelope they must fit inside.

| benchmark | value |
|---|---:|
| Tanzania capitation grant, secondary | **$5.00** per student/year |
| Sub-Saharan Africa system spending, primary | $208 per student/year (2013 PPP) |
| **on-device 4B, 300-student roll** | **$3.33** per student/year |
| on-device 4B, 150-student roll | $6.65 per student/year |
| cloud gpt-5.4-mini, 200 sessions/yr | $8.00 per student/year |
| cloud claude-opus-4-7, 200 sessions/yr | $91.10 per student/year |

At a 300-student roll the on-device 4B fits **inside the existing capitation
grant** — the tutoring is affordable from the per-student budget a Tanzanian
secondary school already receives, without new money. At 150 students it does
not. No cloud option fits at any roll.

This is the strongest form of the feasibility argument, and it is a
scale-dependent claim rather than a general one.

---

## 8. Cost-effectiveness, and an honest limit

Cost-effectiveness ratios here divide unit cost by the **human-graded pass
rate** — a process measure of tutoring quality, not a learning outcome.

| option | $/STH | quality | $/QASTH |
|---|---:|---:|---:|
| on-device 4B | 0.089 | 68% | **0.131** |
| on-device 27B | 0.222 | 97% | 0.229 |
| cloud gpt-5.4-mini | 0.254 | 88% | 0.289 |
| cloud gemini-3.5-flash | 0.315 | 71% | 0.443 |
| cloud claude-opus-4-7 | 2.697 | 100% | 2.697 |

**This is not LAYS per $100 and must not be presented as if it were.** The
field standard for education cost-effectiveness is learning-adjusted years of
schooling per $100 spent (Angrist et al. 2023, over 200 evaluations in 52
countries), and computing it requires a learning outcome from a controlled
trial. This study has no such outcome: it measures whether the tutor teaches
well by expert judgement, not whether students learn more.

Two literature benchmarks worth stating for calibration, and worth being
cautious about:

- The most cost-effective interventions reviewed deliver around **3 LAYS per
  $100**; structured pedagogy and teaching-at-the-right-level lead the ranking.
- **"Additional inputs alone" — textbooks, laptops, tablets, grants, class-size
  reduction without complementary reform — have a median effect on LAYS of
  approximately zero.** Hardware placed in schools without a pedagogical
  intervention does not raise learning.

That second finding is the one this work must answer. A GPU in a school is an
additional input; whether it is instead a *pedagogical* intervention depends on
whether the tutoring it delivers changes learning. The cost analysis
establishes affordability and the grading establishes tutoring quality;
neither establishes learning gains, and the paper should say so plainly.

---

## 9. Sensitivity

The parameters ranked by how much they move the answer:

1. **Sessions per student per year** — scales the entire cloud column and
   leaves on-device untouched. At 80/yr the cheapest cloud tier beats the card;
   at 200–400/yr it does not. This single assumption reverses the conclusion.
2. **Useful life of the card** — 2 to 5 years moves annualised capital from
   $1,345 to $577.
3. **Pupil–teacher ratio** in the labour comparison — 1:1 to 1:40 moves the
   human column by a factor of 40 and decides whether the machine looks cheap
   or expensive.
4. **Wage level** — $0.79 to $2.30/h across the published Tanzanian range.
5. **Discount rate** — 3% to 5% moves annualised capital by ~4%. Immaterial.

Reproduce any cell of the analysis:

```bash
python offline_eval/economics.py --wage 1.50 --ratio 40 --sessions 200 \
    --students 300 --life 3 --discount 0.05
```

---

## Sources

- Levin, H. & McEwan, P. *Cost-Effectiveness Analysis: Methods and
  Applications*; the ingredients method —
  [CBCSE](https://www.cbcse.org/publications/cost-effectiveness-analysis-methods-and-applications),
  [Sage](https://methods.sagepub.com/book/mono/economic-evaluation-in-education-3e/chpt/4-ingredients-method)
- Angrist, N., Evans, D., Filmer, D., Glennerster, R., Rogers, H. & Sabarwal,
  S. (2023) *How to Improve Education Outcomes Most Efficiently?* BSG-WP-2023/057 —
  [Blavatnik School of Government](https://www.bsg.ox.ac.uk/sites/default/files/2023-12/BSG-WP-2023-057%20How%20to%20Improve%20Education%20Outcomes%20Most%20Efficiently.pdf),
  [World Bank PRWP 9450](https://documents1.worldbank.org/curated/en/801901603314530125/pdf/How-to-Improve-Education-Outcomes-Most-Efficiently-A-Comparison-of-150-Interventions-Using-the-New-Learning-Adjusted-Years-of-Schooling-Metric.pdf)
- Filmer, D., Rogers, H., Angrist, N. & Sabarwal, S. (2018) *Learning-Adjusted
  Years of Schooling (LAYS)* — [World Bank PRWP 8591](https://documents1.worldbank.org/curated/en/243261538075151093/pdf/Learning-Adjusted-Years-of-Schooling-LAYS-Defining-A-New-Macro-Measure-of-Education.pdf)
- Tanzanian teacher pay —
  [WageIndicator Tanzania](https://wageindicator.org/en-tz/work-in-tanzania/jobs-and-wages/tanzania-secondary-education-teachers/)
- IADB, *Cost-Effectiveness Analysis of Education and Health Interventions in
  Developing Countries* — [IADB](https://webimages.iadb.org/publications/english/document/Cost-Effectiveness-Analysis-of-Education-and-Health-Interventions-in-Developing-Countries.pdf)
