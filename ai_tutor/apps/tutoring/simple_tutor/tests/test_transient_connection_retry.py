"""A dropped connection to the model must be retried, not surfaced as failure.

2026-08-23: 49 `ConnectionError("Could not connect to Ollama at ...")` ended 24
of 34 eval sessions. Each one degraded a turn to the "I had trouble responding"
placeholder, and two placeholders in a row tripped the repeat detector into a
deadlock — a whole session lost to a socket blip. On a Jetson the same blip
reaches a real student.

Both classifiers missed it for the same two reasons: the builtin is named
`ConnectionError` (no 'apiconnection' substring) and the message reads "Could
not connect to Ollama at ..." (no literal 'connection error').

There are TWO implementations — llm.client.is_transient_error and
engine._is_transient_error — and they have already drifted once (the engine
learned about exc.response.status_code after a Jetson 500s incident; the client
never did). These tests pin them against a shared table so the next divergence
fails the build instead of a sweep.
"""
import requests
from django.test import SimpleTestCase

from ai_tutor.apps.llm.client import is_transient_error as client_classify
from ai_tutor.apps.tutoring.simple_tutor.engine import (
    _is_transient_error as engine_classify,
)

# (exception, should_be_transient, why)
CASES = [
    # --- the 2026-08-23 failure, in both the raised and underlying shapes ----
    (ConnectionError("Could not connect to Ollama at http://localhost:11435. "
                     "Make sure Ollama is running (ollama serve)."), True,
     'the exact exception that ended 24 of 34 sessions'),
    (requests.exceptions.ConnectionError('connection refused'), True,
     'what OllamaClient catches before re-raising'),
    (requests.exceptions.ChunkedEncodingError('response ended prematurely'), True,
     'stream cut mid-response'),
    (ConnectionResetError('Connection reset by peer'), True, 'peer reset'),
    (BrokenPipeError('Broken pipe'), True, 'socket closed under us'),
    (OSError('Connection aborted'), True, 'generic socket failure'),
    (TimeoutError('timed out'), True, 'slow model, worth another attempt'),

    # --- permanent: retrying burns backoff on something that cannot succeed --
    (ValueError('400 invalid tool schema'), False, 'malformed request'),
    (Exception('401 unauthorized'), False, 'bad credentials'),
    (Exception('404 model not found'), False, 'wrong model name'),
    (KeyError('message'), False, 'a bug in our own parsing'),
]


def _http_error(status):
    """requests.HTTPError with the status where requests actually puts it —
    exc.response.status_code, not exc.status_code."""
    import requests
    resp = requests.Response()
    resp.status_code = status
    exc = requests.exceptions.HTTPError(f'{status} Error for url: http://x')
    exc.response = resp
    return exc


# 5xx-with-a-response was the drift that already existed between the two
# implementations: the engine handled it after the Jetson 2026-07-27 incident,
# the client did not. Pinned here so they cannot separate again.
CASES += [
    (_http_error(500), True, 'Ollama 5xx, retryable'),
    (_http_error(503), True, 'service unavailable'),
    (_http_error(404), False, 'wrong model name — retrying cannot help'),
    (_http_error(400), False, 'malformed request'),
]


class TransientClassificationTest(SimpleTestCase):
    def test_client_classifier(self):
        for exc, want, why in CASES:
            with self.subTest(exc=type(exc).__name__, why=why):
                self.assertEqual(
                    client_classify(exc), want,
                    f'llm.client.is_transient_error({exc!r}) — {why}')

    def test_engine_classifier(self):
        for exc, want, why in CASES:
            with self.subTest(exc=type(exc).__name__, why=why):
                self.assertEqual(
                    engine_classify(exc), want,
                    f'engine._is_transient_error({exc!r}) — {why}')

    def test_the_two_implementations_agree(self):
        """They are separate functions that have drifted before. A shared table
        is cheaper than merging them, and catches the next divergence."""
        disagree = [(type(e).__name__, str(e)[:50], client_classify(e), engine_classify(e))
                    for e, _, _ in CASES
                    if client_classify(e) != engine_classify(e)]
        self.assertEqual(disagree, [], (
            'is_transient_error implementations disagree — a failure retried in '
            'one path and not the other is the hardest kind to reproduce'))


class RetryActuallyHappensTest(SimpleTestCase):
    """Classification is only half of it: the engine must call the model again.

    Asserting on the classifier alone would have passed happily while the
    turn still died, because the retry ladder is a separate decision.
    """

    def test_a_dropped_connection_is_retried_and_can_succeed(self):
        from ai_tutor.apps.tutoring.simple_tutor import engine

        calls = {'n': 0}

        def flaky():
            calls['n'] += 1
            if calls['n'] == 1:
                raise ConnectionError(
                    'Could not connect to Ollama at http://localhost:11435.')
            return 'recovered'

        with self.settings():
            out = engine._invoke_with_transient_retry(
                flaky, label='test', provider='local_ollama')
        self.assertEqual(out, 'recovered')
        self.assertEqual(calls['n'], 2, 'the drop was not retried')

    def test_a_permanent_error_is_not_retried(self):
        from ai_tutor.apps.tutoring.simple_tutor import engine

        calls = {'n': 0}

        def always_bad():
            calls['n'] += 1
            raise ValueError('400 invalid tool schema')

        out = engine._invoke_with_transient_retry(
            always_bad, label='test', provider='local_ollama')
        self.assertIsNone(out)
        self.assertEqual(calls['n'], 1, 'burned a retry on a permanent error')


class ConnectionLadderTest(SimpleTestCase):
    """A dropped socket earns more patience than a local 5xx.

    The short local ladder exists because a wasted local generation costs ~92s,
    so attempts 3-6 buy nothing. That reasoning is about a 5xx — the server got
    the request and failed. When the socket drops, the request never arrived,
    so a retry wastes no decode, and 2s is often too short for the transport to
    come back (33 connection failures in one arm on 2026-08-23, 18 recovered).
    """

    def test_local_5xx_keeps_the_single_short_retry(self):
        from ai_tutor.apps.tutoring.simple_tutor import engine
        self.assertEqual(engine._backoff_for('local_ollama'), [2])

    def test_local_connection_failure_gets_a_longer_ladder(self):
        from ai_tutor.apps.tutoring.simple_tutor import engine
        ladder = engine._backoff_for('local_ollama', ConnectionError('x'))
        self.assertGreater(len(ladder), 1)
        self.assertEqual(ladder[0], 2, 'first retry should still be quick')

    def test_cloud_ladder_is_untouched(self):
        from ai_tutor.apps.tutoring.simple_tutor import engine
        self.assertEqual(engine._backoff_for('anthropic'), [2, 5, 12, 30, 60])

    def test_a_connection_blip_survives_more_than_one_attempt(self):
        """The behaviour, not the constant: two consecutive drops used to end
        the turn, and two placeholders in a row end the session."""
        from ai_tutor.apps.tutoring.simple_tutor import engine
        calls = {'n': 0}

        def flaky():
            calls['n'] += 1
            if calls['n'] < 3:
                raise ConnectionError('Could not connect to Ollama')
            return 'recovered'

        out = engine._invoke_with_transient_retry(
            flaky, label='t', provider='local_ollama')
        self.assertEqual(out, 'recovered')
        self.assertEqual(calls['n'], 3)

    def test_a_local_5xx_still_stops_after_one_retry(self):
        """Guards the other direction: this must not become the cloud ladder,
        or one bad turn costs ~11 minutes of wasted generations."""
        from ai_tutor.apps.tutoring.simple_tutor import engine
        import requests
        calls = {'n': 0}

        def always_500():
            calls['n'] += 1
            resp = requests.Response()
            resp.status_code = 500
            exc = requests.exceptions.HTTPError('500 Server Error')
            exc.response = resp
            raise exc

        engine._invoke_with_transient_retry(
            always_500, label='t', provider='local_ollama')
        self.assertEqual(calls['n'], 2, 'one attempt plus one retry')
