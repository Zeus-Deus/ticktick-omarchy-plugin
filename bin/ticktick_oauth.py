"""TickTick public-client OAuth: dynamic registration, PKCE, loopback callback.

Provider metadata: https://ticktick.com/.well-known/oauth-authorization-server
No account files are read, no browser cookies are imported, and no client
secret is embedded. The caller stores a validated access token privately.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import http.server
import json
import secrets
import ssl
from typing import cast
import time
import urllib.error
import urllib.parse
import urllib.request

ISSUER = 'https://ticktick.com'
AUTHORIZATION_URL = ISSUER + '/oauth/authorize'
REGISTRATION_URL = 'https://api.ticktick.com/oauth/register'
TOKEN_URL = 'https://api.ticktick.com/oauth/token'
HTTP_TIMEOUT = 20
MAX_RESPONSE_BYTES = 64 * 1024
SCOPES = 'tasks:read tasks:write'


class OAuthError(RuntimeError):
    """A safe reason code/message, never a provider body or credential."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post(url, body, content_type):
    # Registration/code-verifier bodies must never follow a redirect or go
    # to a host supplied by a callback or an untrusted discovery document.
    if url not in (REGISTRATION_URL, TOKEN_URL):
        raise OAuthError('unsafe_endpoint', 'TickTick sign-in requires a trusted HTTPS endpoint.')
    request = urllib.request.Request(url, data=body, method='POST', headers={
        'Content-Type': content_type, 'Accept': 'application/json',
        'User-Agent': 'omarchy-ticktick',
    })
    opener = urllib.request.build_opener(
        _NoRedirects(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(request, timeout=HTTP_TIMEOUT) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        error.close()
        code = 'registration_failed' if url == REGISTRATION_URL else 'exchange_failed'
        raise OAuthError(code, 'TickTick could not finish sign-in. Please try again.') from None
    except (urllib.error.URLError, OSError):
        raise OAuthError('network', 'Could not reach TickTick. Check your connection and try again.') from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise OAuthError('invalid_response', 'TickTick sent an oversized sign-in response.')
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise OAuthError('invalid_response', 'TickTick sent an invalid sign-in response.') from None
    if not isinstance(result, dict):
        raise OAuthError('invalid_response', 'TickTick sent an invalid sign-in response.')
    return result


def _opaque(value, maximum=4096):
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and all(33 <= ord(char) <= 126 for char in value))


class _CallbackServer(http.server.HTTPServer):
    # One bounded request at a time; no unbounded per-connection threads.
    expected_state = ''
    authorization_code: str | None = None
    error: OAuthError | None = None

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(2)
        return connection, address

    def handle_error(self, request, client_address):
        # BaseServer's traceback/error logging must not disclose callback URLs.
        pass


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # A callback URL contains a short-lived authorization code.
        pass

    def _reply(self, status, message):
        body = ('<!doctype html><html><head><meta charset="utf-8">'
                '<title>TickTick sign-in</title></head><body><h1>TickTick</h1><p>'
                + message + '</p></body></html>').encode()
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        with contextlib.suppress(ConnectionError, OSError):
            self.wfile.write(body)

    def do_GET(self):
        server = cast(_CallbackServer, self.server)
        expected_host = '127.0.0.1:%d' % server.server_port
        if (self.headers.get_all('Host') != [expected_host]
                or self.headers.get('Origin') not in (None, ISSUER)):
            self._reply(400, 'This sign-in request was not recognized. Return to the TickTick widget.')
            return
        if len(self.path) > 8192:
            self._reply(414, 'This sign-in request is too large.')
            return
        try:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path != '/callback':
                self._reply(404, 'Return to the TickTick widget to sign in.')
                return
            fields = urllib.parse.parse_qs(parsed.query, keep_blank_values=True,
                                           strict_parsing=True, max_num_fields=12)
        except ValueError:
            self._reply(400, 'This sign-in request was not recognized.')
            return
        if any(len(values) != 1 for values in fields.values()):
            self._reply(400, 'This sign-in request was not recognized.')
            return
        state = fields.get('state', [''])[0]
        issuer = fields.get('iss', [''])[0]
        if (not hmac.compare_digest(state.encode(), server.expected_state.encode())
                or issuer != ISSUER):
            # Unrelated/malicious callbacks do not cancel the legitimate attempt.
            self._reply(400, 'This sign-in request was not recognized. Return to the TickTick widget.')
            return
        code = fields.get('code', [''])[0]
        error = fields.get('error', [''])[0]
        if error and not code:
            server.error = OAuthError('denied', 'TickTick sign-in was not approved. You can try again.')
            self._reply(200, 'Sign-in was not completed. You can return to Omarchy and try again.')
            return
        if error or not _opaque(code):
            self._reply(400, 'This sign-in request was not recognized.')
            return
        if server.authorization_code is not None:
            self._reply(409, 'This sign-in request has already been received.')
            return
        server.authorization_code = code
        self._reply(200, 'Authorization received. You can return to Omarchy while the connection finishes.')

    def do_POST(self):
        self._reply(405, 'Only the browser sign-in callback is accepted here.')


def authorize(on_event, timeout=180):
    """Emit public progress events and return an OAuth token response privately.

    The caller opens the waiting event's URL after dismissing its own popup.
    The listener closes on success, rejection, timeout and exceptions; this
    module writes no files and has no dependency on an installed desktop app.
    """
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 180:
        raise ValueError('OAuth callback timeout must be positive and at most 180 seconds')
    on_event({'phase': 'preparing'})
    try:
        server = _CallbackServer(('127.0.0.1', 0), _CallbackHandler)
    except OSError:
        raise OAuthError('listener_failed', 'Could not start local sign-in. Please try again.') from None
    with server:
        server.timeout = min(0.2, timeout)
        server.expected_state = secrets.token_urlsafe(32)
        redirect = 'http://127.0.0.1:%d/callback' % server.server_port
        registration = _post(REGISTRATION_URL, json.dumps({
            'client_name': 'Omarchy TickTick', 'application_type': 'native',
            'redirect_uris': [redirect], 'grant_types': ['authorization_code'],
            'response_types': ['code'], 'token_endpoint_auth_method': 'none',
            'scope': SCOPES,
        }).encode(), 'application/json')
        client_id = registration.get('client_id')
        if (not _opaque(client_id, 512)
                or registration.get('token_endpoint_auth_method') != 'none'
                or registration.get('redirect_uris') != [redirect]
                or registration.get('client_secret')):
            raise OAuthError('registration_failed', 'TickTick did not accept a secure public sign-in client.')
        verifier = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        url = AUTHORIZATION_URL + '?' + urllib.parse.urlencode({
            'client_id': client_id, 'redirect_uri': redirect,
            'response_type': 'code', 'scope': SCOPES,
            'state': server.expected_state, 'code_challenge': challenge,
            'code_challenge_method': 'S256',
        })
        on_event({'phase': 'waiting', 'url': url})
        deadline = time.monotonic() + timeout
        while server.authorization_code is None and server.error is None:
            if time.monotonic() >= deadline:
                raise OAuthError('timeout', 'Sign-in timed out after 3 minutes. Please try again.')
            server.handle_request()
        if server.error:
            raise server.error
        code = server.authorization_code
    # Close the listener before spending time exchanging/validating credentials.
    on_event({'phase': 'validating'})
    token = _post(TOKEN_URL, urllib.parse.urlencode({
        'client_id': client_id, 'code': code, 'code_verifier': verifier,
        'grant_type': 'authorization_code', 'redirect_uri': redirect,
    }).encode(), 'application/x-www-form-urlencoded')
    access_token = token.get('access_token')
    token_type = token.get('token_type', 'Bearer')
    if not _opaque(access_token) or not isinstance(token_type, str) or token_type.lower() != 'bearer':
        raise OAuthError('invalid_token', 'TickTick did not return a usable access token.')
    result = {'access_token': access_token, 'token_type': 'Bearer', 'client_id': client_id}
    expires = token.get('expires_in')
    if isinstance(expires, int) and not isinstance(expires, bool) and expires > 0:
        result['expires_in'] = expires
    return result
