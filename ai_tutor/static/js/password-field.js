/* Password fields: reveal toggle, requirement checklist, match indicator.
 *
 * Progressive enhancement. Every <input type="password"> gets a reveal
 * toggle; a field marked data-pw-strength also gets a live checklist and an
 * advisory strength meter. With JavaScript off, every form works exactly as
 * it did before.
 *
 * ── Why these three rules and not others ────────────────────────────────
 * The checklist mirrors AUTH_PASSWORD_VALIDATORS in config/settings.py, so
 * a green tick means the server will accept that aspect. Three of the four
 * configured validators are decidable in the browser:
 *
 *   MinimumLengthValidator          → at least 8 characters
 *   NumericPasswordValidator        → not entirely digits
 *   UserAttributeSimilarityValidator→ not too like the name/username/email,
 *                                     reimplemented below against the same
 *                                     0.7 quick_ratio threshold Django uses
 *
 * The fourth, CommonPasswordValidator, tests a 20,000-entry list. Shipping a
 * subset would show a green tick for passwords the server then rejects, which
 * is worse than not showing the rule — so it is stated as a note instead and
 * enforced on submit.
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
        svg.setAttribute('class', 'icon' + (extraClass ? ' ' + extraClass : ''));
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
        wrap.className = 'pw-field';
        input.parentNode.insertBefore(wrap, input);
        wrap.appendChild(input);

        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'pw-toggle';
        btn.setAttribute('aria-pressed', 'false');
        btn.setAttribute('aria-label', T('Show password'));
        if (input.id) { btn.setAttribute('aria-controls', input.id); }
        btn.appendChild(svgIcon('eye', 'pw-toggle__off'));
        btn.appendChild(svgIcon('eye-off', 'pw-toggle__on'));

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
        var RULES = [
            {
                key: 'length',
                text: T('At least 8 characters'),
                test: function (pw) { return pw.length >= 8; }
            },
            {
                key: 'not-numeric',
                text: T('Not only numbers'),
                test: function (pw) { return !/^\d+$/.test(pw); }
            },
            {
                key: 'not-similar',
                text: T('Not too like your name, username or email'),
                test: function (pw) { return !tooSimilar(pw, readSimilarValues(input)); }
            }
        ];

        var box = document.createElement('div');
        box.className = 'pw-rules pw-rules--idle';

        var label = document.createElement('p');
        label.className = 'pw-rules__label';
        label.textContent = T('Your password needs');
        box.appendChild(label);

        var list = document.createElement('ul');
        list.className = 'pw-rules__list';

        RULES.forEach(function (rule) {
            var li = document.createElement('li');
            li.className = 'pw-rule';
            li.setAttribute('data-rule', rule.key);

            var mark = document.createElement('span');
            mark.className = 'pw-rule__mark';
            mark.appendChild(svgIcon('check', 'pw-rule__tick'));
            var dot = document.createElement('span');
            dot.className = 'pw-rule__dot';
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
        note.className = 'pw-rules__note';
        note.textContent = T('Common passwords are also refused when you submit.');
        box.appendChild(note);

        /* Advisory meter — not a requirement, and labelled in words as well
           as colour so it survives greyscale. */
        var meter = document.createElement('div');
        meter.className = 'pw-meter';
        var track = document.createElement('div');
        track.className = 'pw-meter__track';
        var fill = document.createElement('div');
        fill.className = 'pw-meter__fill';
        track.appendChild(fill);
        var word = document.createElement('span');
        word.className = 'pw-meter__word';
        word.textContent = T('Strength');
        meter.appendChild(track);
        meter.appendChild(word);
        box.appendChild(meter);

        /* One live region for the whole checklist. Announcing each rule as it
           flips would talk over someone still typing. */
        var status = document.createElement('p');
        status.className = 'sr-only';
        status.setAttribute('role', 'status');
        status.setAttribute('aria-live', 'polite');
        box.appendChild(status);

        var host = input.closest('.pw-field') || input;
        host.parentNode.insertBefore(box, host.nextSibling);

        function score(pw, metCount) {
            if (!pw) { return null; }
            if (metCount < RULES.length) { return 'weak'; }
            var variety = 0;
            if (/[a-z]/.test(pw)) { variety += 1; }
            if (/[A-Z]/.test(pw)) { variety += 1; }
            if (/\d/.test(pw)) { variety += 1; }
            if (/[^A-Za-z0-9]/.test(pw)) { variety += 1; }
            if (pw.length >= 14 && variety >= 3) { return 'strong'; }
            if (pw.length >= 12 || variety >= 3) { return 'fair'; }
            return 'fair';
        }

        var lastSpoken = '';

        function evaluate() {
            var pw = input.value;
            box.classList.toggle('pw-rules--idle', pw.length === 0);

            var met = 0;
            RULES.forEach(function (rule) {
                var ok = pw.length > 0 && rule.test(pw);
                rule.el.classList.toggle('pw-rule--met', ok);
                if (ok) { met += 1; }
            });

            var level = score(pw, met);
            fill.className = 'pw-meter__fill' + (level ? ' pw-meter__fill--' + level : '');
            word.className = 'pw-meter__word' + (level ? ' pw-meter__word--' + level : '');
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
        out.className = 'pw-match';
        out.setAttribute('role', 'status');
        out.setAttribute('aria-live', 'polite');
        out.hidden = true;

        var host = input.closest('.pw-field') || input;
        host.parentNode.insertBefore(out, host.nextSibling);

        function evaluate() {
            if (!input.value) { out.hidden = true; return; }
            var ok = input.value === target.value;
            out.hidden = false;
            out.className = 'pw-match ' + (ok ? 'pw-match--ok' : 'pw-match--bad');
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
