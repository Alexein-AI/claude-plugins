# Alexein for Claude Code

Gives Claude Code your [Alexein](https://alexein.ai) memory: it searches what you have told it before answering,
and saves what it learns as you work, so the next session, and your other apps, pick up where this one left off.

## Install

In Claude Code:

```
/plugin marketplace add Alexein-AI/claude-plugins
/plugin install alexein@alexein
```

Then run `/mcp` in Claude Code, choose **alexein** and select **Authenticate**: Alexein's sign-in opens in your
browser. Sign in, choose what Claude Code may do (read only, or read and save), and select **Allow**. No API key is
needed. Until then, `/mcp` lists alexein as needing authentication.

To connect without the plugin:

```
claude mcp add --transport http --scope user alexein https://api.alexein.ai/v1/mcp
```

then run `/mcp`, choose **alexein**, and sign in.

## What it can do

The tools are listed at [docs.alexein.ai/mcp/tools](https://docs.alexein.ai/mcp/tools). Everything Claude Code saves can be
seen and erased in the Alexein app, under **My memory**. To disconnect, select **Disconnect** on Claude Code in **My memory › AI
assistants** in the app, or run `/mcp` and log out.

## Help

[docs.alexein.ai/mcp](https://docs.alexein.ai/mcp) · support@alexein.ai · [Privacy](https://alexein.ai/legal/privacy)
