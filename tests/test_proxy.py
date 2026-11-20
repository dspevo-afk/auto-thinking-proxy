"""End-to-end: agent -> proxy -> fake model server, over real sockets."""
import http.client
import json
import os
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autothink  # noqa: E402
import proxy  # noqa: E402

SEEN = []


class FakeModel(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b'{"data":[{"id":"local"}]}'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        SEEN.append(req)
        if req.get('stream'):
            events = [{'choices': [{'delta': {'reasoning_content': 't%d' % n}}]} for n in range(40)]
            events.append({'choices': [{'delta': {'content': 'done'}}]})
            payload = b''.join(b'data: ' + json.dumps(e).encode() + b'\n\n' for e in events) + b'data: [DONE]\n\n'
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            for i in range(0, len(payload), 37):        # odd-sized writes: events split across reads
                self.wfile.write(payload[i:i + 37])
                self.wfile.flush()
        else:
            body = json.dumps({'choices': [{'message': {'content': 'ok'}}],
                               'usage': {'completion_tokens_details': {'reasoning_tokens': 123}}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def serve(handler):
    srv = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = serve(FakeModel)
        proxy.CFG.upstream = urllib.parse.urlsplit('http://127.0.0.1:%d' % cls.model.server_address[1])
        proxy.CFG.log_path = os.devnull
        cls.proxy = serve(proxy.Handler)

    @classmethod
    def tearDownClass(cls):
        for srv in (cls.proxy, cls.model):
            srv.shutdown()
            srv.server_close()

    def setUp(self):
        SEEN.clear()
        proxy.POOLS = autothink.Pools()

    def post(self, data):
        c = http.client.HTTPConnection('127.0.0.1', self.proxy.server_address[1], timeout=10)
        c.request('POST', '/v1/chat/completions', body=json.dumps(data), headers={'Content-Type': 'application/json'})
        r = c.getresponse()
        out = r.status, r.read()
        c.close()
        return out

    def test_passthrough_get(self):
        c = http.client.HTTPConnection('127.0.0.1', self.proxy.server_address[1], timeout=10)
        c.request('GET', '/v1/models')
        self.assertIn(b'local', c.getresponse().read())
        c.close()

    def test_non_auto_request_untouched(self):
        self.post({'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}], 'reasoning_effort': 'high'})
        self.assertEqual(SEEN[-1]['reasoning_effort'], 'high')
        self.assertNotIn('chat_template_kwargs', SEEN[-1])

    def test_auto_rewrites_request(self):
        msgs = [{'role': 'user', 'content': 'go'},
                {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 'a', 'type': 'function', 'function': {'name': 'edit', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': 'a', 'content': 'Edit applied.'}]
        self.post({'model': 'm', 'messages': msgs, 'reasoning_effort': 'auto'})
        self.assertEqual(SEEN[-1]['reasoning_effort'], 'none')
        self.assertIs(SEEN[-1]['chat_template_kwargs']['enable_thinking'], False)

    def test_stream_passes_through_and_charges_pool(self):
        data = {'model': 'm', 'stream': True, 'messages': [{'role': 'user', 'content': 'plan it'}],
                'reasoning_effort': 'auto-flash'}
        status, body = self.post(data)
        self.assertEqual(status, 200)
        self.assertTrue(body.endswith(b'data: [DONE]\n\n'))
        self.assertEqual(body.count(b'reasoning_content'), 40)
        self.assertEqual(SEEN[-1]['reasoning_effort'], 'low')
        self.assertEqual(SEEN[-1]['thinking_budget_tokens'], 1500)
        self.post(data)                                     # same phase again: 40 tokens already spent
        self.assertEqual(SEEN[-1]['thinking_budget_tokens'], 1460)

    def test_non_stream_uses_reported_reasoning_tokens(self):
        data = {'model': 'm', 'messages': [{'role': 'user', 'content': 'plan'}], 'reasoning_effort': 'auto-flash'}
        self.post(data)
        self.post(data)
        self.assertEqual(SEEN[-1]['thinking_budget_tokens'], 1500 - 123)


if __name__ == '__main__':
    unittest.main()
