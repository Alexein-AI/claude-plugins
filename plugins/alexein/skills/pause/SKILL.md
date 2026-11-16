---
description: "Pause Alexein for Claude Code: nothing from these sessions is recorded or sent to memory, and no memories are added when a session starts, until /alexein:resume."
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alexein_hooks.py" pause *)
---

Run this command with the Bash tool, exactly as written:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alexein_hooks.py" pause "${CLAUDE_PLUGIN_DATA}"
```

Then show the person what it printed, as it is, and nothing more.
