from __future__ import annotations

import json
import re
from dataclasses import replace

from .types import Candidate, CheckResult, JudgeFeedback, Problem, SampleCase


def format_failure(result: CheckResult) -> str:
    feedback = result.judge_feedback
    if feedback is None:
        return result.detail
    return (
        f"{result.detail}\n\n"
        f"Failing input:\n{feedback.testcase}\n"
        f"Actual output:\n{feedback.actual_output}\n"
        f"Expected output:\n{feedback.expected_output}"
    )


def _params(problem: Problem) -> list[dict] | None:
    try:
        metadata = json.loads(problem.meta_data)
    except (TypeError, ValueError):
        return None
    params = metadata.get("params") if isinstance(metadata, dict) else None
    return params if isinstance(params, list) and all(isinstance(item, dict) for item in params) else None


def case_from_feedback(problem: Problem, feedback: JudgeFeedback) -> SampleCase | None:
    if not feedback.testcase.strip() or not feedback.expected_output.strip():
        return None
    params = _params(problem)
    if params is not None and any(
        re.search(r"(?:ListNode|TreeNode|NestedInteger|\bNode\b)", str(item.get("type", "")))
        for item in params
    ):
        return None

    decoder = json.JSONDecoder()
    values = []
    source = feedback.testcase
    offset = 0
    try:
        while offset < len(source):
            remainder = source[offset:].lstrip()
            if not remainder:
                break
            if params is not None and len(values) < len(params):
                name = params[len(values)].get("name")
                if isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
                    label = re.match(rf"{re.escape(name)}\s*=\s*", remainder)
                    if label:
                        remainder = remainder[label.end():]
            value, consumed = decoder.raw_decode(remainder)
            values.append(value)
            offset = len(source) - len(remainder) + consumed
        expected = json.loads(feedback.expected_output)
    except (TypeError, ValueError):
        return None
    if not values or params is not None and len(values) != len(params):
        return None
    return SampleCase(args=values, expected=expected)


def with_judge_cases(problem: Problem, candidate: Candidate, checks: list[CheckResult]) -> Candidate:
    regressions = []
    for check in reversed(checks):
        if check.passed or check.judge_feedback is None:
            continue
        case = case_from_feedback(problem, check.judge_feedback)
        if case is not None and case not in regressions:
            regressions.append(case)
        if len(regressions) == 3:
            break
    if not regressions:
        return candidate
    other = [case for case in candidate.tests if case not in regressions]
    return replace(candidate, tests=(regressions + other)[:3])
