---
description: "Sign the Alexein hooks in through your browser, so Claude Code looks up your memory and saves your sessions on its own. No API key to copy."
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alexein_hooks.py" sign-in *)
---

Run this command with the Bash tool, exactly as written:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alexein_hooks.py" sign-in "${CLAUDE_PLUGIN_DATA}"
```

Then show the person what it printed, as it is, and nothing more.
