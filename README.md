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

## Recall and saving sessions (optional API key)

The plugin also adds hooks to Claude Code. Without an API key, only one runs: at the start of every session it
adds a short note on how to use the memory tools. Nothing is read or sent.

With a key, Claude Code also uses your memory on its own:

- **Recall.** The first message of each session (when it is at least 20 characters) is looked up in your memory,
  and the best matches are added to the session as context. This is one search per session, and it counts as
  one retrieval on your plan. Later messages are not looked up; Claude can still search with the memory tools.
- **Saving sessions.** What you ask and Claude Code's final replies are saved to your memory, together with the
  names of the files it changed and the commands it ran, and whether each one failed. File contents and command
  output are never saved. Exchanges are sent five at a time, and what is left is sent when the conversation is
  compacted or the session ends. Each message saved counts as one event on your plan (two per exchange).
  Anything that looks like a password, key or token is removed before it leaves your computer.
- If a send fails (for example, you are offline), it is kept on your computer and sent again when your next
  session starts.

To add a key, make a personal key in the Alexein app under **My memory › Keys** (choose **Read and write** to
save sessions). Then run `/plugin` in Claude Code, open **Installed**, choose **alexein**, select
**Configure options**, and paste it into **Alexein API key**. The key is kept in your system's secure credential
store, not in your settings file. The same dialog has **Save sessions to memory**: turn it off to keep recall but
save nothing.

Commands:

- `/alexein:pause`: nothing is recorded or sent, and nothing is recalled, until you resume. The memory tools still
  work when Claude uses them.
- `/alexein:resume`: turns recall and saving back on.
- `/alexein:status`: whether a key is set, whether saving is on or paused, and how many exchanges wait to be sent.

The hooks need `python3` on your `PATH`. What they keep on your computer is in Claude Code's data folder for the
plugin (`~/.claude/plugins/data/alexein-alexein/`) and is removed when you uninstall the plugin.

## What it can do

The tools are listed at [docs.alexein.ai/mcp/tools](https://docs.alexein.ai/mcp/tools). Everything Claude Code saves can be
seen and erased in the Alexein app, under **My memory**. To disconnect, select **Disconnect** on Claude Code in **My memory › AI
assistants** in the app, or run `/mcp` and log out.

## Development

`make test` runs the hooks' tests (Python's standard library only; nothing reaches the network). `make check` also
runs `claude plugin validate`. To try the hooks against a local Alexein, set **Alexein API address** to
`http://localhost:8311` (plain `http` is used only for `localhost`).

## Help

[docs.alexein.ai/mcp](https://docs.alexein.ai/mcp) · zainali@alexein.ai · [Privacy](https://alexein.ai/legal/privacy)
