/* Client-side row filter for dashboard tables.
 *
 * Generic: any page with an input carrying [data-filter-target] and rows
 * carrying [data-search] gets a filter, with no per-page script. The student
 * list wires itself up by convention (#student-search over .student-row) for
 * backwards compatibility with the markup that already shipped.
 *
 * This filters the CURRENT PAGE only, which is why the result count is
 * announced — a teacher on page 2 of a paginated roster searching for a name
 * that is on page 5 gets "No students match", not silence.
 */
(function () {
    'use strict';

    function debounce(fn, wait) {
        var timer;
        return function () {
            var args = arguments;
            clearTimeout(timer);
            timer = setTimeout(function () { fn.apply(null, args); }, wait);
        };
    }

    function wire(input, rows, emptyEl) {
        if (!input || !rows.length) { return; }

        var apply = debounce(function () {
            var q = input.value.trim().toLowerCase();
            var shown = 0;

            Array.prototype.forEach.call(rows, function (row) {
                var hay = (row.getAttribute('data-search') || row.textContent).toLowerCase();
                var match = !q || hay.indexOf(q) !== -1;
                row.hidden = !match;
                if (match) { shown += 1; }
            });

            if (emptyEl) { emptyEl.hidden = shown !== 0; }
        }, 120);

        input.addEventListener('input', apply);

        // Escape clears the field and restores the full list — the expected
        // behaviour for a search box, and it saves a trip to the mouse.
        input.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && input.value) {
                e.preventDefault();
                input.value = '';
                apply();
            }
        });
    }

    // Convention-based wiring.
    Array.prototype.forEach.call(
        document.querySelectorAll('[data-filter-target]'),
        function (input) {
            wire(
                input,
                document.querySelectorAll(input.getAttribute('data-filter-target')),
                document.querySelector(input.getAttribute('data-filter-empty') || '')
            );
        }
    );

    // Student roster.
    wire(
        document.getElementById('student-search'),
        document.querySelectorAll('.student-row'),
        document.getElementById('no-matches')
    );
}());
