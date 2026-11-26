"""The same hooks in the other agents: each one's JSON on stdin, run as `run <host> <action>`, the
key in ALEXEIN_API_KEY. What each agent sends follows its own docs (HOSTS in the script); the
transcripts follow files those agents wrote.
"""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from unittest import mock

from test_hooks import SEARCH, Base, Network, Response, hooks, refuse

KEY = "tm_testkey_0123456789abcdef"
QUESTION = "How did we set up the billing ledger again?"


class Hosts(Base):
    def setUp(self) -> None:
        super().setUp()
        self.env = {
            "ALEXEIN_API_KEY": KEY,
            "ALEXEIN_API_URL": "https://api.example.test",
            "HOME": str(self.tmp),
        }
        self.settings = mock.patch.object(hooks, "SETTINGS", self.tmp / "hooks.json")
        self.settings.start()
        self.addCleanup(self.settings.stop)

    def run_as(
        self, host: str, action: str, payload: dict | None = None, net=refuse, env: dict | None = None
    ) -> str:
        environment = {**self.env, **(env or {})}
        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch("sys.stdin", io.StringIO(json.dumps(payload or {}))),
            mock.patch.object(hooks.urllib.request, "urlopen", net),
            redirect_stdout(out),
        ):
            code = hooks.main(["alexein_hooks.py", "run", host, action, str(self.data)])
        self.assertEqual(code, 0)
        return out.getvalue().strip()

    def sent_items(self, net: Network) -> list[dict]:
        adds = [r for r in net.requests if r["url"].endswith("/v2/memory/add")]
        self.assertEqual(len(adds), 1)
        return adds[0]["body"]["items"]


class Copilot(Hosts):
    """Copilot CLI and VS Code, PascalCase events: Claude Code's fields, top-level output."""

    def test_session_start_gives_the_lines(self) -> None:
        out = json.loads(self.run_as("copilot", "session-start", {"session_id": "c1"}))
        self.assertEqual(out, {"additionalContext": hooks.LINES})

    def test_first_search_goes_with_the_next_tool_result(self) -> None:
        net = Network(Response(200, SEARCH))
        base = {"session_id": "c1", "cwd": "/work/billing"}
        self.assertEqual(self.run_as("copilot", "prompt", {**base, "prompt": QUESTION}, net), "")
        self.assertEqual(len(net.requests), 1)
        self.assertEqual(net.requests[0]["headers"]["user-agent"], f"alexein-copilot/{hooks.VERSION}")
        tool = {**base, "tool_name": "view", "tool_input": {"path": "a.py"}}
        out = json.loads(self.run_as("copilot", "tool", tool))
        self.assertIn("Prefers tabs over spaces.", out["additionalContext"])
        self.assertEqual(self.run_as("copilot", "tool", tool), "")  # once

    def test_exchange_from_its_transcript(self) -> None:
        transcript = self.tmp / "events.jsonl"
        transcript.write_text(
            "\n".join(
                json.dumps(e)
                for e in (
                    {"type": "user.message", "data": {"content": "what is my coffee"}},
                    {"type": "assistant.message", "data": {"content": "", "toolRequests": [{}]}},
                    {"type": "assistant.message", "data": {"content": "An oat flat white."}},
                )
            )
        )
        net = Network(Response(202, {}))
        for n in range(5):
            base = {"session_id": "c1", "cwd": "/work/billing"}
            self.run_as("copilot", "prompt", {**base, "prompt": f"q{n}"}, net)
            self.run_as(
                "copilot",
                "tool",
                {**base, "tool_name": "Bash", "tool_input": {"command": "make test"}},
                net,
            )
            self.run_as("copilot", "stop", {**base, "transcript_path": str(transcript)}, net)
        items = self.sent_items(net)
        self.assertEqual(items[0]["session"], "copilot-c1")
        self.assertEqual(items[0]["text"], "q0")
        self.assertEqual(items[1]["author"], {"name": "GitHub Copilot"})
        self.assertEqual(items[1]["text"], "Commands run: `make test`.\n\nAn oat flat white.")
        self.assertEqual(items[0]["metadata"], {"app": "copilot", "project": "billing"})
        add = [r for r in net.requests if r["url"].endswith("/add")][0]
        self.assertEqual(add["headers"]["idempotency-key"], "copilot-c1-1-5")

    def test_no_key_gives_the_lines_and_touches_nothing(self) -> None:
        out = self.run_as("copilot", "session-start", {"session_id": "c1"}, env={"ALEXEIN_API_KEY": ""})
        context = json.loads(out)["additionalContext"]
        self.assertTrue(context.startswith(hooks.LINES + "\n- Automatic recall and saving are off"))
        self.assertIn("run copilot sign-in", context)
        self.assertEqual(self.run_as("copilot", "prompt", {"prompt": QUESTION}, env={"ALEXEIN_API_KEY": ""}), "")


class Cursor(Hosts):
    """Cursor: conversation_id, a prompt hook that only says go on, the reply in its own hook."""

    def test_prompt_always_goes_on(self) -> None:
        out = self.run_as("cursor", "prompt", {"conversation_id": "k1", "prompt": "hi"})
        self.assertEqual(json.loads(out), {"continue": True})
        offline = self.run_as("cursor", "prompt", {"prompt": "hi"}, env={"ALEXEIN_API_KEY": ""})
        self.assertEqual(json.loads(offline), {"continue": True})

    def test_session_start_prints_nothing_its_rule_carries_the_lines(self) -> None:
        self.assertEqual(self.run_as("cursor", "session-start", {"conversation_id": "k1"}), "")

    def test_search_then_tool_then_reply_then_stop(self) -> None:
        net = Network(Response(200, SEARCH), Response(202, {}))
        base = {"conversation_id": "k1", "workspace_roots": ["/work/billing"]}
        self.run_as("cursor", "prompt", {**base, "prompt": QUESTION}, net)
        tool = {
            **base,
            "tool_name": "Write",
            "tool_input": {"file_path": "/work/billing/ledger.py"},
            "cwd": "/work/billing",
        }
        out = json.loads(self.run_as("cursor", "tool", tool, net))
        self.assertIn("Works on the Lisbon billing service.", out["additional_context"])
        self.run_as("cursor", "reply", {**base, "text": "Postgres, in ledger.py."}, net)
        self.run_as("cursor", "stop", {**base, "status": "completed"}, net)
        state = json.loads((self.data / "sessions" / "k1.json").read_text())
        exchange = state["exchanges"][0]
        self.assertEqual(exchange["user"], QUESTION)
        self.assertEqual(exchange["assistant"], "Postgres, in ledger.py.")
        self.assertEqual(exchange["work"], "Files changed: ledger.py.")
        self.assertNotIn("reply", state)

    def test_shell_tool_is_a_command(self) -> None:
        base = {"conversation_id": "k1"}
        self.run_as("cursor", "prompt", {**base, "prompt": "run it"})
        self.run_as("cursor", "tool", {**base, "tool_name": "Shell", "tool_input": {"command": "pnpm test"}})
        work = (self.data / "sessions" / "k1.work.jsonl").read_text()
        self.assertIn("pnpm test", work)


class Codex(Hosts):
    """Codex: Claude Code's fields and output; apply_patch names its files in the patch."""

    def test_prompt_context_as_claude_code_takes_it(self) -> None:
        net = Network(Response(200, SEARCH))
        out = self.run_as("codex", "prompt", {"session_id": "x1", "prompt": QUESTION}, net)
        shown = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(shown["hookEventName"], "UserPromptSubmit")
        self.assertIn("Prefers tabs", shown["additionalContext"])
        self.assertEqual(self.run_as("codex", "session-start", {"session_id": "x1"}), hooks.LINES)

    def test_apply_patch_files(self) -> None:
        base = {"session_id": "x1", "cwd": "/work/billing"}
        self.run_as("codex", "prompt", {**base, "prompt": "fix it"})
        patch = (
            "*** Begin Patch\n*** Update File: src/ledger.py\n@@\n-a\n+b\n"
            "*** Add File: /work/billing/tests/test_ledger.py\n+x\n*** End Patch"
        )
        self.run_as("codex", "tool", {**base, "tool_name": "apply_patch", "tool_input": {"command": patch}})
        self.run_as("codex", "stop", {**base, "last_assistant_message": "Done."})
        state = json.loads((self.data / "sessions" / "x1.json").read_text())
        self.assertEqual(
            state["exchanges"][0]["work"], "Files changed: src/ledger.py, tests/test_ledger.py."
        )


class Gemini(Hosts):
    """Gemini CLI: JSON only on stdout; AfterAgent carries the prompt and the reply."""

    def test_outputs_are_json(self) -> None:
        start = json.loads(self.run_as("gemini", "session-start", {"session_id": "g1"}))
        self.assertEqual(start["hookSpecificOutput"]["hookEventName"], "SessionStart")
        net = Network(Response(200, SEARCH))
        out = json.loads(self.run_as("gemini", "prompt", {"session_id": "g1", "prompt": QUESTION}, net))
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "BeforeAgent")

    def test_after_agent_is_the_exchange(self) -> None:
        base = {"session_id": "g1", "cwd": "/work/billing"}
        self.run_as("gemini", "prompt", {**base, "prompt": "short"})
        self.run_as(
            "gemini",
            "tool",
            {**base, "tool_name": "run_shell_command", "tool_input": {"command": "npm test"}},
        )
        self.run_as("gemini", "stop", {**base, "prompt": "short", "prompt_response": "All green."})
        exchange = json.loads((self.data / "sessions" / "g1.json").read_text())["exchanges"][0]
        self.assertEqual(exchange["assistant"], "All green.")
        self.assertEqual(exchange["work"], "Commands run: `npm test`.")


class Antigravity(Hosts):
    """Antigravity: camelCase, its request wrapped in tags, a prompt hook on every invocation, the
    reply only in its transcript."""

    def test_injects_steps_and_searches_once_per_turn(self) -> None:
        net = Network(Response(200, SEARCH))
        payload = {
            "conversationId": "a1",
            "workspacePaths": ["/work/billing"],
            "lastUserInput": f"<USER_REQUEST>\n{QUESTION}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>t</ADDITIONAL_METADATA>",
        }
        out = json.loads(self.run_as("antigravity", "prompt", payload, net))
        message = out["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("Prefers tabs", message)
        self.assertEqual(net.requests[0]["body"]["query"], QUESTION)
        # The same input again within the turn: no second exchange, no second search.
        self.assertEqual(self.run_as("antigravity", "prompt", payload), "")

    def test_tools_and_transcript(self) -> None:
        transcript = self.tmp / "transcript.jsonl"
        transcript.write_text(
            "\n".join(
                json.dumps(e)
                for e in (
                    {"type": "USER_INPUT", "content": "<USER_REQUEST>\nmake the ledger\n</USER_REQUEST>"},
                    {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "write_to_file"}]},
                    {"type": "PLANNER_RESPONSE", "content": "Made **ledger.py**."},
                )
            )
        )
        base = {"conversationId": "a1", "workspacePaths": ["/work/billing"]}
        self.run_as("antigravity", "prompt", {**base, "lastUserInput": "make the ledger"})
        self.run_as(
            "antigravity",
            "tool",
            {**base, "toolCall": {"name": "write_to_file", "args": {"TargetFile": "/work/billing/ledger.py"}}},
        )
        self.run_as(
            "antigravity",
            "tool",
            {**base, "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest"}}, "error": "exit 1"},
        )
        self.run_as("antigravity", "stop", {**base, "transcriptPath": str(transcript)})
        exchange = json.loads((self.data / "sessions" / "a1.json").read_text())["exchanges"][0]
        self.assertEqual(exchange["user"], "make the ledger")
        self.assertEqual(exchange["assistant"], "Made **ledger.py**.")
        self.assertEqual(exchange["work"], "Files changed: ledger.py. Commands run: `pytest` (failed).")


class Settings(Hosts):
    def test_key_from_the_settings_file(self) -> None:
        (self.tmp / "hooks.json").write_text(json.dumps({"api_key": KEY, "capture": False}))
        cfg = hooks.Config(env={"HOME": str(self.tmp)}, host="cursor")
        self.assertEqual(cfg.key, KEY)
        self.assertFalse(cfg.capture)
        self.assertEqual(cfg.api, hooks.DEFAULT_API)

    def test_data_directories(self) -> None:
        self.assertEqual(
            str(hooks.Config(env={"COPILOT_PLUGIN_DATA": "/d/c", "CLAUDE_PLUGIN_DATA": "/d/x"}, host="copilot").data),
            "/d/c",
        )
        self.assertEqual(str(hooks.Config(env={"PLUGIN_DATA": "/d/p"}, host="codex").data), "/d/p")
        self.assertTrue(str(hooks.Config(env={}, host="gemini").data).endswith(".alexein/hooks/gemini"))

    def test_unknown_host_does_nothing(self) -> None:
        self.assertEqual(self.run_as("emacs", "session-start", {}), "")

    def test_status_names_where_the_key_goes(self) -> None:
        self.run_as("cursor", "session-start", {}, env={"ALEXEIN_API_KEY": ""})
        out = self.run_as("cursor", "status", env={"ALEXEIN_API_KEY": ""})
        self.assertIn("Alexein for Cursor", out)
        self.assertIn("ALEXEIN_API_KEY", out)


class LateTranscript(Hosts):
    """Copilot writes the reply to its transcript a moment after its Stop hook starts."""

    def test_stop_waits_for_the_reply(self) -> None:
        import threading

        transcript = self.tmp / "events.jsonl"
        user = json.dumps({"type": "user.message", "data": {"content": "say mango"}})
        transcript.write_text(user + "\n")

        def late() -> None:
            with transcript.open("a") as handle:
                handle.write(json.dumps({"type": "assistant.message", "data": {"content": "mango"}}) + "\n")

        threading.Timer(0.3, late).start()
        base = {"session_id": "c9", "transcript_path": str(transcript)}
        self.run_as("copilot", "prompt", {**base, "prompt": "say mango"})
        self.run_as("copilot", "stop", base)
        exchange = json.loads((self.data / "sessions" / "c9.json").read_text())["exchanges"][0]
        self.assertEqual(exchange["assistant"], "mango")

    def test_claude_code_never_waits(self) -> None:
        self.assertEqual(hooks.HOSTS[hooks.CLAUDE].prompt_context, True)
        transcript = self.tmp / "empty.jsonl"
        transcript.write_text("")
        env = {
            "CLAUDE_PLUGIN_DATA": str(self.data),
            "CLAUDE_PLUGIN_OPTION_API_KEY": KEY,
            "CLAUDE_PLUGIN_OPTION_API_URL": "https://api.example.test",
        }
        started = __import__("time").monotonic()
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch("sys.stdin", io.StringIO(json.dumps({"session_id": "c8", "transcript_path": str(transcript)}))),
        ):
            hooks.main(["alexein_hooks.py", "stop"])
        self.assertLess(__import__("time").monotonic() - started, 0.5)
