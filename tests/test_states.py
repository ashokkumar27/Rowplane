from __future__ import annotations

import unittest

from helpers import SRC  # noqa: F401
from rowplane.runtime.errors import InvalidStateTransition
from rowplane.runtime.states import can_transition, validate_transition
from rowplane.runtime.task_states import can_task_transition, validate_task_transition


class StateMachineTests(unittest.TestCase):
    def test_allowed_transitions(self) -> None:
        self.assertTrue(can_transition("queued", "thinking"))
        self.assertTrue(can_transition("thinking", "needs_tool"))
        self.assertTrue(can_transition("needs_tool", "tool_running"))
        self.assertTrue(can_transition("tool_running", "queued"))
        self.assertTrue(can_transition("waiting_approval", "queued"))

    def test_terminal_failure_and_blocked_are_global(self) -> None:
        self.assertTrue(can_transition("completed", "failed"))
        self.assertTrue(can_transition("queued", "blocked"))

    def test_rejects_invalid_transition(self) -> None:
        with self.assertRaises(InvalidStateTransition):
            validate_transition("queued", "completed")

    def test_evaluating_is_reserved_by_contract(self) -> None:
        with self.assertRaises(InvalidStateTransition):
            validate_transition("thinking", "evaluating")
        with self.assertRaises(InvalidStateTransition):
            validate_transition("evaluating", "completed")


class TaskStateMachineTests(unittest.TestCase):
    def test_allowed_task_transitions_include_child_waiting(self) -> None:
        self.assertTrue(can_task_transition("queued", "thinking"))
        self.assertTrue(can_task_transition("thinking", "waiting_child"))
        self.assertTrue(can_task_transition("waiting_child", "queued"))
        self.assertTrue(can_task_transition("tool_running", "queued"))

    def test_task_terminal_failure_and_blocked_are_global(self) -> None:
        self.assertTrue(can_task_transition("queued", "blocked"))
        self.assertTrue(can_task_transition("completed", "failed"))

    def test_rejects_invalid_task_transition(self) -> None:
        with self.assertRaises(InvalidStateTransition):
            validate_task_transition("queued", "completed")


if __name__ == "__main__":
    unittest.main()
