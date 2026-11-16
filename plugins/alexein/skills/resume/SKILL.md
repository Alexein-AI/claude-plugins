---
description: "Resume Alexein for Claude Code after /alexein:pause: sessions are recorded and sent to memory again, and memory is looked up for each session's first message."
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alexein_hooks.py" resume *)
---

Run this command with the Bash tool, exactly as written:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alexein_hooks.py" resume "${CLAUDE_PLUGIN_DATA}"
```

Then show the person what it printed, as it is, and nothing more.
