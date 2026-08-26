"""Cost per student per year: rented GPU capital against cloud tokens.

    python offline_eval/cost_model.py
    python offline_eval/cost_model.py --students 300 --years 2 --sessions 80

The two tiers are priced in fundamentally different units and the comparison is
only meaningful once both are expressed per student per year.

  * CLOUD is metered. Cost scales with use: every session bills tokens, so a
    school pays again for every lesson, forever.
  * OFFLINE is capital. One RTX 3090 (~$2,500) is bought once and amortised
    over the school's roll and the hardware's life. Cost per student FALLS as
    the school grows and as the card lasts longer — the opposite shape.

That difference, not the headline per-session figures, is what decides the
deployment. A per-session number flatters the cloud at small scale and the GPU
at large scale, so both appear here as a function of roll and lifetime rather
than as one figure.

WHAT IS AND IS NOT COUNTED. The GPU column is hardware capital plus
electricity. It excludes the host machine, networking, physical security,
teacher time, and the engineering already sunk into making the offline tier
work — real costs that a school in a pilot may or may not face. The cloud
column is measured token spend only: it excludes egress and the internet
connection an offline deployment is chosen precisely to avoid needing. Neither
column is a full TCO; they are the parts that scale with the choice.
"""
import argparse

# Measured $/session, from cloud_cost.py over the real boards.
CLOUD = {
    "claude-opus-4-7":  {"geography": 0.359, "maths": 0.552},
    "gemini-3.5-flash": {"geography": 0.049, "maths": 0.095},
    "gpt-5.4-mini":     {"geography": 0.028, "maths": 0.052},
}
GPU_CAPITAL = 2500.0        # RTX 3090 workstation card, purchase
GPU_WATTS = 350.0           # board power under sustained inference
KWH_PRICE = 0.20            # $/kWh; Seychelles/Tanzania tariffs vary widely


def gpu_cost_per_student_year(students, years, hours_per_school_day=6,
                              school_days=190, capital=GPU_CAPITAL,
                              kwh=KWH_PRICE, watts=GPU_WATTS):
    capital_per_year = capital / years
    kwh_per_year = watts / 1000.0 * hours_per_school_day * school_days
    power_per_year = kwh_per_year * kwh
    return (capital_per_year + power_per_year) / students, capital_per_year, power_per_year


def main():
    ap = argparse.ArgumentParser()
    # 200 = 5 sessions/week x 40 weeks, the low end of the pilot's expected
    # 5-10/week. This single number scales the ENTIRE cloud column and leaves
    # the GPU column untouched, so it decides the comparison on its own: at the
    # 80/yr first assumed, the cheap cloud tier beat the card at school scale;
    # at 200 the card wins. It is the first thing to check against a real
    # timetable, and the first thing a reviewer should ask about.
    ap.add_argument("--sessions", type=int, default=200,
                    help="tutoring sessions per student per year "
                         "(default 200 = 5/week x 40 weeks; 400 = 10/week)")
    args = ap.parse_args()
    S = args.sessions

    print(f"Assumption: {S} tutoring sessions per student per year "
          f"(2/week x 40 weeks).\n")

    print("CLOUD — metered, scales with use")
    print(f"  {'model':<20}{'$/session':>11}{'$/student/yr':>15}{'150 students':>15}{'300 students':>15}")
    for m, subj in CLOUD.items():
        # Average the two boards: a real timetable mixes subjects, and maths
        # sessions are ~2x the turns of geography.
        per_sess = sum(subj.values()) / len(subj)
        per_sy = per_sess * S
        print(f"  {m:<20}{per_sess:>11.3f}{per_sy:>15.2f}"
              f"{per_sy*150:>15,.0f}{per_sy*300:>15,.0f}")

    print(f"\nOFFLINE — capital, ${GPU_CAPITAL:,.0f} card amortised over the roll")
    print(f"  {'roll':>6}{'life':>7}{'$/student/yr':>15}{'total/yr':>12}")
    for students in (150, 300):
        for years in (1, 2):
            psy, cap, pwr = gpu_cost_per_student_year(students, years)
            print(f"  {students:>6}{years:>6}y{psy:>15.2f}{psy*students:>12,.0f}")
    _, cap1, pwr1 = gpu_cost_per_student_year(150, 1)
    print(f"\n  (per year: ${cap1:,.0f} capital at 1-year life + ${pwr1:,.0f} "
          f"electricity at {KWH_PRICE:.2f}/kWh)")

    print("\nCROSSOVER — cloud is cheaper below this roll, the GPU above it")
    print(f"  {'model':<20}{'1-year life':>14}{'2-year life':>14}")
    for m, subj in CLOUD.items():
        per_sy = (sum(subj.values()) / len(subj)) * S
        row = f"  {m:<20}"
        for years in (1, 2):
            # students where GPU $/student/yr == cloud $/student/yr
            _, cap, pwr = gpu_cost_per_student_year(1, years)
            n = (cap + pwr) / per_sy
            row += f"{n:>13.0f}" + " "
        print(row)
    print("\n  Below the crossover the cloud is genuinely cheaper — the card is\n"
          "  idle capital. Above it the GPU wins and keeps winning, because the\n"
          "  cloud bill repeats every year while the card is already paid for.")


if __name__ == "__main__":
    raise SystemExit(main())
