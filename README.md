# auto-thinking-proxy

**Event-driven thinking for local coding agents.** A small OpenAI-compatible proxy that decides, step by step, whether a local reasoning model should think, based on what just happened in the agent's work, not on a fixed setting.

Most steps a coding agent takes are routine: read a file, run `grep`, make the edit it already planned. Thinking on every one of those steps burns time, and on local hardware time is the scarce resource. This proxy turns thinking on only at the moments where it's likely to matter:

| What just happened | Thinking |
|---|---|
| A new user message (new task or instruction) | **low**: plan a little |
| A tool result that shows a failure (failed test, build error, traceback, non-zero exit) | **high**: work out what went wrong |
| Anything else (routine reads, edits, commands) | **none**: just act |

A raised level lasts through up to 6 "look-around" steps (reads, searches, `ls`/`cat`/`grep`, `git status` ...), so the model can keep thinking while it gathers context. It drops to *none* as soon as the agent does something real. A failed look-around (for example a `grep` that finds nothing) is not treated as an error.

### Pooled budgets (`auto-flash`)

With `auto-flash`, all thinking in one *phase* shares a single **1,500-token pool**. A phase starts at a user message or a failed tool call and runs through the look-around steps after it. When the pool runs out, the rest of the phase runs without thinking, and every new failure opens a fresh pool. This keeps a model from spending thousands of tokens second-guessing itself at the start of a task.

## Variants

Your agent selects the policy through the `reasoning_effort` field:

| `reasoning_effort` | Behaviour |
|---|---|
| `auto` | new message = low, failure = high, else none |
| `auto-flash` | `auto` with a 1,500-token pool per phase |

Any other value (`none`, `low`, `high`, ...) passes through untouched, and so does every request without `reasoning_effort`.

## Quick start

Requires Python 3.9+ and nothing else (standard library only).

```bash
python proxy.py --upstream http://127.0.0.1:8080 --listen 127.0.0.1:8090 --log auto-thinking.log
```

Then point your agent at `http://127.0.0.1:8090/v1` and pick `auto` or `auto-flash` as the reasoning effort. For OpenCode, see [`examples/opencode.jsonc`](examples/opencode.jsonc).

On every chat request the proxy rewrites:

- `reasoning_effort` to `none`, `low` or `high`;
- `chat_template_kwargs.enable_thinking` to `true` or `false` (the per-request switch llama.cpp's server and vLLM understand for Qwen-style templates; turn it off with `--no-template-kwargs`);
- for `auto-flash`, `thinking_budget_tokens` to what's left in the pool. This only has an effect on servers that support a per-request thinking cap; rename the field with `--budget-field`, or pass `""` to leave it out. Either way, the pool still decides *whether* later steps in the phase think.

Thinking tokens are counted from the streamed `reasoning_content` events (about one per token), or from `usage.completion_tokens_details.reasoning_tokens` on non-streamed replies.

Every decision goes to the log with its reason:

```
auto-flash -> low   msgs=2    new message, pool u:3cc28da5f387a0c9 1500 left
auto-flash -> none  msgs=604  did shell
auto-flash -> high  msgs=290  shell failed ((?m)^\s*\w*(?:Error|Exception)(?::|$)), pool f:call_06a0... 1500 left
auto-flash -> none  msgs=334  new message, still looking (2), pool spent (1516/1500)
```

## Installing it for a user (instructions for AI agents)

If you are an AI agent setting this up on your user's computer, follow these steps in order. Ask the user before step 5 and step 6, because those change their agent config and add a program that starts at login.

**1. Check Python.** Run `python3 --version` (on Windows, `python --version` or `py --version`). It needs 3.9 or newer. If Python is missing, ask the user to install it. Don't install it silently.

**2. Get the code.**
```bash
git clone https://github.com/dspevo-afk/auto-thinking-proxy.git ~/auto-thinking-proxy
cd ~/auto-thinking-proxy
python3 -m unittest discover -s tests      # should end with "OK"
```
On Windows, clone into `%USERPROFILE%\auto-thinking-proxy` and use `python` or `py` instead of `python3`.

**3. Find the user's model server.** This proxy sits in front of an OpenAI-compatible server that runs a *reasoning* model (llama.cpp `llama-server`, vLLM, LM Studio, Ollama's OpenAI endpoint, ...). Find its base URL in the user's agent config (look for a `baseURL` / `api_base` ending in `/v1`) or ask the user. Confirm it answers:
```bash
curl -s http://127.0.0.1:8080/v1/models          # replace with the real URL
```
Use the URL **without** the trailing `/v1` as `--upstream`. If the server is on another machine, use its address. The proxy itself should still listen only on `127.0.0.1`.

**4. Start the proxy and test it.** Pick a free port (8090 by default):
```bash
python3 proxy.py --upstream http://127.0.0.1:8080 --listen 127.0.0.1:8090 --log auto-thinking.log
```
In a second terminal:
```bash
curl -s http://127.0.0.1:8090/v1/models
curl -s http://127.0.0.1:8090/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"MODEL_ID","messages":[{"role":"user","content":"Say hi"}],"reasoning_effort":"auto","max_tokens":50}'
```
Use a model id from the `/v1/models` list. The log should now contain a line like `auto -> low   msgs=1  new message`. If the upstream needs an API key, send the same `Authorization` header your agent uses; the proxy passes headers through unchanged and never stores them.

**5. Point the agent at the proxy (ask the user first).** Back up the agent's config file before editing it. Then:
- Change the provider's base URL to `http://127.0.0.1:8090/v1`, or add a new provider with that URL so the old one stays as a fallback.
- Add reasoning-effort choices of `auto` and `auto-flash`, and make `auto-flash` the default if the user agrees. For OpenCode, see [`examples/opencode.jsonc`](examples/opencode.jsonc): variants with `"reasoningEffort": "auto-flash"`.
- For other OpenAI-compatible agents, set the request's `reasoning_effort` to `auto-flash` wherever the agent lets you pick an effort level.

Restart the agent, give it a small task, and check that the log shows a mix of `none`, `low` and (after any failed test or command) `high`.

**6. Start it at login (ask the user first).** Replace the paths and the upstream URL with the real ones.

*Linux (systemd user service)*, saved as `~/.config/systemd/user/auto-thinking-proxy.service`:
```ini
[Unit]
Description=auto-thinking-proxy

[Service]
ExecStart=/usr/bin/python3 %h/auto-thinking-proxy/proxy.py --upstream http://127.0.0.1:8080 --listen 127.0.0.1:8090 --log %h/auto-thinking-proxy/auto-thinking.log
Restart=on-failure

[Install]
WantedBy=default.target
```
Then run `systemctl --user daemon-reload && systemctl --user enable --now auto-thinking-proxy`.

*macOS (launchd)*, saved as `~/Library/LaunchAgents/com.auto-thinking-proxy.plist`, then `launchctl load ~/Library/LaunchAgents/com.auto-thinking-proxy.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.auto-thinking-proxy</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string><string>/Users/USER/auto-thinking-proxy/proxy.py</string>
    <string>--upstream</string><string>http://127.0.0.1:8080</string>
    <string>--listen</string><string>127.0.0.1:8090</string>
    <string>--log</string><string>/Users/USER/auto-thinking-proxy/auto-thinking.log</string>
  </array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
</dict></plist>
```

*Windows (Task Scheduler, no console window)*, in PowerShell:
```powershell
$py = (Get-Command pythonw).Source
$dir = "$env:USERPROFILE\auto-thinking-proxy"
$action = New-ScheduledTaskAction -Execute $py -WorkingDirectory $dir -Argument "`"$dir\proxy.py`" --upstream http://127.0.0.1:8080 --listen 127.0.0.1:8090 --log `"$dir\auto-thinking.log`""
Register-ScheduledTask -TaskName "auto-thinking-proxy" -Action $action -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME)
Start-ScheduledTask -TaskName "auto-thinking-proxy"
```

**7. Tell the user** what you changed (config file and backup location, port, startup entry), where the log is, and how to undo it.

**Undo:** restore the agent config backup (or switch back to the old provider), then remove the startup entry:
- Linux: `systemctl --user disable --now auto-thinking-proxy`
- macOS: `launchctl unload ~/Library/LaunchAgents/com.auto-thinking-proxy.plist`
- Windows: `Unregister-ScheduledTask -TaskName auto-thinking-proxy -Confirm:$false`

Then delete the folder.

**Troubleshooting**
- *502 "upstream unreachable"*: the `--upstream` URL is wrong or the model server isn't running.
- *The model always thinks, or never does*: the server may ignore `chat_template_kwargs`. Check the server's docs for its per-request thinking switch (some only honour `reasoning_effort`; run with `--no-template-kwargs` to send only that).
- *Pools never run out*: the server doesn't stream `reasoning_content` (or `reasoning`) fields, so the proxy can't count thinking tokens. The level decisions still work.

## What we've measured (and what we haven't)

This came out of running OpenCode against a local mixture-of-experts model (Swift 1.5 Flash Next, IQ3_XXS) on four 16 GB Tesla P100s. The proxy here is a clean stand-alone extract of the logic in that setup's bridge.

Below is every valid benchmark run from the two days the policy was developed (2026-11-19 and 2026-11-20): 127 runs, each scored by hidden tests the agent never saw.

- **Thinking** is the amount of reasoning text the model streamed, in characters. Token figures are estimates at about 4 characters per token.
- **Wall clock** is the whole run, including the agent's tool and test commands.
- **Baselines:** `low` thinks (at low effort) on every step, which was the everyday setting before this. `off` never thinks. There is no "always high" baseline.

### Summary

| | Thinking saved | Wall clock saved | Hidden-test results |
|---|---|---|---|
| `auto` vs always-`low`, 8 feature tasks | **46%** (≈6,700 tokens) | **24%** (4.4 min), without the frozen-dialog task* | same: 7/8 vs 7/8 |
| `auto` vs always-`low`, spreadsheet app (3 runs each) | **20%** (≈4,500 tokens per run) | **11%** (2.9 min per run) | same: 26.0 vs 26.3 of 27 on average |
| `auto-flash` vs `auto`, one app feature | **46%** (≈3,100 tokens) | **8%** (1.0 min) | both passed |

\* With that task included, `auto` was slower overall on the 8 tasks (36.5 vs 21.8 min). See the note under table A.

Never thinking (`off`) was **not** faster: it needed far more steps and was slower than `low` overall.

### Accuracy

A run *passes* when the hidden tests pass, plus the project's own checks (lint, existing tests) where the benchmark has them.

**Same tasks, different settings:**

| Benchmark | always-`low` | `auto` | `auto-flash` | `off` |
|---|---|---|---|---|
| 8 feature tasks | 7/8 | 7/8 | not run | 7/8 |
| Spreadsheet app, hidden tests over 3 runs | 79/81 (97.5%) | 78/81 (96.3%) | not run | not run |
| App feature (one task, 11-20) | not run | pass | pass | not run |
| Hard task "CO" | not run | fail (3 of 6 hidden tests failed) | not run | fail (1 of 6 failed) |

On the 8 feature tasks the outcome was identical **task by task**: all four settings passed the same 7 tasks and failed the same one (F2) on the same hidden test. That points to F2 being beyond this model, not to the thinking setting.

**All `auto` runs:** 39 of 45 task runs passed (87%), plus 78/81 hidden tests on the spreadsheet runs. The failures:
- F2 (failed by every setting);
- two small tasks: one also failed by `auto-smart`, the other passed by `auto-smart`;
- the two hard round-C tasks;
- the first attempt at the app feature (1 of 34 hidden tests failed). It passed on the second attempt.

**`auto-flash`:**
- Passed the app feature, the one task it shares with `auto`.
- Failed the larger "drops" feature: 14 of 15 hidden tests passed, plus one missing import, after our own watchdog stopped the agent at 300 tool calls.
- On the real working day, the feature mostly worked after one fix round. The remaining bugs were design mistakes: measurements inside a zoomed view came out wrong, a tool button was missing, and the model claimed one thing worked when it didn't.

**Failures by cause, across all 127 runs:**

| Cause | Runs |
|---|---|
| A task every setting failed the same way (F2) | 4 |
| Hidden tests passed, but the project's own check failed (task "XB", every setting that ran it) | 4 |
| One or more hidden tests failed (small tasks, app feature, CO, drops) | 11 |
| Too little thinking: the 500-token pool gave up after 3 steps on the app feature | 1 |

That last row is why `auto-500` was dropped. 1,500 tokens (`auto-flash`) was the smallest pool that didn't make the model give up.

**How much these numbers can tell you:** with 8 or 20 tasks per comparison, a difference of one task is noise. These benchmarks would catch a *large* accuracy drop, and none showed one: the biggest gap against always-`low` is 1 hidden test out of 81. They can't rule out a small drop. The same goes for `auto-flash`, which has only one head-to-head task with `auto`.

### A. 8 feature tasks on a real Python desktop app

Each task has its own hidden tests.

| Setting | Passed | Wall clock, all 8 | Without task F1* | Thinking | Steps |
|---|---|---|---|---|---|
| `low` (always think) | 7/8 | 21.8 min | 18.3 min | 57,675 chars (≈14,400 tok) | 106 |
| **`auto`** | 7/8 | 36.5 min | **13.9 min** | **30,863 chars (≈7,700 tok)** | 104 |
| `off` (never think) | 7/8 | 31.9 min | 17.9 min | 0 | 190 |
| `auto-lite` (dropped) | 7/8 | 21.7 min | 18.7 min | 4,588 chars | 154 |

\* On task F1 the `auto` run spent about 12 of its 22.5 minutes waiting on two test commands that froze on a pop-up dialog and ran into their 5- and 7-minute timeouts. That's a test-environment problem, not thinking. The column without F1 removes that task from every setting.

Per task, `auto` vs `low` (wall-clock seconds and thinking characters):

| Task | B1 | T2 | F2 | N1 | B2 | F1 | F3 | N2 |
|---|---|---|---|---|---|---|---|---|
| `auto` | pass, 79 s, 1,973 | pass, 126 s, 2,708 | fail, 144 s, 1,272 | pass, 150 s, 3,256 | pass, 70 s, 3,200 | pass, 1,352 s, 7,943 | pass, 141 s, 5,038 | pass, 128 s, 5,473 |
| `low` | pass, 92 s, 2,279 | pass, 222 s, 9,290 | fail, 182 s, 4,842 | pass, 152 s, 6,767 | pass, 81 s, 3,185 | pass, 210 s, 10,196 | pass, 216 s, 12,632 | pass, 156 s, 8,484 |

### B. Spreadsheet app built from scratch (27 hidden tests)

| Run | Setting | Hidden tests | Wall clock | Thinking (chars) |
|---|---|---|---|---|
| r5 | `low` | 27/27 | 26.6 min | 88,523 |
| r6 | `low` | 26/27 | 25.0 min | 78,540 |
| r7 | `low` | 26/27 | 26.3 min | 104,868 |
| r1 | `auto` | 25/27 | 24.1 min | 33,587 |
| r5 | `auto` | 26/27 | 23.0 min | 72,657 |
| r6 | `auto` | 27/27 | 22.3 min | 111,966 |
| r7 | `auto-lite` (dropped) | 26/27 | 22.4 min | 31,406 |
| r1 | `low` | 22/27 | 10.9 min | 91,558 |
| r2 | `low` | 6/27 | 10.2 min | 42,919 |

The two `low` runs at the bottom hit a since-fixed bridge bug that made the model quit after 2 and 10 steps, so they're left out of the averages:
- **`low` (r5–r7):** 26.3/27, 26.0 min, 90,644 chars (≈22,700 tokens).
- **`auto` (3 runs):** 26.0/27, 23.1 min, 72,737 chars (≈18,200 tokens).

Thinking varied a lot from run to run: one `auto` run (r6) thought more than any `low` run.

### C. `auto-flash` (1,500-token pool)

| Task | Setting | Result | Wall clock | Thinking (chars) | Steps |
|---|---|---|---|---|---|
| Feature in a real app | `auto` | pass | 12.2 min | 27,364 | 24 |
| same task | **`auto-flash`** | pass | 11.2 min | 14,855 | 53 |
| Larger feature ("drops") | `auto-flash` | fail: 14/15 hidden tests plus one missing import. Our own step-limit watchdog stopped the agent at 300 tool calls, one edit from done | 47.8 min | 25,520 | 354 |

There was no comparison run for the larger feature.

**A real working day with `auto-flash`** (building a feature in a ~55K-line Kotlin Android app, 2026-11-20): 1,055 model steps, **88% with no thinking**. Thinking switched on about 127 times, for new instructions and right after failures, and the 1,500-token pool ran out only 3 times. The feature mostly worked after one fix round. The bugs that remained were design mistakes (wrong measurement scaling, a missing tool button), and the model claimed one feature worked when it didn't. There was no baseline that day, so this shows how little thinking was used, not that quality held.

### D. Other variants we tried and dropped

Same tasks, head to head:

| Comparison | Tasks | Passed | Wall clock | Thinking (chars) |
|---|---|---|---|---|
| `auto` vs `auto-smart` (a separate judge decided when to think) | 20 small tasks | 18 vs 17 | 28.0 vs 34.2 min | 69,973 vs 13,818 |
| `auto` vs `auto-errlow` (failures got low instead of high) | 10 tasks | 10 vs 10 | 23.7 vs 24.3 min | 67,599 vs 74,886 |
| `auto` vs `auto-500` (500-token pool) | 3 tasks | 3 vs 2 | 4.8 vs 3.8 min | 6,227 vs 5,913 |
| `auto` vs `auto-errlow`, app feature (first attempt) | 1 | fail vs fail | 13.1 vs 7.3 min | 51,974 vs 16,733 |
| `auto-500`, app feature | 1 | fail (gave up after 3 steps) | 0.3 min | 1,615 |
| 2 hard tasks: `off` / `auto` / `auto-smart` / two `auto-smart` tweaks | 2 (`off` ran 1) | 0/1, 0/2, 1/2, 1/2, 1/2 | 15.1, 18.5, 21.7, 22.1, 27.3 min | 0, 13,258, 11,803, 30,596, 30,784 |
| `auto-smart`, one more hard task | 1 | pass | 14.1 min | 11,152 |
| `auto-500` with a per-step cap (early try) | 2 | 2 | 3.3 min | 5,677 |

Not counted: 24 runs from benchmark-harness bugs (kept separately and marked invalid), and one run that never started.

### What this does *not* show yet

These are a handful of runs on one model, and run-to-run variation is large. There's no baseline where thinking is *always high*. `auto-flash` has been compared with `auto` on only one task.

The results are consistent with "large cut in thinking, no loss in pass rate, usually faster", but they don't prove it. A proper test would run the same 10–20 tasks with always-on thinking, `auto`, `auto-flash` and no thinking, each several times, scored by hidden tests. If you run something like that, please open an issue with your numbers.

## Related work

The idea that a model shouldn't think on every request isn't new:

- **Models trained to decide when to think:** [AutoThink](https://arxiv.org/abs/2505.10832v3), [AdaptThink](https://huggingface.co/papers/2505.13417) and [Thinkless](https://arxiv.org/abs/2505.13379) use reinforcement learning so the model itself skips thinking on easy problems. They report 50–90% fewer thinking tokens on math benchmarks with no accuracy loss, but they require retraining.
- **Manual switches:** Qwen3's [`/think` and `/no_think`](https://docsearch.algolia.com/mcp/docs/repo/qwenlm/qwen3) soft switches, and hard caps on thinking length, for example [little-coder](https://docsearch.algolia.com/mcp/docs/repo/itayinbarr/little-coder)'s 2,048-token limit.
- **Error-recovery guidance in agent harnesses:** for example [Building Effective AI Coding Agents for the Terminal](https://arxiv.org/pdf/2603.05344).

What this project adds is a policy that sits outside the model (no retraining, works with any OpenAI-compatible server), is driven by the agent's own events (new instruction, tool failure, look-around), and gives each event a token pool. In a short search we didn't find this exact combination published, which doesn't mean nobody has built it.

## Running the tests

```bash
python -m unittest discover -s tests
```

The suite covers the decision rules (planning, failures, look-arounds, pools) and an end-to-end run through a fake model server: request rewriting, streaming pass-through, and token accounting across split stream chunks.

## Not included

The original bridge also re-asks the model when it "stalls" (ends a turn with a plan in its thinking but no tool call) and has a separate policy for chat bots. Both are tied to that setup and are left out here.

## License

MIT
