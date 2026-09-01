/* Teacher dashboard shell behaviour.
 *
 * Moved out of an inline <script> in base.html: an external file is cacheable,
 * needs no CSP nonce, and keeps the template readable. Nothing here knows
 * anything about a page's data — it is chrome only.
 *
 * 1. Off-canvas rail on narrow screens, with real focus management.
 * 2. Remembered open/closed state for the collapsible nav groups.
 */
(function () {
    'use strict';

    var MOBILE = '(max-width: 900px)';
    var rail = document.getElementById('app-rail');
    var scrim = document.getElementById('rail-scrim');
    var toggle = document.getElementById('rail-toggle');

    /* ---------------------------------------------------------------- rail */
    if (rail && scrim && toggle) {
        var lastFocused = null;

        function isNarrow() {
            return window.matchMedia(MOBILE).matches;
        }

        function openRail() {
            lastFocused = document.activeElement;
            rail.classList.add('is-open');
            scrim.classList.add('is-open');
            document.body.classList.add('rail-open');
            toggle.setAttribute('aria-expanded', 'true');
            // Move focus into the drawer, otherwise a keyboard user opens it
            // and their next Tab lands somewhere behind the scrim.
            var first = rail.querySelector('a, button, [tabindex]:not([tabindex="-1"])');
            if (first) { first.focus(); }
        }

        function closeRail(returnFocus) {
            if (!rail.classList.contains('is-open')) { return; }
            rail.classList.remove('is-open');
            scrim.classList.remove('is-open');
            document.body.classList.remove('rail-open');
            toggle.setAttribute('aria-expanded', 'false');
            if (returnFocus) {
                (lastFocused || toggle).focus();
            }
        }

        toggle.addEventListener('click', function () {
            if (rail.classList.contains('is-open')) { closeRail(true); } else { openRail(); }
        });

        scrim.addEventListener('click', function () { closeRail(true); });

        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') { closeRail(true); }
        });

        // Keep focus inside the drawer while it is the only thing on screen.
        rail.addEventListener('keydown', function (e) {
            if (e.key !== 'Tab' || !rail.classList.contains('is-open')) { return; }
            var focusable = rail.querySelectorAll(
                'a[href], button:not([disabled]), summary, input, select, [tabindex]:not([tabindex="-1"])'
            );
            if (!focusable.length) { return; }
            var first = focusable[0];
            var last = focusable[focusable.length - 1];
            if (e.shiftKey && document.activeElement === first) {
                e.preventDefault();
                last.focus();
            } else if (!e.shiftKey && document.activeElement === last) {
                e.preventDefault();
                first.focus();
            }
        });

        // Following a link inside the drawer should dismiss it.
        rail.addEventListener('click', function (e) {
            if (e.target.closest('.nav-link') && isNarrow()) { closeRail(false); }
        });

        // Back above the breakpoint the rail is permanent again — drop the
        // drawer state so it doesn't linger as a scrim over a wide layout.
        window.addEventListener('resize', function () {
            if (!isNarrow()) { closeRail(false); }
        });
    }

    /* ------------------------------------------------- nav group memory */
    // A teacher who works in Curriculum all day shouldn't re-open that group
    // on every page load. Stored per group id; failures are ignored, because
    // a locked-down browser losing this preference is not worth an error.
    var STORE_KEY = 'aitutor.nav.collapsed';

    function readCollapsed() {
        try {
            return JSON.parse(window.localStorage.getItem(STORE_KEY)) || {};
        } catch (err) {
            return {};
        }
    }

    function writeCollapsed(state) {
        try {
            window.localStorage.setItem(STORE_KEY, JSON.stringify(state));
        } catch (err) { /* private mode, quota, or storage disabled */ }
    }

    var collapsed = readCollapsed();

    Array.prototype.forEach.call(
        document.querySelectorAll('.nav-group--collapsible[id]'),
        function (group) {
            if (Object.prototype.hasOwnProperty.call(collapsed, group.id)) {
                group.open = !collapsed[group.id];
            }
            group.addEventListener('toggle', function () {
                collapsed[group.id] = !group.open;
                writeCollapsed(collapsed);
            });
        }
    );
}());
