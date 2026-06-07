from __future__ import annotations

import unittest

from helpers import SRC  # noqa: F401
from rowplane.runtime.commands import DelegateCommand, ToolCommand, command_to_event_payload, parse_command
from rowplane.runtime.errors import MalformedCommand
from rowplane.runtime.sanitize import redact_secrets


class CommandParsingTests(unittest.TestCase):
    def test_parses_valid_tool_command(self) -> None:
        command = parse_command(
            '{"action":"tool","tool_name":"search_policy_documents","arguments":{"q":"x"}}'
        )
        self.assertIsInstance(command, ToolCommand)
        self.assertEqual(command.tool_name, "search_policy_documents")
        self.assertEqual(command.arguments, {"q": "x"})


    def test_parses_valid_delegate_command(self) -> None:
        command = parse_command(
            {
                "action": "delegate",
                "to_agent": "policy_researcher",
                "task": {"question": "Find the policy."},
                "reason": "Need specialist evidence.",
            }
        )

        self.assertIsInstance(command, DelegateCommand)
        self.assertEqual(command.to_agent, "policy_researcher")
        self.assertEqual(command.task, {"question": "Find the policy."})

    def test_rejects_bad_delegate_target(self) -> None:
        with self.assertRaises(MalformedCommand):
            parse_command(
                {
                    "action": "delegate",
                    "to_agent": "PolicyResearcher",
                    "task": {},
                    "reason": "Need specialist evidence.",
                }
            )

    def test_rejects_extra_keys(self) -> None:
        with self.assertRaises(MalformedCommand):
            parse_command({"action": "final", "answer": {}, "extra": True})

    def test_rejects_duplicate_json_keys(self) -> None:
        with self.assertRaises(MalformedCommand):
            parse_command('{"action":"final","answer":{},"answer":{"x":1}}')

    def test_rejects_non_object_json(self) -> None:
        with self.assertRaises(MalformedCommand):
            parse_command('[{"action":"final","answer":{}}]')

    def test_rejects_bad_tool_name(self) -> None:
        with self.assertRaises(MalformedCommand):
            parse_command({"action": "tool", "tool_name": "Shell", "arguments": {}})

    def test_event_payload_redacts_sensitive_keys_and_values(self) -> None:
        sensitive_key = "api" + "_key"
        bearer_value = "Bearer " + "abcdefghijklmnopqrstuvwxyz"
        command = parse_command(
            {
                "action": "tool",
                "tool_name": "demo_tool",
                "arguments": {
                    sensitive_key: "not-a-real-value",
                    "note": bearer_value,
                },
            }
        )
        payload = command_to_event_payload(command)
        self.assertEqual(payload["arguments"][sensitive_key], "[REDACTED]")
        self.assertEqual(payload["arguments"]["note"], "[REDACTED]")
        self.assertEqual(
            redact_secrets("prefix " + "sk-" + "abcdefghijklmnop"),
            "prefix [REDACTED]",
        )


if __name__ == "__main__":
    unittest.main()
