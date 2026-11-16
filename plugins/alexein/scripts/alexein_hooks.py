#!/usr/bin/env python3
"""Alexein's Claude Code hooks, and the commands behind /alexein:pause, resume and status.

One file, the Python standard library only, so installing the plugin is all there is to it.

    session-start   prints how Alexein is used (no network); sends what earlier sessions left unsent
    prompt          on a session's first prompt, one search of the person's memory, given as context
    tool            notes a file changed or a command run (never its contents or its output)
    stop            records the exchange just finished; every BATCH exchanges, sends them
    flush           sends what this session has not sent (PreCompact, SessionEnd)
    send            the detached sender the hooks start
    pause | resume | status

Every hook fails closed and silent: whatever goes wrong is written to `errors.log` in the
plugin's data directory, and the hook exits 0 having printed nothing, so a session never stops
on it. Nothing here touches the network without an API key, and nothing is recorded or sent
while paused. A send runs in its own process so the hook that asked for it returns at once;
a batch that is not accepted stays in `outbox/` and goes with the next session start.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

VERSION = "0.2.0"
DEFAULT_API = "https://api.alexein.ai"
# A personal key reaches its member's own memory under this app user name (the console's
# "X-Alexein-User: me").
USER = "me"
USER_AGENT = f"alexein-claude-code/{VERSION}"

# What SessionStart prints: the console's `sessionLines` (twin-memory-app lib/site/mcp.ts), word
# for word. Facts, not orders: Claude Code's hook docs warn that text framed as system commands
# can trip Claude's prompt-injection defences.
LINES = "\n".join(
    [
        "Alexein is connected over MCP. It is this person's own memory: their work, the people and"
        " projects in it, their preferences, and past decisions and conversations, from every app"
        " they connected.",
        "- memory_search is the source for anything about them, their work, people, plans or past"
        " decisions; this chat alone misses what they said elsewhere.",
        "- memory_open shows a result whole by its ref, memory_around what was said just before and"
        " after an excerpt, and memory_subject one person or project's whole story, oldest first.",
        "- memory_how_they_work shows how they work and write, for drafting or acting for them;"
        " memory_open_items shows what is still open.",
        "- Something they say that should last - a decision, a preference, a fact about them or"
        " their work, small details too - is kept with memory_add, without asking. Passwords, keys"
        " and other secrets are never saved.",
        "- memory_feedback tells memory a search was wrong or missed something.",
        "- General questions that are not about them need no memory call.",
    ]
)

RECALL_MIN_CHARS = 20  # a first prompt shorter than this is no question to search with
RECALL_BUDGET = 2.5  # seconds the first prompt waits for its search, all told
RECALL_TOP = 6
RECALL_ITEM_CHARS = 700
RECALL_CHARS = 4000
QUERY_CHARS = 2000  # the search API's limit on a query

BATCH = 5  # exchanges per send
TURN_CHARS = 8000  # each side of an exchange, as sent
WORK_ITEMS = 12  # files and commands named in one exchange's work line
COMMAND_CHARS = 160
SEND_TIMEOUT = 15
SENDING_STALE = 600  # a claimed batch older than this was left by a sender that died
OUTBOX_DAYS = 30  # a batch not accepted in this long is dropped
IDLE_FLUSH = 6 * 3600  # another session's unsent exchanges, untouched this long, are sent
SESSION_DAYS = 30  # session state kept this long
ERRORS_MAX = 64 * 1024
TRANSCRIPT_TAIL = 2 * 1024 * 1024

FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


# ---------------------------------------------------------------------------------------------
# Settings


def _flag(value: str | None, default: bool) -> bool:
    if value is None or not str(value).strip():
        return default
    return str(value).strip().lower() not in {"false", "0", "no", "off"}


def _usable_api(url: str) -> bool:
    """https anywhere; plain http only to this machine, so a key never crosses a network bare."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme == "https" and parts.hostname:
        return True
    return parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}


def _default_data() -> Path:
    return Path.home() / ".claude" / "plugins" / "data" / "alexein-alexein"


class Config:
    """The plugin's options as Claude Code exports them to a hook (`CLAUDE_PLUGIN_OPTION_<KEY>`),
    and its data directory (`CLAUDE_PLUGIN_DATA`)."""

    def __init__(self, env: dict[str, str] | None = None, data: str | None = None) -> None:
        env = dict(os.environ) if env is None else env
        self.env = env
        self.key = (env.get("CLAUDE_PLUGIN_OPTION_API_KEY") or "").strip()
        self.capture = _flag(env.get("CLAUDE_PLUGIN_OPTION_CAPTURE"), True)
        api = (env.get("CLAUDE_PLUGIN_OPTION_API_URL") or "").strip().rstrip("/")
        self.api = api or DEFAULT_API
        self.api_ok = _usable_api(self.api)
        self.data = Path(data or env.get("CLAUDE_PLUGIN_DATA") or _default_data())

    @property
    def online(self) -> bool:
        """Whether this run may call the API at all."""
        return bool(self.key) and self.api_ok

    @property
    def paused(self) -> bool:
        return (self.data / "paused").exists()

    def dir(self, name: str) -> Path:
        path = self.data / name
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path


# ---------------------------------------------------------------------------------------------
# Files


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_json(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)
    os.replace(temporary, path)


def _append_line(path: Path, value: Any) -> None:
    """One JSON line, in one write to a file opened for appending: hooks that run at once for
    tools called in parallel each land whole."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    line = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


class _Lock:
    """An advisory lock on a file, waited for briefly; past that the hook goes on without it
    rather than hold the session up. Unlocked on platforms without `fcntl`."""

    def __init__(self, path: Path, wait: float = 2.0) -> None:
        self.path, self.wait, self.handle = path, wait, None

    def __enter__(self) -> _Lock:
        try:
            import fcntl
        except ImportError:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.handle = open(self.path, "a")  # noqa: SIM115 - held until __exit__
        deadline = time.monotonic() + self.wait
        while True:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    return self
                time.sleep(0.02)

    def __exit__(self, *_: Any) -> None:
        if self.handle is not None:
            self.handle.close()


def try_lock(path: Path):
    """A lock taken now or not at all: the handle to keep, or None when someone holds it."""
    try:
        import fcntl
    except ImportError:
        return open(path, "a")  # noqa: SIM115
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = open(path, "a")  # noqa: SIM115
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        return None


def log_error(cfg: Config | None, what: str) -> None:
    try:
        path = (cfg.data if cfg else _default_data()) / "errors.log"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists() and path.stat().st_size > ERRORS_MAX:
            kept = path.read_bytes()[-ERRORS_MAX // 2 :]
            path.write_bytes(kept)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{_now()} {what}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------------------------
# Secrets: stripped before anything is kept on disk or leaves the machine. Simple patterns, as
# a backstop; the add also asks the API to redact what it knows.

_SECRET = "[secret]"
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
            re.S,
        ),
        _SECRET,
    ),
    # Keys and tokens known by their prefix.
    (
        re.compile(
            r"\b(?:sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,}"
            r"|tm_[A-Za-z0-9_\-]{12,}"
            r"|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
            r"|glpat-[A-Za-z0-9_\-]{16,}"
            r"|xox[abposr]-[A-Za-z0-9\-]{10,}"
            r"|(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}"
            r"|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}"
            r"|AIza[0-9A-Za-z_\-]{30,}"
            r"|npm_[A-Za-z0-9]{30,}"
            r"|pypi-[A-Za-z0-9_\-]{40,}"
            r"|hf_[A-Za-z0-9]{30,})"
        ),
        _SECRET,
    ),
    # JSON web tokens.
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), _SECRET),
    # A bearer or basic credential in a header.
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{12,}"), r"\1 " + _SECRET),
    # user:password@ in a URL.
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@]+):[^\s@/]+@"), r"\1:" + _SECRET + "@"),
    # A value after a name that says it is secret: password=..., "api_key": "...", TOKEN=...
    (
        re.compile(
            r"(?i)([\"']?[A-Za-z0-9_\-]*(?:passw(?:or)?d|passwd|pwd|secret|token|api[_\-]?key"
            r"|access[_\-]?key|private[_\-]?key|credential|auth[_\-]?key)[A-Za-z0-9_\-]*[\"']?"
            r"\s*[:=]\s*)(\"[^\"\n]*\"|'[^'\n]*'|[^\s,;\"'}{)]+)"
        ),
        r"\1" + _SECRET,
    ),
    # --password value, -p'value' style flags.
    (
        re.compile(r"(?i)(--?(?:password|passwd|token|api-key|secret)[ =])(\S+)"),
        r"\1" + _SECRET,
    ),
]


def strip_secrets(text: str) -> str:
    if not text:
        return text
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# ---------------------------------------------------------------------------------------------
# Network


def _request(cfg: Config, path: str, body: dict[str, Any], timeout: float, idempotency: str = ""):
    """POST to the API: (status, parsed body or None). Raises on a network failure."""
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(cfg.api + path, data=data, method="POST")
    request.add_header("Authorization", f"Bearer {cfg.key}")
    request.add_header("X-Alexein-User", USER)
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", USER_AGENT)
    if idempotency:
        request.add_header("Idempotency-Key", idempotency)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(4 * 1024 * 1024)
            status = response.status
    except urllib.error.HTTPError as refused:
        try:
            raw = refused.read(64 * 1024)
        except OSError:
            raw = b""
        status = refused.code
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        parsed = None
    return status, parsed


# ---------------------------------------------------------------------------------------------
# Session state: sessions/<id>.json holds whether the session has had its search, the prompt
# waiting for its reply, and the exchanges not yet sent; sessions/<id>.work.jsonl the files and
# commands of the exchange under way.


def _session_name(hook: dict[str, Any]) -> str:
    raw = str(hook.get("session_id") or "")
    return re.sub(r"[^A-Za-z0-9_\-]", "", raw)[:100] or "unknown"


def _paths(cfg: Config, sid: str) -> tuple[Path, Path, Path]:
    sessions = cfg.dir("sessions")
    return sessions / f"{sid}.json", sessions / f"{sid}.work.jsonl", sessions / f"{sid}.lock"


def _load(path: Path) -> dict[str, Any]:
    state = _read_json(path, {})
    return state if isinstance(state, dict) else {}


def _write_status(cfg: Config) -> None:
    """What /alexein:status reports about the options: the skill's command runs in the Bash tool,
    which Claude Code gives no plugin option. Whether a key is set, never the key."""
    shown = {
        "key_set": bool(cfg.key),
        "capture": cfg.capture,
        "api_url": cfg.api,
        "api_url_usable": cfg.api_ok,
        "version": VERSION,
        "seen_at": _now(),
    }
    current = _read_json(cfg.data / "status.json", {})
    if isinstance(current, dict) and {k: v for k, v in current.items() if k != "seen_at"} == {
        k: v for k, v in shown.items() if k != "seen_at"
    }:
        return
    _write_json(cfg.data / "status.json", shown)


# ---------------------------------------------------------------------------------------------
# SessionStart


def session_start(cfg: Config, hook: dict[str, Any]) -> str:
    _write_status(cfg)
    if cfg.online and not cfg.paused:
        _sweep(cfg, _session_name(hook))
        if _outbox_waiting(cfg):
            spawn_sender(cfg)
    return LINES


def _sweep(cfg: Config, current: str) -> None:
    """Sessions that ended without a SessionEnd - a crash, a closed terminal - have their unsent
    exchanges sent once they have sat a while; session state past its days is removed."""
    sessions = cfg.dir("sessions")
    now = time.time()
    for path in sessions.glob("*.json"):
        sid = path.stem
        if sid == current:
            continue
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age > SESSION_DAYS * 86400:
            for stale in (path, sessions / f"{sid}.work.jsonl", sessions / f"{sid}.lock"):
                try:
                    stale.unlink()
                except OSError:
                    pass
        elif age > IDLE_FLUSH and cfg.capture:
            _batch(cfg, sid)


# ---------------------------------------------------------------------------------------------
# UserPromptSubmit


def prompt(cfg: Config, hook: dict[str, Any]) -> dict[str, Any] | None:
    _write_status(cfg)
    if not cfg.online or cfg.paused:
        return None
    said = str(hook.get("prompt") or "")
    command = said.lstrip().startswith("/")
    sid = _session_name(hook)
    state_path, work_path, lock_path = _paths(cfg, sid)
    with _Lock(lock_path):
        state = _load(state_path)
        first = not state.get("searched") and not command
        if first:
            state["searched"] = True
        if cfg.capture:
            # A slash command's reply is not a conversation with the person: its turn is skipped.
            state["prompt"] = None if command else strip_secrets(said.strip())[:TURN_CHARS]
            state["prompt_at"] = _now()
            state["skip"] = command
            state["cwd"] = str(hook.get("cwd") or state.get("cwd") or "")
            try:
                work_path.unlink()
            except OSError:
                pass
        if first or cfg.capture:
            _write_json(state_path, state)
    if not first or len(said.strip()) < RECALL_MIN_CHARS:
        return None
    context = recall(cfg, said.strip())
    if not context:
        return None
    return {
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}
    }


def recall(cfg: Config, query: str) -> str:
    """One search, inside RECALL_BUDGET all told (a socket timeout bounds each read, not the
    whole), as facts for the context. "" when it fails, is late or finds nothing."""
    found: dict[str, Any] = {}

    def search() -> None:
        try:
            body = {"query": strip_secrets(query)[:QUERY_CHARS], "fast": True}
            status, answer = _request(cfg, "/v2/memory/search", body, timeout=RECALL_BUDGET)
            found["status"], found["answer"] = status, answer
        except Exception as failed:  # noqa: BLE001 - any failure is no context
            found["error"] = f"{type(failed).__name__}: {failed}"

    worker = threading.Thread(target=search, daemon=True)
    worker.start()
    worker.join(RECALL_BUDGET)
    if worker.is_alive():
        log_error(cfg, "recall: no answer in time")
        return ""
    if "error" in found:
        log_error(cfg, f"recall: {found['error']}")
        return ""
    if found.get("status") != 200 or not isinstance(found.get("answer"), dict):
        log_error(cfg, f"recall: HTTP {found.get('status')}")
        return ""
    return format_memories(found["answer"].get("memories") or [])


def format_memories(memories: list[Any]) -> str:
    lines: list[str] = []
    size = 0
    for memory in memories:
        if len(lines) >= RECALL_TOP or not isinstance(memory, dict):
            continue
        text = " ".join(str(memory.get("text") or "").split())
        if not text:
            continue
        if len(text) > RECALL_ITEM_CHARS:
            text = text[: RECALL_ITEM_CHARS - 3].rstrip() + "..."
        date = str(memory.get("date") or "")[:10]
        line = f"- ({date}) {text}" if date else f"- {text}"
        if size + len(line) > RECALL_CHARS:
            break
        lines.append(line)
        size += len(line) + 1
    if not lines:
        return ""
    head = (
        "From Alexein memory, about this person, found for their first message in this session"
        " (memory_search finds more):"
    )
    return head + "\n" + "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# PostToolUse, PostToolUseFailure: what was done, never what it read or wrote


def tool(cfg: Config, hook: dict[str, Any], failed: bool) -> None:
    if not cfg.online or cfg.paused or not cfg.capture:
        return
    name = str(hook.get("tool_name") or "")
    given = hook.get("tool_input") if isinstance(hook.get("tool_input"), dict) else {}
    if name in FILE_TOOLS:
        target = str(given.get("file_path") or given.get("notebook_path") or "")
        if not target:
            return
        cwd = str(hook.get("cwd") or "")
        try:
            if cwd and os.path.commonpath([os.path.abspath(target), os.path.abspath(cwd)]) == (
                os.path.abspath(cwd)
            ):
                target = os.path.relpath(target, cwd)
        except ValueError:
            pass
        entry = {"kind": "file", "what": strip_secrets(target)[:300], "failed": failed}
    elif name == "Bash":
        command = " ".join(str(given.get("command") or "").split())
        if not command:
            return
        if len(command) > COMMAND_CHARS:
            command = command[: COMMAND_CHARS - 3] + "..."
        entry = {"kind": "command", "what": strip_secrets(command), "failed": failed}
    else:
        return
    _append_line(_paths(cfg, _session_name(hook))[1], entry)


def work_line(entries: list[dict[str, Any]]) -> str:
    """The exchange's files and commands in one line: "Files changed: a.py, b.py. Commands run:
    make test (failed)." The last outcome of a file or a command is the one kept."""
    files: dict[str, bool] = {}
    commands: dict[str, bool] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        what = str(entry.get("what") or "")
        if not what:
            continue
        held = files if entry.get("kind") == "file" else commands
        held.pop(what, None)
        held[what] = bool(entry.get("failed"))

    def listed(found: dict[str, bool], quote: bool) -> str:
        names = list(found.items())
        shown = [
            (f"`{what}`" if quote else what) + (" (failed)" if failed else "")
            for what, failed in names[-WORK_ITEMS:]
        ]
        more = len(names) - len(shown)
        return ", ".join(shown) + (f" and {more} more" if more > 0 else "")

    parts = []
    if files:
        parts.append(f"Files changed: {listed(files, False)}.")
    if commands:
        parts.append(f"Commands run: {listed(commands, True)}.")
    return " ".join(parts)


def _read_work(path: Path) -> list[dict[str, Any]]:
    out = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


# ---------------------------------------------------------------------------------------------
# Stop


def stop(cfg: Config, hook: dict[str, Any]) -> None:
    if not cfg.online or cfg.paused or not cfg.capture:
        return
    sid = _session_name(hook)
    state_path, work_path, lock_path = _paths(cfg, sid)
    with _Lock(lock_path):
        state = _load(state_path)
        if state.get("skip"):
            state["skip"] = False
            state["prompt"] = None
            _write_json(state_path, state)
            _drop(work_path)
            return
        user = state.get("prompt")
        if not user and hook.get("stop_hook_active"):
            # Another hook kept Claude going after this turn's exchange was recorded: the same
            # prompt again, from the transcript, would be a second copy of it.
            return
        reply = str(hook.get("last_assistant_message") or "")
        if not user or not reply:
            last_user, last_reply = read_transcript(str(hook.get("transcript_path") or ""))
            user = user or strip_secrets(last_user)[:TURN_CHARS]
            reply = reply or last_reply
        reply = strip_secrets(reply.strip())[:TURN_CHARS]
        work = work_line(_read_work(work_path))
        _drop(work_path)
        state["prompt"] = None
        if not user and not reply:
            _write_json(state_path, state)
            return
        sequence = int(state.get("seq") or 0) + 1
        state["seq"] = sequence
        state.setdefault("exchanges", []).append(
            {
                "n": sequence,
                "user": user or "",
                "user_at": state.get("prompt_at") or _now(),
                "assistant": reply,
                "assistant_at": _now(),
                "work": work,
            }
        )
        state["cwd"] = str(hook.get("cwd") or state.get("cwd") or "")
        _write_json(state_path, state)
        due = len(state["exchanges"]) >= BATCH
    if due and _batch(cfg, sid):
        spawn_sender(cfg)


def _drop(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def read_transcript(path: str) -> tuple[str, str]:
    """The last thing the person typed and the reply after it, from the end of the session's
    transcript (JSON lines); ("", "") when it cannot be read. Only the tail is read."""
    if not path:
        return "", ""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - TRANSCRIPT_TAIL))
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return "", ""
    user, replies = "", []
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("isSidechain") or entry.get("isMeta"):
            continue
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        role = message.get("role") or entry.get("type")
        text = _text_of(message.get("content"))
        if role == "user" and text and not text.lstrip().startswith("<"):
            user, replies = text, []
        elif role == "assistant" and text:
            replies.append(text)
    return user.strip(), "\n\n".join(replies).strip()


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


# ---------------------------------------------------------------------------------------------
# Batches


def _batch(cfg: Config, sid: str) -> bool:
    """This session's unsent exchanges moved into one batch in `outbox/`, as the add it will be
    sent as. False when there was nothing to send."""
    state_path, _, lock_path = _paths(cfg, sid)
    with _Lock(lock_path):
        state = _load(state_path)
        exchanges = [e for e in state.get("exchanges") or [] if isinstance(e, dict)]
        if not exchanges:
            return False
        first, last = exchanges[0].get("n"), exchanges[-1].get("n")
        name = f"cc-{sid}-{first}-{last}"
        body = add_body(sid, exchanges, str(state.get("cwd") or ""))
        _write_json(
            cfg.dir("outbox") / f"{name}.json", {"id": name, "made_at": _now(), "body": body}
        )
        state["exchanges"] = []
        _write_json(state_path, state)
    return True


def add_body(sid: str, exchanges: list[dict[str, Any]], cwd: str) -> dict[str, Any]:
    """POST /v2/memory/add's body: the exchanges as one conversation, the person's turns theirs
    (`author.self`, role user) and Claude Code's an assistant's. One `session` for the whole
    Claude Code session, so its batches are read as one conversation; ids made of the session
    and the exchange's number, so a batch sent again is the same items, not new ones."""
    session = f"claude-code-{sid}"
    metadata = {"app": "claude-code"}
    project = os.path.basename(cwd.rstrip("/\\")) if cwd else ""
    if project:
        metadata["project"] = project[:100]
    items: list[dict[str, Any]] = []
    for exchange in exchanges:
        n = exchange.get("n")
        user = strip_secrets(str(exchange.get("user") or "")).strip()
        reply = strip_secrets(str(exchange.get("assistant") or "")).strip()
        work = strip_secrets(str(exchange.get("work") or "")).strip()
        if user:
            items.append(
                {
                    "id": f"{session}:{n}:user",
                    "kind": "message",
                    "text": user[:TURN_CHARS],
                    "session": session,
                    "role": "user",
                    "author": {"self": True},
                    "at": exchange.get("user_at") or _now(),
                    "metadata": metadata,
                }
            )
        # The work first: an assistant's turn is read for its opening.
        said = "\n\n".join(part for part in (work, reply) if part)
        if said:
            items.append(
                {
                    "id": f"{session}:{n}:assistant",
                    "kind": "message",
                    "text": said[: TURN_CHARS + 600],
                    "session": session,
                    "role": "assistant",
                    "author": {"name": "Claude Code"},
                    "at": exchange.get("assistant_at") or _now(),
                    "metadata": metadata,
                }
            )
    return {"items": items, "redact": True}


def _outbox_waiting(cfg: Config) -> bool:
    outbox = cfg.dir("outbox")
    now = time.time()
    for claimed in outbox.glob("*.sending"):
        try:
            if now - claimed.stat().st_mtime > SENDING_STALE:
                os.replace(claimed, claimed.with_suffix(".json"))
        except OSError:
            continue
    return any(outbox.glob("*.json"))


def flush(cfg: Config, hook: dict[str, Any]) -> None:
    """PreCompact and SessionEnd: what this session has not sent, sent now. SessionEnd's hooks
    share a budget of 1.5 seconds, so the send is handed to its own process."""
    if not cfg.online or cfg.paused:
        return
    made = cfg.capture and _batch(cfg, _session_name(hook))
    if made or _outbox_waiting(cfg):
        spawn_sender(cfg)


def spawn_sender(cfg: Config) -> None:
    """The sender, detached in a session of its own so it outlives the hook and Claude Code."""
    env = dict(cfg.env)
    env["CLAUDE_PLUGIN_DATA"] = str(cfg.data)
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "cwd": str(cfg.data),
        "env": env,
    }
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        options["start_new_session"] = True
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "send"], **options)


def send(cfg: Config) -> int:
    """Every batch in `outbox/`, oldest first: sent and removed; refused for what it holds (400,
    413, 422) moved to `rejected/` and not sent again; anything else - offline, a 5xx, a key
    refused or over its limit - left for the next try. One sender at a time; it stops at the
    first failure that is not the batch's own. Returns how many were accepted."""
    if not cfg.online or cfg.paused:
        return 0
    holder = try_lock(cfg.data / "send.lock")
    if holder is None:
        return 0
    accepted = 0
    try:
        _outbox_waiting(cfg)
        outbox = cfg.dir("outbox")
        for path in sorted(outbox.glob("*.json"), key=_mtime):
            if cfg.paused:
                break
            claimed = path.with_suffix(".sending")
            try:
                os.replace(path, claimed)
            except OSError:
                continue
            batch = _read_json(claimed, None)
            if not isinstance(batch, dict) or not isinstance(batch.get("body"), dict):
                _reject(cfg, claimed, "unreadable")
                continue
            if _age_days(batch) > OUTBOX_DAYS:
                _reject(cfg, claimed, "too old")
                continue
            try:
                status, answer = _request(
                    cfg, "/v2/memory/add", batch["body"], SEND_TIMEOUT, str(batch.get("id") or "")
                )
            except Exception as failed:  # noqa: BLE001 - kept for the next try
                _keep(cfg, claimed, batch, f"{type(failed).__name__}: {failed}")
                break
            if 200 <= status < 300:
                _drop(claimed)
                accepted += 1
                _write_json(
                    cfg.data / "last_send.json",
                    {"at": _now(), "ok": True, "batch": batch.get("id")},
                )
            elif status in (400, 413, 422):
                _reject(cfg, claimed, f"HTTP {status}: {_detail(answer)}")
            else:
                _keep(cfg, claimed, batch, f"HTTP {status}: {_detail(answer)}")
                break
    finally:
        holder.close()
    return accepted


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _age_days(batch: dict[str, Any]) -> float:
    try:
        made = dt.datetime.fromisoformat(str(batch.get("made_at")).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (dt.datetime.now(dt.timezone.utc) - made).total_seconds() / 86400


def _detail(answer: Any) -> str:
    if isinstance(answer, dict) and answer.get("detail") is not None:
        return str(answer["detail"])[:300]
    return ""


def _keep(cfg: Config, claimed: Path, batch: dict[str, Any], why: str) -> None:
    batch["attempts"] = int(batch.get("attempts") or 0) + 1
    batch["last_error"] = why
    _write_json(claimed.with_suffix(".json"), batch)
    _drop(claimed)
    _write_json(cfg.data / "last_send.json", {"at": _now(), "ok": False, "error": why})
    log_error(cfg, f"send {batch.get('id')}: {why}; kept for the next try")


def _reject(cfg: Config, claimed: Path, why: str) -> None:
    target = cfg.dir("rejected") / claimed.with_suffix(".json").name
    try:
        os.replace(claimed, target)
    except OSError:
        _drop(claimed)
    _write_json(cfg.data / "last_send.json", {"at": _now(), "ok": False, "error": why})
    log_error(cfg, f"send {claimed.stem}: {why}; not sent again")


# ---------------------------------------------------------------------------------------------
# /alexein:pause, /alexein:resume, /alexein:status


def pause(cfg: Config) -> str:
    cfg.data.mkdir(parents=True, exist_ok=True, mode=0o700)
    (cfg.data / "paused").write_text(_now() + "\n", encoding="utf-8")
    return (
        "Alexein is paused for Claude Code: nothing from these sessions is recorded or sent, and no"
        " memories are added at the start of a session. The memory tools still work when asked."
        " /alexein:resume turns it back on."
    )


def resume(cfg: Config) -> str:
    _drop(cfg.data / "paused")
    return (
        "Alexein is on again for Claude Code: sessions are recorded and sent, and recall is back."
    )


def status(cfg: Config) -> str:
    shown = _read_json(cfg.data / "status.json", {})
    shown = shown if isinstance(shown, dict) else {}
    waiting = 0
    for path in list(cfg.dir("outbox").glob("*.json")) + list(cfg.dir("outbox").glob("*.sending")):
        batch = _read_json(path, {})
        body = batch.get("body") if isinstance(batch, dict) else None
        items = body.get("items") if isinstance(body, dict) else []
        waiting += len(
            {str(i.get("id", "")).rsplit(":", 1)[0] for i in items if isinstance(i, dict)}
        )
    recording = 0
    for path in cfg.dir("sessions").glob("*.json"):
        state = _read_json(path, {})
        recording += len(state.get("exchanges") or []) if isinstance(state, dict) else 0
    rejected = len(list(cfg.dir("rejected").glob("*.json")))
    last = _read_json(cfg.data / "last_send.json", {})

    if "key_set" not in shown:
        key = "unknown until the next session starts"
    elif shown["key_set"]:
        key = "set"
    else:
        key = (
            "not set. Memory tools still work through sign-in; recall at the start of a session"
            " and saving sessions need a key (/plugin > Installed > alexein > Configure options)"
        )
    lines = [
        "Alexein for Claude Code",
        f"- API key: {key}",
        f"- Saving sessions: {'on' if shown.get('capture', True) else 'off (the capture option)'}",
        f"- Paused: {'yes (/alexein:resume turns it back on)' if cfg.paused else 'no'}",
        f"- Exchanges waiting to be sent: {waiting + recording}"
        + (
            f" ({waiting} ready, {recording} still being recorded)" if waiting and recording else ""
        ),
    ]
    if shown.get("api_url") and shown.get("api_url") != DEFAULT_API:
        usable = "" if shown.get("api_url_usable", True) else " (not used: http only to localhost)"
        lines.append(f"- API: {shown['api_url']}{usable}")
    if isinstance(last, dict) and last.get("at"):
        outcome = "accepted" if last.get("ok") else f"failed, {last.get('error')}"
        lines.append(f"- Last send: {last['at']}, {outcome}")
    if rejected:
        lines.append(f"- Batches the API refused, kept in {cfg.data / 'rejected'}: {rejected}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------


def _hook_input() -> dict[str, Any]:
    try:
        value = json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


HOOKS = {"session-start", "prompt", "tool", "tool-failed", "stop", "flush"}
COMMANDS = {"send", "pause", "resume", "status"}


def main(argv: list[str]) -> int:
    cfg: Config | None = None
    try:
        action = argv[1] if len(argv) > 1 else ""
        data = argv[2] if len(argv) > 2 else None
        if action not in HOOKS | COMMANDS:
            return 0
        cfg = Config(data=data)
        if action in COMMANDS:
            if action == "send":
                send(cfg)
            else:
                print({"pause": pause, "resume": resume, "status": status}[action](cfg))
            return 0
        hook = _hook_input()
        if action == "session-start":
            print(session_start(cfg, hook))
        elif action == "prompt":
            out = prompt(cfg, hook)
            if out:
                print(json.dumps(out))
        elif action in ("tool", "tool-failed"):
            tool(cfg, hook, failed=action == "tool-failed")
        elif action == "stop":
            stop(cfg, hook)
        elif action == "flush":
            flush(cfg, hook)
    except BaseException as failed:  # noqa: BLE001 - a hook never breaks the session
        if isinstance(failed, KeyboardInterrupt):
            return 0
        log_error(cfg, f"{argv[1:2]}: {type(failed).__name__}: {failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
