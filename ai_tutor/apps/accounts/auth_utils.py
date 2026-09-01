"""Signing in a user the application just created.

``django.contrib.auth.login()`` needs to know which backend authenticated the
user. It reads that from ``user.backend``, an attribute ``authenticate()``
sets on its way out. A login view is therefore fine: it calls
``authenticate()`` first, and the attribute is there.

A registration view is not. It builds the account itself with
``User.objects.create_user()``, so nothing ever set ``.backend``. While the
project had a single entry in ``AUTHENTICATION_BACKENDS`` Django simply used
it; once ``AxesStandaloneBackend`` was added alongside ``ModelBackend`` for
login lockout, Django could no longer guess and started raising::

    ValueError: You have multiple authentication backends configured and
    therefore must provide the `backend` argument or set the `backend`
    attribute on the user.

That broke student self-registration, staff invitation registration and the
desktop claim flow — each one committing the new account and *then* dying, so
the person saw an error and their retry was refused as a duplicate username.

Use :func:`login_created_user` from any view that creates its own account.
"""

from django.contrib.auth import login

#: The backend that actually verifies passwords. Axes sits in front of it as a
#: lockout gate and never authenticates anyone on its own, so a user we
#: created ourselves belongs to this one.
PASSWORD_BACKEND = 'django.contrib.auth.backends.ModelBackend'


def login_created_user(request, user):
    """Start a session for a user this request just created.

    Equivalent to ``login(request, user)`` but names the backend explicitly,
    which is required whenever ``AUTHENTICATION_BACKENDS`` holds more than one
    entry and the user did not come from ``authenticate()``.

    Do **not** use this to complete a normal sign-in — those paths call
    ``authenticate(request, ...)``, which both enforces the axes lockout and
    sets ``user.backend`` for you.
    """
    login(request, user, backend=PASSWORD_BACKEND)
