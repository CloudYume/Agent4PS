import json

from leetcode_agent.judge_feedback import case_from_feedback, format_failure, with_judge_cases
from leetcode_agent.types import Candidate, CheckResult, JudgeFeedback, Problem, ProblemRef, SampleCase


REF = ProblemRef(37, "Sudoku Solver", "sudoku-solver", "https://leetcode.cn/problems/sudoku-solver/")


def problem(params):
    return Problem(REF, "Solve in place", "class Solution: pass", meta_data=json.dumps({"params": params}))


def test_sudoku_judge_case_is_a_single_in_place_argument():
    puzzle = [[".", "1"], ["2", "."]]
    expected = [["3", "1"], ["2", "4"]]
    feedback = JudgeFeedback(
        "board = \n" + json.dumps(puzzle),
        json.dumps([["4", "1"], ["2", "3"]]),
        json.dumps(expected),
    )
    case = case_from_feedback(problem([{"name": "board", "type": "character[][]"}]), feedback)
    assert case == SampleCase(args=[puzzle], expected=expected)
    text = format_failure(CheckResult(False, "Wrong Answer", feedback))
    assert "Failing input:\nboard =" in text
    assert "Actual output:\n" + feedback.actual_output in text
    assert "Expected output:\n" + feedback.expected_output in text


def test_judge_case_parses_multiple_named_arguments_and_prioritizes_regression():
    current = problem([
        {"name": "nums", "type": "integer[]"},
        {"name": "target", "type": "integer"},
    ])
    feedback = JudgeFeedback("nums = [1, 2]\ntarget = 3", "-1", "0")
    regression = SampleCase(args=[[1, 2], 3], expected=0)
    assert case_from_feedback(current, feedback) == regression
    candidate = Candidate("code", "summary", "approach", "O(n)", "O(1)", [SampleCase([[], 0], -1)])
    amended = with_judge_cases(current, candidate, [CheckResult(False, "Wrong Answer", feedback)])
    assert amended.tests == [regression, candidate.tests[0]]
    assert candidate.tests == [SampleCase([[], 0], -1)]


def test_pow_failure_parses_unlabeled_multiline_float_and_integer():
    current = Problem(
        ProblemRef(50, "Pow(x, n)", "powx-n", "https://leetcode.cn/problems/powx-n/"),
        "Compute x to the n-th power.", "class Solution: pass",
        meta_data=json.dumps({"params": [
            {"name": "x", "type": "double"}, {"name": "n", "type": "integer"},
        ]}),
    )
    feedback = JudgeFeedback("-0.9999999968539456\n-1669585506", "191.06373", "191.0637")
    assert case_from_feedback(current, feedback) == SampleCase(
        args=[-0.9999999968539456, -1669585506], expected=191.0637,
    )


def test_custom_structures_and_unparseable_outputs_are_not_added_as_local_cases():
    tree = problem([{"name": "root", "type": "TreeNode"}])
    assert case_from_feedback(tree, JudgeFeedback("[1,2,3]", "2", "1")) is None
    simple = problem([{"name": "x", "type": "integer"}])
    assert case_from_feedback(simple, JudgeFeedback("x = 1", "2", "not JSON")) is None
