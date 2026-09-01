/* Student shell behaviour.
 *
 * Two jobs, both small:
 *   1. Close the account menu on outside click / Escape — <details> handles
 *      opening, keyboard focus and screen-reader semantics on its own, but it
 *      has no concept of "click elsewhere to dismiss".
 *   2. Start the offline banner and the service worker, which used to be an
 *      inline script in base.html.
 */
(function () {
    'use strict';

    /* ------------------------------------------------------- account menu */
    var menu = document.getElementById('account-menu');

    if (menu) {
        document.addEventListener('click', function (e) {
            if (menu.open && !menu.contains(e.target)) { menu.open = false; }
        });

        document.addEventListener('keydown', function (e) {
            if (e.key !== 'Escape' || !menu.open) { return; }
            menu.open = false;
            // Send focus back to the trigger, or the next Tab starts from the
            // top of the document.
            var summary = menu.querySelector('summary');
            if (summary) { summary.focus(); }
        });

        // Tabbing past the last item should close it rather than leaving an
        // open panel floating over the page.
        menu.addEventListener('focusout', function (e) {
            if (menu.open && !menu.contains(e.relatedTarget)) { menu.open = false; }
        });
    }

    /* --------------------------------------------------- network + PWA */
    document.addEventListener('DOMContentLoaded', function () {
        if (window.NetHelpers) {
            window.NetHelpers.installOfflineBanner();
            window.NetHelpers.registerServiceWorker();
        }
    });
}());
