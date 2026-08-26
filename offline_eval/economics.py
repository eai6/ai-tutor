"""Unit-cost analysis of tutoring delivery: on-device, cloud API, human labour.

    python offline_eval/economics.py
    python offline_eval/economics.py --wage 3.0 --ratio 25 --sessions 200

METHOD. The unit of output is one **student-tutoring-hour (STH)** — one student
receiving one hour of tutoring. Everything is reduced to average total cost per
STH so that three technologies with different cost structures can be compared
on a common denominator.

The three have genuinely different cost functions, and that is the analysis:

  * HUMAN LABOUR is pure variable cost. ATC = w / r, where w is the hourly wage
    and r the pupil-teacher ratio. It does not fall with volume: doubling the
    hours doubles the bill. Returns to scale come only from raising r, which
    trades directly against the individual attention that makes tutoring work.

  * CLOUD API is also pure variable cost, metered per token. MC = ATC, constant.
    There are no economies of scale to the buyer at all — the thousandth session
    costs what the first did.

  * ON-DEVICE is fixed cost with near-zero marginal cost up to a hard capacity
    constraint. ATC = (K/L + E) / Q, falling hyperbolically in Q. This is the
    classic declining-average-cost case, and it is why utilisation, not the
    headline price, decides whether the hardware is worth buying.

CAPACITY IS A REAL CONSTRAINT, NOT AN ASYMPTOTE. The GPU can only serve so many
students at once before latency exceeds what a student will sit through. That
ceiling is measured (see RTX3090_CAPACITY.md), and it caps Q — so ATC falls
until the card saturates and then stops falling. A model that lets Q rise
without bound would report an arbitrarily low cost per hour and be wrong.

WHAT IS EXCLUDED, on every option, so the comparison stays like-for-like:
premises, electricity for lighting, administration, curriculum development, and
the sunk engineering cost of building the system. The human column excludes
recruitment, training and absence cover; the cloud column excludes the internet
connection that on-device provision exists to avoid requiring. These are the
costs that vary WITH the choice, not a full budget for running a school.
"""
import argparse
import math

# ---- measured from the boards (see LATENCY_AND_COST.md) --------------------
TURNS_PER_SESSION = {"maths": 18.0, "geography": 8.0}
TUTOR_SEC_PER_TURN = {          # median, maths board
    "qwen3-4B": 7.46, "qwen3.8-27B": 14.86,
    "claude-opus-4-7": 3.81, "gemini-3.5-flash": 15.75, "gpt-5.4-mini": 1.50,
}
COST_PER_SESSION = {            # metered $, mean of both boards
    "claude-opus-4-7": 0.456, "gemini-3.5-flash": 0.072, "gpt-5.4-mini": 0.040,
}
CONCURRENCY = {"qwen3-4B": 48, "qwen3.8-27B": 4}     # at a 30s turn budget
QUALITY = {                     # human-graded pass rate, geography, n=169
    "claude-opus-4-7": 1.00, "qwen3.8-27B": 0.97, "gpt-5.4-mini": 0.88,
    "gemini-3.5-flash": 0.71, "qwen3-4B": 0.68,
}

# ---- capital and operating assumptions ------------------------------------
K_CAPITAL = 2500.0        # RTX 3090, purchase
GPU_WATTS, KWH = 350.0, 0.20
HOURS_DAY, SCHOOL_DAYS = 6, 190
DISCOUNT = 0.05           # social discount rate; education CEA commonly 3-5%
LIFE_YEARS = 3            # useful life; 3y is the standard for computer hardware

# Reference points from the literature, for calibration rather than decoration:
SSA_SPEND_PER_STUDENT = 208.0   # Sub-Saharan Africa, primary, 2013 PPP USD
                                # (Bashir et al. 2018, cited in Angrist et al. 2023)
TZ_CAPITATION_SECONDARY = 5.0   # Tanzania capitation grant, US$/secondary
                                # student/year, 2018-19


def crf(rate: float, years: int) -> float:
    """Capital recovery factor — annualises a lump sum over its useful life.

    The ingredients method (Levin & McEwan) annualises durable goods rather
    than expensing them in year one, because a card bought once yields service
    over several years. Dividing capital by life, as a first pass usually does,
    omits the opportunity cost of the funds tied up in the asset; the CRF does
    not. At 5% over 3 years it adds ~10% to the annual charge, which is small
    against the uncertainty in usage but wrong to leave out of a paper that
    calls itself an economic analysis.
    """
    if rate <= 0:
        return 1.0 / years
    return rate * (1 + rate) ** years / ((1 + rate) ** years - 1)


def session_minutes(arm, subject="maths", think_sec=30):
    """Wall-clock a REAL session takes.

    The eval's simulated student answers instantly; a real one reads, thinks
    and types. Session length therefore is not the measured harness duration —
    it is turns x (tutor latency + think time), and think time dominates for
    every fast model.
    """
    t = TURNS_PER_SESSION[subject]
    return t * (TUTOR_SEC_PER_TURN[arm] + think_sec) / 60.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wage", type=float, default=6.0,
                    help="teacher/tutor hourly cost to the school, $ (default 6)")
    ap.add_argument("--ratio", type=int, default=20,
                    help="students per teacher in the comparison (default 20)")
    ap.add_argument("--students", type=int, default=300)
    ap.add_argument("--sessions", type=int, default=200,
                    help="sessions per student per year (5/week x 40 weeks)")
    ap.add_argument("--life", type=int, default=LIFE_YEARS,
                    help="useful life of the card, years (3 = hardware standard)")
    ap.add_argument("--discount", type=float, default=DISCOUNT,
                    help="social discount rate (education CEA commonly 0.03-0.05)")
    ap.add_argument("--think", type=int, default=30, help="student think seconds/turn")
    a = ap.parse_args()

    E = GPU_WATTS / 1000 * HOURS_DAY * SCHOOL_DAYS * KWH
    ann = K_CAPITAL * crf(a.discount, a.life)
    print("Unit of output: one student-tutoring-hour (STH).")
    print(f"Roll {a.students}, {a.sessions} sessions/student/yr, {a.think}s think time.")
    print(f"Capital ${K_CAPITAL:,.0f} annualised over {a.life}y at {a.discount:.0%} "
          f"= ${ann:,.0f}/yr (CRF {crf(a.discount, a.life):.4f}); "
          f"electricity ${E:,.0f}/yr.\n")

    # --- demand side: how many STH the school actually consumes -------------
    print("DEMAND — hours of tutoring the school consumes")
    print(f"  {'arm':<20}{'min/session':>13}{'h/student/yr':>14}{'school STH/yr':>15}")
    demand = {}
    for arm in TUTOR_SEC_PER_TURN:
        m = session_minutes(arm, "maths", a.think)
        h_sy = m * a.sessions / 60
        Q = h_sy * a.students
        demand[arm] = (m, h_sy, Q)
        print(f"  {arm:<20}{m:>13.1f}{h_sy:>14.1f}{Q:>15,.0f}")

    # --- how many are actually tutoring at once ----------------------------
    #
    # The whole roll is never online together. Demand is expressed as annual
    # student-hours and spread across the school year, so the figure that
    # matters against a concurrency ceiling is the AVERAGE number tutoring at
    # any instant — annual hours divided by the hours the lab is open. For a
    # 300-pupil school that is single figures, not 300.
    open_hours = HOURS_DAY * SCHOOL_DAYS
    print(f"\nCONCURRENCY — the roll is never online at once")
    print(f"  lab open {HOURS_DAY}h/day x {SCHOOL_DAYS} days = {open_hours:,} hours/year")
    print(f"  {'arm':<20}{'avg online':>12}{'% of roll':>11}{'card ceiling':>14}{'headroom':>11}")
    for arm in TUTOR_SEC_PER_TURN:
        avg = demand[arm][2] / open_hours
        ceiling = CONCURRENCY.get(arm)
        if ceiling:
            head = f"{ceiling/avg:.1f}x" if avg else "-"
            print(f"  {arm:<20}{avg:>12.1f}{avg/a.students:>10.0%}"
                  f"{ceiling:>14}{head:>11}")
        else:
            print(f"  {arm:<20}{avg:>12.1f}{avg/a.students:>10.0%}"
                  f"{'n/a (cloud)':>14}{'-':>11}")
    print("  Peak will exceed the average whenever tutoring is timetabled into\n"
          "  class periods rather than spread freely; a card whose ceiling is\n"
          "  below the PEAK will queue, not fail.")

    # --- supply side: what one card can deliver ----------------------------
    print(f"\nSUPPLY — annual throughput of one RTX 3090 at a 30s turn budget")
    print(f"  {'arm':<20}{'concurrent':>12}{'STH/yr capacity':>18}{'utilisation':>13}")
    for arm, n in CONCURRENCY.items():
        cap = n * open_hours
        util = demand[arm][2] / cap
        cards = math.ceil(util)
        flag = f"  needs {cards} cards" if cards > 1 else ""
        print(f"  {arm:<20}{n:>12}{cap:>18,.0f}{util:>12.0%}{flag}")

    # --- average total cost per STH ----------------------------------------
    print(f"\nAVERAGE TOTAL COST per student-tutoring-hour")
    print(f"  {'option':<34}{'$/STH':>10}{'cost structure':>22}")
    rows = []
    for arm, n in CONCURRENCY.items():
        _, _, Q = demand[arm]
        cap = n * HOURS_DAY * SCHOOL_DAYS
        # Demand above one card's capacity is met by BUYING ANOTHER CARD, not
        # by capping output. Truncating Q at capacity would price the unserved
        # hours at zero and report the over-subscribed option as cheap — the
        # 27B needs three cards at this roll, and its cost must say so.
        cards = math.ceil(Q / cap)
        atc = cards * (K_CAPITAL * crf(a.discount, a.life) + E) / Q
        tag = "fixed + ~0 marginal" + (f", {cards} cards" if cards > 1 else "")
        rows.append((f"on-device {arm}", atc, tag))
    for arm, c in COST_PER_SESSION.items():
        hrs = session_minutes(arm, "maths", a.think) / 60
        rows.append((f"cloud {arm}", c / hrs, "pure variable"))
    rows.append((f"human tutor, 1:{a.ratio} @ ${a.wage:.2f}/h",
                 a.wage / a.ratio, "pure variable"))
    rows.append((f"human tutor, 1:1 @ ${a.wage:.2f}/h", a.wage, "pure variable"))
    for name, atc, kind in sorted(rows, key=lambda r: r[1]):
        print(f"  {name:<34}{atc:>10.3f}{kind:>22}")

    # --- cost-effectiveness ------------------------------------------------
    print(f"\nCOST-EFFECTIVENESS — $ per QUALITY-ADJUSTED student-tutoring-hour")
    print("  (ATC divided by human-graded pass rate; lower is better)")
    print(f"  {'option':<34}{'$/STH':>9}{'quality':>9}{'$/QASTH':>10}")
    ce = []
    for arm, n in CONCURRENCY.items():
        _, _, Q = demand[arm]
        cards = math.ceil(Q / (n * HOURS_DAY * SCHOOL_DAYS))
        atc = cards * (K_CAPITAL * crf(a.discount, a.life) + E) / Q
        ce.append((f"on-device {arm}", atc, QUALITY[arm]))
    for arm, c in COST_PER_SESSION.items():
        hrs = session_minutes(arm, "maths", a.think) / 60
        ce.append((f"cloud {arm}", c / hrs, QUALITY[arm]))
    for name, atc, q in sorted(ce, key=lambda r: r[1] / r[2]):
        print(f"  {name:<34}{atc:>9.3f}{q:>9.0%}{atc/q:>10.3f}")

    # --- break-even against human labour -----------------------------------
    print(f"\nBREAK-EVEN — teacher-hours displaced to repay ${K_CAPITAL:,.0f} of capital")
    print(f"  {'wage $/h':>10}{'1:1 tutoring':>16}{f'1:{a.ratio} class':>16}")
    for w in (1.5, 3.0, 6.0, 12.0):
        print(f"  {w:>10.2f}{K_CAPITAL/w:>15,.0f}h{K_CAPITAL/(w/a.ratio):>15,.0f}h")
    print("\n  Read the 1:1 column as the hours of individual tutoring the card\n"
          "  substitutes for before it has paid for itself. The class column is\n"
          "  the harsher test: it asks the card to beat a teacher already\n"
          f"  spreading their time across {a.ratio} pupils.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
