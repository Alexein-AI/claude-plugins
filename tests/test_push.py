"""Memory pushed into the agent (the C2 flags), and the browser sign-in (C1).

ALEXEIN_RECALL_EVERY_PROMPT, ALEXEIN_RECALL_BEFORE_EDIT and ALEXEIN_ASK_CATCH are read from the
environment, off unless "1". The sign-in runs against a fake console on 127.0.0.1; nothing else
is reached.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stdout
from unittest import mock

from test_hooks import SCRIPT, SEARCH, Base, Network, Response, hooks, refuse

LEDGER = "How did we set up the billing ledger again?"
FLAGS = {name: "1" for name in hooks.FLAGS}

# A quick search's answer: `answerable` null, the page's leading lines, then excerpts, then the
# page's rest (`known_after`), which bore on nothing asked.
RATE = {
    "query": "q",
    "answerable": None,
    "memories": [
        {"type": "known", "text": "Prefers tabs over spaces.", "ref": "k1"},
        {
            "type": "fact",
            "text": "The scheduler rate limit default is 50 since November; it was 100.",
            "ref": "f1",
            "date": "2026-11-02",
        },
        {
            "type": "excerpt",
            "text": "Lunch is at noon on Fridays.",
            "ref": "e1",
            "date": "2026-10-01",
        },
        {
            "type": "excerpt",
            "text": "We agreed the scheduler limit stays configurable per tenant.",
            "ref": "e2",
            "date": "2026-11-03",
        },
        {"type": "known", "text": "The rate limit is tracked per scheduler run.", "ref": "k9"},
    ],
}


def search_count(net: Network) -> int:
    return sum(1 for r in net.requests if r["url"].endswith("/v2/memory/search"))


class Flags(Base):
    def setUp(self) -> None:
        super().setUp()
        self.env = {**self.env, **FLAGS}

    def context(self, out: str, event: str) -> str:
        shown = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(shown["hookEventName"], event)
        return shown["additionalContext"]


class EveryPrompt(Flags):
    base = {"session_id": "s1", "cwd": "/work/kiln"}

    def test_a_later_prompt_on_a_new_topic_is_looked_up_and_gated(self) -> None:
        net = Network(Response(200, SEARCH), Response(200, RATE))
        self.run_hook("prompt", {**self.base, "prompt": LEDGER}, net=net)
        out = self.run_hook(
            "prompt",
            {**self.base, "prompt": "Now change the scheduler rate limit default for new tenants"},
            net=net,
        )
        self.assertEqual(search_count(net), 2)
        self.assertEqual(net.requests[1]["body"]["fast"], True)  # the quick path, free
        context = self.context(out, "UserPromptSubmit")
        self.assertTrue(context.startswith(hooks.RECALL_AGAIN_HEAD + "\n"))
        self.assertIn("rate limit default is 50", context)
        self.assertIn("configurable per tenant", context)
        self.assertNotIn("tabs over spaces", context)  # shares no topic word
        self.assertNotIn("Lunch", context)
        self.assertNotIn("per scheduler run", context)  # after the evidence: the page's rest

    def test_the_same_topic_or_a_go_ahead_is_not_looked_up_again(self) -> None:
        net = Network(Response(200, SEARCH))
        self.run_hook("prompt", {**self.base, "prompt": LEDGER}, net=net)
        for said in (
            "How did we set up the billing ledger, again please?",
            "ok, go ahead and do it",
            "thanks, that looks good to me",
            "short one",
            "/alexein:status please now",
        ):
            with self.subTest(said=said):
                self.assertEqual(self.run_hook("prompt", {**self.base, "prompt": said}), "")

    def test_memory_saying_it_holds_nothing_adds_nothing(self) -> None:
        net = Network(Response(200, SEARCH), Response(200, {**RATE, "answerable": False}))
        self.run_hook("prompt", {**self.base, "prompt": LEDGER}, net=net)
        out = self.run_hook(
            "prompt", {**self.base, "prompt": "change the scheduler rate limit default"}, net=net
        )
        self.assertEqual(out, "")

    def test_a_line_is_shown_once_a_session(self) -> None:
        net = Network(Response(200, SEARCH), Response(200, RATE), Response(200, RATE))
        self.run_hook("prompt", {**self.base, "prompt": LEDGER}, net=net)
        first = self.run_hook(
            "prompt", {**self.base, "prompt": "change the scheduler rate limit default"}, net=net
        )
        self.assertIn("default is 50", first)
        second = self.run_hook(
            "prompt",
            {**self.base, "prompt": "which tenants override the scheduler rate limit config"},
            net=net,
        )
        self.assertEqual(search_count(net), 3)
        self.assertEqual(second, "")

    def test_bounded_in_size_and_time(self) -> None:
        many = {
            "answerable": None,
            "memories": [
                {"type": "fact", "text": "scheduler rate limit " + "x" * 3000, "ref": f"r{n}"}
                for n in range(20)
            ],
        }
        net = Network(Response(200, SEARCH), Response(200, many))
        self.run_hook("prompt", {**self.base, "prompt": LEDGER}, net=net)
        out = self.run_hook(
            "prompt", {**self.base, "prompt": "change the scheduler rate limit default"}, net=net
        )
        context = self.context(out, "UserPromptSubmit")
        body = context[len(hooks.RECALL_AGAIN_HEAD) :]
        self.assertLessEqual(len(body), hooks.PUSH_CHARS + 1)
        self.assertLessEqual(body.count("\n- "), hooks.PUSH_TOP)

        def slow():
            time.sleep(1.0)
            return Response(200, RATE)

        net = Network(slow)
        with mock.patch.object(hooks, "RECALL_BUDGET", 0.2):
            started = time.monotonic()
            out = self.run_hook(
                "prompt",
                {**self.base, "prompt": "which owner decided the tenant onboarding policy"},
                net=net,
            )
            self.assertLess(time.monotonic() - started, 0.8)
        self.assertEqual(out, "")
        self.assertIn("recall again: no answer in time", (self.data / "errors.log").read_text())

    def test_off_by_default(self) -> None:
        env = {k: v for k, v in self.env.items() if k not in FLAGS}
        net = Network(Response(200, SEARCH))
        self.run_hook("prompt", {**self.base, "prompt": LEDGER}, env=env, net=net)
        out = self.run_hook(
            "prompt", {**self.base, "prompt": "change the scheduler rate limit default"}, env=env
        )
        self.assertEqual(out, "")  # `refuse`: no second search
        for value in ("0", "", "true-ish?", "no"):
            env = {**self.env, "ALEXEIN_RECALL_EVERY_PROMPT": value}
            self.assertFalse(hooks.Config(env=env).recall_every, value)

    def test_the_first_prompt_is_as_it_was(self) -> None:
        net = Network(Response(200, SEARCH))
        out = self.run_hook("prompt", {**self.base, "prompt": LEDGER}, net=net)
        self.assertTrue(self.context(out, "UserPromptSubmit").startswith(hooks.RECALL_HEAD))

    def test_copilot_takes_it_with_the_next_tool_result(self) -> None:
        env = {"ALEXEIN_API_KEY": "tm_testkey_0123456789abcdef", **FLAGS}
        env["ALEXEIN_API_URL"] = "https://api.example.test"
        net = Network(Response(200, SEARCH), Response(200, RATE))
        base = {"session_id": "c1", "cwd": "/work/kiln"}
        run = self.run_host
        run("copilot", "prompt", {**base, "prompt": LEDGER}, env, net)
        run("copilot", "tool", {**base, "tool_name": "view", "tool_input": {"path": "a"}}, env)
        self.assertEqual(
            run("copilot", "prompt", {**base, "prompt": "change the scheduler rate limit"}, env, net),
            "",
        )
        out = run("copilot", "tool", {**base, "tool_name": "view", "tool_input": {"path": "a"}}, env)
        self.assertIn("default is 50", json.loads(out)["additionalContext"])

    def run_host(self, host, action, payload, env, net=refuse) -> str:
        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(hooks.urllib.request, "urlopen", net),
            redirect_stdout(out),
        ):
            hooks.main(["alexein_hooks.py", "run", host, action, str(self.data)])
        return out.getvalue().strip()


EDIT = {
    "session_id": "s1",
    "cwd": "/work/kiln",
    "tool_name": "Edit",
    "tool_input": {
        "file_path": "/work/kiln/src/scheduler/rateLimit.ts",
        "old_string": "const SECRET_OLD_BODY = 1",
        "new_string": "export const DEFAULT_RATE_LIMIT = 100;\nconst perTenantLimit = DEFAULT_RATE_LIMIT;",
    },
}


class BeforeEdit(Flags):
    def test_the_first_edit_of_a_file_brings_what_memory_holds_about_it(self) -> None:
        net = Network(Response(200, RATE))
        out = self.run_hook("pre-tool", EDIT, net=net)
        shown = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(shown["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", shown)  # never decides the edit
        context = shown["additionalContext"]
        self.assertTrue(context.startswith(hooks.EDIT_HEAD.format(path="src/scheduler/rateLimit.ts")))
        self.assertIn("default is 50 since November", context)
        self.assertNotIn("Lunch", context)
        self.assertNotIn("tabs", context)
        query = net.requests[0]["body"]["query"]
        self.assertIn("rateLimit", query)
        self.assertIn("src/scheduler/rateLimit.ts", query)
        self.assertIn("DEFAULT_RATE_LIMIT", query)
        self.assertNotIn("SECRET_OLD_BODY", query)  # names the edit writes, never what it replaced
        self.assertTrue(net.requests[0]["body"]["fast"])
        # The same file again this session: no search (`refuse`).
        self.assertEqual(self.run_hook("pre-tool", EDIT), "")

    def test_another_file_is_looked_up_and_nothing_relevant_adds_nothing(self) -> None:
        net = Network(Response(200, {"answerable": None, "memories": [RATE["memories"][2]]}))
        other = {
            **EDIT,
            "tool_name": "Write",
            "tool_input": {"file_path": "/work/kiln/src/billing/invoice.py", "content": "x = 1"},
        }
        self.assertEqual(self.run_hook("pre-tool", other, net=net), "")
        self.assertEqual(search_count(net), 1)

    def test_off_without_the_flag_or_a_key_or_while_paused(self) -> None:
        env = {k: v for k, v in self.env.items() if k != "ALEXEIN_RECALL_BEFORE_EDIT"}
        self.assertEqual(self.run_hook("pre-tool", EDIT, env=env), "")
        env = {k: v for k, v in self.env.items() if k != "CLAUDE_PLUGIN_OPTION_API_KEY"}
        self.assertEqual(self.run_hook("pre-tool", EDIT, env=env), "")
        self.command("pause")
        self.assertEqual(self.run_hook("pre-tool", EDIT), "")

    def test_a_slow_memory_never_holds_the_edit(self) -> None:
        def slow():
            time.sleep(1.0)
            return Response(200, RATE)

        with mock.patch.object(hooks, "RECALL_BUDGET", 0.2):
            started = time.monotonic()
            self.assertEqual(self.run_hook("pre-tool", EDIT, net=Network(slow)), "")
            self.assertLess(time.monotonic() - started, 0.8)
        self.assertEqual(self.run_hook("pre-tool", EDIT, net=Network(urllib.error.URLError("x"))), "")

    def test_the_hook_is_on_the_edit_tools_only(self) -> None:
        import re

        config = json.loads((SCRIPT.parents[1] / "hooks" / "hooks.json").read_text())["hooks"]
        matcher = re.compile(config["PreToolUse"][0]["matcher"])
        for name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            self.assertTrue(matcher.fullmatch(name), name)
        for name in ("Bash", "Read", "mcp__alexein__memory_search"):
            self.assertFalse(matcher.fullmatch(name), name)

    def test_cursor_and_copilot_take_it_with_the_edits_result(self) -> None:
        env = {
            "ALEXEIN_API_KEY": "tm_testkey_0123456789abcdef",
            "ALEXEIN_API_URL": "https://api.example.test",
            **FLAGS,
        }
        runner = EveryPrompt.run_host
        cursor = {
            "conversation_id": "k1",
            "tool_name": "Write",
            "tool_input": {"file_path": "/work/kiln/src/scheduler/rateLimit.ts", "content": "x"},
            "cwd": "/work/kiln",
        }
        out = runner(self, "cursor", "tool", cursor, env, Network(Response(200, RATE)))
        self.assertIn("default is 50", json.loads(out)["additional_context"])
        copilot = {
            "session_id": "c1",
            "cwd": "/work/kiln",
            "tool_name": "edit",
            "tool_input": {"path": "/work/kiln/src/scheduler/rateLimit.ts", "new_str": "y"},
        }
        out = runner(self, "copilot", "tool", copilot, env, Network(Response(200, RATE)))
        self.assertIn("default is 50", json.loads(out)["additionalContext"])

    def test_antigravity_takes_it_at_its_next_model_call(self) -> None:
        env = {
            "ALEXEIN_API_KEY": "tm_testkey_0123456789abcdef",
            "ALEXEIN_API_URL": "https://api.example.test",
            **FLAGS,
        }
        runner = EveryPrompt.run_host
        base = {"conversationId": "a1", "workspacePaths": ["/work/kiln"]}
        turn = {**base, "lastUserInput": "<USER_REQUEST>\nok go\n</USER_REQUEST>"}
        runner(self, "antigravity", "prompt", turn, env)
        call = {
            "name": "replace_file_content",
            "args": {"TargetFile": "/work/kiln/src/scheduler/rateLimit.ts"},
        }
        out = runner(
            self, "antigravity", "tool", {**base, "toolCall": call}, env, Network(Response(200, RATE))
        )
        self.assertEqual(out, "")
        out = json.loads(runner(self, "antigravity", "prompt", turn, env))
        self.assertIn("default is 50", out["injectSteps"][0]["systemMessage"]["systemMessage"])
        self.assertEqual(runner(self, "antigravity", "prompt", turn, env), "")  # once


ASKS = "I can't tell from the code which one is used.\n\nWhich queue backend is actually running in production now?"
TASK = "Make the job runner use the production queue backend for retries"
FOUND = {
    "answerable": None,
    "memories": [
        {
            "type": "fact",
            "text": "Production runs the Redis queue backend since the October migration.",
            "ref": "f7",
            "date": "2026-10-20",
        }
    ],
}


class Asked(Base):
    def test_questions_for_a_fact_are_caught(self) -> None:
        for reply, question in (
            (ASKS, "Which queue backend is actually running in production now?"),
            (
                "Done reading.\n\nLet me know your team's deployment freeze policy.",
                "Let me know your team's deployment freeze policy.",
            ),
            (
                "Two options here. Which retry backoff does the team use?\n\n- linear\n- exponential",
                "Which retry backoff does the team use?",
            ),
            ("Should the default batch size be 50 or 100?", "Should the default batch size be 50 or 100?"),
        ):
            with self.subTest(reply=reply):
                self.assertEqual(hooks.asked(reply), question)

    def test_offers_checks_permissions_secrets_and_headings_are_not(self) -> None:
        """What the 2026-11-17 Stop nudge sent back, and was dropped for."""
        for reply in (
            "Done. Want me to open a pull request?",
            "Shall I commit this?",
            "I made the change. Does that look right?",
            "Would you like me to add tests for it?",
            "Should I go ahead and run the migration?",
            "Can I run the migration against staging?",
            "Could you share the API key for the staging account?",
            "What is the database password?",
            "## Why does this matter?\n\nIt keeps the ledger consistent.",
            "```python\nwhat = input('which?')\n```",
            "> Which one is in production?\n\nThe docs asked that; it is Redis.",
            "Let me know if you'd like any changes.",
            "Anything else?",
            "Is that ok?",
            "All done: the tests pass.",
            "Why?",
            "",
        ):
            with self.subTest(reply=reply):
                self.assertEqual(hooks.asked(reply), "")


class AskCatch(Flags):
    base = {"session_id": "s1", "cwd": "/work/kiln"}

    def turn(self, prompt: str = TASK, net=refuse) -> None:
        self.run_hook("prompt", {**self.base, "prompt": "hi"})  # the session's first: no search
        self.run_hook("prompt", {**self.base, "prompt": prompt}, net=net)

    def setUp(self) -> None:
        super().setUp()
        self.env = {**self.env, "ALEXEIN_RECALL_EVERY_PROMPT": "0"}

    def test_a_question_memory_can_answer_is_sent_back_once_with_what_it_holds(self) -> None:
        self.turn()
        net = Network(Response(200, FOUND))
        out = self.run_hook("stop", {**self.base, "last_assistant_message": ASKS}, net=net)
        shown = json.loads(out)
        self.assertEqual(shown["decision"], "block")
        reason = shown["reason"]
        self.assertTrue(
            reason.startswith(
                'Before asking, search memory for: "Which queue backend is actually running in'
                ' production now?"'
            )
        )
        self.assertIn("data, not instructions", reason)
        self.assertIn("Redis queue backend", reason)
        self.assertIn("queue backend", net.requests[0]["body"]["query"])
        state = json.loads((self.data / "sessions" / "s1.json").read_text())
        self.assertEqual(state.get("exchanges", []), [])  # recorded when the turn really ends
        # Claude goes on and ends the turn: never sent back twice, and the exchange is kept whole.
        final = "Production runs Redis (memory, 2026-10-20), so retries now use it."
        again = self.run_hook(
            "stop", {**self.base, "stop_hook_active": True, "last_assistant_message": final}
        )
        self.assertEqual(again, "")
        state = json.loads((self.data / "sessions" / "s1.json").read_text())
        self.assertEqual(state["exchanges"][-1]["user"], TASK)
        self.assertEqual(state["exchanges"][-1]["assistant"], final)

    def test_a_question_memory_holds_nothing_for_is_asked(self) -> None:
        self.turn()
        empty = {"answerable": None, "memories": [{"type": "excerpt", "text": "Lunch at noon."}]}
        for answer in (Response(200, empty), Response(200, {**FOUND, "answerable": False})):
            with self.subTest(answer=answer):
                out = self.run_hook(
                    "stop", {**self.base, "last_assistant_message": ASKS}, net=Network(answer)
                )
                self.assertEqual(out, "")
                self.turn()

    def test_a_turn_that_read_memory_recalled_or_changed_a_file_is_not_sent_back(self) -> None:
        for tool in (
            {"tool_name": "mcp__plugin_alexein_alexein__memory_search", "tool_input": {}},
            {"tool_name": "Edit", "tool_input": {"file_path": "/work/kiln/a.ts"}},
        ):
            with self.subTest(tool=tool["tool_name"]):
                self.turn()
                self.run_hook("tool", {**self.base, **tool})
                self.assertEqual(
                    self.run_hook("stop", {**self.base, "last_assistant_message": ASKS}), ""
                )
        # Recalled: the session's first prompt had its search.
        net = Network(Response(200, SEARCH))
        self.run_hook("prompt", {"session_id": "s2", "prompt": TASK}, net=net)
        self.assertEqual(
            self.run_hook("stop", {"session_id": "s2", "last_assistant_message": ASKS}), ""
        )

    def test_a_failed_lookup_still_asks_for_a_search(self) -> None:
        self.turn()
        out = self.run_hook(
            "stop",
            {**self.base, "last_assistant_message": ASKS},
            net=Network(urllib.error.URLError("offline")),
        )
        reason = json.loads(out)["reason"]
        self.assertEqual(
            reason,
            hooks.CATCH_PLAIN.format(
                question="Which queue backend is actually running in production now?"
            ),
        )

    def test_at_most_twice_a_session(self) -> None:
        for n in range(3):
            self.turn(f"{TASK} {n}")
            out = self.run_hook(
                "stop",
                {**self.base, "last_assistant_message": ASKS},
                net=Network(Response(200, FOUND)) if n < 2 else refuse,
            )
            self.assertEqual(bool(out), n < 2)
            self.run_hook("stop", {**self.base, "stop_hook_active": True, "last_assistant_message": "ok"})

    def test_without_a_key_only_where_the_memory_tools_worked(self) -> None:
        env = {k: v for k, v in self.env.items() if k != "CLAUDE_PLUGIN_OPTION_API_KEY"}
        base = self.base
        self.run_hook("prompt", {**base, "prompt": TASK}, env=env)
        self.assertEqual(self.run_hook("stop", {**base, "last_assistant_message": ASKS}, env=env), "")
        # A memory tool worked in an earlier turn: the agent is asked to search, as it can.
        self.run_hook("prompt", {**base, "prompt": "what did we decide about retries"}, env=env)
        self.run_hook("tool", {**base, "tool_name": "mcp__alexein__memory_search"}, env=env)
        self.run_hook("stop", {**base, "last_assistant_message": "Linear backoff."}, env=env)
        self.run_hook("prompt", {**base, "prompt": TASK + " now"}, env=env)
        out = self.run_hook("stop", {**base, "last_assistant_message": ASKS}, env=env)
        self.assertEqual(json.loads(out)["decision"], "block")

    def test_off_by_default(self) -> None:
        env = {k: v for k, v in self.env.items() if k not in FLAGS}
        self.run_hook("prompt", {**self.base, "prompt": TASK}, env=env)
        self.assertEqual(
            self.run_hook("stop", {**self.base, "last_assistant_message": ASKS}, env=env), ""
        )

    def test_cursor_follows_up_and_antigravity_continues(self) -> None:
        env = {
            "ALEXEIN_API_KEY": "tm_testkey_0123456789abcdef",
            "ALEXEIN_API_URL": "https://api.example.test",
            "ALEXEIN_ASK_CATCH": "1",
        }
        run = EveryPrompt.run_host
        base = {"conversation_id": "k1"}
        run(self, "cursor", "prompt", {**base, "prompt": "hi"}, env)
        run(self, "cursor", "prompt", {**base, "prompt": TASK}, env)
        run(self, "cursor", "reply", {**base, "text": ASKS}, env)
        out = run(self, "cursor", "stop", {**base, "status": "completed"}, env, Network(Response(200, FOUND)))
        self.assertIn("Before asking", json.loads(out)["followup_message"])
        # Its follow-up comes back as a new loop: never sent back again.
        self.assertEqual(run(self, "cursor", "stop", {**base, "loop_count": 1}, env), "")

        transcript = self.tmp / "ag.jsonl"
        transcript.write_text(
            json.dumps({"type": "USER_INPUT", "content": f"<USER_REQUEST>{TASK}</USER_REQUEST>"})
            + "\n"
            + json.dumps({"type": "PLANNER_RESPONSE", "content": ASKS})
            + "\n"
        )
        base = {"conversationId": "a1", "transcriptPath": str(transcript)}
        turn = {**base, "lastUserInput": f"<USER_REQUEST>{TASK}</USER_REQUEST>"}
        run(self, "antigravity", "prompt", {**base, "lastUserInput": "hi"}, env)
        run(self, "antigravity", "prompt", turn, env)
        out = json.loads(run(self, "antigravity", "stop", base, env, Network(Response(200, FOUND))))
        self.assertEqual(out["decision"], "continue")
        run(self, "antigravity", "prompt", turn, env)  # its next model call, the same turn
        self.assertEqual(run(self, "antigravity", "stop", base, env), "")


class InTheEnvironment(Base):
    """The flags and the key set where `claude -p` is started reach a real hook process."""

    def test_a_real_process_reads_the_flags_and_the_key_from_the_environment(self) -> None:
        asked: list[dict] = []

        class API(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                return None

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                asked.append({"path": self.path, "auth": self.headers["Authorization"], **body})
                raw = json.dumps(RATE if len(asked) > 1 else SEARCH).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = http.server.HTTPServer(("127.0.0.1", 0), API)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        env = {
            "PATH": os.environ.get("PATH", ""),
            "CLAUDE_PLUGIN_DATA": str(self.data),
            "ALEXEIN_API_KEY": "tm_envkey_0123456789abcdef",
            "ALEXEIN_API_URL": f"http://127.0.0.1:{server.server_address[1]}",
            "ALEXEIN_RECALL_EVERY_PROMPT": "1",
        }

        def run(said: str) -> str:
            return subprocess.run(
                [sys.executable, str(SCRIPT), "prompt"],
                input=json.dumps({"session_id": "p1", "prompt": said}),
                capture_output=True,
                text=True,
                env=env,
                timeout=20,
            ).stdout

        self.assertIn(hooks.RECALL_HEAD, run(LEDGER))
        self.assertIn("default is 50", run("Now change the scheduler rate limit default for tenants"))
        self.assertEqual([a["path"] for a in asked], ["/v2/memory/search"] * 2)
        self.assertEqual(asked[0]["auth"], "Bearer tm_envkey_0123456789abcdef")


# ---------------------------------------------------------------------------------------------
# Sign-in (C1), against a fake console


class Console:
    """The console's side of the desktop sign-in, as notes/desktop-sign-in.md has it, with C1's
    `client`. `allow` is what the person does on the card."""

    def __init__(self, allow: str = "allow", token_status: int = 200) -> None:
        self.allow, self.token_status = allow, token_status
        self.asked: dict[str, str] = {}
        self.exchanged: dict = {}
        self.codes: dict[str, dict] = {}
        console = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                return None

            def reply(self, status: int, body: dict | None = None, location: str = "") -> None:
                raw = json.dumps(body or {}).encode()
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802
                url = urllib.parse.urlsplit(self.path)
                if url.path == "/desktop/signed-in":
                    console.finished = url.query
                    return self.reply(200, {"page": "signed in"})
                q = dict(urllib.parse.parse_qsl(url.query))
                console.asked = q
                answer = {"state": q["state"]}
                if console.allow == "cancel":
                    answer["error"] = "access_denied"
                else:
                    code = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
                    console.codes[code] = q
                    answer["code"] = code
                return self.reply(302, location=f"{q['redirect_uri']}?{urllib.parse.urlencode(answer)}")

            def do_POST(self):  # noqa: N802
                if self.path == "/api/desktop/revoke":
                    return self.reply(200, {"revoked": True})
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                console.exchanged = body
                if console.token_status == 307:
                    return self.reply(307, location="https://elsewhere.example/steal")
                granted = console.codes.pop(body.get("code"), None)
                challenge = base64.urlsafe_b64encode(
                    hashlib.sha256(body.get("code_verifier", "").encode()).digest()
                ).rstrip(b"=").decode()
                if (
                    console.token_status != 200
                    or not granted
                    or granted["code_challenge"] != challenge
                    or granted["redirect_uri"] != body.get("redirect_uri")
                    or granted.get("client", "desktop") != body.get("client", "desktop")
                ):
                    return self.reply(400, {"error": "invalid_grant"})
                return self.reply(
                    200, {"key": "tm_minted_0123456789abcdef", "api_url": "https://api.example.test"}
                )

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


def browser(url: str) -> bool:
    """The person's browser: follows the console's redirects to the listener and back."""

    def visit() -> None:
        try:
            urllib.request.urlopen(url, timeout=10).read()
        except (OSError, urllib.error.URLError):
            pass

    threading.Thread(target=visit, daemon=True).start()
    return True


class SignIn(Base):
    def setUp(self) -> None:
        super().setUp()
        self.env = {
            "CLAUDE_PLUGIN_DATA": str(self.data),
            "CLAUDE_CODE_SESSION_ATTENDED": "1",
        }

    def console(self, **kwargs) -> Console:
        console = Console(**kwargs)
        self.addCleanup(console.close)
        self.env["ALEXEIN_CONSOLE_URL"] = console.url
        return console

    def helper(self, host: str = "claude-code", wait: float = 10.0) -> str:
        cfg = hooks.Config(env=self.env, data=str(self.data), host=host)
        cfg.data.mkdir(parents=True, exist_ok=True)
        return hooks.sign_in_helper(cfg, opener=browser, wait=wait)

    def test_allow_keeps_a_key_the_hooks_then_use(self) -> None:
        console = self.console()
        self.assertEqual(self.helper(), "signed in")
        asked = console.asked
        self.assertEqual(asked["client"], "claude-code")
        self.assertEqual(console.exchanged["client"], "claude-code")
        deadline = time.monotonic() + 5
        while not getattr(console, "finished", None) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(console.finished, "client=claude-code")
        self.assertEqual(asked["code_challenge_method"], "S256")
        self.assertRegex(asked["redirect_uri"], r"^http://127\.0\.0\.1:\d+/callback$")
        self.assertEqual(len(asked["code_challenge"]), 43)
        self.assertTrue(asked["device"])
        self.assertNotIn(console.exchanged["code_verifier"], json.dumps(asked))
        kept = self.data / hooks.SIGNED_IN
        self.assertEqual(kept.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("tm_minted", (self.data / hooks.SIGN_IN_STATE).read_text())
        cfg = hooks.Config(env=self.env)
        self.assertEqual((cfg.key, cfg.key_source, cfg.api), ("tm_minted_0123456789abcdef", "sign-in", "https://api.example.test"))
        # The next prompt searches with it, with no restart; status says so.
        net = Network(Response(200, SEARCH))
        self.run_hook("prompt", {"session_id": "s1", "prompt": LEDGER}, net=net)
        self.assertEqual(net.requests[0]["headers"]["authorization"], "Bearer tm_minted_0123456789abcdef")
        shown = self.command("status")
        self.assertIn("API key: signed in", shown)
        self.assertIn("Automatic recall: on", shown)
        self.assertNotIn("tm_minted", shown)

    def test_each_agent_names_itself(self) -> None:
        for host in ("cursor", "copilot", "antigravity"):
            with self.subTest(host=host):
                console = self.console()
                (self.data / hooks.SIGNED_IN).unlink(missing_ok=True)
                self.assertEqual(self.helper(host), "signed in")
                self.assertEqual(console.asked["client"], host)
        self.assertEqual(hooks.HOSTS["gemini"].client, "")

    def test_cancel_keeps_nothing_and_waits_a_week(self) -> None:
        self.console(allow="cancel")
        self.assertEqual(self.helper(), "cancelled")
        self.assertFalse((self.data / hooks.SIGNED_IN).exists())
        tried = json.loads((self.data / hooks.SIGN_IN_STATE).read_text())
        three_days = time.time() - 3 * 86400
        tried["opened_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(three_days))
        (self.data / hooks.SIGN_IN_STATE).write_text(json.dumps(tried))
        self.run_hook("session-start", {"session_id": "s1", "source": "startup"})
        self.assertEqual(self.helpers, [])
        self.assertIn("Last sign-in:", self.command("status"))
        self.assertIn("cancelled", self.command("status"))

    def test_a_refused_exchange_or_a_redirect_keeps_nothing(self) -> None:
        for status in (400, 307):
            with self.subTest(status=status):
                self.console(token_status=status)
                self.assertEqual(self.helper(), "failed")
                self.assertFalse((self.data / hooks.SIGNED_IN).exists())

    def test_only_this_sign_ins_answer_is_taken(self) -> None:
        self.console(allow="cancel")
        found = {}

        def stray_then_browser(url: str) -> bool:
            """Another page finds the port first; then the person's browser comes."""
            redirect = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["redirect_uri"]

            def visits() -> None:
                for path in ("?code=x&state=wrong-state-0000000", "/../other?code=x"):
                    try:
                        urllib.request.urlopen(redirect + path, timeout=5)
                    except urllib.error.HTTPError as refused:
                        found[path] = refused.code
                browser(url)

            threading.Thread(target=visits, daemon=True).start()
            return True

        cfg = hooks.Config(env=self.env, data=str(self.data))
        self.assertEqual(hooks.sign_in_helper(cfg, opener=stray_then_browser, wait=10), "cancelled")
        self.assertEqual(sorted(found.values()), [400, 404])

    def test_nobody_there_or_no_browser(self) -> None:
        self.console()
        cfg = hooks.Config(env=self.env, data=str(self.data))
        self.assertEqual(hooks.sign_in_helper(cfg, opener=lambda url: False, wait=0.3), "failed")
        self.assertEqual(hooks.sign_in_helper(cfg, opener=lambda url: True, wait=0.3), "not finished")
        self.assertFalse((self.data / hooks.SIGNED_IN).exists())

    def test_one_at_a_time(self) -> None:
        self.console()
        holder = hooks.try_lock(self.data / "sign-in.lock")
        self.addCleanup(holder.close)
        self.assertEqual(self.helper(), "busy")
        self.run_hook("session-start", {"session_id": "s1", "source": "startup"})
        self.assertEqual(self.helpers, [])
        self.assertIn("already open", self.command("sign-in"))

    def test_session_start_opens_it_only_where_a_person_can_see_it(self) -> None:
        self.console()
        for env, payload in (
            ({"CLAUDE_CODE_SESSION_ATTENDED": "0"}, {"source": "startup"}),  # claude -p
            ({"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, {"source": "startup"}),
            ({"CI": "true"}, {"source": "startup"}),
            ({"SSH_CONNECTION": "1 2 3 4"}, {"source": "startup"}),
            ({"ALEXEIN_SIGN_IN": "0"}, {"source": "startup"}),
            ({}, {"source": "resume"}),
            ({}, {"source": "compact"}),
        ):
            with self.subTest(env=env, payload=payload):
                out = self.run_hook("session-start", {"session_id": "s1", **payload}, env={**self.env, **env})
                self.assertEqual(self.helpers, [])
                self.assertTrue(out.startswith(hooks.LINES + "\n- Automatic recall and saving are off"))
        with mock.patch.object(hooks.sys, "platform", "darwin"):
            self.run_hook("session-start", {"session_id": "s1", "source": "startup"})
        self.assertEqual(self.helpers, ["sign-in-helper"])

    def test_a_revoked_signed_in_key_signs_in_again_a_key_set_by_hand_does_not(self) -> None:
        self.console()
        self.assertEqual(self.helper(), "signed in")
        self.run_hook(
            "prompt",
            {"session_id": "s1", "prompt": LEDGER},
            net=Network(Response(401, {"detail": "revoked"})),
        )
        self.assertIn("the signed-in key was revoked", self.command("status"))
        with mock.patch.object(hooks.sys, "platform", "darwin"):
            self.run_hook("session-start", {"session_id": "s2", "source": "startup"})
        self.assertEqual(self.helpers, ["sign-in-helper"])
        by_hand = {**self.env, "CLAUDE_PLUGIN_OPTION_API_KEY": "tm_byhand_0123456789abcdef"}
        (self.data / hooks.SIGN_IN_STATE).unlink()
        self.run_hook("session-start", {"session_id": "s3", "source": "startup"}, env=by_hand)
        self.assertEqual(self.helpers, ["sign-in-helper"])

    def test_the_command_starts_it_and_a_key_set_by_hand_wins(self) -> None:
        self.console()
        self.assertIn("browser is opening", self.command("sign-in"))
        self.assertEqual(self.helpers, ["sign-in-helper"])
        self.env["CLAUDE_PLUGIN_OPTION_API_KEY"] = "tm_byhand_0123456789abcdef"
        self.assertIn("already set", self.command("sign-in"))
        self.assertEqual(self.helpers, ["sign-in-helper"])

    def test_the_console_beside_the_api(self) -> None:
        cfg = hooks.Config(env={})
        self.assertEqual(hooks.console_url(cfg), "https://memory.alexein.ai")
        cfg = hooks.Config(env={"ALEXEIN_API_KEY": "k", "ALEXEIN_API_URL": "http://localhost:8210"})
        self.assertEqual(hooks.console_url(cfg), "http://localhost:3003")
        cfg = hooks.Config(env={"ALEXEIN_CONSOLE_URL": "http://console.example"})
        self.assertEqual(hooks.console_url(cfg), "")  # plain http only to this computer

    def test_a_kept_key_for_a_bare_http_api_is_not_used(self) -> None:
        self.data.mkdir(parents=True)
        (self.data / hooks.SIGNED_IN).write_text(
            json.dumps({"key": "tm_x_0123456789abcdef", "api_url": "http://api.example"})
        )
        self.assertEqual(hooks.Config(env=self.env).key, "")

    def test_the_skills_run_the_commands(self) -> None:
        skills = SCRIPT.parents[1] / "skills"
        for name in ("sign-in", "status"):
            text = (skills / name / "SKILL.md").read_text()
            self.assertIn(f'alexein_hooks.py" {name} "${{CLAUDE_PLUGIN_DATA}}"', text)
            self.assertIn("disable-model-invocation: true", text)
