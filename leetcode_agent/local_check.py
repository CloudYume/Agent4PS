from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile

from .types import Candidate, CheckResult, Problem


_HARNESS = r'''
import contextlib
import io
import json
import math
import sys
import traceback

class ListNode:
    def __init__(self, val=0, next=None):
        self.val = val
        self.next = next

def to_list_node(values, node_class):
    if values is None:
        return None
    head = None
    for value in reversed(values):
        node = node_class(value)
        node.next = head
        head = node
    return head

def from_list_node(head):
    values = []
    seen = set()
    while head is not None:
        if id(head) in seen or len(values) >= 10000:
            raise AssertionError("returned linked list contains a cycle or is too long")
        seen.add(id(head))
        values.append(head.val)
        head = head.next
    return values

def matches(actual, expected):
    if isinstance(expected, float) and not isinstance(actual, bool):
        return (isinstance(actual, (int, float)) and isinstance(expected, (int, float))
                and math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-5))
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(
            matches(left, right) for left, right in zip(actual, expected)
        )
    if isinstance(actual, dict) and isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            matches(actual[key], expected[key]) for key in expected
        )
    return actual == expected

payload = json.load(sys.stdin)
scope = {"ListNode": ListNode}
output = io.StringIO()
try:
    with contextlib.redirect_stdout(output):
        exec(compile(payload["code"], "solution.py", "exec"), scope)
        instance = scope["Solution"]()
        method = getattr(instance, payload["method"])
        for index, case in enumerate(payload["tests"], start=1):
            args = [
                to_list_node(value, scope["ListNode"]) if position in payload["list_inputs"] else value
                for position, value in enumerate(case["args"])
            ]
            actual = method(*args)
            if payload["in_place"] and actual is None and case["args"] and case["expected"] is not None:
                actual = args[0]
            if payload["list_output"]:
                actual = from_list_node(actual)
            if not matches(actual, case["expected"]):
                raise AssertionError(
                    f"case {index}: expected {case['expected']!r}, got {actual!r}"
                )
    print(json.dumps({"passed": True, "detail": f"{len(payload['tests'])} local cases passed"}))
except Exception:
    print(json.dumps({"passed": False, "detail": traceback.format_exc(limit=5)[-3000:]}))
'''


def _method_contract(tree: ast.Module) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Solution":
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and not member.name.startswith("_"):
                    return member
    return None


def _list_node_contract(method: ast.FunctionDef | ast.AsyncFunctionDef,
                        problem: Problem | None) -> tuple[list[int], bool]:
    args = [*method.args.posonlyargs, *method.args.args][1:]
    input_types = [bool(arg.annotation and any(
        isinstance(node, ast.Name) and node.id == "ListNode" for node in ast.walk(arg.annotation)
    )) for arg in args]
    output_type = bool(method.returns and any(
        isinstance(node, ast.Name) and node.id == "ListNode" for node in ast.walk(method.returns)
    ))
    if problem and problem.meta_data:
        try:
            metadata = json.loads(problem.meta_data)
        except (TypeError, ValueError):
            metadata = {}
        if isinstance(metadata, dict):
            params = metadata.get("params")
            if isinstance(params, list):
                for index, param in enumerate(params[:len(input_types)]):
                    if isinstance(param, dict) and "ListNode" in str(param.get("type", "")):
                        input_types[index] = True
            result = metadata.get("return")
            if isinstance(result, dict) and "ListNode" in str(result.get("type", "")):
                output_type = True
    return [index for index, is_list in enumerate(input_types) if is_list], output_type


def check_candidate(candidate: Candidate, problem: Problem | None = None) -> CheckResult:
    try:
        tree = ast.parse(candidate.code)
        compile(tree, "solution.py", "exec")
    except SyntaxError as exc:
        return CheckResult(False, f"Python syntax error: {exc}")
    if not candidate.tests:
        return CheckResult(True, "Python syntax passed; no JSON-compatible local cases")
    contract = _method_contract(tree)
    if not contract:
        return CheckResult(False, "local cases require a public method on class Solution")
    list_inputs, list_output = _list_node_contract(contract, problem)
    data = {
        "code": candidate.code,
        "method": contract.name,
        "in_place": isinstance(contract.returns, ast.Constant) and contract.returns.value is None,
        "list_inputs": list_inputs,
        "list_output": list_output,
        "tests": [{"args": test.args, "expected": test.expected} for test in candidate.tests],
    }
    environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
    }
    with tempfile.TemporaryDirectory(prefix="leetcode-check-") as directory:
        try:
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-c", _HARNESS],
                input=json.dumps(data, ensure_ascii=False),
                text=True,
                capture_output=True,
                cwd=directory,
                env=environment,
                timeout=8,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CheckResult(False, "local cases exceeded 8 seconds")
    try:
        report = json.loads(result.stdout.strip().splitlines()[-1])
        return CheckResult(bool(report["passed"]), f"local cases: {report['detail']}")
    except (IndexError, KeyError, ValueError, TypeError):
        detail = (result.stderr or result.stdout or "unknown local process error")[-1200:]
        return CheckResult(False, f"local process failed: {detail}")

