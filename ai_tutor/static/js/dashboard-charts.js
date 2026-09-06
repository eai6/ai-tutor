// Literal utility strings, not names assembled at runtime.
//
// Tailwind's scanner reads source text; a class this file builds by
// concatenation is invisible to it and ships with no styles. The BEM names are
// kept alongside the utilities because other rules select through them —
// .chart__col's hover rule reaches .chart__bar by name.
const CHART = {
    gridline: 'border-t [border-top-style:dashed] border-t-border h-0',
    gridlineBase: 'border-t border-t-border-strong',
    col: 'flex-1 min-w-0 flex flex-col justify-end items-stretch [&:hover_.chart__bar]:bg-primary-dark',
    bar: 'motion-reduce:transition-none w-full rounded-tl-xs rounded-tr-xs bg-accent-solid [transition:height_var(--dur-slow)_var(--ease-out),_background_var(--dur-fast)_var(--ease-out)]',
    xLabel: 'flex-1 min-w-0 text-center whitespace-nowrap',
    xLabelShown: 'overflow-visible',
};

/* Overview charts.
 *
 * One div-based chart: sessions over time. No charting library — production
 * has no CDN, the shape is simple, and a 90 KB dependency to draw thirty
 * rectangles is a bad trade.
 *
 * Moved out of an inline <script> in home.html. Beyond being cacheable and
 * nonce-free, the split let the ~15 style properties this code used to set
 * per bar move into charts.css. What is left here sets only the two things
 * that are genuinely data: a height percentage and a left offset.
 *
 * Accessibility: the bar strips carry a text summary via aria-label, so a
 * screen reader gets the shape rather than a run of empty divs. Individual
 * bars keep a title for hover.
 */
(function () {
    'use strict';

    function readJSON(id, fallback) {
        var el = document.getElementById(id);
        if (!el) { return fallback; }
        try {
            return JSON.parse(el.textContent);
        } catch (err) {
            return fallback;
        }
    }

    /** Smallest "nice" upper bound >= value, where nice is {1,2,5} x 10^n.
     *  Keeps Y-axis ticks round integers instead of awkward fractions. */
    function niceCeil(value) {
        if (value <= 0) { return 1; }
        var exp = Math.floor(Math.log(value) / Math.LN10);
        var base = Math.pow(10, exp);
        var m = value / base;
        var nice = m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10;
        return nice * base;
    }

    /** Evenly spaced ticks from yMax down to 0, deduped.
     *  Counts are integers, so a small yMax rounds several ticks to the same
     *  value — a two-attempt window rendered "2, 2, 1, 1, 0". */
    function ticksFor(yMax, count) {
        var out = [];
        for (var i = count - 1; i >= 0; i--) {
            var t = Math.round((yMax * i) / (count - 1));
            if (out.indexOf(t) === -1) { out.push(t); }
        }
        return out;
    }

    function renderAxis(el, ticks) {
        if (!el) { return; }
        ticks.forEach(function (t) {
            var span = document.createElement('span');
            span.textContent = t;
            el.appendChild(span);
        });
    }

    function renderGrid(el, count) {
        if (!el) { return; }
        for (var i = 0; i < count; i++) {
            var line = document.createElement('div');
            line.className = 'chart__gridline ' + CHART.gridline
                + (i === count - 1 ? ' chart__gridline--base ' + CHART.gridlineBase : '');
            el.appendChild(line);
        }
    }

    /** One bar column. Height is the only inline style — everything else,
     *  colour included, comes from charts.css. */
    function addBar(barsEl, heightPct, hasValue, title) {
        var col = document.createElement('div');
        col.className = 'chart__col ' + CHART.col;

        var bar = document.createElement('div');
        bar.className = 'chart__bar ' + CHART.bar;
        bar.style.height = heightPct + '%';
        bar.style.minHeight = hasValue ? '2px' : '0';
        bar.title = title;

        col.appendChild(bar);
        barsEl.appendChild(col);
    }

    function addXLabel(xEl, text, shown) {
        var cell = document.createElement('div');
        cell.className = 'chart__x-label ' + CHART.xLabel
            + (shown ? ' chart__x-label--shown ' + CHART.xLabelShown : '');
        cell.textContent = shown ? text : '';
        if (text) { cell.title = text; }
        xEl.appendChild(cell);
    }

    /* ------------------------------------------------- sessions over time */
    (function sessionsChart() {
        var data = readJSON('activity-data', []);
        var barsEl = document.getElementById('activity-bars');
        if (!barsEl || !data.length) { return; }

        var yMax = niceCeil(Math.max.apply(null, data.map(function (d) { return d.sessions; }).concat([1])));
        var ticks = ticksFor(yMax, 5);

        renderAxis(document.getElementById('activity-y'), ticks);
        renderGrid(document.getElementById('activity-grid'), ticks.length);

        var xEl = document.getElementById('activity-x');
        // Thin the labels so they never collide. At 14 buckets every bar is
        // labelled; over a long range only every Nth is, which is what keeps
        // a 12-month window legible instead of a grey smear.
        var labelEvery = Math.max(1, Math.ceil(data.length / 14));
        var lastLabelledMonth = null;
        var total = 0;

        data.forEach(function (d, idx) {
            total += d.sessions;
            addBar(
                barsEl,
                (d.sessions / yMax) * 100,
                d.sessions > 0,
                d.date + ': ' + d.sessions + (d.sessions === 1 ? ' session' : ' sessions')
            );

            // Day-of-month alone is ambiguous once the window spans months.
            // Carry the month on the first LABELLED bar of each month —
            // compared against the last labelled month, not the previous bar,
            // because the true first-of-month bar is usually not labelled.
            var parts = d.date.split(' ');
            var shown = idx % labelEvery === 0;
            var text = d.date;
            if (shown) {
                text = (lastLabelledMonth !== parts[0]) ? d.date : (parts[1] || d.date);
                lastLabelledMonth = parts[0];
            }
            addXLabel(xEl, shown ? text : d.date, shown);
        });

        barsEl.setAttribute('role', 'img');
        barsEl.setAttribute(
            'aria-label',
            total + ' sessions across ' + data.length + ' buckets, from ' +
            data[0].date + ' to ' + data[data.length - 1].date + '.'
        );
    }());
}());
