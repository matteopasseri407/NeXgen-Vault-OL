# n8n workflow templates

Import-ready n8n workflows for the optional self-hosted n8n stack
(`03-INFRA/deploy/n8n/`). None of these are auto-imported or auto-activated
by anything in this repo — import them by hand in the n8n UI if you want
them.

## `vault-grooming-reminder.json`

The automatic half of the vault gardener (`vault-groom.sh`/`.ps1`, see
`03-INFRA/vault-grooming-playbook.md`). The grooming pass itself is
deliberately **on-demand only, never self-scheduled**: two machines
grooming the same shared vault would collide on git, and an unattended
writer is exactly the risk the gardener is designed to avoid. The only
automatic piece is a *reminder* — a periodic nudge to go run the gardener
by hand — and it lives on n8n (the always-on remote backend) rather than
on either laptop, so it fires regardless of which machine happens to be on.

**What it does:** fires every 14 days, builds a short reminder message.
That's it — it does not run `vault-groom` itself, does not gate on how much
debt has accumulated, and does not touch the vault. If the vault is
already clean, `vault-groom preview` is a free read-only look; the reminder's
only job is to make sure you don't forget it exists.

**What it does NOT do out of the box:** send you anything. The shipped
workflow stops at building the message text — it has no opinion on
Telegram vs. Slack vs. email vs. a webhook, because that's a personal
choice this repo can't make for you.

**To use it:**

1. Import `vault-grooming-reminder.json` into your n8n instance (n8n UI →
   Workflows → Import from File).
2. Add your own notification node after "Build reminder text" (Telegram,
   Slack, email, a webhook — whatever channel you already use for other
   alerts) and wire it to the `message` field.
3. Activate the workflow.
4. Adjust the 14-day interval on the "Every 14 days" node if you want a
   different cadence.
