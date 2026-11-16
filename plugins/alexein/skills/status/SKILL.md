---
description: "Show Alexein's state in Claude Code: whether an API key is set, whether saving is on or paused, and how many exchanges wait to be sent."
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alexein_hooks.py" status *)
---

Run this command with the Bash tool, exactly as written:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alexein_hooks.py" status "${CLAUDE_PLUGIN_DATA}"
```

Then show the person what it printed, as it is, and nothing more.
