"""OAuth uses dummy credentials, mocked TickTick HTTP, and real loopback callbacks."""
import contextlib
import importlib.util
import io
from email.message import Message
import json
from pathlib import Path
import queue
import ssl
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request


MODULE = Path(__file__).resolve().parents[1] / 'bin' / 'ticktick_oauth.py'
spec = importlib.util.spec_from_file_location('ticktick_oauth', MODULE)
assert spec is not None and spec.loader is not None
oauth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oauth)


class OAuthTests(unittest.TestCase):
    def launch(self, token=None, timeout=2.0):
        events = queue.Queue()
        outcome = queue.Queue()
        calls = []

        def post(url, body, content_type):
            calls.append((url, body, content_type))
            if url == oauth.REGISTRATION_URL:
                registration = json.loads(body)
                return {'client_id': 'dummy-client',
                        'redirect_uris': registration['redirect_uris'],
                        'token_endpoint_auth_method': 'none'}
            return token or {'access_token': 'dummy-access-token', 'token_type': 'Bearer'}

        def run():
            try:
                outcome.put(oauth.authorize(events.put, timeout=timeout))
            except Exception as error:
                outcome.put(error)

        patch = mock.patch.object(oauth, '_post', side_effect=post)
        patch.start()
        thread = threading.Thread(target=run)
        thread.start()
        self.addCleanup(patch.stop)
        self.addCleanup(thread.join, 5)
        waiting = None
        for _ in range(3):
            event = events.get(timeout=3)
            if event['phase'] == 'waiting':
                waiting = event
                break
        assert waiting is not None
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(waiting['url']).query)
        return outcome, calls, params, events

    def callback(self, params, **changes):
        values = {'state': params['state'][0], 'code': 'dummy-code', 'iss': oauth.ISSUER}
        values.update(changes)
        values = {k: v for k, v in values.items() if v is not None}
        url = params['redirect_uri'][0] + '?' + urllib.parse.urlencode(values, doseq=True)
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status, dict(response.headers), response.read()

    def test_public_registration_pkce_and_success(self):
        outcome, calls, params, events = self.launch()
        registration = json.loads(calls[0][1])
        self.assertEqual(registration['client_name'], 'Omarchy TickTick')
        self.assertEqual(registration['token_endpoint_auth_method'], 'none')
        self.assertNotIn('client_secret', registration)
        self.assertEqual(urllib.parse.urlsplit(params['redirect_uri'][0]).hostname, '127.0.0.1')
        self.assertEqual(params['code_challenge_method'], ['S256'])
        self.assertNotIn('code_verifier', params)
        self.assertNotIn('access_token', params)
        status, headers, body = self.callback(params)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        self.assertNotIn(b'dummy-code', body)
        result = outcome.get(timeout=3)
        self.assertIsInstance(result, dict)
        self.assertEqual(result['access_token'], 'dummy-access-token')
        self.assertEqual(result['client_id'], 'dummy-client')
        exchange = urllib.parse.parse_qs(calls[1][1].decode())
        import base64
        import hashlib
        challenge = base64.urlsafe_b64encode(hashlib.sha256(exchange['code_verifier'][0].encode()).digest()).rstrip(b'=').decode()
        self.assertEqual(params['code_challenge'], [challenge])
        self.assertEqual(exchange['redirect_uri'], params['redirect_uri'])
        self.assertEqual(exchange['code'], ['dummy-code'])
        self.assertEqual(events.get(timeout=1)['phase'], 'validating')
        self.assertEqual(len(calls), 2)
        # Listener is closed before a token response is handed to the caller.
        with self.assertRaises((urllib.error.URLError, ConnectionError, TimeoutError)):
            self.callback(params)

    def test_invalid_state_does_not_cancel_the_real_login(self):
        outcome, calls, params, _ = self.launch()
        for state in ['wrong-state', '\u2603']:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self.callback(params, state=state)
            self.assertEqual(raised.exception.code, 400)
            raised.exception.close()
        self.assertTrue(outcome.empty())
        self.assertEqual(len(calls), 1)
        self.callback(params)
        self.assertIsInstance(outcome.get(timeout=3), dict)

    def test_missing_wrong_and_duplicate_issuer_or_state_are_rejected(self):
        outcome, calls, params, _ = self.launch()
        for values in [{'iss': None}, {'iss': 'https://other.invalid'},
                       {'state': [params['state'][0], params['state'][0]]}]:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self.callback(params, **values)
            self.assertEqual(raised.exception.code, 400)
            raised.exception.close()
        self.assertTrue(outcome.empty())
        self.callback(params)
        self.assertIsInstance(outcome.get(timeout=3), dict)
        self.assertEqual(len(calls), 2)

    def test_denial_requires_the_right_state_and_never_exchanges_code(self):
        outcome, calls, params, _ = self.launch()
        self.callback(params, code=None, error='access_denied')
        result = outcome.get(timeout=3)
        self.assertIsInstance(result, oauth.OAuthError)
        self.assertEqual(result.code, 'denied')
        self.assertEqual(len(calls), 1)

    def test_timeout_closes_listener_and_does_not_create_credentials(self):
        outcome, calls, params, _ = self.launch(timeout=0.2)
        result = outcome.get(timeout=3)
        self.assertIsInstance(result, oauth.OAuthError)
        self.assertEqual(result.code, 'timeout')
        self.assertEqual(len(calls), 1)
        with self.assertRaises((urllib.error.URLError, ConnectionError, TimeoutError)):
            self.callback(params)

    def test_host_and_origin_checks(self):
        outcome, _, params, _ = self.launch()
        values = {'state': params['state'][0], 'code': 'dummy-code', 'iss': oauth.ISSUER}
        url = params['redirect_uri'][0] + '?' + urllib.parse.urlencode(values)
        for headers in [{'Host': 'attacker.invalid'}, {'Origin': 'https://attacker.invalid'}]:
            req = urllib.request.Request(url, headers=headers)
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(req, timeout=3)
            self.assertEqual(raised.exception.code, 400)
            raised.exception.close()
        self.assertTrue(outcome.empty())
        self.callback(params)
        self.assertIsInstance(outcome.get(timeout=3), dict)

    def test_no_callback_or_provider_credentials_are_printed(self):
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
            outcome, _, params, _ = self.launch()
            self.callback(params)
            self.assertIsInstance(outcome.get(timeout=3), dict)
        self.assertEqual(capture.getvalue(), '')

    def test_malformed_access_token_is_never_returned(self):
        outcome, _, params, _ = self.launch(token={'access_token': 'dummy\nsecret'})
        self.callback(params)
        result = outcome.get(timeout=3)
        self.assertIsInstance(result, oauth.OAuthError)
        self.assertNotIn('dummy', str(result))
        self.assertNotIn('secret', str(result))

    def test_private_client_identity_or_redirect_mismatch_is_rejected(self):
        for payload in [{'client_id': 'id', 'token_endpoint_auth_method': 'client_secret_basic'},
                        {'client_id': 'id', 'token_endpoint_auth_method': 'none', 'redirect_uris': ['https://other.invalid']}]:
            with self.subTest(payload=payload), mock.patch.object(oauth, '_post', return_value=payload):
                with self.assertRaises(oauth.OAuthError):
                    oauth.authorize(lambda event: None, timeout=0.1)

    def test_all_provider_redirects_are_refused(self):
        handler = oauth._NoRedirects()
        request = urllib.request.Request(oauth.TOKEN_URL, data=b'code=dummy&code_verifier=dummy')
        for target in ['https://other.invalid', 'http://ticktick.com/oauth/token', oauth.TOKEN_URL]:
            for code in [301, 302, 303, 307, 308]:
                self.assertIsNone(handler.redirect_request(request, None, code, 'Redirect', {}, target))

    def test_remote_http_and_untrusted_destinations_are_refused_before_network(self):
        with mock.patch.object(oauth.urllib.request, 'build_opener') as opener:
            for url in ['http://api.ticktick.com/oauth/token', 'https://other.invalid/token',
                        'https://api.ticktick.com@other.invalid/oauth/token']:
                with self.assertRaises(oauth.OAuthError):
                    oauth._post(url, b'code=dummy', 'application/x-www-form-urlencoded')
            opener.assert_not_called()

    def test_provider_transport_verifies_tls_caps_body_and_hides_errors(self):
        for body in [b'x' * (oauth.MAX_RESPONSE_BYTES + 1), b'not-json', b'[]']:
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = body
            opener = mock.Mock()
            opener.open.return_value = response
            with mock.patch.object(oauth.urllib.request, 'build_opener', return_value=opener) as build:
                with self.assertRaises(oauth.OAuthError):
                    oauth._post(oauth.TOKEN_URL, b'code=dummy', 'application/x-www-form-urlencoded')
            handlers = build.call_args.args
            tls = next(h for h in handlers if isinstance(h, urllib.request.HTTPSHandler))
            context = getattr(tls, '_context')
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(any(isinstance(h, oauth._NoRedirects) for h in handlers))
            self.assertEqual(opener.open.call_args.kwargs['timeout'], oauth.HTTP_TIMEOUT)
            response.read.assert_called_once_with(oauth.MAX_RESPONSE_BYTES + 1)
        error = urllib.error.HTTPError(oauth.TOKEN_URL, 400, 'dummy-secret', Message(), io.BytesIO(b'dummy-secret'))
        opener.open.side_effect = error
        with mock.patch.object(oauth.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaises(oauth.OAuthError) as raised:
                oauth._post(oauth.TOKEN_URL, b'code=dummy', 'application/x-www-form-urlencoded')
        self.assertNotIn('dummy-secret', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
