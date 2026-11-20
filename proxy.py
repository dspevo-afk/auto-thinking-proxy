#!/usr/bin/env python3
"""auto-thinking-proxy: an OpenAI-compatible proxy that decides per step whether a local model should think.

    python proxy.py --upstream http://127.0.0.1:8080 --listen 127.0.0.1:8090

Point your agent (OpenCode, Aider, anything OpenAI-compatible) at http://127.0.0.1:8090/v1 and send
reasoning_effort "auto" or "auto-flash". Every other request passes through untouched.
Python 3.9+, standard library only.
"""
from __future__ import annotations

import argparse
import http.client
import json
import re
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import autothink

REASONING_EVENT = re.compile(rb'"reasoning(?:_content)?"\s*:\s*"(?!")')   # about one streamed event per token
HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailers',
               'transfer-encoding', 'upgrade', 'host', 'content-length', 'accept-encoding'}


class Config:
    upstream: urllib.parse.SplitResult
    log_path: str | None = None
    template_kwargs = True
    budget_field: str | None = 'thinking_budget_tokens'
    timeout = 3600.0


CFG = Config()
POOLS = autothink.Pools()


def log(line: str) -> None:
    stamp = time.strftime('%Y-%m-%d %H:%M:%S ')
    if CFG.log_path:
        try:
            with open(CFG.log_path, 'a', encoding='utf-8') as f:
                f.write(stamp + line + '\n')
        except OSError:
            pass
    else:
        print(stamp + line, file=sys.stderr, flush=True)


def apply_policy(data: dict):
    """Rewrites a chat request in place. Returns the pool anchor to charge (or None)."""
    rsn = data.get('reasoning')
    variant = str(data.get('reasoning_effort') or (rsn.get('effort') if isinstance(rsn, dict) else '') or '').lower()
    if variant == 'off':
        variant, level, budget, anchor, why = 'off', 'none', None, None, 'effort off'
    elif variant in autothink.VARIANTS:
        level, budget, anchor, why = autothink.decide(data.get('messages'), variant, POOLS)
    else:
        return None
    data['reasoning_effort'] = level
    data.pop('reasoning', None)
    if CFG.template_kwargs:                       # llama.cpp / vLLM style per-request thinking switch
        kw = data.get('chat_template_kwargs')
        kw = kw if isinstance(kw, dict) else {}
        kw['enable_thinking'] = level != 'none'
        data['chat_template_kwargs'] = kw
    if budget is not None and CFG.budget_field:
        data[CFG.budget_field] = budget
    log('%s -> %-4s  msgs=%d  %s' % (variant, level, len(data.get('messages') or []), why))
    return anchor


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):            # keep stderr for decisions only
        pass

    def do_GET(self):
        self._forward(None)

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        self._forward(self.rfile.read(length) if length else b'')

    def _forward(self, body):
        anchor = None
        if body and self.command == 'POST' and self.path.rstrip('/').endswith('/chat/completions'):
            try:
                data = json.loads(body)
            except ValueError:
                data = None
            if isinstance(data, dict):
                anchor = apply_policy(data)
                body = json.dumps(data).encode('utf-8')
        up = CFG.upstream
        conn_cls = http.client.HTTPSConnection if up.scheme == 'https' else http.client.HTTPConnection
        conn = conn_cls(up.hostname, up.port or (443 if up.scheme == 'https' else 80), timeout=CFG.timeout)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
        if body is not None:
            headers['Content-Length'] = str(len(body))
        path = (up.path.rstrip('/') + self.path) if up.path and up.path != '/' else self.path
        try:
            conn.request(self.command, path, body=body, headers=headers)
            resp = conn.getresponse()
        except OSError as e:
            msg = json.dumps({'error': {'message': 'upstream unreachable: %s' % e}}).encode()
            self.send_response(502)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in HOP_HEADERS:
                self.send_header(k, v)
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()
        streaming = 'text/event-stream' in (resp.getheader('Content-Type') or '')
        reasoning, tail, whole = 0, b'', []
        try:
            while True:
                chunk = resp.read1(65536) if hasattr(resp, 'read1') else resp.read(65536)
                if not chunk:
                    break
                if anchor:
                    if streaming:                   # count complete lines only (an event may span two reads)
                        buf = tail + chunk
                        cut = buf.rfind(b'\n') + 1
                        reasoning += len(REASONING_EVENT.findall(buf[:cut]))
                        tail = buf[cut:]
                    else:
                        whole.append(chunk)
                self.wfile.write(b'%x\r\n%s\r\n' % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b'0\r\n\r\n')
            self.wfile.flush()
        except OSError:
            pass                                    # client went away; still charge what was generated
        finally:
            conn.close()
            if anchor:
                if streaming:
                    reasoning += len(REASONING_EVENT.findall(tail))
                else:
                    reasoning = non_stream_reasoning(b''.join(whole))
                POOLS.add(anchor, reasoning)


def non_stream_reasoning(raw: bytes) -> int:
    """Reasoning tokens of a non-streamed reply: usage if the server reports it, else ~4 characters per token."""
    try:
        d = json.loads(raw)
    except ValueError:
        return 0
    details = (d.get('usage') or {}).get('completion_tokens_details') or {}
    if isinstance(details.get('reasoning_tokens'), int):
        return details['reasoning_tokens']
    text = ''.join(str(((c.get('message') or {}).get('reasoning_content')) or '') for c in d.get('choices') or [])
    return len(text) // 4


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--upstream', required=True, help='model server base URL, e.g. http://127.0.0.1:8080')
    ap.add_argument('--listen', default='127.0.0.1:8090', help='host:port to listen on (default 127.0.0.1:8090)')
    ap.add_argument('--log', help='append decisions to this file (default: stderr)')
    ap.add_argument('--no-template-kwargs', action='store_true',
                    help='do not set chat_template_kwargs.enable_thinking (only reasoning_effort is rewritten)')
    ap.add_argument('--budget-field', default='thinking_budget_tokens',
                    help='request field for the per-step thinking cap of pooled variants ("" to omit)')
    args = ap.parse_args(argv)
    CFG.upstream = urllib.parse.urlsplit(args.upstream)
    CFG.log_path = args.log
    CFG.template_kwargs = not args.no_template_kwargs
    CFG.budget_field = args.budget_field or None
    host, _, port = args.listen.rpartition(':')
    server = ThreadingHTTPServer((host or '127.0.0.1', int(port)), Handler)
    log('listening on %s -> %s' % (args.listen, args.upstream))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
