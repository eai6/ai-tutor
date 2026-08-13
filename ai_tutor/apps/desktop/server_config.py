"""Where this device sends work, if anywhere.

One setting: the address of the school's server. The desktop build teaches
entirely offline and stays that way until someone fills this in.

Kept deliberately small. The only real work is turning what a person types into
a usable base URL — people type a bare hostname, or paste a page URL out of
their browser, and both should just work.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse, urlunparse

from django.utils.translation import gettext as _

#: Fail fast to "offline" rather than leaving someone staring at a
#: spinner on a classroom connection.
REQUEST_TIMEOUT = 20

#: Dotted labels (letters, digits, hyphens) or an IPv6 literal. urlparse has
#: already lowercased and stripped the brackets from the latter.
_HOSTNAME_RE = re.compile(
    r'^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$'
    r'|^[0-9a-f:]+$'
)


class ServerConfigError(Exception):
    """A message meant for the screen, not a traceback."""


def _is_local_address(host: str) -> bool:
    """True for loopback and private ranges — a school LAN, not the internet.

    A name that will not resolve counts as not-local: if we cannot tell, assume
    the case that needs protecting.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not (addr.is_loopback or addr.is_private or addr.is_link_local):
            return False
    return bool(infos)


def normalise(raw: str) -> str:
    """Turn typed input into a base URL, or raise ServerConfigError."""
    value = (raw or '').strip()
    if not value:
        raise ServerConfigError(_('Enter the address of your school server.'))

    if '://' not in value:
        # Default to https for anything on the internet — defaulting to http
        # would silently downgrade everyone who just types their hostname.
        #
        # But not for a machine on this network. A school server on
        # 192.168.1.50, or the loopback address during setup, is almost never
        # running TLS, so https:// there produces a connection failure for
        # someone who typed exactly the right address.
        host_only = value.split('/', 1)[0].split(':', 1)[0]
        scheme = 'http' if _is_local_address(host_only) else 'https'
        value = f'{scheme}://{value}'

    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https'):
        raise ServerConfigError(
            _('The address should start with https:// — “%(scheme)s” is not '
              'supported.') % {'scheme': parsed.scheme}
        )
    # A hostname, an IPv4 address, or a bracketed IPv6 literal. Without this,
    # anything at all is accepted — "not a server" becomes the perfectly
    # well-formed URL "https://not a server" and is only found to be wrong much
    # later, when a sync silently fails.
    if not parsed.hostname or not _HOSTNAME_RE.match(parsed.hostname):
        raise ServerConfigError(_('That does not look like a server address.'))

    if parsed.scheme == 'http' and not _is_local_address(parsed.hostname):
        # Student work would cross the public internet unencrypted. Allowed on
        # a school's own network, where there is often no certificate.
        raise ServerConfigError(
            _('Refusing to use an unencrypted connection to %(host)s — student '
              'work would be sent in the clear. Use https:// instead, or a '
              'server on your own network.') % {'host': parsed.hostname}
        )

    # Keep scheme + host[:port]; drop any path or query pasted from a browser.
    return urlunparse((parsed.scheme, parsed.netloc, '', '', '', '')).rstrip('/')


def save(raw: str) -> str:
    """Validate and store the school server's address."""
    from ai_tutor.apps.desktop.models import DeviceState

    url = normalise(raw)
    state = DeviceState.load()
    state.server_url = url
    state.save(update_fields=['server_url'])
    return url


def sign_in(username: str, password: str) -> dict:
    """Sign the student in to the school server. Raises ServerConfigError.

    Two things happen, and both matter:

    1. The server issues this student a token, which is what lets their
       finished work be uploaded later. No administrator is involved.
    2. The same credentials are mirrored into the local account, so the student
       can sign in on this computer afterwards with no internet at all. Without
       that, signing in here would work once and then lock them out the moment
       the connection dropped — which is most of the time.
    """
    import requests
    from ai_tutor.apps.desktop.models import DeviceState

    state = DeviceState.load()
    base = state.effective_server_url
    if not base:
        raise ServerConfigError(_('Set the server address first.'))

    username = (username or '').strip()
    if not username or not password:
        raise ServerConfigError(_('Enter your username and password.'))

    try:
        response = requests.post(
            f'{base}/api/v1/auth/login/',
            json={'username': username, 'password': password},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.SSLError:
        raise ServerConfigError(
            _('Could not verify the security certificate for %(host)s.')
            % {'host': base})
    except requests.exceptions.RequestException:
        raise ServerConfigError(
            _('Could not reach %(host)s. Check the address and that this '
              'computer is online.') % {'host': base})

    if response.status_code == 404:
        # Reached something that is not this application — almost always a typo
        # in the address, and worth saying so rather than "wrong password".
        raise ServerConfigError(
            _('%(host)s answered, but it is not an AI Tutor server.')
            % {'host': base})
    if response.status_code in (400, 401):
        raise ServerConfigError(_('That username or password is not right.'))
    if response.status_code != 200:
        raise ServerConfigError(
            _('The server refused the sign-in (error %(code)s).')
            % {'code': response.status_code})

    data = response.json()
    access = data.get('access') or ''
    if not access:
        raise ServerConfigError(_('The server did not return a sign-in token.'))

    user = data.get('user') or {}
    state.server_username = username
    state.server_user_id = user.get('id')
    state.access_token = access
    state.refresh_token = data.get('refresh') or ''
    state.save(update_fields=['server_username', 'server_user_id',
                              'access_token', 'refresh_token'])

    _mirror_local_account(username, password, user)
    return data


def _mirror_local_account(username: str, password: str, remote_user: dict):
    """Make the same credentials work offline on this computer.

    The student signs in here once while online; every sign-in after that is
    against the local database and needs no connection. The local row is a
    separate account that happens to share a username and password — it is not
    the server's row, and nothing about it is authoritative.
    """
    from django.contrib.auth.models import User

    local, _created = User.objects.get_or_create(
        username=username,
        defaults={
            'first_name': (remote_user.get('first_name') or '')[:150],
            'last_name': (remote_user.get('last_name') or '')[:150],
        },
    )
    # Always reset: if they changed it on the server, the new one should work
    # here too, and this is the only moment we ever see it in the clear.
    local.set_password(password)
    local.save()


def clear() -> None:
    """Back to offline-only. Lessons and student work on the device are kept."""
    from ai_tutor.apps.desktop.models import DeviceState

    state = DeviceState.load()
    state.server_url = ''
    # The sign-in goes with it. A computer no longer pointed at that server has
    # no business keeping tokens for it. The local account stays, so the
    # student can still sign in here and keep working offline.
    state.access_token = ''
    state.refresh_token = ''
    state.server_user_id = None
    state.server_username = ''
    state.save(update_fields=['server_url', 'access_token', 'refresh_token',
                              'server_user_id', 'server_username'])


def status() -> dict:
    """What the settings screen shows."""
    from django.conf import settings
    from ai_tutor.apps.desktop.models import DeviceState

    state = DeviceState.load()
    from ai_tutor.apps.desktop.models import SyncOutbox
    return {
        'server_url': state.effective_server_url,
        'last_sync_at': state.last_sync_at,
        # Address alone is not enough to deliver anything — the server rejects
        # an unregistered device. The screen must not claim otherwise.
        'signed_in': bool(state.access_token),
        'server_username': state.server_username,
        'pending': SyncOutbox.objects.filter(
            status=SyncOutbox.Status.PENDING).count(),
        # A scripted rollout pins the address in the environment; the screen
        # must not offer to change something it cannot change.
        'pinned_by_admin': bool((getattr(settings, 'SYNC_SERVER_URL', '') or '').strip()),
    }
