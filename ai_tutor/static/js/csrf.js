/**
 * CSRF token access for fetch() callers.
 *
 * The token is read from the DOM, not from document.cookie: the csrftoken
 * cookie is HttpOnly, so document.cookie cannot see it and a caller that tries
 * gets an empty string. Read it from the page instead.
 *
 * Two entry points:
 *
 *   csrfToken()             preferred
 *   getCookie('csrftoken')  alias, kept so existing call sites keep working
 *
 * getCookie answers ONLY for 'csrftoken'; it is not a general cookie reader.
 *
 * Source order: <meta name="csrf-token"> in the page head (both base templates
 * render it), falling back to the hidden input Django writes for
 * {% csrf_token %}. The meta tag is the reliable one — many pages issue fetch()
 * calls without containing a form.
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
