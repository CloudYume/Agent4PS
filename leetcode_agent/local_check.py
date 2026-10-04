from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import replace

from .types import Candidate, CheckResult, Problem


_HARNESS = r'''
import contextlib
import io
import json
import math
import sys
import traceback
import typing
from collections import deque

class ListNode:
    def __init__(self, val=0, next=None):
        self.val = val
        self.next = next

class TreeNode:
    def __init__(self, val=0, left=None, right=None):
        self.val = val
        self.left = left
        self.right = right

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

def to_tree_node(values, node_class):
    if not values or values[0] is None:
        return None
    root = node_class(values[0])
    queue = deque([root])
    index = 1
    while queue and index < len(values):
        node = queue.popleft()
        for side in ("left", "right"):
            if index >= len(values):
                break
            value = values[index]
            index += 1
            if value is not None:
                child = node_class(value)
                setattr(node, side, child)
                queue.append(child)
    return root

def from_tree_node(root):
    if root is None:
        return []
    values = []
    seen = set()
    queue = deque([root])
    while queue:
        node = queue.popleft()
        if node is None:
            values.append(None)
            continue
        if id(node) in seen or len(seen) >= 10000:
            raise AssertionError("returned tree contains a cycle or is too large")
        seen.add(id(node))
        values.append(node.val)
        queue.extend((node.left, node.right))
    while values and values[-1] is None:
        values.pop()
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
scope = {"ListNode": ListNode, "TreeNode": TreeNode, "typing": typing}
scope.update({name: getattr(typing, name) for name in (
    "Any", "Optional", "List", "Dict", "Set", "Tuple", "Union", "Deque",
    "DefaultDict", "Iterable", "Sequence", "Mapping", "MutableMapping",
    "Collection", "FrozenSet", "Callable", "Iterator", "Generator",
)})
output = io.StringIO()
try:
    with contextlib.redirect_stdout(output):
        exec(compile(payload["code"], "solution.py", "exec"), scope)
        instance = scope["Solution"]()
        method = getattr(instance, payload["method"])
        for index, case in enumerate(payload["tests"], start=1):
            args = [
                to_list_node(value, scope["ListNode"]) if payload["input_types"][position] == "ListNode"
                else to_tree_node(value, scope["TreeNode"]) if payload["input_types"][position] == "TreeNode"
                else value
                for position, value in enumerate(case["args"])
            ]
            actual = method(*args)
            if payload["in_place"] and actual is None and case["args"] and case["expected"] is not None:
                actual = args[0]
                result_type = payload["input_types"][0]
            else:
                result_type = payload["output_type"]
            if result_type == "ListNode":
                actual = from_list_node(actual)
            elif result_type == "TreeNode":
                actual = from_tree_node(actual)
            if not matches(actual, case["expected"]):
                raise AssertionError(
                    f"case {index}: expected {case['expected']!r}, got {actual!r}"
                )
    print(json.dumps({"passed": True, "detail": f"{len(payload['tests'])} local cases passed"}))
except Exception:
    print(json.dumps({"passed": False, "detail": traceback.format_exc(limit=5)[-3000:]}))
'''


def _method_contract(tree: ast.Module, name: str | None = None) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Solution":
            for member in node.body:
                if (isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and not member.name.startswith("_")
                        and (name is None or member.name == name)):
                    return member
    return None


def _method_arity(method: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[int, int]:
    return (len(method.args.posonlyargs) + len(method.args.args), len(method.args.kwonlyargs))


def _node_kind(annotation: ast.expr | None) -> str | None:
    if isinstance(annotation, ast.Name) and annotation.id in {"ListNode", "TreeNode"}:
        return annotation.id
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            return _node_kind(ast.parse(annotation.value, mode="eval").body)
        except SyntaxError:
            return None
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        if isinstance(annotation.left, ast.Constant) and annotation.left.value is None:
            return _node_kind(annotation.right)
        if isinstance(annotation.right, ast.Constant) and annotation.right.value is None:
            return _node_kind(annotation.left)
    if isinstance(annotation, ast.Subscript):
        wrapper = annotation.value
        name = wrapper.id if isinstance(wrapper, ast.Name) else (
            wrapper.attr if isinstance(wrapper, ast.Attribute) else None
        )
        if name == "Union" and isinstance(annotation.slice, ast.Tuple):
            values = annotation.slice.elts
            if len(values) == 2 and any(isinstance(value, ast.Constant) and value.value is None
                                        for value in values):
                return _node_kind(next(value for value in values if not (
                    isinstance(value, ast.Constant) and value.value is None)))
        if name == "Optional":
            return _node_kind(annotation.slice)
    return None


def _metadata(problem: Problem | None) -> dict:
    if not problem or not problem.meta_data:
        return {}
    try:
        value = json.loads(problem.meta_data)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


_VALUE_TYPES = {
    "integer", "long", "double", "float", "boolean", "character", "string",
    "char", "void", "null", "int", "bool", "str", "number", "object",
}
_ANNOTATION_NAMES = {
    "int", "float", "bool", "str", "bytes", "object", "None", "Any",
    "list", "dict", "List", "Dict", "Iterable", "Sequence",
    "Mapping", "MutableMapping", "Collection", "Optional", "Union",
}
_JUDGE_APIS = {"guess", "isBadVersion", "read4", "knows", "rand7"}
_STANDARD_NODE_CLASSES = {
    node.name: node for node in ast.parse(_HARNESS).body
    if isinstance(node, ast.ClassDef) and node.name in {"ListNode", "TreeNode"}
}


def _metadata_type_kind(type_name: str) -> str | None:
    name = re.sub(r"\s+", "", type_name)
    if name in {"ListNode", "TreeNode"}:
        return name
    while name.endswith("[]"):
        name = name[:-2]
    while name.startswith("list<") and name.endswith(">"):
        name = name[5:-1]
        while name.endswith("[]"):
            name = name[:-2]
    return "json" if name in _VALUE_TYPES else None


def _platform_node_classes(tree: ast.Module, problem: Problem | None) -> list[ast.ClassDef]:
    if problem is None:
        return []
    metadata = _metadata(problem)
    if metadata.get("manual") is True:
        return []
    node_names = set(re.findall(r"(?m)^\s*#\s*class\s+(ListNode|TreeNode)\s*:",
                                problem.starter_code))
    fields = metadata.get("params")
    fields = [*fields, metadata.get("return")] if isinstance(fields, list) else [metadata.get("return")]
    for field in fields:
        if isinstance(field, dict) and isinstance(field.get("type"), str):
            kind = _metadata_type_kind(field["type"])
            if kind in _STANDARD_NODE_CLASSES:
                node_names.add(kind)
    return [node for node in tree.body if isinstance(node, ast.ClassDef)
            and node.name in node_names]


def normalize_platform_node_classes(candidate: Candidate, problem: Problem) -> Candidate:
    try:
        tree = ast.parse(candidate.code)
    except SyntaxError:
        return candidate
    removable = [node for node in _platform_node_classes(tree, problem)
                 if ast.dump(node, include_attributes=False) == ast.dump(
                     _STANDARD_NODE_CLASSES[node.name], include_attributes=False)]
    if not removable:
        return candidate
    lines = candidate.code.splitlines(keepends=True)
    for node in reversed(removable):
        del lines[node.lineno - 1:node.end_lineno]
    return replace(candidate, code="".join(lines).lstrip("\r\n"))


def _annotation_kind(annotation: ast.expr | None) -> str | None:
    if annotation is None:
        return "json"
    node = _node_kind(annotation)
    if node:
        return node
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            return _annotation_kind(ast.parse(annotation.value, mode="eval").body)
        except SyntaxError:
            return None
    for item in ast.walk(annotation):
        if isinstance(item, ast.Constant) and isinstance(item.value, str):
            if _annotation_kind(item) != "json":
                return None
        if isinstance(item, ast.Name) and item.id not in _ANNOTATION_NAMES | {"typing"}:
            return None
        if isinstance(item, ast.Attribute) and (
            not isinstance(item.value, ast.Name) or item.value.id != "typing"
            or item.attr not in _ANNOTATION_NAMES
        ):
            return None
        if isinstance(item, ast.Name) and item.id in {"typing"} and item is annotation:
            return None
    return "json"


def _node_contract(method: ast.FunctionDef | ast.AsyncFunctionDef,
                   metadata: dict) -> tuple[list[str | None], str | None]:
    args = [*method.args.posonlyargs, *method.args.args][1:]
    input_types = [_node_kind(arg.annotation) for arg in args]
    output_type = _node_kind(method.returns)
    params = metadata.get("params")
    if isinstance(params, list):
        for index, param in enumerate(params[:len(input_types)]):
            if isinstance(param, dict) and isinstance(param.get("type"), str):
                kind = _metadata_type_kind(param["type"])
                if kind in {"ListNode", "TreeNode"}:
                    input_types[index] = kind
    result = metadata.get("return")
    if isinstance(result, dict) and isinstance(result.get("type"), str):
        kind = _metadata_type_kind(result["type"])
        if kind in {"ListNode", "TreeNode"}:
            output_type = kind
    return input_types, output_type


def _unsupported_custom_structure(method: ast.FunctionDef | ast.AsyncFunctionDef,
                                  metadata: dict) -> str | None:
    params = metadata.get("params")
    fields = [*params, metadata.get("return")] if isinstance(params, list) else [metadata.get("return")]
    for field in fields:
        if isinstance(field, dict) and isinstance(field.get("type"), str):
            if _metadata_type_kind(field["type"]) is None:
                return field["type"]
    annotations = [arg.annotation for arg in [*method.args.posonlyargs, *method.args.args][1:]]
    annotations.append(method.returns)
    for annotation in annotations:
        if _annotation_kind(annotation) is None:
            return ast.unparse(annotation)
    return None


def check_candidate(candidate: Candidate, problem: Problem | None = None) -> CheckResult:
    try:
        tree = ast.parse(candidate.code)
        compile(tree, "solution.py", "exec")
    except SyntaxError as exc:
        return CheckResult(False, f"Python syntax error: {exc}")
    platform_classes = _platform_node_classes(tree, problem)
    if platform_classes:
        name = platform_classes[0].name
        return CheckResult(False, f"LeetCode supplies {name}; remove the top-level class {name} "
                           "and use the judge-provided node type")
    expected = None
    starter = None
    if problem:
        try:
            starter = ast.parse(problem.starter_code)
        except SyntaxError:
            pass
        if starter:
            expected = _method_contract(starter)
            if expected is None:
                starter_classes = [node for node in starter.body if isinstance(node, ast.ClassDef)]
                if len(starter_classes) == 1:
                    required = starter_classes[0]
                    submitted = next((node for node in tree.body if isinstance(node, ast.ClassDef)
                                      and node.name == required.name), None)
                    if submitted is None:
                        return CheckResult(False, f"Python submission must define class {required.name}")
                    for method in required.body:
                        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            continue
                        implementation = next((node for node in submitted.body
                                               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                                               and node.name == method.name), None)
                        if implementation is None or _method_arity(implementation) != _method_arity(method):
                            return CheckResult(False, f"{required.name}.{method.name} signature does not match "
                                               "the Python3 starter")
    if expected:
        contract = _method_contract(tree, expected.name)
        if contract is None:
            return CheckResult(False, f"Python submission must define Solution.{expected.name}")
        if isinstance(contract, ast.AsyncFunctionDef) or _method_arity(contract) != _method_arity(expected):
            return CheckResult(False, f"Solution.{expected.name} signature does not match the Python3 starter")
    else:
        contract = _method_contract(tree)
    if not candidate.tests:
        return CheckResult(True, "Python syntax passed; no JSON-compatible local cases")
    metadata = _metadata(problem)
    if metadata.get("manual") is True:
        return CheckResult(True, "Python syntax passed; local cases skipped for LeetCode manual judge")
    if not contract:
        if starter and not any(isinstance(node, ast.ClassDef) and node.name == "Solution"
                               for node in starter.body):
            return CheckResult(True, "Python syntax passed; local cases skipped for design problem")
        return CheckResult(False, "local cases require a public method on class Solution")
    unsupported = _unsupported_custom_structure(contract, metadata)
    if unsupported:
        return CheckResult(True, f"Python syntax passed; local cases skipped for unsupported {unsupported} structure")
    solution = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Solution")
    if any(isinstance(base, ast.Name) and base.id != "object" for base in solution.bases):
        return CheckResult(True, "Python syntax passed; local cases skipped for LeetCode base class")
    defined = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    judge_apis = {node.func.id for node in ast.walk(contract) if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Name) and node.func.id in _JUDGE_APIS - defined}
    if judge_apis:
        return CheckResult(True, f"Python syntax passed; local cases skipped for LeetCode judge API {sorted(judge_apis)[0]}")
    input_types, output_type = _node_contract(contract, metadata)
    data = {
        "code": candidate.code,
        "method": contract.name,
        "in_place": isinstance(contract.returns, ast.Constant) and contract.returns.value is None,
        "input_types": input_types,
        "output_type": output_type,
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

