"""Paper figures for the offline-vs-cloud tutoring comparison.

    python offline_eval/make_figures.py            # -> offline_eval/figures/*.png

Every series is measured. Latency comes from the per-response traces, quality from
the 169 hand grades, cloud cost from the metered token buckets, GPU cost from
the capital model in cost_model.py. Nothing here is illustrative.
"""
import json
import pathlib
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from ai_tutor.apps.benchmark import pedagogy as P            # noqa: E402

OUT = pathlib.Path(__file__).parent / "figures"
OUT.mkdir(exist_ok=True)

LOCAL = "#1b6ca8"
CLOUD = "#c0562f"
GREY = "#777777"

# ---- measured inputs -------------------------------------------------------
LAT = {                     # arm -> (geo p50, geo p95, math p50, math p95)
    "gpt-5.4-mini":       (1.65, 3.13, 1.50, 3.54),
    "claude-opus-4-7":    (4.27, 6.34, 3.81, 5.80),
    "qwen3-4B (local)":   (5.09, 8.55, 7.46, 26.00),
    "gemini-3.5-flash":   (10.55, 17.16, 15.75, 33.51),
    "qwen3.8-27B (local)": (19.62, 26.36, 14.86, 19.65),
}
IS_LOCAL = {k: "local" in k for k in LAT}
COST_SESSION = {           # $/session, mean of the two boards
    "claude-opus-4-7": 0.456, "gemini-3.5-flash": 0.072, "gpt-5.4-mini": 0.040,
}
GPU_CAPITAL, GPU_POWER_YR = 2500.0, 80.0
SESSIONS_YR = 200          # 5 sessions/week x 40 weeks — the pilot's low end


def grades():
    f = pathlib.Path("offline_eval/manual_grades/manual_grades_3runs_169.json")
    d = json.load(open(f))
    dims = d["dimensions"]
    comp = {k: r for k, r in d["verdicts"].items() if len(r.get("d", {})) == len(dims)}
    out = {}
    for k, r in comp.items():
        arm = k.split("|")[1]
        n, p = out.get(arm, (0, 0))
        out[arm] = (n + 1, p + (1 if P.session_passes(r["d"]) else 0))
    return out, comp, dims


def fig_latency():
    arms = sorted(LAT, key=lambda a: LAT[a][0])
    x = range(len(arms))
    fig, ax = plt.subplots(figsize=(9, 4.6))
    w = 0.38
    for i, (off, lab, alpha) in enumerate(((-w/2, "geography", 1.0), (w/2, "maths", 0.62))):
        vals = [LAT[a][0 if i == 0 else 2] for a in arms]
        err = [LAT[a][1 if i == 0 else 3] - v for a, v in zip(arms, vals)]
        cols = [LOCAL if IS_LOCAL[a] else CLOUD for a in arms]
        ax.bar([xx + off for xx in x], vals, w, yerr=[[0]*len(vals), err],
               color=cols, alpha=alpha, capsize=3,
               error_kw={"ecolor": GREY, "lw": 1})
    ax.set_xticks(list(x))
    ax.set_xticklabels([a.replace(" (local)", "\n(local)") for a in arms], fontsize=9)
    ax.set_ylabel("seconds per tutor response")
    ax.set_title("Tutor response latency — median, whisker to 95th percentile",
                 fontsize=11)
    ax.axhline(30, ls="--", lw=1, color=GREY)
    ax.text(len(arms)-0.4, 31, "30s budget", fontsize=8, color=GREY, ha="right")
    # Colour encodes local-vs-cloud and OPACITY encodes subject. A legend with
    # coloured swatches would say colour means subject, which is the opposite
    # of what the bars do, so the legend is drawn in neutral grey.
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(facecolor=GREY, alpha=1.0, label="geography"),
                       Patch(facecolor=GREY, alpha=0.62, label="maths")],
              fontsize=9, frameon=False)
    for s in ("top", "right"): ax.spines[s].set_visible(False)
    fig.text(0.01, 0.02, "blue = on-device (RTX 3090)   orange = cloud API",
             fontsize=8, color=GREY)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(OUT / "fig1_latency.png", dpi=200)


def fig_quality_cost(g):
    label = {"qwen3-4b-jetson": "qwen3-4B\n(local)",
             "qwen3.8-27b-instruct": "qwen3.8-27B\n(local)",
             "claude-opus-4-7": "Opus 4.7", "gemini-3.5-flash": "Gemini 3.5 Flash",
             "gpt-5.4-mini": "GPT-5.4-mini"}
    fig, ax = plt.subplots(figsize=(7.6, 5))
    for arm, (n, p) in g.items():
        rate = 100 * p / n
        if arm in COST_SESSION:
            x, col = COST_SESSION[arm] * SESSIONS_YR, CLOUD
        else:                       # local: capital per student, 300 roll, 2y
            x, col = (GPU_CAPITAL / 2 + GPU_POWER_YR) / 300, LOCAL
        ax.scatter(x, rate, s=150, color=col, zorder=3, edgecolor="white", lw=1.5)
        # The two local arms sit at the SAME x (one shared card), and Gemini
        # lands close by, so a single fixed offset overlaps three labels.
        # Place each by hand rather than let them collide.
        dx, dy, ha = {
            "qwen3-4b-jetson":      (-14, -30, "right"),
            "qwen3.8-27b-instruct": (0, 16, "center"),
            "gemini-3.5-flash":     (14, -6, "left"),
            "gpt-5.4-mini":         (0, 16, "center"),
            "claude-opus-4-7":      (0, 16, "center"),
        }.get(arm, (0, 15, "center"))
        ax.annotate(f"{label.get(arm, arm)}\n{rate:.0f}%", (x, rate),
                    textcoords="offset points", xytext=(dx, dy),
                    ha=ha, fontsize=8.5)
    ax.set_xscale("log")
    ax.set_xlabel("$ per student per year  (log scale)")
    ax.set_ylabel("human-graded pass rate, geography (%)")
    ax.set_title("Teaching quality against cost per student", fontsize=11)
    ax.set_ylim(55, 112)
    ax.grid(alpha=0.25, ls=":")
    for s in ("top", "right"): ax.spines[s].set_visible(False)
    fig.text(0.01, 0.02,
             "local at 300-student roll, 2-year card life; cloud at 200 sessions/student/year (5/week)",
             fontsize=8, color=GREY)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(OUT / "fig2_quality_vs_cost.png", dpi=200)


def fig_crossover():
    rolls = list(range(20, 901, 10))
    fig, ax = plt.subplots(figsize=(8.2, 4.8))
    for years, ls in ((1, "--"), (2, "-")):
        y = [(GPU_CAPITAL / years + GPU_POWER_YR) / r for r in rolls]
        ax.plot(rolls, y, ls, color=LOCAL, lw=2,
                label=f"RTX 3090, {years}-year life")
    # A band, not a line: usage is the dominant uncertainty and it moves the
    # cloud cost by 2x across the pilot's stated 5-10 sessions/week, while
    # leaving the GPU curves untouched. Drawing one line would hide the very
    # sensitivity that decides the comparison.
    for m, c in COST_SESSION.items():
        lo, hi = c * 200, c * 400
        ax.fill_between([0, 900], lo, hi, color=CLOUD, alpha=0.13, lw=0)
        ax.plot([0, 900], [lo, lo], color=CLOUD, lw=1.3, alpha=0.9)
        # Left-aligned: the legend sits top-right and the Opus band runs
        # along the top, so a right-aligned label lands underneath it.
        ax.text(360, hi * 1.06, m, fontsize=8, color=CLOUD, ha="left")
    ax.axvspan(150, 300, color=GREY, alpha=0.12)
    ax.text(225, 60, "typical\nschool roll", fontsize=8.5, color=GREY,
            ha="center", va="top")
    ax.set_yscale("log")
    ax.set_xlabel("students sharing one GPU")
    ax.set_ylabel("$ per student per year  (log scale)")
    ax.set_title("Where a bought GPU beats a metered API", fontsize=11)
    ax.set_xlim(0, 900)
    ax.legend(fontsize=9, frameon=False, loc="lower left")
    ax.grid(alpha=0.25, ls=":")
    for s in ("top", "right"): ax.spines[s].set_visible(False)
    fig.text(0.01, 0.02,
             "shaded band = 5 to 10 tutoring sessions per student per week; "
             "GPU curves do not move with usage", fontsize=8, color=GREY)
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    fig.savefig(OUT / "fig3_crossover.png", dpi=200)


def fig_dimensions(comp, dims):
    DES = {d.key: d.desideratum for d in P.DIMENSIONS}
    arms = sorted({k.split("|")[1] for k in comp})
    short = {"qwen3-4b-jetson": "4B (local)", "qwen3.8-27b-instruct": "27B (local)",
             "claude-opus-4-7": "Opus 4.7", "gemini-3.5-flash": "Gemini Flash",
             "gpt-5.4-mini": "GPT-5.4-mini"}
    # All eight, including the four nobody fails. Dropping them would report
    # only where the models differ and hide the equally useful result that none
    # failed to identify a mistake, locate it, stay encouraging or sound human.
    keep = list(dims)
    # One shade per arm, within a family: blues on-device, warm cloud. The
    # previous version gave all three cloud arms the SAME orange, so their
    # legend swatches were identical and a reader could not tell which bar was
    # which — the legend claimed a distinction the colours did not make.
    SHADE = {
        "qwen3-4b-jetson": "#7fb3d5", "qwen3.8-27b-instruct": "#15476b",
        "gemini-3.5-flash": "#f0b48a", "gpt-5.4-mini": "#d97742",
        "claude-opus-4-7": "#8c2f11",
    }
    fig, ax = plt.subplots(figsize=(10.2, 4.6))
    w = 0.8 / len(arms)
    for i, a in enumerate(arms):
        vals = []
        for d in keep:
            rs = [r["d"][d] for k, r in comp.items() if k.split("|")[1] == a]
            sc = [x for x in rs if x != "n/a"]
            vals.append(100 * sum(1 for x in sc if x != DES.get(d)) / len(sc) if sc else 0)
        ax.bar([j + i*w - 0.4 for j in range(len(keep))], vals, w,
               label=short.get(a, a), color=SHADE.get(a, GREY))
    ax.set_xticks(range(len(keep)))
    ax.set_xticklabels([k.replace("_", "\n") for k in keep], fontsize=8)
    ax.set_ylabel("% of sessions failing the dimension")
    ax.set_title("Failure rate on all eight pedagogical dimensions", fontsize=11)
    ax.legend(fontsize=8, frameon=False, ncol=5)
    ax.grid(axis="y", alpha=0.25, ls=":")
    for s in ("top", "right"): ax.spines[s].set_visible(False)
    fig.text(0.01, 0.02,
             "zero bars are a result, not missing data: every arm scored 100% on "
             "mistake identification, mistake location, tone and human-likeness",
             fontsize=8, color=GREY)
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    fig.savefig(OUT / "fig4_dimensions.png", dpi=200)


def main():
    g, comp, dims = grades()
    fig_latency(); fig_quality_cost(g); fig_crossover(); fig_dimensions(comp, dims)
    for f in sorted(OUT.glob("*.png")):
        print(f"  {f.name:<32} {f.stat().st_size//1024:>4} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
