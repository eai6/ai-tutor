"""Chart family — bar, line, pie, scatter (delegates to figure_render),
plus a new histogram template.
"""

from __future__ import annotations

from typing import Optional

from . import _common as c


def _legacy_chart(spec: dict) -> Optional[str]:
    """Delegate the existing chart kinds to figure_render's mature
    implementation. Avoids duplication and keeps backwards compat."""
    from ai_tutor.apps.curriculum.figure_render import render_figure_spec
    return render_figure_spec(spec)


def histogram(spec: dict) -> Optional[str]:
    """Histogram of frequency vs equal-width bins.

    Spec:
      bins: [bin_lo, bin_lo, ...] OR list of [lo, hi] pairs
      frequencies: [int, ...]  (one per bin)
      title?, x_label?, y_label?, source?
    """
    bins = spec.get('bins') or []
    frequencies = spec.get('frequencies') or []
    if not bins or not frequencies or len(bins) - 1 != len(frequencies) and len(bins) != len(frequencies):
        return None

    # Normalize bins → list of (lo, hi) pairs.
    pairs = []
    if isinstance(bins[0], (list, tuple)):
        pairs = [(float(lo), float(hi)) for lo, hi in bins]
    else:
        # assume edges; len = N+1 → N bins
        edges = [float(b) for b in bins]
        if len(edges) == len(frequencies) + 1:
            pairs = list(zip(edges[:-1], edges[1:]))
        elif len(edges) == len(frequencies):
            # treat as single-value lower bounds, infer width from gaps
            pairs = [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]
            pairs.append((edges[-1], edges[-1] + (edges[-1] - edges[-2] if len(edges) >= 2 else 1)))
    if not pairs:
        return None

    title_text = spec.get('title') or ''
    x_label = spec.get('x_label') or ''
    y_label = spec.get('y_label') or 'Frequency'
    source = spec.get('source') or ''

    W, H = 480, 320
    pad_l, pad_r, pad_t, pad_b = 56, 16, 40, 56
    plot_w = W - pad_l - pad_r
    plot_h = H - pad_t - pad_b

    fmax = max(frequencies)
    # nice y-ticks
    step = max(1, _nice_step(fmax, 5))
    ymax = ((fmax // step) + (1 if fmax % step else 0)) * step
    if ymax <= 0:
        ymax = step

    parts = [c.svg_open(W, H), c.title(title_text, x=W // 2)]

    # Y axis + grid
    n_ticks = ymax // step + 1
    for i in range(n_ticks):
        yval = i * step
        py = pad_t + plot_h - (yval / ymax) * plot_h
        parts.append(c.line(pad_l, py, pad_l + plot_w, py, stroke=c.GRID, width=1))
        parts.append(c.label(c.fmt(yval), x=pad_l - 6, y=py + 3, anchor='end', color=c.MUTED))

    # X axis
    xmin = pairs[0][0]
    xmax = pairs[-1][1]
    span = xmax - xmin if xmax > xmin else 1
    parts.append(c.line(pad_l, pad_t + plot_h, pad_l + plot_w, pad_t + plot_h, stroke=c.STROKE, width=1.5))
    parts.append(c.line(pad_l, pad_t, pad_l, pad_t + plot_h, stroke=c.STROKE, width=1.5))

    # Bars (touching, no gap)
    for (lo, hi), freq in zip(pairs, frequencies):
        x0 = pad_l + ((lo - xmin) / span) * plot_w
        x1 = pad_l + ((hi - xmin) / span) * plot_w
        h_px = (freq / ymax) * plot_h
        y0 = pad_t + plot_h - h_px
        parts.append(c.rect(x0, y0, x1 - x0, h_px, fill=c.PALETTE[0], stroke='#fff', width=1))

    # X tick labels at every edge
    edges = sorted({lo for lo, _ in pairs} | {pairs[-1][1]})
    for e in edges:
        ex = pad_l + ((e - xmin) / span) * plot_w
        parts.append(c.line(ex, pad_t + plot_h, ex, pad_t + plot_h + 4, stroke=c.STROKE, width=1))
        parts.append(c.label(c.fmt(e), x=ex, y=pad_t + plot_h + 16, color=c.MUTED, size=11))

    if x_label:
        parts.append(c.label(x_label, x=pad_l + plot_w / 2, y=H - 18, color=c.TEXT, size=12))
    if y_label:
        parts.append(
            f'<text x="{14}" y="{pad_t + plot_h / 2}" text-anchor="middle" '
            f'fill="{c.TEXT}" font-size="12" '
            f'transform="rotate(-90 14 {pad_t + plot_h / 2})">{c.esc(y_label)}</text>'
        )
    if source:
        parts.append(c.caption(source, x=W // 2, y=H - 4))

    parts.append(c.svg_close())
    return ''.join(parts)


def _nice_step(maxv, n_ticks):
    if maxv <= 0:
        return 1
    raw = maxv / max(1, n_ticks)
    import math
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    norm = raw / mag
    if norm < 1.5:
        return 1 * mag
    if norm < 3:
        return 2 * mag
    if norm < 7:
        return 5 * mag
    return 10 * mag


RENDERERS = {
    'bar': _legacy_chart,
    'line': _legacy_chart,
    'pie': _legacy_chart,
    'doughnut': _legacy_chart,
    'scatter': _legacy_chart,
    'histogram': histogram,
}
