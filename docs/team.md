# Team use today

This is for anyone evaluating NeXgen Engine for more than one person — a
couple of colleagues, or a small company. Read this before you adopt it as
shared infrastructure: the security and identity model is mono-user by
design today, and that has concrete consequences for a team.

## What "mono-user" means, concretely

- **One profile, not one per person.** `99-INDEX/USER-PROFILE.md` holds a
  single hardware/host description and a single model-team configuration
  for the whole vault. The framework has no concept of "which teammate is
  driving right now" — there's one file, and whoever last edited it wins.
- **One secrets archive, one passphrase.** `99-SECRETS/archive/master-secrets.md.gpg`
  is a single GPG-encrypted archive "protected by a passphrase only you
  know" (see `99-SECRETS/README.md`). There's no per-person credential
  scoping: if a team shares the passphrase, they share every secret in the
  archive equally; there's no way to give one teammate read access to the
  n8n token without also handing them the OCR service key.
- **No per-file access control beyond your OS.** As `SECURITY.md` already
  states: "Any agent CLI with filesystem access to [the vault] can read and
  write everything inside." That's true for a solo user and it's just as
  true if three people share a clone — nothing in the framework stops one
  teammate's agent from editing another's notes, profile, or the shared
  secrets registry.
- **No role separation, no per-person audit trail.** There's no concept of
  an admin vs. a regular member, and no built-in log of who (as opposed to
  which machine) made a given change beyond whatever `git commit` authorship
  your own setup happens to produce.

## What this means in practice if a small team adopts it today

- **Sharing one vault clone** (the shortest path to "everyone sees the same
  notes") means everyone is in the same trust domain: one shared profile,
  one shared secrets passphrase, no per-person boundary. Fine for a couple
  of people who already trust each other with everything; not a fit for a
  team that wants to compartmentalize who can see what.
- **Giving everyone their own separate clone** avoids sharing credentials,
  but then nobody's notes, profile, or model routing are shared automatically
  — you lose the "team knowledge base" benefit and you're back to N
  independent single-user installs that happen to use the same framework.
- The `MULTI` installation profile (see `README.md` → Installation profiles)
  does **not** solve this: it keeps *one person's* CLIs and machines in
  sync with each other, not multiple people's identities in sync with one
  shared vault. It's the wrong tool for team onboarding even though the name
  suggests otherwise.

Today, most small teams end up somewhere between these two options and
accept the trade-off explicitly, rather than getting a clean answer from the
framework.

## What's not here

Real multi-user support — per-person identity, scoped secrets, a
role/permission model, per-person audit — is not implemented in this
release. It is deliberately out of scope for this document too: it is a
separate, larger piece of work already planned, not something to bolt on as
a documentation-only patch. This file exists so you can make an informed
call about adopting NeXgen Engine for a team *today*, with today's actual
constraints, rather than finding them out after the fact.

## A declarative routing aid is not multi-user support

`99-INDEX/USER-PROFILE.md` has an optional "Team members" section for
declaring who besides the vault owner uses this framework, so the Council
`seats.yaml` doesn't have to be one file everyone edits at once, and a
skill can be marked `scope: personal` so `skills-sync.py` only puts it on
its owner's machine. That is all it does: a routing/organizational aid —
who owns which host, which seat file, which skills — not a security
boundary. Every constraint above still holds exactly as written: still one
profile file whoever edits it last wins, still one secrets archive with
one shared passphrase, still no OS-independent access control, still no
per-person audit trail. Declaring team members makes seat and skill
routing more convenient; it does not add identity, isolation, or
permissions.
