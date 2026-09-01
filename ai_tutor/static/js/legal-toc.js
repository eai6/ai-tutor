/* Table of contents for a legal document.
 *
 * Built from the rendered document's own headings rather than maintained
 * alongside it, so it cannot drift from text an administrator edits in the
 * dashboard. The heading ids come from render_markdown (dashboard_extras).
 *
 * Progressive enhancement: with no JavaScript the contents panel stays
 * hidden and the document reads normally.
 */
(function () {
    'use strict';

    var body = document.getElementById('legal-body');
    var toc = document.getElementById('legal-toc');
    var list = document.getElementById('legal-toc-list');

    if (body && toc && list) {
        // Whichever level the document actually uses for its sections. An
        // administrator writing "## 1. Data" and one writing "### 1. Data"
        // both get a contents list; listing every level would mirror the
        // document instead of summarising it.
        var headings = body.querySelectorAll('h2[id]');
        if (headings.length < 2) { headings = body.querySelectorAll('h3[id]'); }

        if (headings.length >= 2) {
            Array.prototype.forEach.call(headings, function (h) {
                var li = document.createElement('li');
                var a = document.createElement('a');
                a.href = '#' + h.id;
                a.textContent = h.textContent;
                li.appendChild(a);
                list.appendChild(li);
            });
            toc.hidden = false;
            // Turns on the two-column grid; see marketing/legal.css.
            var layout = toc.closest('.legal');
            if (layout) { layout.classList.add('legal--with-toc'); }
        }
    }

    /* --------------------------------------------------------------- print */
    var printBtn = document.getElementById('legal-print');
    if (printBtn) {
        printBtn.addEventListener('click', function () {
            window.print();
        });
    }
}());
