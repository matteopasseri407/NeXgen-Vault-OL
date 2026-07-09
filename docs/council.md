# AI Council

`council.py` is a local orchestrator that convenes other agentic CLIs you
already have (paid via their own flat subscription, never a pay-per-use API
key opened just for this) as advisors: brainstorming, challenging a plan, or
cross-vendor code review. It is explicit Python code, not an LLM, that
decides who speaks and when.

It is an optional expansion. With no `seats.yaml` configured it is
completely inert — nothing about the rest of the engine depends on it.

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
2. Edit it: pick the seats you have, following the comments in the
   template. Each seat needs `vendor`, `cli`, and `model`; `cli` must be one
   of `opencode`, `agy`, `codex` — those are the only CLIs `council.py`
   knows how to invoke today. `zero_retention: true` only if you've
   confirmed that with a primary source (the provider's own privacy page),
   not a summary.
3. Run any command below. Without a valid `seats.yaml`, `council` stops and
   tells you exactly what's missing.

## The four modes

Every seat is called "without hands": it receives text and returns text,
never filesystem or shell access, regardless of which CLI is behind it.

**Brainstorm** — one seat, 1+ rounds, each round after the first must attack
its own previous conclusion instead of just restating it:
```bash
python3 03-INFRA/agent-universal-layer/council/council.py brainstorm \
  "Should we cache this at the edge or the origin?" \
  --context notes.md --rounds 2 --seat gemini
```

**Challenge** — one seat tries to find the dominant flaw in a plan; `APPROVE`
is made costly (it has to rule out at least two risk categories explicitly,
or it doesn't count):
```bash
python3 03-INFRA/agent-universal-layer/council/council.py challenge \
  "Single cron job tars the data dir nightly to one S3 bucket." --seat gemini
```

**Code review** — cross-vendor by construction: it refuses to run if
`--author-vendor` matches the seat's own vendor:
```bash
python3 03-INFRA/agent-universal-layer/council/council.py code-review \
  changes.diff --author-vendor anthropic --seat gemini
```

**Relay** — a sequential staffage of up to 5 stages (e.g.
architect→builder→reviewer→judge), each stage seeing the full original
brief plus the previous stage's output quoted as untrusted data, never as
an instruction. Today only `opencode` seats can take part in a relay
sequence; `agy`/`codex` seats work in the other three modes but not yet
here.
```bash
python3 03-INFRA/agent-universal-layer/council/council.py relay \
  "Design a rate limiter for the public API." \
  --sequence "architect=glm,builder=qwen,reviewer=deepseek-free"
```

Every mode accepts `--context FILE` for extra background and
`--allow-training-risk` to use a seat that lacks a confirmed zero-retention
guarantee — only for non-sensitive technical checks, never for a real brief.

## Where sessions go

Each invocation writes to
`~/.local/state/council/sessions/council-<slug>-<timestamp>/`: the brief,
one file per round/stage, and a final `verdict.md`. Nothing is written into
the vault except what you choose to copy out yourself.

```bash
council.py clean                 # removes sessions older than 7 days
council.py clean --ttl-days 1    # custom retention
council.py clean --all           # wipes every session now
```

## Guardrails

- **Egress**: every context/diff file is scanned for likely secrets before
  it reaches a seat. A match stops the call before anything is sent.
- **Zero-retention**: a seat without `zero_retention: true` in your
  `seats.yaml` refuses to run unless you pass `--allow-training-risk`.
- **Quota**: `--max-rounds` (brainstorm) and `--max-seats` (relay) cap how
  much a session can spend even if you ask for more.

## Current limitations

- `codex` seats are implemented but not yet verified live end-to-end (no
  OpenAI quota available at the time this was wired in) — code-reviewed
  only. `opencode` and `agy` seats are verified live on all three
  non-relay modes.
- Automated regression tests currently cover only the `relay` mode.
  `brainstorm`/`challenge`/`code-review` are exercised live in this repo's
  history but are not yet under CI.
- Seats via CLI are slow (minutes, not seconds): this is for brainstorming,
  challenging, and review, not a quick question mid-task.
