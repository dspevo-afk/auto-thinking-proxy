"""Event-driven thinking for coding agents: pick a reasoning level for each step from what just happened.

The agent asks for reasoning_effort "auto" or "auto-flash". Instead of thinking on every
step, this walks back from the newest message and decides:

* a new user message (a new task or instruction)          -> "low"   (plan a little)
* a tool result that shows a failure (test, build, crash)  -> "high"  (work out what went wrong)
* anything else (routine reads, edits, commands)           -> "none"  (just act)

A raised level survives up to STICKY_MAX "look-around" steps (reads, searches, ls/cat/grep, git status ...), so the
model can think while it gathers context, and drops to "none" as soon as it does something real.

"auto-flash" shares ONE 1500-token thinking budget per phase: a phase starts at a user message or
at a failed tool call and runs through the look-around steps after it. When the pool is spent, the rest of the phase
runs without thinking. Every new failure opens a fresh pool.

This module is pure (no I/O). proxy.py applies it to OpenAI-compatible chat requests.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict

SHELL_TOOLS = ('shell', 'bash', 'execute', 'run', 'terminal', 'exec', 'command')
EDIT_TOOLS = ('edit', 'write', 'patch', 'apply_patch', 'multiedit')
LOOKUP_TOOLS = {'read', 'glob', 'grep', 'list', 'ls', 'webfetch', 'websearch', 'codesearch', 'todoread',
                'lsp', 'skill', 'view', 'find', 'search'}

FAIL_PATTERNS = [re.compile(p) for p in (
    r'Traceback \(most recent call last\)',
    r'(?m)^\s*(?:FAILED|ERROR)\b(?!\s*[:=]?\s*0\b)',             # pytest summary lines, ERROR collecting ...
    r'(?m)^E {2,}\S',                                           # pytest assertion detail
    r'\b[1-9]\d* (?:failed|errors?)\b(?!\W*\(?\d* ?fixed, 0 remaining)',   # "2 failed", "1 error"
    r'(?m)^\s*\w*(?:Error|Exception)(?::|$)',                   # AssertionError: ..., SyntaxError: ...
    r'(?m)^[^\s:]+:\d+(?::\d+)?:? (?:fatal )?error\b',          # file:line: error (gcc, mypy, tsc)
    r'(?m)^(?:fatal )?error(?:\[\w+\])?:',                      # rustc, generic "error:"
    r'\berror (?:TS|CS)\d+', r'(?m)^npm ERR!', r'BUILD FAILED', r'FAILURE: Build failed',
    r'(?m)^make(?:\[\d+\])?: \*\*\*',
    r'CHECK FAILED', r'(?i)\bexit(?:ed)?(?: with)? (?:code|status)[: ]+[1-9]', r'(?i)\bcommand failed\b',
    r'(?i)\bcompilation (?:failed|terminated)\b', r'(?m)^\s*[Ee]rror: ',
)]
DIAGNOSTICS = re.compile(r'(?i)LSP errors detected|<diagnostics\b|<file_diagnostics>')

LOOKUP_CMD = re.compile(
    r'^\s*(?:cd\s+\S+\s*[;&|]+\s*)?(?:ls|dir|pwd|cat|type|head|tail|tree|find|rg|grep|wc|file|stat|where|which|echo'
    r'|Get-ChildItem|gci|Get-Content|gc|Select-String|sls|Test-Path|Resolve-Path|Get-Location|Get-Item|Measure-Object'
    r'|git\s+(?:status|log|diff|show|branch|ls-files|rev-parse|blame)|(?:python3?|py)(?:\.exe)?\s+(?:--version|-V)'
    r'|pip3?\s+(?:list|show|--version)|(?:python3?|py)\s+-m\s+(?:pip\s+(?:list|show)|pytest\s+--version)'
    r'|node\s+(?:-v|--version))\b', re.I)
NOT_LOOKUP = re.compile(
    r'pytest(?!\s+--version)|unittest|python\s+-m\s+(?!json|pip\s+(?:list|show)|pytest\s+--version)|npm(?!\s+(?:ls|-v))'
    r'|node\s(?!-v|--version)|cargo|make|gradle|dotnet|\bgo\s+(?:test|build)|(?<!2)>|Remove-|rm\s')

STICKY_MAX = 6          # look-around steps a raised level survives
POOL_MIN = 50           # fewer tokens than this left = thinking off for the rest of the phase

# reasoning_effort value -> pool size in tokens (None = not pooled)
VARIANTS = {
    'auto': None,         # new message = low, failure = high, else none
    'auto-flash': 1500,   # like auto, one 1500-token pool per phase
}


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(p.get('text', '') for p in content if isinstance(p, dict))
    return ''


def tool_calls(msg: dict):
    """[(id, lowercased name, args dict)] of an assistant message's tool calls."""
    out = []
    for tc in msg.get('tool_calls') or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get('function') or {}
        try:
            args = json.loads(fn.get('arguments') or '{}')
        except ValueError:
            args = {}
        out.append((tc.get('id'), str(fn.get('name') or '').lower(), args if isinstance(args, dict) else {}))
    return out


def is_lookup(name: str, args: dict) -> bool:
    """A step that only looks around (read, search, ls, git status ...)."""
    if name in LOOKUP_TOOLS:
        return True
    if any(k in name for k in SHELL_TOOLS):
        cmd = str(args.get('command') or args.get('cmd') or '')
        return bool(cmd) and len(cmd) < 400 and bool(LOOKUP_CMD.match(cmd)) and not NOT_LOOKUP.search(cmd)
    return False


def failure(name: str, text: str):
    """Why a tool result counts as a failure (the matching pattern), or None."""
    if any(k in name for k in SHELL_TOOLS):
        return next((p.pattern for p in FAIL_PATTERNS if p.search(text)), None)
    if any(k in name for k in EDIT_TOOLS) and DIAGNOSTICS.search(text):
        return 'diagnostics'
    return None


class Pools:
    """Thinking tokens used per phase, shared by all requests (thread-safe, bounded)."""

    def __init__(self, max_phases: int = 4096):
        self._used: OrderedDict[str, int] = OrderedDict()
        self._lock = threading.Lock()
        self._max = max_phases

    def used(self, anchor: str) -> int:
        with self._lock:
            return self._used.get(anchor, 0)

    def add(self, anchor: str, tokens: int) -> None:
        with self._lock:
            self._used[anchor] = self._used.get(anchor, 0) + max(0, int(tokens))
            self._used.move_to_end(anchor)
            while len(self._used) > self._max:
                self._used.popitem(last=False)


def _user_anchor(msgs, i) -> str:
    """Stable id for the phase that starts at user message i (same conversation + same message = same pool)."""
    sysm = msgs[0] if msgs and isinstance(msgs[0], dict) and msgs[0].get('role') == 'system' else {}
    first = next((x for x in msgs if isinstance(x, dict) and x.get('role') == 'user'), {})
    key = '%s|%s|%d|%s' % (_text(sysm.get('content'))[:400], _text(first.get('content'))[:1000], i,
                           _text(msgs[i].get('content'))[:1000])
    return 'u:' + hashlib.sha1(key.encode('utf-8', 'replace')).hexdigest()[:16]


def decide(messages, variant: str, pools: Pools | None = None):
    """Returns (level, budget, anchor, why).

    level: "none" | "low" | "high"; budget: thinking tokens allowed for this step (pooled variants) or None;
    anchor: the phase id whose pool this step spends (or None); why: a short human-readable reason.
    """
    if variant not in VARIANTS:
        raise ValueError('unknown variant %r' % variant)
    msgs = messages or []
    i, looks, level, why, anchor = len(msgs) - 1, 0, 'none', 'start', None
    while i >= 0:
        m = msgs[i] if isinstance(msgs[i], dict) else {}
        role = m.get('role')
        if role == 'user':
            level, why, anchor = 'low', 'new message', _user_anchor(msgs, i)
            break
        if role != 'tool':
            level, why = 'none', 'last=%s' % role
            break
        results = {}
        while i >= 0 and isinstance(msgs[i], dict) and msgs[i].get('role') == 'tool':
            results[msgs[i].get('tool_call_id')] = _text(msgs[i].get('content')).replace('\r\n', '\n')
            i -= 1
        asst = msgs[i] if i >= 0 and isinstance(msgs[i], dict) else {}
        calls = tool_calls(asst)
        hit = None
        for cid, name, args in calls:
            if is_lookup(name, args):
                continue                # a look-around that failed (typo, nothing found) is not a code error
            hit = failure(name, results.get(cid, ''))
            if hit:
                hit, anchor = '%s failed (%s)' % (name, hit[:40]), 'f:%s' % cid
                break
        if hit:
            level, why = 'high', hit
            break
        if calls and all(is_lookup(n, a) for _c, n, a in calls):
            looks += 1
            i -= 1                      # on to the step before this look-around
            continue
        level, why = 'none', 'did %s' % ','.join(n for _c, n, _a in calls)[:40]
        break
    if level != 'none' and looks:
        if looks > STICKY_MAX:
            level, why = 'none', why + ', %d look-arounds (over %d)' % (looks, STICKY_MAX)
        else:
            why += ', still looking (%d)' % looks
    budget = None
    cap = VARIANTS[variant]
    if cap is None or level == 'none':
        return level, budget, (anchor if cap and level != 'none' else None), why
    if anchor is None:
        return level, budget, None, why
    left = cap - (pools.used(anchor) if pools else 0)
    if left < POOL_MIN:
        return 'none', None, None, why + ', pool spent (%d/%d)' % (cap - left, cap)
    return level, left, anchor, why + ', pool %s %d left' % (anchor, left)
