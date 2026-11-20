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
| `auto-500` | `auto` with a 500-token pool per phase |
| `auto-errlow` | `auto`, but failures get low instead of high |
| `auto-lite` | no planning step; failures get low |
| `off` | always none |

Any other value (`low`, `high`, ...) passes through untouched, and so does every request without `reasoning_effort`.

## Quick start

Requires Python 3.9+ and nothing else (standard library only).

```bash
python proxy.py --upstream http://127.0.0.1:8080 --listen 127.0.0.1:8090 --log auto-thinking.log
```

Then point your agent at `http://127.0.0.1:8090/v1` and pick `auto` or `auto-flash` as the reasoning effort. For OpenCode, see [`examples/opencode.jsonc`](examples/opencode.jsonc).

On every chat request the proxy rewrites:

- `reasoning_effort` to `none`, `low` or `high`;
- `chat_template_kwargs.enable_thinking` to `true` or `false` (the per-request switch llama.cpp's server and vLLM understand for Qwen-style templates; turn it off with `--no-template-kwargs`);
- for pooled variants, `thinking_budget_tokens` to what's left in the pool. This only has an effect on servers that support a per-request thinking cap; rename the field with `--budget-field`, or pass `""` to leave it out. Either way, the pool still decides *whether* later steps in the phase think.

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

**One real working day (`auto-flash`, building a feature in a ~55K-line Kotlin Android app):**

- 946 model steps. About **88% ran with no thinking.**
- Thinking switched on 109 times: 19 for new messages, 90 right after failures.
- The 1,500-token pool ran out only **3 times**.
- The feature mostly worked after one fix round. The bugs that remained were design mistakes (wrong measurement scaling, a missing tool button), the kind more thinking doesn't obviously prevent. The model also claimed one feature worked when it didn't.

**Small benchmarks with hidden tests (OpenCode, same model):**

| Benchmark | `low` (always think a little) | `auto` | `auto-lite` | `off` |
|---|---|---|---|---|
| Spreadsheet app from scratch, 27 hidden tests | 27 / 26 / 26 passed, 25–27 min | 26 / 27 passed, 22–23 min | 26 passed, 22 min | not run |
| 8 feature tasks on a real app | 7/8, 21.8 min | 7/8, 14.0 min* | 7/8, 21.7 min | 7/8, 31.9 min, 15 repeats |

\* one run hung and is excluded.

**What this does *not* show yet.** These are a handful of runs on one model, with no baseline where thinking is *always high*. The results are consistent with "large cut in thinking, little or no loss in quality, often faster", but they don't prove it. A proper test would run the same 10–20 tasks with always-on thinking, `auto-flash` and no thinking, each several times, scored by hidden tests. If you run something like that, please open an issue with your numbers.

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
