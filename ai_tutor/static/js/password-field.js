// Literal utility strings for the field this file builds.
//
// Every element below is created in JavaScript, so none of it was ever in a
// template for the stylesheet migration to convert. The reveal toggle lost
// its 36px box and its "show one glyph at a time" rules, and rendered as two
// full-size black shapes stacked under the input.
//
// The BEM names travel alongside the utilities, because rules select through
// them — the toggle hides .pw-toggle-on by name — and because this file
// looks .pw-field and .pw-rule--met up itself.
//
// The markers this file creates use single hyphens, not BEM's double
// underscore. Tailwind reads _ as a space inside an arbitrary variant, so the
// name would need a backslash in the CSS class — and a backslash cannot
// survive a JavaScript string literal and the scanner's raw read of the same
// text at once. A hyphen needs no escape from either.
const PW = {
    // .icon used to come from css/shared/base.css. Without it the sprite has
    // no intrinsic size and the browser falls back to the SVG default of
    // 300x150 — two black shapes stacked under the field.
    icon: "w-[1em] h-[1em] flex-none fill-none stroke-current [stroke-width:1.75] [stroke-linecap:round] [stroke-linejoin:round] [vertical-align:-0.125em]",
    field: "relative block [&>input]:pr-11 [&>input]:w-full",
    toggle: "absolute top-[50%] right-[6px] [transform:translateY(-50%)] w-9 h-9 grid place-items-center p-0 bg-transparent border border-transparent rounded-sm text-text-muted cursor-pointer [transition:background_var(--dur-fast)_var(--ease-out),_color_var(--dur-fast)_var(--ease-out)] hover:bg-surface-hover hover:text-text [&_.icon]:text-[1.05rem] [&_.pw-toggle-on]:hidden [&[aria-pressed='true']_.pw-toggle-off]:hidden [&[aria-pressed='true']_.pw-toggle-on]:block [&[aria-pressed='true']]:text-accent",
    rules: "mt-3 py-3 px-4 bg-surface-sunken border border-border rounded-md [&[hidden]]:hidden",
    rulesLabel: "text-xs font-bold text-text-secondary mb-2",
    rulesList: "list-none grid gap-1",
    rule: "flex items-start gap-2 text-sm leading-snug text-danger transition-colors duration-[var(--dur-fast)] ease-[var(--ease-out)] [&[hidden]]:hidden",
    ruleMark: "flex-none w-[1.05rem] h-[1.05rem] mt-[1px] grid place-items-center rounded-full bg-danger-surface border border-danger-border text-danger text-[0.65rem] [transition:background_var(--dur-fast)_var(--ease-out),_border-color_var(--dur-fast)_var(--ease-out),_color_var(--dur-fast)_var(--ease-out)] [&_.pw-rule-tick]:hidden [&_.pw-rule-dot]:block",
    rulesNote: "mt-2 pt-2 border-t [border-top-style:dashed] border-t-border text-xs text-text-muted",
    meter: "flex items-center gap-3 mt-3",
    meterTrack: "flex-1 h-[6px] bg-surface border border-border rounded-full overflow-hidden",
    meterFill: "h-full w-0 rounded-full bg-danger-solid [transition:width_var(--dur-base)_var(--ease-out),_background_var(--dur-base)_var(--ease-out)]",
    meterWord: "text-xs font-bold min-w-18 text-right text-text-muted",
    match: "flex items-center gap-2 mt-2 text-sm font-medium [&[hidden]]:hidden",
    srOnly: "absolute w-[1px] h-[1px] p-0 m-[-1px] overflow-hidden [clip:rect(0,_0,_0,_0)] whitespace-nowrap border-0",
    // Room for the toggle, on the input itself. This was
    // `.pw-field > input { padding-right: 2.75rem !important }` — 2.75rem is
    // the button's 2.25rem plus its inset. It has to be applied here rather
    // than in the template because the wrapper and the button are built at
    // runtime, and `!important` is gone with the sheet, so it is applied last
    // and wins on source order against the page's own field padding.
    fieldInput: "!pr-11",
    ruleMet: "text-success [&_.pw-rule-mark]:bg-success-surface [&_.pw-rule-mark]:border-success-border [&_.pw-rule-mark]:text-success [&_.pw-rule-tick]:block [&_.pw-rule-dot]:hidden",
    meterFillWeak: "w-[33%] bg-danger-solid",
    meterFillFair: "w-[66%] bg-warning-solid",
    meterFillStrong: "w-full bg-success-solid",
    meterWordWeak: "text-danger",
    meterWordFair: "text-warning",
    meterWordStrong: "text-success",
    matchOk: "text-success",
    matchBad: "text-danger",
};

/* Password fields: reveal toggle, requirement checklist, match indicator.
 *
 * Progressive enhancement. Every <input type="password"> gets a reveal
 * toggle; a field marked data-pw-strength also gets a live checklist and an
 * advisory strength meter. With JavaScript off, every form works exactly as
 * it did before.
 *
 * ── Which rules are listed, and why ─────────────────────────────────────
 * The checklist mirrors AUTH_PASSWORD_VALIDATORS in config/settings.py, so a
 * green tick means the server will accept that aspect. Four rules are always
 * listed, because they are the four a person can act on directly:
 *
 *   MinimumLengthValidator      → at least 8 characters
 *   CharacterVarietyValidator   → a letter, a number, a symbol
 *
 * Their character classes are ASCII and are copied character-for-character
 * from apps/accounts/password_validators.py. Widening them means changing
 * both files together; see that module for why they are ASCII at all.
 *
 * Two more validators run on the server and are named under the list rather
 * than ticked in it:
 *
 *   CommonPasswordValidator     → a 20,000-entry list. Shipping a subset
 *                                 would tick green for passwords the server
 *                                 then refuses, which is worse than silence.
 *   UserAttributeSimilarityValidator
 *                               → decidable here (quickRatio below matches
 *                                 Django's 0.7 threshold), but it is a trap
 *                                 rather than a target: nobody sets out to
 *                                 satisfy it. It joins the list only at the
 *                                 moment it fails, so the everyday list stays
 *                                 the four rules that are worth aiming at,
 *                                 and nobody sees four green ticks and is
 *                                 then refused on submit.
 *
 * ── When the checklist appears ──────────────────────────────────────────
 * Not until the first keystroke. Sitting open on an untouched field, it is
 * four rules nobody has broken yet taking up a third of the form.
 *
 * ── Markup contract ──────────────────────────────────────────────────────
 *   data-pw-strength            build the checklist for this field
 *   data-pw-similar-to="#a,#b"  inputs whose values must not resemble it
 *   data-pw-confirms="#id"      this field must match that one
 *   data-pw-no-toggle           skip the reveal toggle
 */
(function () {
    'use strict';

    var T = (window.gettext || function (s) { return s; });

    /* ------------------------------------------------------------ helpers */

    function svgIcon(name, extraClass) {
        var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('class', 'icon ' + PW.icon + (extraClass ? ' ' + extraClass : ''));
        svg.setAttribute('aria-hidden', 'true');
        svg.setAttribute('focusable', 'false');
        var use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
        use.setAttribute('href', '#i-' + name);
        svg.appendChild(use);
        return svg;
    }

    /** Python difflib.SequenceMatcher.quick_ratio(), which is what Django's
     *  UserAttributeSimilarityValidator actually calls. An upper bound on the
     *  real ratio, computed from the multiset intersection of the characters:
     *
     *      2 * sum(min(count_a[c], count_b[c])) / (len(a) + len(b))
     */
    function quickRatio(a, b) {
        var total = a.length + b.length;
        if (total === 0) { return 1.0; }

        var counts = Object.create(null);
        var i;
        for (i = 0; i < b.length; i++) {
            counts[b[i]] = (counts[b[i]] || 0) + 1;
        }
        var matches = 0;
        for (i = 0; i < a.length; i++) {
            var c = a[i];
            if (counts[c] > 0) { counts[c] -= 1; matches += 1; }
        }
        return (2.0 * matches) / total;
    }

    /** Django splits each attribute on non-word runs and also tests the whole
     *  value, failing if ANY part crosses the threshold. */
    function tooSimilar(password, values) {
        var pw = password.toLowerCase();
        for (var i = 0; i < values.length; i++) {
            var value = (values[i] || '').toLowerCase().trim();
            if (!value) { continue; }
            var parts = value.split(/\W+/).filter(Boolean).concat([value]);
            for (var j = 0; j < parts.length; j++) {
                if (quickRatio(pw, parts[j]) >= 0.7) { return true; }
            }
        }
        return false;
    }

    function readSimilarValues(input) {
        var sel = input.getAttribute('data-pw-similar-to');
        if (!sel) { return []; }
        var out = [];
        sel.split(',').forEach(function (one) {
            var el = document.querySelector(one.trim());
            if (el && el.value) { out.push(el.value); }
        });
        return out;
    }

    /* ------------------------------------------------------- reveal toggle */

    function addToggle(input) {
        if (input.hasAttribute('data-pw-no-toggle')) { return; }
        if (input.parentNode && input.parentNode.classList.contains('pw-field')) { return; }

        var wrap = document.createElement('div');
        wrap.className = 'pw-field ' + PW.field;
        input.parentNode.insertBefore(wrap, input);
        wrap.appendChild(input);
        input.className = (input.className + ' ' + PW.fieldInput).trim();

        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'pw-toggle ' + PW.toggle;
        btn.setAttribute('aria-pressed', 'false');
        btn.setAttribute('aria-label', T('Show password'));
        if (input.id) { btn.setAttribute('aria-controls', input.id); }
        btn.appendChild(svgIcon('eye', 'pw-toggle-off'));
        btn.appendChild(svgIcon('eye-off', 'pw-toggle-on'));

        btn.addEventListener('click', function () {
            var shown = input.type === 'text';
            // Swap on the input itself so password managers keep tracking it.
            input.type = shown ? 'password' : 'text';
            btn.setAttribute('aria-pressed', shown ? 'false' : 'true');
            btn.setAttribute('aria-label', shown ? T('Show password') : T('Hide password'));
            // Typing position survives the swap; without this the caret jumps
            // to the start in Safari.
            var pos = input.value.length;
            input.focus();
            try { input.setSelectionRange(pos, pos); } catch (e) { /* number-ish inputs */ }
        });

        wrap.appendChild(btn);
    }

    /* ---------------------------------------------------------- checklist */

    function buildRules(input) {
        /* Kept in step with LETTER / NUMBER / SYMBOL in
           apps/accounts/password_validators.py. */
        var RULES = [
            {
                key: 'length',
                text: T('At least 8 characters'),
                test: function (pw) { return pw.length >= 8; }
            },
            {
                key: 'letter',
                text: T('A letter'),
                test: function (pw) { return /[A-Za-z]/.test(pw); }
            },
            {
                key: 'number',
                text: T('A number'),
                test: function (pw) { return /[0-9]/.test(pw); }
            },
            {
                key: 'symbol',
                text: T('A symbol, such as ! ? # or @'),
                test: function (pw) { return /[^A-Za-z0-9]/.test(pw); }
            }
        ];

        /* Shown only while it is failing — see the header note. Not counted
           towards the meter, which reports on the four rules above. */
        var GUARDS = [
            {
                key: 'not-similar',
                text: T('Too like your name, username or email'),
                test: function (pw) { return !tooSimilar(pw, readSimilarValues(input)); }
            }
        ];

        var box = document.createElement('div');
        box.className = 'pw-rules ' + PW.rules;
        box.hidden = true;

        var label = document.createElement('p');
        label.className = 'pw-rules__label ' + PW.rulesLabel;
        label.textContent = T('Your password needs');
        box.appendChild(label);

        var list = document.createElement('ul');
        list.className = 'pw-rules__list ' + PW.rulesList;

        RULES.concat(GUARDS).forEach(function (rule) {
            var li = document.createElement('li');
            li.className = 'pw-rule ' + PW.rule;
            li.setAttribute('data-rule', rule.key);

            var mark = document.createElement('span');
            mark.className = 'pw-rule-mark ' + PW.ruleMark;
            mark.appendChild(svgIcon('check', 'pw-rule-tick'));
            var dot = document.createElement('span');
            dot.className = 'pw-rule-dot';
            dot.textContent = '·';
            mark.appendChild(dot);

            var text = document.createElement('span');
            text.textContent = rule.text;

            li.appendChild(mark);
            li.appendChild(text);
            list.appendChild(li);
            rule.el = li;
        });

        box.appendChild(list);

        var note = document.createElement('p');
        note.className = 'pw-rules__note ' + PW.rulesNote;
        note.textContent = T('Common passwords are also refused when you submit.');
        box.appendChild(note);

        /* Advisory meter — not a requirement, and labelled in words as well
           as colour so it survives greyscale. */
        var meter = document.createElement('div');
        meter.className = 'pw-meter ' + PW.meter;
        var track = document.createElement('div');
        track.className = 'pw-meter__track ' + PW.meterTrack;
        var fill = document.createElement('div');
        fill.className = 'pw-meter__fill ' + PW.meterFill;
        track.appendChild(fill);
        var word = document.createElement('span');
        word.className = 'pw-meter__word ' + PW.meterWord;
        word.textContent = T('Strength');
        meter.appendChild(track);
        meter.appendChild(word);
        box.appendChild(meter);

        /* One live region for the whole checklist. Announcing each rule as it
           flips would talk over someone still typing. */
        var status = document.createElement('p');
        status.className = 'sr-only ' + PW.srOnly;
        status.setAttribute('role', 'status');
        status.setAttribute('aria-live', 'polite');
        box.appendChild(status);

        var host = input.closest('.pw-field') || input;
        host.parentNode.insertBefore(box, host.nextSibling);

        /* Once the four rules are met the password already holds a letter, a
           digit and a symbol, so counting character classes no longer tells
           two passing passwords apart — length does, and length is what
           actually buys entropy. Mixed case is the one class not required,
           so it is the only variety still worth a look. */
        function score(pw, metCount) {
            if (!pw) { return null; }
            if (metCount < RULES.length) { return 'weak'; }
            var mixedCase = /[a-z]/.test(pw) && /[A-Z]/.test(pw);
            if (pw.length >= 14 || (pw.length >= 12 && mixedCase)) { return 'strong'; }
            return 'fair';
        }

        var lastSpoken = '';

        function evaluate() {
            var pw = input.value;
            box.hidden = pw.length === 0;

            var met = 0;
            RULES.forEach(function (rule) {
                var ok = rule.test(pw);
                rule.el.classList.toggle('pw-rule--met', ok);
                PW.ruleMet.split(' ').forEach(function (c) {
                    rule.el.classList.toggle(c, ok);
                });
                if (ok) { met += 1; }
            });

            /* A guard is invisible until it trips, so it never reads as a
               rule left to satisfy. */
            GUARDS.forEach(function (rule) {
                rule.el.hidden = rule.test(pw);
            });

            var level = score(pw, met);
            fill.className = 'pw-meter__fill ' + PW.meterFill
                + (level ? ' pw-meter__fill--' + level + ' ' + PW['meterFill' + level.charAt(0).toUpperCase() + level.slice(1)] : '');
            word.className = 'pw-meter__word ' + PW.meterWord
                + (level ? ' pw-meter__word--' + level + ' ' + PW['meterWord' + level.charAt(0).toUpperCase() + level.slice(1)] : '');
            word.textContent = level
                ? { weak: T('Weak'), fair: T('Fair'), strong: T('Strong') }[level]
                : T('Strength');

            var spoken = met + '/' + RULES.length;
            if (spoken !== lastSpoken) {
                lastSpoken = spoken;
                status.textContent = met + ' ' + T('of') + ' ' + RULES.length + ' ' +
                    T('password requirements met');
            }
        }

        input.addEventListener('input', evaluate);
        // The similarity rule depends on the other fields, so recheck when
        // they change too — typing a username after the password should not
        // leave a stale green tick.
        (input.getAttribute('data-pw-similar-to') || '').split(',').forEach(function (one) {
            var el = one.trim() && document.querySelector(one.trim());
            if (el) { el.addEventListener('input', evaluate); }
        });

        evaluate();
    }

    /* ------------------------------------------------------------- confirm */

    function buildMatch(input) {
        var target = document.querySelector(input.getAttribute('data-pw-confirms'));
        if (!target) { return; }

        var out = document.createElement('p');
        out.className = 'pw-match ' + PW.match;
        out.setAttribute('role', 'status');
        out.setAttribute('aria-live', 'polite');
        out.hidden = true;

        var host = input.closest('.pw-field') || input;
        host.parentNode.insertBefore(out, host.nextSibling);

        function evaluate() {
            if (!input.value) { out.hidden = true; return; }
            var ok = input.value === target.value;
            out.hidden = false;
            out.className = 'pw-match ' + PW.match + ' '
                + (ok ? 'pw-match--ok ' + PW.matchOk : 'pw-match--bad ' + PW.matchBad);
            out.textContent = ok ? T('Passwords match') : T("Passwords don't match yet");
        }

        input.addEventListener('input', evaluate);
        target.addEventListener('input', evaluate);
    }

    /* ---------------------------------------------------------------- init */

    function init() {
        var fields = document.querySelectorAll('input[type="password"]');
        Array.prototype.forEach.call(fields, addToggle);
        Array.prototype.forEach.call(
            document.querySelectorAll('input[data-pw-strength]'), buildRules);
        Array.prototype.forEach.call(
            document.querySelectorAll('input[data-pw-confirms]'), buildMatch);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
}());
