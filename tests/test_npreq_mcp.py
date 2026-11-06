"""Tests for npreq-mcp: python tests/test_npreq_mcp.py

No network: the REST API is a fake transport, and the stdio tests run the real script as a child process
against a base it can never reach.
"""
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / 'npreq_mcp.py'
sys.path.insert(0, str(HERE.parent))
import npreq_mcp as m  # noqa: E402

KEY = 'npq_test_' + 'Kq7Rw2' * 7               # a made-up key, shaped like one
TOKEN1 = 'npat_test_' + 'Tf3Hb8' * 7
TOKEN2 = 'npat_test_' + 'Vx5Pn1' * 7
SECRETS = (KEY, TOKEN1, TOKEN2, KEY[9:], TOKEN1[10:], TOKEN2[10:])
MODERN = {m.META_VERSION: '2026-07-28', m.META_CAPS: {}, 'io.modelcontextprotocol/clientInfo': {'name': 't', 'version': '1'}}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeApi:
    """A REST API double: records every request and answers from a script of (status, headers, body)."""

    def __init__(self, routes=None):
        self.calls = []
        self.routes = routes or {}
        self.tokens = [TOKEN1, TOKEN2]
        self.minute = 0
        self.limit = None

    def __call__(self, method, url, headers):
        self.calls.append((method, url, dict(headers)))
        path = url[len('https://api.example.test'):]
        if path in self.routes:
            answer = self.routes[path]
            return answer(headers) if callable(answer) else answer
        if method == 'POST' and path == '/v1/token':
            if headers.get('Authorization') != f'Bearer {KEY}':
                return 401, {}, b'{"error":"unauthorized"}'
            return 200, {}, json.dumps({'access_token': self.tokens.pop(0), 'token_type': 'Bearer',
                                        'expires_in': 21600, 'expires_at': '2026-11-02T00:00:00Z',
                                        'scopes': ['meta', 'states:read']}).encode()
        if self.limit is not None and path.startswith('/v1/states'):
            self.minute += 1
            if self.minute > self.limit:
                return 429, {'retry-after': '37'}, b'{"error":"rate_limited"}'
        if path == '/v1/meta':
            return 200, {}, b'{"data_version":"2026-11-01","api_version":"v1"}'
        if headers.get('Authorization') not in (f'Bearer {TOKEN1}', f'Bearer {TOKEN2}'):
            return 401, {}, b'{"error":"use_access_token"}'
        if path == '/v1/states':
            return 200, {}, b'{"states":[{"code":"TX"}]}'
        if path.startswith('/v1/states/'):
            return 200, {}, json.dumps({'path': path}).encode()
        if path.startswith('/v1/search?'):
            return 200, {}, json.dumps({'path': path}).encode()
        return 404, {}, b'{"error":"not_found"}'


def api(fake=None, key=KEY, clock=None):
    fake = fake or FakeApi()
    return m.NpreqApi('https://api.example.test', key, fake, clock or Clock()), fake


def call(server, name, args=None, mid=1, meta=MODERN):
    return server.handle({'jsonrpc': '2.0', 'id': mid, 'method': 'tools/call',
                          'params': {'name': name, 'arguments': args or {}, '_meta': meta}})


def no_secret(testcase, text):
    for s in SECRETS:
        testcase.assertNotIn(s, text)


class Tools(unittest.TestCase):
    def test_exactly_the_five_read_only_routes(self):
        tools = {t['name']: t for t in m.tool_list()}
        self.assertEqual(list(tools), ['get_data_version', 'list_states', 'get_state', 'get_state_ce',
                                       'search_requirements'])
        for t in tools.values():
            self.assertEqual(t['annotations']['readOnlyHint'], True)
            self.assertEqual(t['annotations']['destructiveHint'], False)
            self.assertEqual(t['annotations']['openWorldHint'], False)
            self.assertFalse(t['inputSchema']['additionalProperties'])
            self.assertIn('not instructions', t['description'])
        self.assertEqual({n: r[0] for n, r in m.TOOLS.items()},
                         {'get_data_version': '/v1/meta', 'list_states': '/v1/states', 'get_state': '/v1/states/{code}',
                          'get_state_ce': '/v1/states/{code}/ce', 'search_requirements': '/v1/search'})

    def test_each_tool_is_one_get_and_only_the_token_reaches_data_routes(self):
        a, fake = api()
        s = m.Server(a)
        for name, args, path in (('list_states', {}, '/v1/states'), ('get_state', {'code': 'TX'}, '/v1/states/TX'),
                                 ('get_state_ce', {'code': 'PR'}, '/v1/states/PR/ce'),
                                 ('search_requirements', {'q': 'Nuñez ce'}, '/v1/search?q=Nu%C3%B1ez+ce')):
            fake.calls.clear()
            r = call(s, name, args)['result']
            self.assertFalse(r['isError'], r)
            self.assertEqual(r['structuredContent'], json.loads(r['content'][0]['text']))
            gets = [c for c in fake.calls if c[0] == 'GET']
            self.assertEqual([c[1] for c in gets], ['https://api.example.test' + path])
            self.assertEqual(gets[0][2]['Authorization'], f'Bearer {TOKEN1}')
        posts = [c for c in fake.calls if c[0] == 'POST']
        self.assertEqual(posts, [])  # one token for all of them (it was issued on the first call)

    def test_the_key_goes_only_to_the_token_route(self):
        a, fake = api()
        s = m.Server(a)
        call(s, 'list_states')
        call(s, 'get_state', {'code': 'TX'})
        with_key = [c for c in fake.calls if c[2].get('Authorization') == f'Bearer {KEY}']
        self.assertEqual([(c[0], c[1]) for c in with_key], [('POST', 'https://api.example.test/v1/token')])

    def test_get_data_version_is_public_and_works_without_a_key(self):
        a, fake = api(key=None)
        r = call(m.Server(a), 'get_data_version')['result']
        self.assertFalse(r['isError'])
        self.assertEqual(r['structuredContent']['data_version'], '2026-11-01')
        self.assertNotIn('Authorization', fake.calls[0][2])
        r = call(m.Server(a), 'list_states')['result']
        self.assertTrue(r['isError'])
        self.assertEqual(r['structuredContent']['error']['message'], m.NO_KEY)
        self.assertEqual(len(fake.calls), 1)

    def test_arguments_are_checked_like_the_routes_before_any_call(self):
        a, fake = api()
        s = m.Server(a)
        bad = [('get_state', {'code': 'tx'}), ('get_state', {'code': 'TXX'}), ('get_state', {}),
               ('get_state', {'code': 'TX', 'x': 1}), ('get_state_ce', {'code': 7}), ('list_states', {'x': 1}),
               ('search_requirements', {'q': 'a'}), ('search_requirements', {'q': 'x' * 65}),
               ('search_requirements', {'q': 'ce; drop'}), ('search_requirements', {'q': 'ce\n'}),
               ('search_requirements', {'q': '<b>'})]
        for name, args in bad:
            r = call(s, name, args)['result']
            self.assertTrue(r['isError'], (name, args))
            self.assertEqual(r['structuredContent']['error']['status'], 400)
        self.assertEqual(fake.calls, [])
        for q in ('Nuñez', "O'Neil", 'CE 2026', 'opioid-pain', 'Dr. X', 'ab', 'é' * 64, '１２'):
            self.assertTrue(m.valid_query(q), q)

    def test_unknown_tool_and_bad_arguments_are_protocol_errors(self):
        s = m.Server(api()[0])
        self.assertEqual(call(s, 'drop_tables')['error']['code'], -32602)
        r = s.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                      'params': {'name': 'list_states', 'arguments': [], '_meta': MODERN}})
        self.assertEqual(r['error']['code'], -32602)


class Tokens(unittest.TestCase):
    def test_renewed_when_fewer_than_five_minutes_are_left(self):
        clock = Clock()
        a, fake = api(clock=clock)
        s = m.Server(a)
        call(s, 'list_states')
        clock.t += 21600 - 301
        call(s, 'list_states')
        self.assertEqual(sum(c[0] == 'POST' for c in fake.calls), 1)
        clock.t += 2                                  # now 299 s left
        call(s, 'list_states')
        self.assertEqual(sum(c[0] == 'POST' for c in fake.calls), 2)
        self.assertEqual(fake.calls[-1][2]['Authorization'], f'Bearer {TOKEN2}')

    def test_one_token_expired_renews_once_and_retries_once(self):
        fake = FakeApi()
        seen = []

        def states(headers):
            seen.append(headers['Authorization'])
            if headers['Authorization'] == f'Bearer {TOKEN1}':
                return 401, {}, b'{"error":"token_expired"}'
            return 200, {}, b'{"states":[]}'
        fake.routes['/v1/states'] = states
        r = call(m.Server(api(fake)[0]), 'list_states')['result']
        self.assertFalse(r['isError'], r)
        self.assertEqual(seen, [f'Bearer {TOKEN1}', f'Bearer {TOKEN2}'])

    def test_a_second_token_expired_is_not_retried_again(self):
        fake = FakeApi()
        fake.routes['/v1/states'] = (401, {}, b'{"error":"token_expired"}')
        r = call(m.Server(api(fake)[0]), 'list_states')['result']
        self.assertTrue(r['isError'])
        self.assertEqual(sum(c[0] == 'GET' for c in fake.calls), 2)
        self.assertEqual(r['structuredContent']['error']['status'], 401)

    def test_an_evicted_or_blocked_token_renews_once_and_succeeds(self):
        # The 11th live token on a key evicts the oldest, which then gets a plain 401 unauthorized
        # while the key is still good. One renew, one retry, then it works; no second refusal to feed the IP lockout.
        fake = FakeApi()
        seen = []

        def states(headers):
            seen.append(headers['Authorization'])
            if headers['Authorization'] == f'Bearer {TOKEN1}':
                return 401, {}, b'{"error":"unauthorized"}'
            return 200, {}, b'{"states":[]}'
        fake.routes['/v1/states'] = states
        s = m.Server(api(fake)[0])
        r = call(s, 'list_states')['result']
        self.assertFalse(r['isError'], r)
        self.assertEqual(seen, [f'Bearer {TOKEN1}', f'Bearer {TOKEN2}'])
        self.assertEqual(sum(c[0] == 'POST' for c in fake.calls), 2)
        self.assertFalse(call(s, 'list_states', mid=2)['result']['isError'])
        self.assertEqual(seen[-1], f'Bearer {TOKEN2}')            # the new token is kept

    def test_a_revoked_key_fails_on_the_next_call(self):
        fake = FakeApi()
        s = m.Server(api(fake)[0])
        self.assertFalse(call(s, 'list_states')['result']['isError'])
        fake.routes['/v1/states'] = (401, {}, b'{"error":"unauthorized"}')   # the API re-checks the key each call
        fake.routes['/v1/token'] = (401, {}, b'{"error":"unauthorized"}')    # and refuses the revoked key itself
        fake.calls.clear()
        r = call(s, 'list_states')['result']
        self.assertTrue(r['isError'])
        self.assertEqual(r['structuredContent']['error']['status'], 401)
        self.assertEqual(r['structuredContent']['error']['message'], m.MESSAGES[401])
        self.assertEqual([(c[0], c[1].rsplit('/v1', 1)[1]) for c in fake.calls], [('GET', '/states'), ('POST', '/token')])

    def test_a_key_refused_at_the_token_route_is_held_for_the_session(self):
        # After /v1/token refuses the key (401 or 403), every later token-needing call
        # answers at once with no request, so retried tool calls cannot walk the user's IP into the lockout.
        for status, code in ((401, 'unauthorized'), (403, 'forbidden')):
            fake = FakeApi()
            fake.routes['/v1/token'] = (status, {}, json.dumps({'error': code}).encode())
            s = m.Server(api(fake)[0])
            first = call(s, 'list_states')['result']
            self.assertTrue(first['isError'])
            self.assertEqual(first['structuredContent']['error']['status'], status)
            self.assertEqual(first['structuredContent']['error']['message'], m.MESSAGES[status])
            self.assertEqual(len(fake.calls), 1)
            for mid, (name, args) in enumerate((('list_states', {}), ('get_state', {'code': 'TX'}),
                                                ('get_state_ce', {'code': 'PR'}), ('search_requirements', {'q': 'ce'})),
                                               start=2):
                r = call(s, name, args, mid=mid)['result']
                self.assertTrue(r['isError'], (status, name))
                self.assertEqual(r['structuredContent']['error']['status'], status)
                self.assertEqual(r['structuredContent']['error']['message'], m.KEY_HELD)
                self.assertEqual(r['structuredContent']['error']['code'], code)   # the API's code is kept
                no_secret(self, json.dumps(r))
            self.assertEqual(len(fake.calls), 1, status)              # nothing sent after the refusal
            pub = call(s, 'get_data_version', mid=9)['result']        # the public route still works
            self.assertFalse(pub['isError'], pub)
            self.assertEqual([c[1].rsplit('/v1', 1)[1] for c in fake.calls], ['/token', '/meta'])

    def test_the_held_message_names_the_key_its_ip_allowlist_and_the_base(self):
        # A 403 at /v1/token is a good key from an IP off its allowlist, or https_required.
        for words in ('NPREQ_API_KEY', 'allowlist', 'NPREQ_API_BASE', 'restart'):
            self.assertIn(words, m.KEY_HELD)

    def test_a_revoked_key_found_on_a_data_call_is_held_after_its_one_renew(self):
        fake = FakeApi()
        s = m.Server(api(fake)[0])
        self.assertFalse(call(s, 'list_states')['result']['isError'])
        fake.routes['/v1/states'] = (401, {}, b'{"error":"unauthorized"}')
        fake.routes['/v1/token'] = (401, {}, b'{"error":"unauthorized"}')
        self.assertTrue(call(s, 'list_states', mid=2)['result']['isError'])
        fake.calls.clear()
        r = call(s, 'list_states', mid=3)['result']
        self.assertEqual(r['structuredContent']['error']['message'], m.KEY_HELD)
        self.assertEqual(fake.calls, [])

    def test_a_temporary_token_refusal_is_not_held(self):
        for status, hdrs, body in ((429, {'retry-after': '5'}, b'{"error":"rate_limited"}'),
                                   (503, {'retry-after': '5'}, b'{"error":"unavailable"}'),
                                   (500, {}, b'{"error":"server_error"}')):
            fake = FakeApi()
            fake.routes['/v1/token'] = (status, hdrs, body)
            s = m.Server(api(fake)[0])
            self.assertEqual(call(s, 'list_states')['result']['structuredContent']['error']['status'], status)
            del fake.routes['/v1/token']                              # the API recovers
            r = call(s, 'list_states', mid=2)['result']
            self.assertFalse(r['isError'], (status, r))
            self.assertEqual(sum(c[0] == 'POST' for c in fake.calls), 2)

    def test_a_key_that_is_not_shaped_like_one_is_never_sent(self):
        for bad in (TOKEN1, 'hunter2', 'npq_live_', 'npq_live_' + 'a' * 30 + '!'):
            a, fake = api(key=bad)
            r = call(m.Server(a), 'list_states')['result']
            self.assertEqual(r['structuredContent']['error']['message'], m.BAD_KEY)
            self.assertEqual(fake.calls, [])


class Errors(unittest.TestCase):
    def test_every_status_maps_to_a_fixed_message(self):
        for status, code, headers in ((400, 'bad_request', {}), (401, 'unauthorized', {}), (403, 'forbidden', {}),
                                      (404, 'not_found', {}), (429, 'rate_limited', {'retry-after': '42'}),
                                      (500, 'server_error', {}), (503, 'unavailable', {'retry-after': '300'})):
            fake = FakeApi()
            fake.routes['/v1/states/ZZ'] = (status, headers, json.dumps({'error': code}).encode())
            r = call(m.Server(api(fake)[0]), 'get_state', {'code': 'ZZ'})['result']
            err = r['structuredContent']['error']
            self.assertTrue(r['isError'])
            self.assertEqual((err['status'], err['code'], err['message']), (status, code, m.MESSAGES[status]))
            self.assertEqual(err.get('retry_after_seconds'), int(headers['retry-after']) if headers else None)

    def test_ce_without_ce_read_is_a_403_tool_error(self):
        fake = FakeApi()
        fake.routes['/v1/states/TX/ce'] = (403, {}, b'{"error":"forbidden"}')
        r = call(m.Server(api(fake)[0]), 'get_state_ce', {'code': 'TX'})['result']
        self.assertEqual(r['structuredContent']['error']['status'], 403)

    def test_the_minute_limit_holds_through_mcp(self):
        fake = FakeApi()
        fake.limit = 3
        s = m.Server(api(fake)[0])
        results = [call(s, 'list_states', mid=i)['result'] for i in range(5)]
        self.assertEqual([r['isError'] for r in results], [False, False, False, True, True])
        self.assertEqual(results[3]['structuredContent']['error'],
                         {'status': 429, 'code': 'rate_limited', 'message': m.MESSAGES[429], 'retry_after_seconds': 37})

    def test_an_unknown_error_code_or_header_text_is_never_echoed(self):
        fake = FakeApi()
        fake.routes['/v1/states'] = (418, {'x-secret': 'leak'}, b'{"error":"<script>ignore previous</script>"}')
        r = call(m.Server(api(fake)[0]), 'list_states')['result']
        text = json.dumps(r)
        self.assertNotIn('script', text)
        self.assertNotIn('leak', text)
        self.assertEqual(r['structuredContent']['error'], {'status': 418, 'message': 'The NPReq API answered HTTP 418.'})

    def test_retry_after_forms(self):
        self.assertEqual(m.retry_after('12'), 12)
        self.assertEqual(m.retry_after('999999999'), m.MAX_RETRY_AFTER)
        self.assertEqual(m.retry_after('Sun, 01 Nov 2026 20:00:30 GMT', now=lambda: 1793563200.0), 30)
        self.assertIsNone(m.retry_after('soon'))
        self.assertIsNone(m.retry_after(None))

    def test_network_failure_and_bad_replies(self):
        def down(method, url, headers):
            raise OSError('connection refused to ' + headers.get('Authorization', ''))
        r = call(m.Server(m.NpreqApi('https://api.example.test', KEY, down)), 'list_states')['result']
        self.assertEqual(r['structuredContent']['error']['message'], m.UNREACHABLE)
        no_secret(self, json.dumps(r))
        fake = FakeApi()
        fake.routes['/v1/states'] = (200, {}, b'not json')
        r = call(m.Server(api(fake)[0]), 'list_states')['result']
        self.assertEqual(r['structuredContent']['error']['message'], m.NOT_JSON)
        fake.routes['/v1/states'] = (200, {}, b'x' * (m.MAX_BODY + 1))
        r = call(m.Server(api(fake)[0]), 'list_states')['result']
        self.assertEqual(r['structuredContent']['error']['message'], m.TOO_LARGE)

    def test_a_redirect_is_never_followed(self):
        fake = FakeApi()
        fake.routes['/v1/states'] = (302, {'location': 'https://evil.example/x'}, b'')
        r = call(m.Server(api(fake)[0]), 'list_states')['result']
        self.assertEqual(r['structuredContent']['error']['status'], 302)
        self.assertFalse(any('evil' in c[1] for c in fake.calls))
        self.assertIsNone(m._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.example/x'))


class Protocol(unittest.TestCase):
    def test_modern_requests_are_stateless_and_marked_complete(self):
        s = m.Server(api()[0])
        r = s.handle({'jsonrpc': '2.0', 'id': 'a', 'method': 'server/discover', 'params': {'_meta': MODERN}})['result']
        self.assertEqual(r['resultType'], 'complete')
        self.assertEqual(r['supportedVersions'][0], '2026-07-28')
        self.assertEqual(r['capabilities'], {'tools': {'listChanged': False}})
        self.assertEqual(r['_meta'][m.META_SERVER]['name'], 'npreq-mcp')
        r = s.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {'_meta': MODERN}})['result']
        self.assertEqual(len(r['tools']), 5)
        self.assertEqual(r['resultType'], 'complete')

    def test_modern_version_and_meta_checks(self):
        s = m.Server(api()[0])
        r = s.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list',
                      'params': {'_meta': {m.META_VERSION: '1900-01-01', m.META_CAPS: {}}}})
        self.assertEqual(r['error']['code'], -32022)
        self.assertEqual(r['error']['data'], {'supported': list(m.SUPPORTED_VERSIONS), 'requested': '1900-01-01'})
        r = s.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {'_meta': {m.META_VERSION: '2026-07-28'}}})
        self.assertEqual(r['error']['code'], -32602)
        r = s.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        self.assertEqual(r['error']['code'], -32602)
        self.assertIn('2026-07-28', r['error']['message'])

    def test_legacy_initialize_selects_the_handshake_for_the_process(self):
        s = m.Server(api()[0])
        r = s.handle({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize',
                      'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'c'}}})
        self.assertEqual(r['result']['protocolVersion'], '2025-06-18')
        self.assertNotIn('resultType', r['result'])
        self.assertEqual(r['result']['serverInfo']['name'], 'npreq-mcp')
        self.assertIsNone(s.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'}))
        r = s.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        self.assertEqual(len(r['result']['tools']), 5)
        r = call(s, 'get_data_version', meta={})
        self.assertFalse(r['result']['isError'])
        s2 = m.Server(api()[0])
        r = s2.handle({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {'protocolVersion': '2099-01-01'}})
        self.assertEqual(r['result']['protocolVersion'], m.LEGACY_VERSIONS[0])

    def test_json_rpc_edges(self):
        s = m.Server(api()[0])
        self.assertEqual(s.handle([])['error']['code'], -32600)
        self.assertEqual(s.handle({'jsonrpc': '1.0', 'id': 1, 'method': 'ping'})['error']['code'], -32600)
        self.assertEqual(s.handle({'jsonrpc': '2.0', 'id': None, 'method': 'ping'})['error']['code'], -32600)
        self.assertEqual(s.handle({'jsonrpc': '2.0', 'id': True, 'method': 'ping'})['error']['code'], -32600)
        self.assertEqual(s.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'ping', 'params': []})['error']['code'], -32602)
        self.assertEqual(s.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'resources/list',
                                   'params': {'_meta': MODERN}})['error']['code'], -32601)
        self.assertEqual(s.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'ping', 'params': {'_meta': MODERN}})['result'],
                         {'resultType': 'complete', '_meta': {m.META_SERVER: m.SERVER_INFO}})

    def test_serve_reads_lines_and_answers_each_request(self):
        s = m.Server(api()[0])
        lines = [json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'ping', 'params': {'_meta': MODERN}}), '',
                 'not json', json.dumps({'jsonrpc': '2.0', 'method': 'notifications/x'}),
                 'x' * (m.MAX_LINE + 10)]
        out = io.BytesIO()
        m.serve(s, io.BytesIO(('\n'.join(lines) + '\n').encode()), out)
        replies = [json.loads(line) for line in out.getvalue().decode().splitlines()]
        self.assertEqual([r.get('id') for r in replies], [1, None, None])
        self.assertEqual([r.get('error', {}).get('code') for r in replies], [None, -32700, -32600])


DEEP = '[' * 100_000 + ']' * 100_000          # nesting far past Python's recursion limit, well under MAX_LINE


def ping(mid):
    return json.dumps({'jsonrpc': '2.0', 'id': mid, 'method': 'ping', 'params': {'_meta': MODERN}})


def serve_lines(server, lines):
    out = io.BytesIO()
    m.serve(server, io.BytesIO(('\n'.join(lines) + '\n').encode('utf-8')), out)
    return [json.loads(line) for line in out.getvalue().decode('ascii').splitlines()]


class CrashPaths(unittest.TestCase):
    """Malformed or hostile input. Each input once ended the server with an exception; now each gets a
    fixed JSON-RPC error or tool error, and the server keeps answering the next message."""

    def test_v1_deeply_nested_json_on_stdin_is_a_parse_error(self):
        replies = serve_lines(m.Server(api()[0]), [DEEP, '{"a":' * 50_000 + '1' + '}' * 50_000, ping(7)])
        self.assertEqual([r.get('error', {}).get('code') for r in replies], [-32700, -32700, None])
        self.assertEqual(replies[-1]['id'], 7)

    def test_v2_a_lone_surrogate_in_an_id_is_answered_as_ascii_json(self):
        replies = serve_lines(m.Server(api()[0]), ['{"jsonrpc":"2.0","id":"\\ud800","method":"ping","params":{"_meta":'
                                                   + json.dumps(MODERN) + '}}', ping(8)])
        self.assertEqual(replies[0]['id'], '\ud800')                        # escaped on the wire, the same id back
        self.assertEqual(replies[0]['result']['resultType'], 'complete')
        self.assertEqual(replies[1]['id'], 8)

    def test_an_unexpected_error_inside_a_request_is_an_internal_error(self):
        s = m.Server(api()[0])
        with mock.patch.object(m.Server, 'handle', side_effect=RuntimeError('boom')):
            replies = serve_lines(s, [ping(9), ping(10)])
        self.assertEqual([(r['id'], r['error']['code'], r['error']['message']) for r in replies],
                         [(9, -32603, 'Internal error'), (10, -32603, 'Internal error')])
        self.assertNotIn('boom', json.dumps(replies))                       # never the exception text

    def test_v3_a_deeply_nested_api_reply_is_a_tool_error(self):
        fake = FakeApi()
        fake.routes['/v1/states'] = (200, {}, DEEP.encode())
        s = m.Server(api(fake)[0])
        r = call(s, 'list_states')['result']
        self.assertTrue(r['isError'])
        self.assertEqual(r['structuredContent']['error']['message'], m.NOT_JSON)
        self.assertEqual(call(s, 'get_data_version')['result']['isError'], False)

    def test_v3_the_rest_client_itself_turns_a_deep_reply_into_not_json(self):
        """The client's own catch, not only call_tool's: NpreqApi.get raises ApiError, never RecursionError."""
        fake = FakeApi()
        fake.routes['/v1/meta'] = (200, {}, DEEP.encode())
        with self.assertRaises(m.ApiError) as cm:
            api(fake)[0].get('/v1/meta', False)
        self.assertEqual(cm.exception.message, m.NOT_JSON)

    def test_v3_a_deeply_nested_error_body_has_no_code(self):
        fake = FakeApi()
        fake.routes['/v1/states/ZZ'] = (404, {}, DEEP.encode())
        err = call(m.Server(api(fake)[0]), 'get_state', {'code': 'ZZ'})['result']['structuredContent']['error']
        self.assertEqual((err['status'], err.get('code'), err['message']), (404, None, m.MESSAGES[404]))

    def test_v4_a_non_string_error_code_is_no_code(self):
        for body in (b'{"error":[]}', b'{"error":{"x":1}}', b'{"error":7}', b'{"error":null}', b'[]', b'"x"'):
            fake = FakeApi()
            fake.routes['/v1/states/ZZ'] = (403, {}, body)
            err = call(m.Server(api(fake)[0]), 'get_state', {'code': 'ZZ'})['result']['structuredContent']['error']
            self.assertEqual((err['status'], err.get('code'), err['message']), (403, None, m.MESSAGES[403]), body)

    def token_reply(self, expires_in_json):
        fake = FakeApi()
        body = ('{"access_token":"%s","token_type":"Bearer","expires_in":%s}' % (TOKEN1, expires_in_json)).encode()
        fake.routes['/v1/token'] = lambda headers: (200, {}, body)
        return fake

    def test_v5_an_expires_in_that_is_not_a_finite_positive_number_is_refused(self):
        for value in ('NaN', 'Infinity', '-Infinity', '1e400', '1' + '0' * 400, '0', '-5', '"21600"', 'true', 'null'):
            fake = self.token_reply(value)
            r = call(m.Server(api(fake)[0]), 'list_states')['result']
            self.assertTrue(r['isError'], value)
            self.assertEqual(r['structuredContent']['error']['message'], m.NOT_JSON, value)

    def test_v5_a_very_long_expires_in_is_capped_so_the_token_is_still_renewed(self):
        clock = Clock()
        fake = self.token_reply('1e300')
        s = m.Server(api(fake, clock=clock)[0])
        call(s, 'list_states')
        clock.t += m.MAX_TOKEN_TTL - m.RENEW_BEFORE + 1                  # past the capped lifetime's renew point
        call(s, 'list_states')
        self.assertEqual(sum(c[0] == 'POST' for c in fake.calls), 2)


class Config(unittest.TestCase):
    def test_only_an_https_origin_is_accepted(self):
        self.assertEqual(m.check_base('https://api-staging.npreq.com'), 'https://api-staging.npreq.com')
        self.assertEqual(m.check_base('https://api.npreq.com/'), 'https://api.npreq.com')
        for bad in ('http://api.npreq.com', 'api.npreq.com', 'https://', 'https://u:p@api.npreq.com',
                    'https://api.npreq.com/v1', 'https://api.npreq.com?x=1', 'file:///etc/passwd', 'ftp://x'):
            with self.assertRaises(m.ConfigError, msg=bad):
                m.check_base(bad)


ROOT = HERE.parent
SHELL_KEY = 'npq_live_' + 'Zs4Mc9' * 7         # a different made-up key, as if already set in the user's shell


def resolve(value, options):
    """Claude Code's substitution of ${user_config.KEY} and ${CLAUDE_PLUGIN_ROOT} in a stdio server's fields."""
    for k, v in options.items():
        value = value.replace('${user_config.%s}' % k, v)
    return value.replace('${CLAUDE_PLUGIN_ROOT}', str(ROOT))


class Plugin(unittest.TestCase):
    """The Claude Code plugin's manifest, MCP config and marketplace, and that the key the plugin passes is the
    one used. (claude plugin validate --strict is run on the folder before each release; it needs the claude CLI.)"""

    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((ROOT / '.claude-plugin' / 'plugin.json').read_text(encoding='utf-8'))
        cls.mcp = json.loads((ROOT / '.mcp.json').read_text(encoding='utf-8'))
        cls.market = json.loads((ROOT / '.claude-plugin' / 'marketplace.json').read_text(encoding='utf-8'))

    def test_the_key_is_a_required_sensitive_setting(self):
        self.assertEqual(self.manifest['name'], 'npreq')
        self.assertEqual(self.manifest['version'], m.VERSION)
        self.assertEqual(self.manifest['license'], 'MIT')
        key = self.manifest['userConfig']['api_key']
        self.assertEqual({k: key[k] for k in ('type', 'sensitive', 'required')},
                         {'type': 'string', 'sensitive': True, 'required': True})
        self.assertTrue(key['title'] and key['description'])
        self.assertNotIn('default', key)

    def test_the_base_is_a_choice_of_the_two_npreq_apis_never_free_text(self):
        uc = self.manifest['userConfig']
        self.assertEqual(sorted(uc), ['api_base', 'api_key'])
        base = uc['api_base']
        self.assertEqual(base['options'], ['https://api.npreq.com', 'https://api-staging.npreq.com'])
        self.assertEqual(base['default'], m.DEFAULT_BASE)
        self.assertFalse(base.get('sensitive') or base.get('multiple'))
        for option in base['options']:
            self.assertEqual(m.check_base(option), option)

    def test_the_server_gets_exactly_the_plugin_settings(self):
        servers = self.mcp['mcpServers']
        self.assertEqual(list(servers), ['npreq'])
        s = servers['npreq']
        self.assertEqual(s['command'], 'python3')
        self.assertEqual(s['args'], ['${CLAUDE_PLUGIN_ROOT}/npreq_mcp.py'])
        self.assertEqual(s['env'], {'NPREQ_API_KEY': '${user_config.api_key}', 'NPREQ_API_BASE': '${user_config.api_base}'})
        self.assertTrue(Path(resolve(s['args'][0], {})).is_file())
        self.assertNotIn('mcpServers', self.manifest)       # one server definition, in .mcp.json

    def test_the_plugin_key_wins_over_one_already_in_the_shell(self):
        """The shell has SHELL_KEY; the plugin passes KEY and staging. Claude Code sets the server's env entries over
        the inherited environment, so the server reads KEY, and only KEY reaches /v1/token."""
        env = {'NPREQ_API_KEY': SHELL_KEY, 'NPREQ_API_BASE': 'https://api.npreq.com', 'PATH': '/usr/bin'}
        env.update({k: resolve(v, {'api_key': KEY, 'api_base': 'https://api-staging.npreq.com'})
                    for k, v in self.mcp['mcpServers']['npreq']['env'].items()})
        base, key = m.read_config(env)
        self.assertEqual((base, key), ('https://api-staging.npreq.com', KEY))
        self.assertNotIn('NPREQ_API_KEY', env)               # taken out of the environment once read
        a, fake = api(key=key)
        self.assertFalse(call(m.Server(a), 'list_states')['result']['isError'])
        posts = [c for c in fake.calls if c[0] == 'POST']
        self.assertEqual([c[2]['Authorization'] for c in posts], [f'Bearer {KEY}'])
        self.assertNotIn(SHELL_KEY, json.dumps(fake.calls))

    def test_an_unset_or_empty_base_means_production(self):
        self.assertEqual(m.read_config({'NPREQ_API_KEY': KEY}), (m.DEFAULT_BASE, KEY))
        self.assertEqual(m.read_config({'NPREQ_API_BASE': ' '}), (m.DEFAULT_BASE, None))
        with self.assertRaises(m.ConfigError):
            m.read_config({'NPREQ_API_BASE': 'http://api.npreq.com'})

    def test_the_marketplace_lists_this_folder(self):
        self.assertEqual(self.market['name'], 'npreq')
        self.assertTrue(self.market['owner']['name'])
        self.assertEqual([(p['name'], p['source']) for p in self.market['plugins']], [('npreq', './')])

    def test_the_skill_and_the_public_files_are_there(self):
        skill = (ROOT / 'skills' / 'npreq' / 'SKILL.md').read_text(encoding='utf-8')
        self.assertTrue(skill.startswith('---\nname: npreq\ndescription: '))
        for tool in m.TOOLS:
            self.assertIn(f'`{tool}`', skill)
        readme = (ROOT / 'README.md').read_text(encoding='utf-8')
        self.assertGreaterEqual(len(readme.split()), 40)
        for must in ('POST /v1/token', 'not for clinical or licensing decisions', '## Privacy', 'Claude Code only',
                     'v2.1.271', '**On Windows**', 'python3 --version',
                     *(r[0].split('{')[0] for r in m.TOOLS.values())):
            self.assertIn(must, readme)
        self.assertNotIn('$NPREQ_API_KEY', readme)          # no setup that reads the key from the shell
        self.assertTrue((ROOT / 'LICENSE').read_text(encoding='utf-8').startswith('MIT License'))
        self.assertTrue((ROOT / 'SECURITY.md').is_file())

    def test_cli_takes_no_argument_and_reads_argv(self):
        """The console script calls cli() with no argument (main(argv) would need one)."""
        out = io.StringIO()
        with mock.patch.object(sys, 'argv', ['npreq-mcp', '--version']), mock.patch.object(sys, 'stdout', out):
            with self.assertRaises(SystemExit) as cm:
                m.cli()
        self.assertEqual((cm.exception.code, out.getvalue().strip()), (0, f'npreq-mcp {m.VERSION}'))
        pyproject = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
        self.assertIn('npreq-mcp = "npreq_mcp:cli"', pyproject)
        self.assertIn(f'version = "{m.VERSION}"', pyproject)


sys.path.insert(0, str(HERE.parent / 'tools'))
import check_release  # noqa: E402


class Release(unittest.TestCase):
    """The PyPI package, the MCP Registry entry and the release workflow. Nothing is published here."""

    @classmethod
    def setUpClass(cls):
        cls.server = json.loads((ROOT / 'server.json').read_text(encoding='utf-8'))
        cls.plugin = json.loads((ROOT / '.claude-plugin' / 'plugin.json').read_text(encoding='utf-8'))

    def test_every_release_file_carries_one_version(self):
        self.assertEqual(check_release.problems(f'v{m.VERSION}'), [])
        self.assertEqual(check_release.problems('v9.9.9'), [f"tag 'v9.9.9' is not v{m.VERSION}"])

    def test_the_check_catches_a_drifted_file_and_a_missing_mcp_name(self):
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for rel in ('pyproject.toml', 'npreq_mcp.py', 'server.json', 'README.md', '.claude-plugin/plugin.json'):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(ROOT / rel, root / rel)
            server = json.loads((root / 'server.json').read_text(encoding='utf-8'))
            server['packages'][0]['version'] = '0.0.9'
            (root / 'server.json').write_text(json.dumps(server), encoding='utf-8')
            readme = (root / 'README.md').read_text(encoding='utf-8').replace('<!-- mcp-name:', '<!-- name:')
            (root / 'README.md').write_text(readme, encoding='utf-8')
            found = check_release.problems(None, root)
        self.assertEqual(len(found), 2, found)
        self.assertIn("server.json pypi package version is '0.0.9'", found[0])
        self.assertIn('mcp-name: io.github.Miles-Medical-Service/npreq', found[1])

    def test_the_registry_entry_fits_the_schema_and_matches_the_plugin(self):
        s = self.server
        self.assertEqual(s['$schema'], 'https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json')
        self.assertRegex(s['name'], r'^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+$')
        self.assertTrue(1 <= len(s['description']) <= 100 and 1 <= len(s['title']) <= 100)
        self.assertEqual(s['repository'], {'url': self.plugin['repository'], 'source': 'github'})
        (pkg,) = s['packages']
        self.assertEqual((pkg['registryType'], pkg['registryBaseUrl'], pkg['identifier'], pkg['runtimeHint']),
                         ('pypi', 'https://pypi.org', 'npreq-mcp', 'uvx'))
        self.assertEqual(pkg['transport'], {'type': 'stdio'})
        env = {e['name']: e for e in pkg['environmentVariables']}
        self.assertEqual(sorted(env), ['NPREQ_API_BASE', 'NPREQ_API_KEY'])
        self.assertTrue(env['NPREQ_API_KEY']['isRequired'] and env['NPREQ_API_KEY']['isSecret'])
        self.assertNotIn('default', env['NPREQ_API_KEY'])
        base = env['NPREQ_API_BASE']
        self.assertEqual(base['choices'], self.plugin['userConfig']['api_base']['options'])
        self.assertEqual(base['default'], m.DEFAULT_BASE)
        self.assertFalse(base['isSecret'] or base['isRequired'])

    def test_workflow_actions_are_pinned_to_commit_shas(self):
        import re
        for wf in sorted((ROOT / '.github' / 'workflows').glob('*.yml')):
            uses = re.findall(r'^\s*-?\s*uses:\s*(\S+)', wf.read_text(encoding='utf-8'), re.M)
            self.assertTrue(uses, wf.name)
            for u in uses:
                self.assertRegex(u, r'^[\w.-]+/[\w.-]+@[0-9a-f]{40}$', f'{wf.name}: {u}')

    def test_the_registry_namespace_is_github_proven(self):
        """io.github.Miles-Medical-Service, proven by the release workflow's
        GitHub OIDC token, so no DNS record and no private key. The registry compares the namespace case-sensitively
        with the token's repository_owner, so it must be the owner login exactly as in the repo URL."""
        self.assertEqual(self.server['name'], 'io.github.Miles-Medical-Service/npreq')
        owner = self.plugin['repository'].split('github.com/', 1)[1].split('/', 1)[0]
        self.assertEqual(self.server['name'].split('/', 1)[0], 'io.github.' + owner)
        self.assertEqual(self.server['repository']['url'].split('github.com/', 1)[1].split('/', 1)[0], owner)

    def test_only_the_approved_publish_jobs_can_mint_an_oidc_token(self):
        text = (ROOT / '.github' / 'workflows' / 'release.yml').read_text(encoding='utf-8').replace('\r\n', '\n')
        top, jobs = text.split('\njobs:\n', 1)
        self.assertIn('permissions:\n  contents: read', top)
        self.assertIn("tags: ['v*']", top)
        build, rest = jobs.split('\n  publish-pypi:\n', 1)
        publish, registry = rest.split('\n  publish-registry:\n', 1)
        self.assertNotIn('id-token', build)
        for job, needs in ((publish, 'needs: build'), (registry, 'needs: publish-pypi')):
            self.assertIn('    environment:\n      name: pypi', job)
            self.assertIn('id-token: write', job)
            self.assertIn(needs, job)
        self.assertIn('sha256sum -c -', registry)                           # mcp-publisher checked before it runs
        self.assertRegex(registry, r'MCP_PUBLISHER_SHA256: [0-9a-f]{64}\n')
        self.assertIn('releases/download/v1.8.7/', registry)                # a pinned release, never latest
        self.assertIn('login github-oidc', registry)
        self.assertIn('python3 tools/check_release.py "$TAG"', build)       # the tag reaches the shell only via env
        self.assertNotIn('${{ github.ref_name }}"', build)
        self.assertNotIn('password', text)                                  # Trusted Publishing: no stored token


class Process(unittest.TestCase):
    """The real script as a child, the way an MCP client starts it: the key never shows."""

    def run_child(self, base, messages, extra_env=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith('NPREQ_')}
        env.update({'NPREQ_API_KEY': KEY, 'NPREQ_API_BASE': base, 'PYTHONIOENCODING': 'utf-8'}, **(extra_env or {}))
        stdin = ''.join(json.dumps(x) + '\n' for x in messages)
        return subprocess.run([sys.executable, '-I', str(SCRIPT)], input=stdin.encode(), capture_output=True,
                              env=env, timeout=120)

    def test_http_base_is_refused_at_start_up(self):
        p = self.run_child('http://api.npreq.com', [])
        self.assertEqual(p.returncode, 2)
        self.assertIn(b'https://', p.stderr)
        no_secret(self, p.stdout.decode() + p.stderr.decode())

    def test_arguments_are_refused_so_the_key_cannot_ride_argv(self):
        p = subprocess.run([sys.executable, '-I', str(SCRIPT), '--key', KEY], capture_output=True, timeout=60)
        self.assertEqual(p.returncode, 2)
        no_secret(self, p.stdout.decode() + p.stderr.decode())

    def test_a_full_session_never_shows_the_key(self):
        # 127.0.0.1:9 (discard) refuses at once, so every data call fails without touching any real host.
        msgs = [{'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
                {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}]
        msgs += [{'jsonrpc': '2.0', 'id': 2 + i, 'method': 'tools/call', 'params': {'name': n, 'arguments': a}}
                 for i, (n, a) in enumerate([('get_data_version', {}), ('list_states', {}), ('get_state', {'code': 'TX'}),
                                             ('get_state_ce', {'code': 'TX'}), ('search_requirements', {'q': 'ce'})])]
        p = self.run_child('https://127.0.0.1:9', msgs)
        self.assertEqual(p.returncode, 0, p.stderr)
        out, err = p.stdout.decode(), p.stderr.decode()
        replies = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([r['id'] for r in replies], [0, 1, 2, 3, 4, 5, 6])
        for r in replies[2:]:
            self.assertTrue(r['result']['isError'])
            self.assertEqual(r['result']['structuredContent']['error']['message'], m.UNREACHABLE)
        self.assertIn('tools/call list_states: 0', err)
        no_secret(self, out + err)


if __name__ == '__main__':
    unittest.main(verbosity=2)
