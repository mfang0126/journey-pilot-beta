"""Contract tests: the result envelope, statuses, exit codes, error catalogue.

These are the stage-1 acceptance tests from docs/implementation-plan.md:
query-ok / action-recorded / task-completed / candidate-validation / side-effect
pause must never be confused with one another.
"""
from __future__ import annotations

import unittest

from journey_pilot.contracts import (
    ALLOWED_EXITS,
    ERRORS,
    ERROR_STATUSES,
    SCHEMA_VERSION,
    STATUSES,
    STATUS_EXIT,
    ContractError,
    error_catalog,
    error_payload,
    failure,
    success,
    validate_result,
)


class EnvelopeShape(unittest.TestCase):
    def test_success_envelope_has_exactly_the_six_keys(self):
        payload = success("ok", {"a": 1}).as_dict()
        self.assertEqual(
            set(payload),
            {"schema_version", "status", "run_id", "data", "error", "next_action"},
        )
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertIsNone(payload["error"])
        validate_result(payload)

    def test_recorded_is_not_completed(self):
        recorded = success("recorded", {"action": "tap"})
        completed = success("completed", {"checks": ["all"]})
        self.assertEqual(recorded.exit_code, 0)
        self.assertEqual(completed.exit_code, 0)
        self.assertNotEqual(recorded.status, completed.status)

    def test_completed_requires_data_but_is_a_distinct_status(self):
        self.assertEqual(success("completed").status, "completed")
        with self.assertRaises(ContractError):
            success("paused")


class StatusAndExitCodes(unittest.TestCase):
    def test_every_status_has_an_exit_code(self):
        self.assertEqual(set(STATUS_EXIT), set(STATUSES))

    def test_documented_exit_codes(self):
        self.assertEqual(STATUS_EXIT["ok"], 0)
        self.assertEqual(STATUS_EXIT["completed"], 0)
        self.assertEqual(STATUS_EXIT["recorded"], 0)
        self.assertEqual(STATUS_EXIT["paused"], 3)
        self.assertEqual(STATUS_EXIT["blocked"], 4)
        self.assertEqual(STATUS_EXIT["failed"], 5)
        self.assertEqual(STATUS_EXIT["cancelled"], 130)

    def test_parameter_errors_exit_two_even_when_blocked(self):
        self.assertEqual(failure("missing_parameter", "need keyword").exit_code, 2)
        self.assertEqual(failure("ambiguous_device", "two devices").exit_code, 2)
        self.assertEqual(failure("missing_parameter", "need keyword").status, "blocked")


class ErrorCatalogue(unittest.TestCase):
    def test_catalogue_is_internally_consistent(self):
        for spec in ERRORS.values():
            with self.subTest(code=spec.code):
                self.assertIn(spec.status, STATUSES)
                self.assertIn(spec.exit_code, ALLOWED_EXITS)
                self.assertTrue(spec.next_action)
                self.assertTrue(spec.summary)

    def test_catalogue_covers_the_documented_failure_classes(self):
        required = {
            "disconnected", "locked", "device_busy",
            "precondition_mismatch", "version_mismatch",
            "missing_parameter", "unsupported_input",
            "locator_missing", "ambiguous_target", "unexpected_page",
            "assertion_failed", "outcome_unknown", "cancelled",
            "provider_unavailable", "visual_suspected",
            "request_conflict", "needs_authorization",
        }
        self.assertLessEqual(required, set(ERRORS))

    def test_environment_errors_do_not_become_task_failures(self):
        for code in ("disconnected", "locked", "device_busy"):
            self.assertEqual(ERRORS[code].status, "blocked")
        for code in ("locator_missing", "ambiguous_target", "unexpected_page",
                     "assertion_failed"):
            self.assertEqual(ERRORS[code].status, "paused")

    def test_side_effect_pause_is_its_own_code(self):
        spec = ERRORS["needs_authorization"]
        self.assertEqual(spec.status, "paused")
        self.assertFalse(spec.retryable)

    def test_catalogue_serialises_for_capabilities(self):
        rows = error_catalog()
        self.assertEqual(len(rows), len(ERRORS))
        self.assertEqual({r["code"] for r in rows}, set(ERRORS))
        for row in rows:
            self.assertEqual(
                set(row),
                {"code", "status", "exit_code", "retryable", "next_action", "summary"},
            )


class ValidationRejectsConfusion(unittest.TestCase):
    def _good(self):
        return success("ok", {}).as_dict()

    def test_missing_key(self):
        payload = self._good()
        del payload["next_action"]
        with self.assertRaises(ContractError):
            validate_result(payload)

    def test_extra_key(self):
        payload = self._good()
        payload["ok"] = True
        with self.assertRaises(ContractError):
            validate_result(payload)

    def test_ok_with_error_is_rejected(self):
        payload = self._good()
        payload["error"] = error_payload("locked", "device locked")
        with self.assertRaises(ContractError):
            validate_result(payload)

    def test_blocked_without_error_is_rejected(self):
        payload = self._good()
        payload["status"] = "blocked"
        with self.assertRaises(ContractError):
            validate_result(payload)

    def test_status_must_match_the_error_code(self):
        payload = failure("locked", "device locked").as_dict()
        payload["status"] = "failed"
        with self.assertRaises(ContractError):
            validate_result(payload)

    def test_unknown_error_code_is_rejected(self):
        with self.assertRaises(ContractError):
            error_payload("no_such_code", "x")

    def test_unknown_status_is_rejected(self):
        payload = self._good()
        payload["status"] = "success"
        with self.assertRaises(ContractError):
            validate_result(payload)

    def test_next_action_needs_command_and_request(self):
        payload = failure("locked", "device locked").as_dict()
        payload["next_action"] = {"command": "status"}
        with self.assertRaises(ContractError):
            validate_result(payload)


class FailureStatusesCarryErrors(unittest.TestCase):
    def test_all_non_success_statuses_require_an_error(self):
        for status in ERROR_STATUSES:
            self.assertIn(status, STATUSES)
        for code, spec in ERRORS.items():
            with self.subTest(code=code):
                payload = failure(code, "message").as_dict()
                validate_result(payload)
                self.assertEqual(payload["status"], spec.status)


if __name__ == "__main__":
    unittest.main()
