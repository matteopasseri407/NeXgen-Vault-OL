# 99-SECRETS

Local secret store for this vault. **Everything in this folder is git-ignored
except three files**: this `README.md`, `.gitkeep`, and `secrets-registry.md`.
Never commit plaintext secrets.

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

## Workflow (mirrors AGENTS.md → Secrets)

Whenever you create, discover, rotate, or materially change a secret:

1. Update the encrypted archive.
2. Update `secrets-registry.md` (name + env var, no value).
3. Do both before considering the task done.

If the passphrase isn't available, say so and leave a pending line in the
registry. Never paste a value into a normal vault note or a final summary.

## Handling the encrypted archive (GPG)

```bash
cd 99-SECRETS && mkdir -p archive

# first time: start from an empty file
: > /tmp/secrets.md

# later: decrypt to edit
gpg -d archive/master-secrets.md.gpg > /tmp/secrets.md

# ...edit /tmp/secrets.md...

# re-encrypt (symmetric passphrase) and wipe the plaintext
gpg -c -o archive/master-secrets.md.gpg /tmp/secrets.md
shred -u /tmp/secrets.md
```

Use whatever key/passphrase manager you prefer; the above is the minimal
symmetric flow.
