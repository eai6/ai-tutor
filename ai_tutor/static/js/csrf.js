/**
 * CSRF token access for fetch() callers.
 *
 * The token is read from the DOM, never from document.cookie. That is not a
 * style preference — the csrftoken cookie is set with HttpOnly (settings.py,
 * CSRF_COOKIE_HTTPONLY), so document.cookie cannot see it and any caller that
 * tries gets an empty string and a 403 from Django.
 *
 * Finding F-05 of the 2026-08-13 assessment. Django leaves the CSRF cookie
 * readable on purpose, so that front-end code can copy it into an X-CSRFToken
 * header, and that default is fine on its own. It stops being fine in
 * combination: with no enforced CSP to contain an XSS (F-02) and 22 places
 * where request input reaches an HTML attribute (F-07), a readable token is
 * what lets injected script forge state-changing requests that would otherwise
 * be blocked. It was the cheapest link in that chain to remove.
 *
 * Two entry points, because the codebase had grown both spellings:
 *
 *   csrfToken()          preferred
 *   getCookie('csrftoken')  kept working, so that 21 existing call sites did
 *                           not all have to change shape at once
 *
 * getCookie deliberately answers ONLY for 'csrftoken'. Reviving it as a general
 * cookie reader would quietly reintroduce the pattern this file exists to
 * remove. Nothing in the application reads any other cookie from JavaScript.
 *
 * Source order: <meta name="csrf-token"> in the page head, which both base
 * templates render, falling back to the hidden input Django writes for
 * {% csrf_token %}. The meta tag is the reliable one — plenty of pages issue
 * fetch() calls without containing a form.
 */
(function () {
    'use strict';

    function csrfToken() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        if (meta && meta.content) {
            return meta.content;
        }
        var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        return input ? input.value : '';
    }

    function getCookie(name) {
        if (name !== 'csrftoken') {
            console.warn(
                'getCookie() only serves the CSRF token; ' +
                'reading cookies from JavaScript is deliberately not supported. ' +
                'Requested: ' + name
            );
            return '';
        }
        return csrfToken();
    }

    window.csrfToken = csrfToken;
    window.getCookie = getCookie;
})();
