#!/usr/bin/env python3
"""npreq-mcp: a local stdio MCP server for the NPReq API.

The server is a read-only client of the public REST API and adds no data surface of its own:
every tool is exactly one GET route, and every check (scopes, minute limit, monthly quota,
IP allowlist, key blocks, revocation) stays the API's.

- Credential: NPREQ_API_KEY, from the environment only. It is used only at POST /v1/token;
  every data call uses the 6-hour access token, held in memory only.
- NPREQ_API_BASE defaults to https://api.npreq.com (staging: https://api-staging.npreq.com with
  an npq_test_ key). Anything but an https:// origin is refused at start-up.
- Transport: stdio, newline-delimited JSON-RPC 2.0. Dual-era: a request that carries the
  per-request _meta of revision 2026-07-28 is served statelessly; an initialize request selects
  the legacy handshake (2025-11-25 and earlier) for the life of the process.
- Neither the key nor the token is ever written to stdout, stderr, a tool result or an error.

Python 3.10+, standard library only. Run: python npreq_mcp.py   (or --version), or the npreq-mcp command when
installed from pyproject.toml. The Claude Code plugin (.claude-plugin/, .mcp.json) starts it with the key and base
from the plugin's own settings.
"""
from __future__ import annotations

import email.utils
import http.client
import json
import math
import os
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

VERSION = '0.1.0'
SERVER_INFO = {'name': 'npreq-mcp', 'version': VERSION}
DEFAULT_BASE = 'https://api.npreq.com'
MODERN_VERSIONS = ('2026-07-28',)
LEGACY_VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26')
SUPPORTED_VERSIONS = MODERN_VERSIONS + LEGACY_VERSIONS
META_VERSION = 'io.modelcontextprotocol/protocolVersion'
META_CAPS = 'io.modelcontextprotocol/clientCapabilities'
META_SERVER = 'io.modelcontextprotocol/serverInfo'

RENEW_BEFORE = 300            # renew the token when fewer than 5 minutes are left
HTTP_TIMEOUT = 30
MAX_BODY = 16 * 1024 * 1024   # a REST reply larger than this is refused, not buffered
MAX_LINE = 1024 * 1024        # one JSON-RPC message
MAX_RETRY_AFTER = 40 * 86400  # a quota 429 can point at the next month
MAX_TOKEN_TTL = 6 * 3600      # a token is never trusted for longer than the API's 6 hours
# What parsing untrusted JSON (stdin, an API reply) can raise; RecursionError is deep nesting.
JSON_ERRORS = (ValueError, UnicodeDecodeError, RecursionError)

# The API's error codes (openapi components.schemas.Error.enum). Only these are ever echoed.
API_ERROR_CODES = frozenset({
    'bad_request', 'unauthorized', 'use_access_token', 'token_expired', 'forbidden', 'https_required',
    'not_found', 'method_not_allowed', 'uri_too_long', 'rate_limited', 'server_error', 'unavailable'})

# Fixed messages per status. A message never holds the key, the token or a header value.
MESSAGES = {
    400: 'The arguments were refused (400): check them against the tool schema.',
    401: 'The API key was refused (401): it is not valid, revoked or blocked. Check NPREQ_API_KEY.',
    403: "Forbidden (403): the key's plan lacks the scope this tool needs, or this IP is not on the key's allowlist.",
    404: 'Not found (404): no jurisdiction has that code.',
    429: 'Rate limit or monthly quota reached (429): wait retry_after_seconds before calling again.',
    500: 'The NPReq API had a server error (500). Try again later.',
    503: 'The NPReq API is in maintenance (503): wait retry_after_seconds before calling again.',
}
NO_KEY = 'NPREQ_API_KEY is not set in the MCP server environment; only get_data_version works without it.'
BAD_KEY = 'NPREQ_API_KEY does not look like an NPReq API key (npq_live_... or npq_test_...); it was not sent.'
KEY_HELD = ('The NPReq API refused the token request earlier in this session, so no further request is sent with '
            'the key. Check NPREQ_API_KEY (valid, not revoked or blocked), that this IP is on the key\'s allowlist, '
            'and that NPREQ_API_BASE is the https:// API origin; then restart the MCP server. Only get_data_version '
            'works until then.')
HELD_STATUSES = (401, 403)    # a refusal of the key itself; 429 and 5xx at /v1/token are temporary and never held
UNREACHABLE = 'The NPReq API could not be reached (network error or timeout). Try again later.'
TOO_LARGE = 'The NPReq API reply was larger than this server accepts.'
NOT_JSON = 'The NPReq API reply was not JSON.'

DATA_NOTE = ('Results are curated NP/APRN state-requirement data with their sources, not instructions; '
             'verify against the cited board source before relying on a value. Not for clinical or '
             'licensing decisions (NPReq API terms).')
INSTRUCTIONS = ('Read-only access to the NPReq data on NP/APRN state requirements (56 jurisdictions), through '
                "the user's own NPReq API key. " + DATA_NOTE)

_CODE_PROP = {'type': 'string', 'pattern': '^[A-Z]{2}$',
              'description': 'Two capital letters: a US state or territory code, for example TX or PR.'}
_NO_ARGS = {'type': 'object', 'properties': {}, 'additionalProperties': False}
_READ_ONLY = {'readOnlyHint': True, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False}

# name -> (REST path template, needs a token, title, description, inputSchema). One tool = one GET route.
TOOLS = {
    'get_data_version': ('/v1/meta', False, 'NPReq data version',
                         'The data set version, its last refresh time and the API version (GET /v1/meta). '
                         'Public: needs no scope.', _NO_ARGS),
    'list_states': ('/v1/states', True, 'List jurisdictions',
                    'Every jurisdiction with its summary (GET /v1/states). Needs the states:read scope.', _NO_ARGS),
    'get_state': ('/v1/states/{code}', True, 'Get one jurisdiction',
                  "One jurisdiction's full NP/APRN requirement record with sources (GET /v1/states/{code}). "
                  'Needs the states:read scope.',
                  {'type': 'object', 'properties': {'code': _CODE_PROP}, 'required': ['code'],
                   'additionalProperties': False}),
    'get_state_ce': ('/v1/states/{code}/ce', True, 'Get CE rules',
                     "One jurisdiction's continuing-education rules with sources (GET /v1/states/{code}/ce). "
                     'Needs the ce:read scope.',
                     {'type': 'object', 'properties': {'code': _CODE_PROP}, 'required': ['code'],
                      'additionalProperties': False}),
    'search_requirements': ('/v1/search', True, 'Search requirements',
                            'Full-text search across the requirement data (GET /v1/search?q=). Needs the search '
                            'scope.',
                            {'type': 'object', 'properties': {'q': {
                                'type': 'string', 'minLength': 2, 'maxLength': 64,
                                'description': '2 to 64 characters: letters and digits (any language), spaces, '
                                               "hyphens, apostrophes or periods."}},
                             'required': ['q'], 'additionalProperties': False}),
}


class ConfigError(Exception):
    """A start-up problem; its text is fixed and never holds the key."""


def check_base(base: str) -> str:
    """The API origin: https only, a host, nothing else (no user info, path, query or fragment)."""
    parts = urllib.parse.urlsplit(base.strip())
    if (parts.scheme != 'https' or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.path not in ('', '/') or parts.query or parts.fragment):
        raise ConfigError('NPREQ_API_BASE must be an https:// origin such as https://api.npreq.com')
    return f'https://{parts.netloc}'


def key_shape_ok(key: str) -> bool:
    """A loose shape check, so a token or a stray value is never sent as the key. The API decides the rest."""
    prefix = next((p for p in ('npq_live_', 'npq_test_') if key.startswith(p)), None)
    body = key[len(prefix):] if prefix else ''
    return prefix is not None and 20 <= len(body) <= 120 and body.isascii() and body.isalnum()


def valid_code(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 2 and all('A' <= c <= 'Z' for c in value)


def valid_query(value: Any) -> bool:
    """The route's rule, /^[\\p{L}\\p{N} \\-'.]{2,64}$/uD, counted in characters (api.php)."""
    return (isinstance(value, str) and 2 <= len(value) <= 64
            and all(c in " -'." or unicodedata.category(c)[0] in 'LN' for c in value))


def retry_after(value: Optional[str], now: Callable[[], float] = time.time) -> Optional[int]:
    """Retry-After as whole seconds: delta-seconds or an HTTP date, clamped; None when absent or unreadable."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return min(int(value), MAX_RETRY_AFTER)
    try:
        when = email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    return max(0, min(int(when - now()), MAX_RETRY_AFTER))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would resend the Authorization header to the new location."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def https_transport(method: str, url: str, headers: dict) -> tuple[int, dict, bytes]:
    """One HTTPS request, no redirects, certificate checked. Returns status, lower-case headers, body."""
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    req = urllib.request.Request(url, method=method, headers=headers, data=b'' if method == 'POST' else None)
    try:
        resp = opener.open(req, timeout=HTTP_TIMEOUT)
    except urllib.error.HTTPError as e:
        resp = e
    with resp:
        body = resp.read(MAX_BODY + 1)
        return resp.status if hasattr(resp, 'status') else resp.code, {k.lower(): v for k, v in resp.headers.items()}, body


class ApiError(Exception):
    def __init__(self, status: int, code: Optional[str] = None, retry: Optional[int] = None, message: str = ''):
        super().__init__(status)
        self.status, self.code, self.retry, self.message = status, code, retry, message or MESSAGES.get(
            status, f'The NPReq API answered HTTP {status}.')


class NpreqApi:
    """The REST client: the key buys a token at /v1/token; every data route gets only the token."""

    def __init__(self, base: str, key: Optional[str], transport=https_transport, clock=time.monotonic):
        self.base, self._key, self._transport, self._clock = base, key, transport, clock
        self._token: Optional[str] = None
        self._expires = 0.0
        # A 401 or 403 from /v1/token (its status and the API's code), held for the life of the process. Each
        # refusal counts toward the API's IP lockout (10 in 600 s), so a refused key is never sent again; the user
        # fixes the key, its IP allowlist or the base, and restarts the server.
        self._key_refused: Optional[ApiError] = None

    def _send(self, method: str, path: str, bearer: Optional[str]) -> tuple[int, dict, bytes]:
        headers = {'Accept': 'application/json', 'User-Agent': f'npreq-mcp/{VERSION}'}
        if bearer:
            headers['Authorization'] = f'Bearer {bearer}'
        try:
            status, hdrs, body = self._transport(method, self.base + path, headers)
        except (OSError, http.client.HTTPException, ValueError):
            raise ApiError(0, message=UNREACHABLE) from None
        if len(body) > MAX_BODY:
            raise ApiError(0, message=TOO_LARGE)
        return status, hdrs, body

    @staticmethod
    def _error(status: int, hdrs: dict, body: bytes) -> ApiError:
        try:
            code = json.loads(body.decode('utf-8')).get('error')
        except (*JSON_ERRORS, AttributeError):
            code = None
        # Only a string can be one of the API's codes; a list or object is unhashable and is no code.
        return ApiError(status, code if isinstance(code, str) and code in API_ERROR_CODES else None,
                        retry_after(hdrs.get('retry-after')) if status in (429, 503) else None)

    def _renew(self) -> str:
        if not self._key:
            raise ApiError(0, message=NO_KEY)
        if not key_shape_ok(self._key):
            raise ApiError(0, message=BAD_KEY)
        if self._key_refused is not None:
            raise ApiError(self._key_refused.status, self._key_refused.code, message=KEY_HELD)
        status, hdrs, body = self._send('POST', '/v1/token', self._key)
        if status != 200:
            err = self._error(status, hdrs, body)
            if status in HELD_STATUSES:
                self._key_refused = err
            raise err
        try:
            data = json.loads(body.decode('utf-8'))
            token, ttl = data['access_token'], data['expires_in']
            # A JSON number only (not a string or a bool), finite and positive; float() of a huge integer
            # raises OverflowError, and NaN or Infinity would fetch a token every call or never renew early.
            if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
                raise ValueError('expires_in')
            ttl = float(ttl)
        except (*JSON_ERRORS, KeyError, TypeError, OverflowError):
            raise ApiError(0, message=NOT_JSON) from None
        if (not isinstance(token, str) or not token.startswith(('npat_live_', 'npat_test_'))
                or not math.isfinite(ttl) or ttl <= 0):
            raise ApiError(0, message=NOT_JSON)
        self._token, self._expires = token, self._clock() + min(ttl, MAX_TOKEN_TTL)
        return token

    def _current(self) -> str:
        if self._token and self._expires - self._clock() >= RENEW_BEFORE:
            return self._token
        return self._renew()

    def get(self, path: str, needs_token: bool) -> Any:
        """GET a data route with the token (renewed before expiry). On a 401 for a request that carried a token, whatever
        its code, renew once and retry once: a token can also be refused because it expired, was evicted (the 11th live
        token on a key blocks the oldest) or was blocked, and the key behind it may still be good. A
        revoked key then fails at POST /v1/token. One retry only, so a refusal never loops into the IP lockout."""
        status, hdrs, body = self._send('GET', path, self._current() if needs_token else None)
        if needs_token and status == 401:
            self._token = None
            status, hdrs, body = self._send('GET', path, self._renew())
        if status != 200:
            raise self._error(status, hdrs, body)
        try:
            return json.loads(body.decode('utf-8'))
        except JSON_ERRORS:                                   # deep nesting is RecursionError
            raise ApiError(0, message=NOT_JSON) from None


def _tool_error(err: ApiError) -> dict:
    detail = {'status': err.status, 'message': err.message}
    if err.code:
        detail['code'] = err.code
    if err.retry is not None:
        detail['retry_after_seconds'] = err.retry
    payload = {'error': detail}
    return {'content': [{'type': 'text', 'text': json.dumps(payload)}], 'structuredContent': payload, 'isError': True}


def call_tool(api: NpreqApi, name: str, args: dict) -> dict:
    """One tool call -> a CallToolResult. Arguments are checked exactly as the route checks them."""
    path, needs_token, _, _, schema = TOOLS[name]
    allowed = set(schema['properties'])
    if set(args) - allowed or set(schema.get('required', [])) - set(args):
        return _tool_error(ApiError(400, 'bad_request', message=f'Arguments must be exactly: {sorted(allowed) or "none"}.'))
    if 'code' in allowed and not valid_code(args['code']):
        return _tool_error(ApiError(400, 'bad_request', message='code must be two capital letters, for example TX.'))
    if 'q' in allowed and not valid_query(args['q']):
        return _tool_error(ApiError(400, 'bad_request', message=MESSAGES[400] + ' q: 2 to 64 letters, digits, '
                                    "spaces, hyphens, apostrophes or periods."))
    if 'code' in allowed:
        path = path.replace('{code}', args['code'])
    if 'q' in allowed:
        path += '?' + urllib.parse.urlencode({'q': args['q']})
    try:
        data = api.get(path, needs_token)
        text = json.dumps(data, ensure_ascii=False)
    except ApiError as err:
        return _tool_error(err)
    except (RecursionError, ValueError):                       # a reply too deep to write back out
        return _tool_error(ApiError(0, message=NOT_JSON))
    return {'content': [{'type': 'text', 'text': text}], 'structuredContent': data, 'isError': False}


def tool_list() -> list[dict]:
    return [{'name': n, 'title': t, 'description': f'{d} {DATA_NOTE}', 'inputSchema': s, 'annotations': dict(_READ_ONLY)}
            for n, (_, _, t, d, s) in TOOLS.items()]


class Server:
    """The JSON-RPC side. handle() takes one decoded message and returns the reply, or None for a notification."""

    def __init__(self, api: NpreqApi, log=lambda text: None):
        self.api, self.log = api, log
        self.legacy: Optional[str] = None  # the negotiated legacy revision once initialize was answered

    @staticmethod
    def _err(mid, code: int, message: str, data=None) -> dict:
        error = {'code': code, 'message': message}
        if data is not None:
            error['data'] = data
        return {'jsonrpc': '2.0', 'id': mid, 'error': error}

    def handle(self, msg: Any) -> Optional[dict]:
        if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0' or not isinstance(msg.get('method'), str):
            mid = msg.get('id') if isinstance(msg, dict) and isinstance(msg.get('id'), (str, int)) else None
            return self._err(mid, -32600, 'Invalid Request')
        if 'id' not in msg:
            return None                                     # a notification: never answered
        mid, method = msg['id'], msg['method']
        if mid is None or isinstance(mid, bool) or not isinstance(mid, (str, int)):
            return self._err(None, -32600, 'Invalid Request: id must be a string or an integer')
        params = msg.get('params', {})
        if not isinstance(params, dict):
            return self._err(mid, -32602, 'Invalid params: params must be an object')
        meta = params.get('_meta') if isinstance(params.get('_meta'), dict) else {}

        if method == 'initialize' and META_VERSION not in meta:
            asked = params.get('protocolVersion')
            self.legacy = asked if asked in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
            return self._ok(mid, {'protocolVersion': self.legacy, 'capabilities': {'tools': {'listChanged': False}},
                                  'serverInfo': dict(SERVER_INFO), 'instructions': INSTRUCTIONS}, modern=False)
        if META_VERSION in meta:
            version = meta[META_VERSION]
            if version not in MODERN_VERSIONS:
                return self._err(mid, -32022, 'Unsupported protocol version',
                                 {'supported': list(SUPPORTED_VERSIONS), 'requested': version})
            if not isinstance(meta.get(META_CAPS), dict):
                return self._err(mid, -32602, f'Invalid params: _meta must include {META_CAPS}')
            modern = True
        elif self.legacy:
            modern = False
        else:
            return self._err(mid, -32602, f'Invalid params: _meta must include {META_VERSION} (supported: '
                             f'{", ".join(SUPPORTED_VERSIONS)}), or the client must send initialize first')

        if method == 'server/discover':
            return self._ok(mid, {'supportedVersions': list(SUPPORTED_VERSIONS),
                                  'capabilities': {'tools': {'listChanged': False}}, 'instructions': INSTRUCTIONS}, modern)
        if method == 'ping':
            return self._ok(mid, {}, modern)
        if method == 'tools/list':
            return self._ok(mid, {'tools': tool_list()}, modern)
        if method == 'tools/call':
            name, args = params.get('name'), params.get('arguments', {})
            if not isinstance(name, str) or name not in TOOLS:
                return self._err(mid, -32602, 'Unknown tool')
            if not isinstance(args, dict):
                return self._err(mid, -32602, 'Invalid params: arguments must be an object')
            result = call_tool(self.api, name, args)
            status = result['structuredContent']['error']['status'] if result['isError'] else 200
            self.log(f'tools/call {name}: {status}')
            return self._ok(mid, result, modern)
        return self._err(mid, -32601, 'Method not found')

    @staticmethod
    def _ok(mid, result: dict, modern: bool) -> dict:
        if modern:
            result = {'resultType': 'complete', **result, '_meta': {META_SERVER: dict(SERVER_INFO)}}
        return {'jsonrpc': '2.0', 'id': mid, 'result': result}


def serve(server: Server, stdin, stdout) -> None:
    """Newline-delimited JSON-RPC on binary stdin/stdout until EOF."""
    while True:
        line = stdin.readline(MAX_LINE + 1)
        if not line:
            return
        if len(line) > MAX_LINE:
            while line and not line.endswith(b'\n'):   # drop the rest of the oversized message
                line = stdin.readline(MAX_LINE)
            reply = Server._err(None, -32600, 'Invalid Request: message too large')
        elif not line.strip():
            continue
        else:
            try:
                msg = json.loads(line.decode('utf-8'))
            except JSON_ERRORS:                                # deep nesting is a parse error too
                reply = Server._err(None, -32700, 'Parse error')
            else:
                try:
                    reply = server.handle(msg)
                except Exception:                              # one bad message never ends the server
                    reply = Server._err(_request_id(msg), -32603, 'Internal error')
        if reply is not None:
            stdout.write(_encode(reply) + b'\n')
            stdout.flush()


def _request_id(msg: Any):
    """The id to answer with when a request failed inside: its own id when that is a valid one, else null."""
    mid = msg.get('id') if isinstance(msg, dict) else None
    return mid if isinstance(mid, (str, int)) and not isinstance(mid, bool) else None


def _encode(reply: dict) -> bytes:
    """One reply as ASCII JSON. Escaping every non-ASCII character keeps a lone surrogate (valid in a JSON
    string, not in UTF-8) encodable; a reply that cannot be written at all becomes an internal error for its id."""
    try:
        return json.dumps(reply, ensure_ascii=True).encode('ascii')
    except (RecursionError, ValueError, TypeError):
        return json.dumps(Server._err(_request_id(reply), -32603, 'Internal error')).encode('ascii')


def read_config(environ) -> tuple[str, Optional[str]]:
    """The base and the key from the process environment, exactly as the MCP client set it (the Claude Code plugin
    sets both from its own settings, over anything inherited from the shell). The key is taken out of the environment
    once read. Raises ConfigError for a base that is not an https:// origin."""
    key = environ.pop('NPREQ_API_KEY', '').strip() or None
    return check_base(environ.get('NPREQ_API_BASE', '').strip() or DEFAULT_BASE), key


def main(argv: list[str]) -> int:
    if argv == ['--version']:
        print(f'npreq-mcp {VERSION}')
        return 0
    if argv:
        print('usage: npreq-mcp [--version]   (the key comes from NPREQ_API_KEY only)', file=sys.stderr)
        return 2
    try:
        base, key = read_config(os.environ)
    except ConfigError as e:
        print(f'npreq-mcp: {e}', file=sys.stderr)
        return 2

    def log(text: str) -> None:
        print(f'npreq-mcp: {text}', file=sys.stderr, flush=True)

    log(f'ready on stdio for {base}' + ('' if key else ' (NPREQ_API_KEY not set: only get_data_version works)'))
    serve(Server(NpreqApi(base, key), log), sys.stdin.buffer, sys.stdout.buffer)
    return 0


def cli() -> None:
    """The console-script entry point (pyproject [project.scripts]): it takes no argument, so it reads sys.argv."""
    sys.exit(main(sys.argv[1:]))


if __name__ == '__main__':
    cli()
