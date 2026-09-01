/* Flash messages: a close button on each, and a timer on the ones that are
 * only a confirmation.
 *
 * Two rules decide whether a banner leaves on its own:
 *
 *   success / info  — "Welcome back", "Settings updated". The page behind it
 *                     already shows the result, so the banner is a receipt.
 *                     It goes.
 *   error / warning — "Email already in use", "Current password is incorrect".
 *                     These name something still to be done, and a message
 *                     that removes itself before it is read is worse than no
 *                     message. They stay until dismissed.
 *
 * Timing follows WCAG 2.2.1: the countdown pauses while the pointer is over
 * the banner or the keyboard focus is inside it, so nothing disappears out
 * from under someone in the middle of reading it. The close button is built
 * here rather than in the template, so with JavaScript off there is neither a
 * dead control nor a disappearing banner.
 */
(function () {
    'use strict';

    var T = (window.gettext || function (s) { return s; });

    var TRANSIENT = /\balert--(success|info)\b/;

    /* Long enough to read, scaled by how much there is to read, capped so a
       stray paragraph cannot pin a banner to the page. */
    var BASE_MS = 4000;
    var PER_CHAR_MS = 45;
    var MAX_MS = 12000;

    function lifetime(text) {
        return Math.min(BASE_MS + text.length * PER_CHAR_MS, MAX_MS);
    }

    function closeButton() {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'alert__close';
        btn.setAttribute('aria-label', T('Dismiss this message'));

        var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('class', 'icon');
        svg.setAttribute('aria-hidden', 'true');
        var use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
        use.setAttribute('href', '#i-close');
        svg.appendChild(use);
        btn.appendChild(svg);
        return btn;
    }

    function remove(alert) {
        if (alert.classList.contains('is-leaving')) { return; }
        // Height has to be a number before it can animate to zero; it is auto
        // until we say otherwise.
        alert.style.maxHeight = alert.scrollHeight + 'px';
        // Read it back so the browser commits that height as the start value
        // rather than collapsing the two writes into one frame.
        void alert.offsetHeight;
        alert.classList.add('is-leaving');

        var done = function () {
            var region = alert.parentNode;
            if (!region) { return; }
            region.removeChild(alert);
            // An empty region still carries its bottom margin.
            if (!region.querySelector('.alert')) { region.remove(); }
        };
        alert.addEventListener('transitionend', done, { once: true });
        // transitionend never fires under prefers-reduced-motion, and a
        // backgrounded tab can drop it too.
        window.setTimeout(done, 600);
    }

    function decorate(alert) {
        // The message text is wrapped so the close button cannot be pushed
        // onto its own line by a long one.
        var span = alert.querySelector('span');
        if (span) { span.classList.add('alert__text'); }

        var btn = closeButton();
        btn.addEventListener('click', function () { remove(alert); });
        alert.appendChild(btn);

        // extra_tags='sticky' on the message. For the rare success that is not
        // just a receipt — one carrying an address, a code, or an instruction
        // the page itself does not repeat.
        if (alert.classList.contains('sticky')) { return; }
        if (!TRANSIENT.test(alert.className)) { return; }

        var ms = lifetime(alert.textContent.trim());
        var timer = window.setTimeout(function () { remove(alert); }, ms);
        var cancel = function () { window.clearTimeout(timer); };

        // Pause, and do not restart: once someone has reached for it, the
        // banner is theirs to close.
        alert.addEventListener('mouseenter', cancel);
        alert.addEventListener('focusin', cancel);
    }

    function init() {
        Array.prototype.forEach.call(
            document.querySelectorAll('.messages .alert'), decorate);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
}());
