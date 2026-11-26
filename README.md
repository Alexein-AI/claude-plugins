# Alexein for Claude Code

Gives Claude Code your [Alexein](https://alexein.ai) memory: it searches what you have told it before answering,
and saves what it learns as you work, so the next session, and your other apps, pick up where this one left off.

## One memory across your AI tools

Alexein is your own memory, not Claude Code's. Connect it to the other AI tools you use too (ChatGPT, Cursor, GitHub
Copilot, Antigravity and more) and they all share it: a preference you mention in one, a decision you make in
another, or a detail about your project is there in Claude Code the next time it matters, and the other way round.
It also holds what you chose to connect to Alexein, such as Slack, GitHub or email, so answers can come from your
team's conversations and past decisions, not only this chat.

## Install

In Claude Code, send these one at a time, and let each finish:

```
/plugin marketplace add Alexein-AI/claude-plugins
```

```
/plugin install alexein@alexein
```

Claude Code asks where to install it: choose **Install for you (user scope)**. It then asks for the plugin's
options, or says "3 userConfig options not yet set": all three are optional, so leave the API key empty and keep
the others as they are.

Two sign-ins follow, both in your browser and neither needing a key. The memory tools: run `/mcp` in Claude Code,
choose **alexein** and select **Authenticate**, sign in, choose what Claude Code may do (read only, or read and
save), and select **Allow**; until then, `/mcp` lists alexein as needing authentication. The hooks that use memory
on their own: Alexein's sign-in opens by itself when the next session starts (or run `/alexein:sign-in`); select
**Allow** there too (see below).

To connect without the plugin:

```
claude mcp add --transport http --scope user alexein https://api.alexein.ai/v1/mcp
```

then run `/mcp`, choose **alexein**, and sign in.

### Updates

Claude Code does not update plugins from a marketplace you added yourself unless you turn it on: run `/plugin`,
open **Marketplaces**, choose **alexein** and select **Turn on auto-update**. To update by hand, run
`claude plugin marketplace update alexein`, then `claude plugin update alexein@alexein`, and restart Claude Code.

## Recall and saving sessions

The plugin also adds hooks to Claude Code, so it uses your memory on its own, without waiting for Claude to decide
to. They need the plugin signed in to your Alexein account, apart from the memory tools' own sign-in:

- **Sign-in.** The first time a session starts without it, your browser opens Alexein's sign-in, where you choose
  **Allow**. Nothing is copied or pasted. If you leave it, it opens again at most once a day, three times in all;
  after **Cancel** it never opens by itself again. `/alexein:sign-in` opens it whenever you like, and
  `ALEXEIN_SIGN_IN=0` in the environment keeps it from ever opening by itself. Recall starts with your next
  message. The key it
  makes is a personal key labelled **Claude Code · your computer's name**, listed under **My memory › Keys** where
  you can revoke it; it is kept in the plugin's data folder, readable only by you. Sessions started by scripts
  (`claude -p`), in CI or over SSH never open a browser. With a self-hosted **Alexein API address**, it opens only
  once `ALEXEIN_CONSOLE_URL` names that Alexein's app; otherwise set a personal key from it.
- Until then, at the start of every session the hooks add only a short note on how to use the memory tools, and
  that recall is off. Nothing is read or sent.

Signed in, or with an API key:

- **Recall.** The first message of each session (when it is at least 20 characters) is looked up in your memory,
  and the best matches are added to the session as context. This is one search per session, and it counts as
  one retrieval on your plan. Later messages are not looked up; Claude can still search with the memory tools.
- **Saving sessions.** What you ask and Claude Code's final replies are saved to your memory, together with the
  names of the files it changed and the commands it ran, and whether each one failed. File contents and command
  output are never saved. An exchange answered from your memory, with nothing changed or run, is not saved, so
  what memory already holds does not come back as something new. Exchanges are sent five at a time, and what
  is left is sent when the conversation is compacted or the session ends. Each message saved counts as one event on your plan (two per exchange).
  Anything that looks like a password, key or token is removed before it leaves your computer.
- If a send fails (for example, you are offline), it is kept on your computer and sent again when your next
  session starts. If the API refuses the key (it was revoked), nothing is looked up or saved with it again until
  you set a new one, and `/alexein:status` says so.

To use a key of your own instead of signing in, make a personal key in the Alexein app under **My memory › Keys**
(choose **Read and write** to save sessions). Then run `/plugin` in Claude Code, open **Installed**, choose **alexein**, select
**Configure options**, and paste it into **Alexein API key**. The key is kept in your system's secure credential
store, not in your settings file. The same dialog has **Save sessions to memory**: turn it off to keep recall but
save nothing.

Without the dialog (a script, a remote machine), pass the key when you install, with
`claude plugin install alexein@alexein --config api_key=<your key>`, or set it later by sending
`{"api_key": "<your key>"}` on standard input to `claude plugin configure alexein@alexein --values-stdin`. The
hooks use a key only once Alexein says it is a personal key: a project's key (the one your own app uses) would save
your sessions to an app user of that project, so it is not used, and `/alexein:status` says so.

Commands:

- `/alexein:pause`: nothing is recorded or sent, and nothing is recalled, until you resume. The memory tools still
  work when Claude uses them.
- `/alexein:resume`: turns recall and saving back on.
- `/alexein:sign-in`: opens the browser sign-in now.
- `/alexein:status`: whether the hooks are signed in or a key is set (and whether the API refused it), whether
  automatic recall and saving are on or paused, and how many exchanges wait to be sent.

The hooks need Python 3 on your `PATH`, as `python3` or, where that is missing (as on Windows), `python`. Without
it, the memory tools still work, but Claude Code reports a hook error at each session start. What they keep on your computer is in Claude Code's data folder for the
plugin (`~/.claude/plugins/data/alexein-alexein/`) and is removed when you uninstall the plugin.

## What it can do

The tools are listed at [docs.alexein.ai/mcp/tools](https://docs.alexein.ai/mcp/tools). Everything Claude Code saves can be
seen and erased in the Alexein app, under **My memory**. To disconnect, select **Disconnect** on Claude Code in **My memory › AI
assistants** in the app, or run `/mcp` and log out.

## The same hooks in other agents

`plugins/alexein/scripts/alexein_hooks.py` also runs in other coding agents, as
`alexein_hooks.py run <host> <action>`; what each agent sends is turned into what Claude Code sends, so the same
code does the work. Their plugins carry a copy (`make sync` copies it into checkouts beside this one):

| Agent | Plugin | How Alexein is used | First-message recall | Saving sessions |
|---|---|---|---|---|
| GitHub Copilot (CLI and VS Code) | [copilot-plugin](https://github.com/Alexein-AI/copilot-plugin), `com.github.copilot/hooks/hooks.json` | session start | with the first tool result (its prompt hook can't add context) | reply from its transcript |
| Cursor | [cursor-plugin](https://github.com/Alexein-AI/cursor-plugin), `hooks/hooks.json` | an always-on rule | with the first tool result | reply from `afterAgentResponse` |
| Antigravity | [antigravity-plugin](https://github.com/Alexein-AI/antigravity-plugin), `hooks.json` | `rules/AGENTS.md` | `PreInvocation` | reply from its transcript; no session end, so the rest goes at a later session start |
| Codex | none yet: plugin hooks run only for plugins installed by hand in its desktop app | session start | yes | `last_assistant_message` |
| Gemini CLI | none yet (an extension can carry hooks) | session start | yes | `AfterAgent` |

Outside Claude Code there are no plugin options: the key is `ALEXEIN_API_KEY` (or `api_key` in
`~/.config/alexein/hooks.json`), used only if it is a personal key, with `ALEXEIN_CAPTURE=false` to keep recall only and `ALEXEIN_API_URL` for a
self-hosted API. Without a key, Copilot, Cursor and Antigravity sign in through the browser as Claude Code does
(`run <host> sign-in` opens it on demand); until then only the note on how to use the memory tools is added. Each agent keeps its state in
its plugin data folder where it has one, or `~/.alexein/hooks/<host>/`; `run <host> pause | resume | status`
work there as the slash commands do here.

## Development

`make test` runs the hooks' tests (Python's standard library only; nothing beyond this computer is reached: the
sign-in's tests run a stand-in console on `127.0.0.1`). `make check` also runs `claude plugin validate`.

Three behaviours are off unless their environment variable is `1`, while they are measured:
`ALEXEIN_RECALL_EVERY_PROMPT` (a later message on a new topic is looked up too), `ALEXEIN_RECALL_BEFORE_EDIT` (the
first edit of a file in a session brings what memory holds about it: the search sent to your memory names the file
and up to six names from the edit, such as functions and settings, never the file's contents) and
`ALEXEIN_ASK_CATCH` (a reply that ends by asking you something memory may hold is sent back once to look first; not
in Cursor, which would take the reply as your own message). Each adds only what bears on what was asked, at most
about 600 tokens, and each lookup waits at most 2.5 seconds. `ALEXEIN_SIGN_IN=0` keeps the browser sign-in from
opening on its own; `ALEXEIN_CONSOLE_URL` points it at another Alexein app (`http://localhost:3003` beside a local API). To try the hooks against a local Alexein, set **Alexein API address** to
`http://localhost:8210`, where a local Alexein's API listens (plain `http` is used only for `localhost`).

## Help

[docs.alexein.ai/mcp](https://docs.alexein.ai/mcp) · zainali@alexein.ai · [Privacy](https://alexein.ai/legal/privacy)
