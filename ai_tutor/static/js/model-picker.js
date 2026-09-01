/* Model name: pick from a list instead of typing an id from memory.
 *
 * The settings page asked an administrator to type `claude-sonnet-4-20250514`
 * exactly, into a box too narrow to show the whole string. A typo there does
 * not fail on save — the config stores fine and the next tutoring call is what
 * breaks, a long way from the page that caused it.
 *
 * The text input is still the field that submits; this only drives it. So:
 *   - no view or form change,
 *   - with JavaScript off the page is exactly what it was,
 *   - and "Other" always exists, because model ids change faster than
 *     apps/llm/catalog.py will.
 *
 * Markup contract on the <select>:
 *   data-model-input="#id"      the text input it drives
 *   data-provider-select="#id"  the provider whose list to show
 *   data-catalog="text|image"
 */
(function () {
    'use strict';

    var T = (window.gettext || function (s) { return s; });
    var OTHER = '__other__';

    function readCatalog(id) {
        var el = document.getElementById(id);
        if (!el) { return {}; }
        try { return JSON.parse(el.textContent) || {}; } catch (e) { return {}; }
    }

    var CATALOGS = {
        text: readCatalog('text-model-catalog'),
        image: readCatalog('image-model-catalog')
    };

    function setup(picker) {
        var input = document.querySelector(picker.getAttribute('data-model-input'));
        var provider = document.querySelector(picker.getAttribute('data-provider-select'));
        var catalog = CATALOGS[picker.getAttribute('data-catalog')] || {};
        if (!input || !provider) { return; }

        // The input is the source of truth; the picker is a way to set it.
        // Keeping it in the DOM (just hidden) means the form posts unchanged.
        var wrap = document.createElement('div');
        wrap.className = 'model-custom';
        input.parentNode.insertBefore(wrap, input);
        wrap.appendChild(input);

        var hint = document.createElement('p');
        hint.className = 'model-custom__hint';
        hint.textContent = T('Type the model id exactly as the provider publishes it.');
        wrap.appendChild(hint);

        function render(preserve) {
            var models = catalog[provider.value] || [];
            var current = preserve ? input.value : '';
            picker.textContent = '';

            models.forEach(function (pair) {
                var opt = document.createElement('option');
                opt.value = pair[0];
                // id under the label: an admin recognises "Claude Sonnet 4",
                // but what actually gets saved is the id, so show both.
                opt.textContent = pair[1] + ' — ' + pair[0];
                picker.appendChild(opt);
            });

            var other = document.createElement('option');
            other.value = OTHER;
            other.textContent = models.length
                ? T('Other — type a model id')
                : T('Type a model id');
            picker.appendChild(other);

            // A saved value the catalog does not list is not an error — it is
            // a newer model. Select "Other" and show it rather than silently
            // replacing what an administrator configured.
            var known = models.some(function (pair) { return pair[0] === current; });
            picker.value = (current && known) ? current : OTHER;
            applyMode();
        }

        function applyMode() {
            var custom = picker.value === OTHER;
            wrap.hidden = !custom;
            if (!custom) { input.value = picker.value; }
        }

        picker.addEventListener('change', function () {
            if (picker.value === OTHER) {
                wrap.hidden = false;
                input.focus();
            } else {
                input.value = picker.value;
                wrap.hidden = true;
            }
        });

        // The provider's own onchange already rewrites the input with that
        // provider's default; re-render after it so the picker agrees.
        provider.addEventListener('change', function () {
            window.setTimeout(function () { render(true); }, 0);
        });

        render(true);
    }

    function init() {
        Array.prototype.forEach.call(
            document.querySelectorAll('.model-picker'), setup);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
}());
