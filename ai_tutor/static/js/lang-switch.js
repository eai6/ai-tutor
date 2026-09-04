/* Language picker — submit on change.
 *
 * The picker is a plain <form> with a <select> and a submit button, so it
 * works with no JavaScript at all: pick a language, press the button. That
 * matters here more than on most products — this runs offline on a machine in
 * a classroom, and the public pages otherwise ship no script.
 *
 * With JavaScript, the button is redundant: changing the select submits. So
 * the button is hidden HERE rather than in the template, which is the only
 * ordering that is safe. Hidden in CSS and revealed by script, a slow or
 * blocked script leaves a select nobody can act on; hidden by the script that
 * replaces it, the control is always operable.
 *
 * No inline handler: `onchange="this.form.submit()"` is what CSP_STRICT_SCRIPTS
 * exists to forbid, and the dashboard already carries enough of those to block
 * enforcement. See apps/safety/csp.py.
 */
(function () {
    'use strict';

    function init() {
        var forms = document.querySelectorAll('[data-lang-switch]');
        Array.prototype.forEach.call(forms, function (form) {
            var select = form.querySelector('select[name="language"]');
            if (!select) { return; }

            var go = form.querySelector('[data-lang-go]');
            if (go) { go.hidden = true; }

            select.addEventListener('change', function () {
                // Nothing to do when the choice lands back on the language
                // already showing — a needless round trip that also loses
                // the reader's scroll position.
                if (select.value === select.getAttribute('data-current')) {
                    return;
                }
                form.submit();
            });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
}());
