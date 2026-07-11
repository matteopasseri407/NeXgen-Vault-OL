# AI Council

`council.py` is a local orchestrator that convenes other agentic CLIs you
already have (paid via their own flat subscription, never a pay-per-use API
key opened just for this) as advisors: brainstorming, challenging a plan, or
cross-vendor code review. It is explicit Python code, not an LLM, that
decides who speaks and when.

It is an optional expansion. With no `seats.yaml` configured it is
completely inert. Nothing about the rest of the engine depends on it.

## Setup

1. Copy the schema template into your data root and fill in seats you
   actually have:
   ```bash
   mkdir -p ~/KnowledgeVault/03-INFRA/agent-universal-layer/council
   cp 03-INFRA/agent-universal-layer/council/seats.yaml.example \
      ~/KnowledgeVault/03-INFRA/agent-universal-layer/council/seats.yaml
   ```
   (adjust the path if `AGENT_VAULT_DATA`/`KNOWLEDGE_VAULT_PATH` points
   somewhere else). This file is your data, never committed to the engine.
2. Keep `schema_version: 1` at the root, then pick the seats you have,
   following the comments in the template. Each seat needs `vendor`, `cli`,
   `model`, and an explicit `zero_retention: true|false`. `cli` must be one
   of `opencode`, `agy`, `codex`. Those are the only CLIs `council.py` knows
   how to invoke today. Set `zero_retention: true` only if you've confirmed
   that with a primary source, not a summary. You can also set an optional
   positive `timeout_seconds` for a seat that is known to need more or less
   time.
3. In a MULTI setup, run `agent-sync apply` once to install the `council`
   launcher on Linux and the `council.cmd` wrapper on Windows.
   Then run any command below.
   Without a valid `seats.yaml`, Council stops and reports what is missing.
   In a MINIMAL setup, or if the launcher is not on your `PATH`, replace
   `council` below with `python3 03-INFRA/agent-universal-layer/council/council.py`.

## The four modes

Every seat is called "without hands": it receives text and returns text,
never filesystem or shell access, regardless of which CLI is behind it.

**Brainstorm.** One seat, 1+ rounds. Each round after the first must attack
its own previous conclusion instead of just restating it:
```bash
council brainstorm \
  "Should we cache this at the edge or the origin?" \
  --context notes.md --rounds 2 --seat gemini
```

**Challenge.** One seat tries to find the dominant flaw in a plan. `APPROVE`
is made costly (it has to rule out at least two risk categories explicitly,
or it doesn't count):
```bash
council challenge \
  "Single cron job tars the data dir nightly to one S3 bucket." --seat gemini
```

**Code review.** It refuses to run if
`--author-vendor` matches the seat's own vendor:
```bash
council code-review \
  changes.diff --author-vendor anthropic --seat gemini
```

**Relay.** A sequential staffage of up to 5 stages, for example
architect→builder→reviewer→judge), each stage seeing the full original
brief plus the previous stage's output quoted as untrusted data, never as
an instruction. Today only `opencode` seats can take part in a relay
sequence; `agy`/`codex` seats work in the other three modes but not yet
here.
```bash
council relay \
  "Design a rate limiter for the public API." \
  --sequence "architect=glm,builder=qwen,reviewer=deepseek-free"
```

Every mode accepts `--context FILE` for extra background and
`--allow-training-risk` to use a seat that lacks a confirmed zero-retention
guarantee. Use it only for non-sensitive technical checks, never for a real brief.

## Timeouts

Every seat call is bounded by a conservative default of 300 seconds. Set a
positive `timeout_seconds` in one seat to make that its normal budget, or use
`--timeout-seconds` to override the budget for one invocation. The command-line
value wins over the seat value, which wins over the default. Zero, negative,
infinite, and boolean values are rejected.

```bash
council challenge "Review this plan" --seat gemini --timeout-seconds 90
```

## Session handling

Council creates a private working directory for the brief, per-stage output,
and the final verdict.
It removes that directory after returning a result, and after an error, unless
you pass `--keep-session`.
On POSIX, kept session directories use mode `700` and their files use `600`.
Council removes expired kept sessions when it starts.
Nothing is written into the vault automatically.
If a result matters later, save a short decision or diagnosis through your
normal vault workflow, not the raw Council transcript.

```bash
council brainstorm "Review this plan" --keep-session
```

```bash
council clean                 # removes kept sessions older than 7 days
council clean --ttl-days 1    # custom retention
council clean --all           # removes every kept session now
```

## Guardrails

- **Egress**: the original brief, including context and diffs, is scanned for
  likely secrets before any seat receives it. A match stops the call.
- **Generated output**: if a seat emits a value that looks like a secret,
  Council redacts the affected line before it reaches the next relay stage or
  a kept session. The relay can continue with the remaining analysis.
- **Zero-retention**: a seat without `zero_retention: true` in your
  `seats.yaml` refuses to run unless you pass `--allow-training-risk`.
- **Quota**: `--max-rounds` (brainstorm) and `--max-seats` (relay) cap how
  much a session can spend even if you ask for more.

## Current limitations

- `codex` seats are implemented but not yet verified live end-to-end because
  no OpenAI quota was available when they were wired in. They have only a
  code review. `opencode` and `agy` seats are verified live on all three
  non-relay modes.
- Automated regression tests cover the control flow for all four modes,
  session cleanup, relay fallback, and the Linux launcher. They use fake
  seats, so they do not replace live checks of each vendor CLI.
- The Windows launcher has a portable regression test, but still needs a
  physical Windows run before this Alpha feature can be called cross-platform.
- Large prompts use stdin for Codex and Antigravity, and a protected temporary
  attachment for OpenCode. The automated regression coverage is portable, but
  the current vendor adapters still need live end-to-end verification.
- Seats via CLI are slow (minutes, not seconds): this is for brainstorming,
  challenging, and review, not a quick question mid-task.
