/* Documentation index — search filter and print.
 *
 * Progressive enhancement. With no JavaScript the index page is a complete
 * set of links and the search field simply does nothing, which is the right
 * failure: 23 sections all reachable beats a search box that 404s.
 *
 * The filter runs in two tiers, and the second one arrives late on purpose.
 *
 *   1. data-terms, rendered by the server onto every row: the section's
 *      number, title, summary and subheadings. Present on first paint, so
 *      the first keystroke is answered immediately.
 *   2. the word index at /docs/search-index.json, fetched once on the first
 *      keystroke. 45 KB that most visitors never need, and it is what makes
 *      "NAT gateway", "pgvector" or "Jetson" find the section that discusses
 *      them rather than nothing.
 *
 * If the fetch fails the search keeps working on tier one, which is why
 * nothing here reports the error to the reader — there is no broken state to
 * report, only a narrower one.
 */
(function () {
    'use strict';

    var form = document.getElementById('docs-search');
    var input = document.getElementById('docs-search-input');
    var status = document.getElementById('docs-search-status');

    if (form && input && status) {
        var rows = Array.prototype.slice.call(
            document.querySelectorAll('[data-terms]')
        );
        // Containers that disappear once nothing inside them matches, in the
        // order they nest: a card before the band that holds it.
        var groups = [
            document.querySelectorAll('.docs-card'),
            document.querySelectorAll('.docs-part')
        ];

        var words = null;      // slug -> the section's distinct words
        var pending = false;

        function loadIndex() {
            if (words || pending) { return; }
            pending = true;
            fetch(form.getAttribute('data-index'), { credentials: 'same-origin' })
                .then(function (r) { return r.ok ? r.json() : null; })
                .then(function (data) {
                    if (!data) { return; }
                    words = data;
                    apply(input.value);   // re-run with the wider index
                })
                .catch(function () { /* tier one still works */ });
        }

        // Every word of the query has to appear, and each one is matched on
        // its own. The word index is a sorted set of the section's distinct
        // words, so "NAT gateway" is never a substring of it however many
        // times both words occur — only "nat" and "gateway" separately are.
        // Matching per token also makes "cost model" mean both, which is what
        // someone typing two words means.
        function matches(row, tokens) {
            var terms = row.getAttribute('data-terms');
            var bag = words && words[row.getAttribute('data-slug')];
            return tokens.every(function (token) {
                return terms.indexOf(token) !== -1
                    || (!!bag && bag.indexOf(token) !== -1);
            });
        }

        function apply(query) {
            var q = query.trim().toLowerCase();
            var tokens = q ? q.split(/\s+/) : [];
            // Distinct sections, not rows: one section can appear both in a
            // card list and as another card's footer link, and the reader is
            // being told how many sections matched.
            var seen = {};

            rows.forEach(function (row) {
                var hit = !q || matches(row, tokens);
                row.hidden = !hit;
                if (hit) { seen[row.getAttribute('data-slug')] = true; }
            });

            groups.forEach(function (nodes) {
                Array.prototype.forEach.call(nodes, function (node) {
                    node.hidden = !!q && !node.querySelector('[data-terms]:not([hidden])');
                });
            });

            if (!q) {
                status.hidden = true;
                status.textContent = '';
                return;
            }
            var matched = Object.keys(seen).length;
            status.hidden = false;
            status.textContent = matched === 0
                ? 'No section matches “' + query.trim() + '”.'
                : matched + (matched === 1 ? ' section matches “' : ' sections match “')
                    + query.trim() + '”.';
        }

        input.addEventListener('input', function () {
            loadIndex();
            apply(input.value);
        });

        // Submitting opens the first match rather than reloading the page —
        // pressing Enter after typing is a request to go there.
        form.addEventListener('submit', function (event) {
            event.preventDefault();
            var first = document.querySelector(
                '.docs-card__item:not([hidden]) a, .docs-card__more:not([hidden]) a'
            );
            if (first && input.value.trim()) { window.location.href = first.href; }
        });

        // Restores the page on Escape. A type="search" field clears itself in
        // some browsers and not others; the filter has to be told either way.
        input.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') { input.value = ''; apply(''); }
        });

        // A value survives a back button in some browsers.
        if (input.value) { loadIndex(); apply(input.value); }
    }

    var print = document.getElementById('docs-print');
    if (print) {
        print.addEventListener('click', function () { window.print(); });
    }
}());
