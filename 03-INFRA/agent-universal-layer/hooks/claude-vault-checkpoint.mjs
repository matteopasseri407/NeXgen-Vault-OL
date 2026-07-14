#!/usr/bin/env node
// Canonical Claude Code hook for the user's KnowledgeVault.
// Universal across machines: lives in the vault, deployed by agent-sync to the Claude runtime
// (~/.claude/) and wired into ~/.claude/settings.json on every OS.
//
// One event, one script:
//   SessionStart (source resume|compact): inject a short briefing so a reloaded/compacted
//     session re-grounds in the vault instead of guessing.
//
// The hook only injects context; the actual write still needs the model. It never blocks,
// never writes, never prints secrets. On any error it exits 0 silently (must not break sessions).

process.stdin.on("error", () => process.exit(0));
process.stdout.on("error", () => process.exit(0));

const chunks = [];
process.stdin.on("data", (c) => chunks.push(c));
process.stdin.on("end", () => {
  let event = {};
  try {
    event = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
  } catch {
    process.exit(0);
  }

  const name = event.hook_event_name || event.hookEventName || "";
  const source = event.source || event.trigger || "";
  let context = "";

  if (name === "SessionStart") {
    // Brief only when context was actually lost (resume/after-compact), not on fresh manual starts.
    if (source === "resume" || source === "compact") {
      context = [
        "[KnowledgeVault briefing] This session resumed or its context was just compacted.",
        "If the task touches the user's world, re-ground per AGENTS.md 'Probe first' before acting:",
        "one targeted vault read (get_start_here, then 04-NOW/current-focus, recent_activity), not a full reload.",
      ].join(" ");
    }
  }

  if (context) {
    try {
      process.stdout.write(
        JSON.stringify({
          hookSpecificOutput: { hookEventName: name, additionalContext: context },
        }) + "\n"
      );
    } catch {
      // ignore
    }
  }
});
