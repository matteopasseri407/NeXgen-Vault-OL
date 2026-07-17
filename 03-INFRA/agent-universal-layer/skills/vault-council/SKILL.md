---
name: vault-council
description: Convene the AI Council (other installed agentic CLIs as advisors) on a question, plan, or piece of code. Use when the user wants a second opinion, a cross-vendor review, or to stress-test a decision.
---

# Convene the council

The text after the command is the question or plan to put to the council.

1. Check the council is configured: without a `seats.yaml` in the data
   root (`03-INFRA/agent-universal-layer/council/seats.yaml`) the council
   is deliberately inert. If it is missing, explain the one-time setup
   from `docs/council.md` and stop.
2. Pick the mode that fits the request, or run `council propose` to let
   the router suggest one: `brainstorm` (generate options), `challenge`
   (find the flaws in a plan), `code-review` (cross-vendor review),
   `relay` (staged pipeline of seats).
3. Before convening, tell the user which mode and seat(s) you are about
   to use and that the run spends those CLIs' own subscription quota;
   proceed on confirmation.
4. Run it, e.g. `council challenge "<the question>" --seat <seat>`, and
   wait for the transcript.
5. Synthesize the answers: what the advisors agree on, where they
   disagree (attribute positions to seats), and your own recommendation
   as one plain-language paragraph. Do not paste raw transcripts.
