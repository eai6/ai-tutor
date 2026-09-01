/* Copy button on every code block.
 *
 * These pages are a command reference — someone reading them is retyping shell
 * lines into a terminal, and a mistyped `--rm ghcr.io/eai6/ai-tutor:latest`
 * fails in a way that does not name the typo. Selecting multi-line commands by
 * hand also picks up the wrapped-line breaks.
 *
 * Progressive enhancement: no button exists without JavaScript, and the code
 * stays selectable either way.
 */
(function () {
    'use strict';

    var T = (window.gettext || function (s) { return s; });

    /* The two glyphs, built as DOM rather than assigned through innerHTML.
       The label beside them comes from gettext(), and a translation catalogue
       is project-authored but still not something to pass to an HTML parser —
       createElementNS + textContent cannot inject markup at all. Inline
       rather than from the sprite because these are standalone pages that load
       no other icons, and one path beats a 7 KB sprite include. */
    var SVG_NS = 'http://www.w3.org/2000/svg';

    var GLYPHS = {
        copy: ['M9 9h10a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2V11a2 2 0 0 1 2-2z',
               'M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1'],
        done: ['M20 6L9 17l-5-5']
    };

    function glyph(name, weight) {
        var svg = document.createElementNS(SVG_NS, 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('width', '15');
        svg.setAttribute('height', '15');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('stroke-width', weight || '2');
        svg.setAttribute('stroke-linecap', 'round');
        svg.setAttribute('stroke-linejoin', 'round');
        svg.setAttribute('aria-hidden', 'true');
        GLYPHS[name].forEach(function (d) {
            var path = document.createElementNS(SVG_NS, 'path');
            path.setAttribute('d', d);
            svg.appendChild(path);
        });
        return svg;
    }

    /** Replace a button's contents with an icon and a label, no HTML parsing. */
    function setFace(btn, name, label, weight) {
        btn.textContent = '';
        btn.appendChild(glyph(name, weight));
        var span = document.createElement('span');
        span.textContent = label;
        btn.appendChild(span);
    }

    function textOf(pre) {
        // textContent, not innerText: innerText collapses the blank lines that
        // separate one command from the next in these blocks.
        var code = pre.querySelector('code');
        return (code || pre).textContent.replace(/\s+$/, '');
    }

    /** navigator.clipboard needs a secure context. A self-hosting guide is read
     *  over plain HTTP on a LAN often enough that the fallback matters. */
    function copy(text) {
        if (navigator.clipboard && window.isSecureContext) {
            return navigator.clipboard.writeText(text);
        }
        return new Promise(function (resolve, reject) {
            var ta = document.createElement('textarea');
            ta.value = text;
            ta.setAttribute('readonly', '');
            ta.style.position = 'fixed';
            ta.style.top = '-1000px';
            document.body.appendChild(ta);
            ta.select();
            try {
                document.execCommand('copy') ? resolve() : reject();
            } catch (e) {
                reject(e);
            }
            document.body.removeChild(ta);
        });
    }

    function decorate(pre) {
        if (pre.parentNode.classList.contains('copy-wrap')) { return; }

        var wrap = document.createElement('div');
        wrap.className = 'copy-wrap';
        pre.parentNode.insertBefore(wrap, pre);
        wrap.appendChild(pre);

        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'copy-btn';
        setFace(btn, 'copy', T('Copy'));
        btn.setAttribute('aria-label', T('Copy code to clipboard'));

        // Announced separately so a screen reader hears the result without the
        // button's own label being rewritten under the user's cursor.
        var status = document.createElement('span');
        status.className = 'copy-status';
        status.setAttribute('role', 'status');
        status.setAttribute('aria-live', 'polite');

        var reset;
        btn.addEventListener('click', function () {
            copy(textOf(pre)).then(function () {
                btn.classList.add('is-done');
                setFace(btn, 'done', T('Copied'), '2.5');
                status.textContent = T('Copied to clipboard');
            }, function () {
                btn.classList.add('is-failed');
                setFace(btn, 'copy', T('Press Ctrl+C'));
                status.textContent = T('Copy failed — select the text and press Ctrl+C');
            });

            window.clearTimeout(reset);
            reset = window.setTimeout(function () {
                btn.classList.remove('is-done', 'is-failed');
                setFace(btn, 'copy', T('Copy'));
                status.textContent = '';
            }, 2000);
        });

        wrap.appendChild(btn);
        wrap.appendChild(status);
    }

    function init() {
        Array.prototype.forEach.call(document.querySelectorAll('pre'), decorate);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
}());
