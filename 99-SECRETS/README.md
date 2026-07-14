# 99-SECRETS

Local secret store for this vault. **Everything in this folder is git-ignored
except three files**: this `README.md`, `.gitkeep`, and `secrets-registry.md`.
Never commit plaintext secrets.

If more than one person will use this vault or the self-hosted stack it
points at, read `docs/team.md` first: this archive has one passphrase and
no per-person scoping, so sharing it means sharing every secret inside it
equally.

## Two files, two jobs

- **`archive/master-secrets.md.gpg`** — the encrypted archive. Holds the actual
  secret *values* (passwords, API keys, tokens, SSH keys, webhook secrets,
  tunnel credentials), GPG-encrypted with a passphrase only you know. Created on
  your first secret. Git-ignored: it never leaves the machine unless you
  deliberately back it up. **If you forget that passphrase, everything in this
  archive is gone permanently — there is no recovery.** Keep it in a password
  manager, or write down the passphrase itself (never the plaintext secrets)
  somewhere durable and separate from this machine.
- **`secrets-registry.md`** — the non-sensitive index. Lists *which* secrets
  exist: name, provider, the env var they map to, scope, last-rotated date.
  **Never any values.** Tracked in git so the map stays in sync across machines.

## Workflow

The secrets workflow — when to update the archive and the registry, and the
"do both before the task is done" rule — is defined once in `AGENTS.md` →
Secrets. This file does not restate that policy; it only covers the local GPG
mechanics below.

## Handling the encrypted archive (GPG)

```bash
cd 99-SECRETS && mkdir -p archive

# Every step below puts the FULL plaintext archive on disk, however
# briefly. mktemp + chmod 600 BEFORE any content lands -- never a plain
# redirect to a predictable path like /tmp/secrets.md, which is created
# at the umask's default mode (often world-readable) for as long as the
# edit takes. Same principle already used elsewhere in this repo:
# bootstrap-vps.sh's `chmod 600 .env` and council.py's _write_private_text.
SECRETS_TMP="$(mktemp)"
chmod 600 "$SECRETS_TMP"

# first time: start from an empty file (SECRETS_TMP is already empty and
# private from mktemp/chmod above — nothing else to do)

# later: decrypt to edit
gpg -d archive/master-secrets.md.gpg > "$SECRETS_TMP"

# ...edit $SECRETS_TMP...

# re-encrypt (symmetric passphrase) and wipe the plaintext
gpg -c -o archive/master-secrets.md.gpg "$SECRETS_TMP"
shred -u "$SECRETS_TMP"
```

Use whatever key/passphrase manager you prefer; the above is the minimal
symmetric flow.
