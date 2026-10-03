from leetcode_agent.local_check import check_candidate
from leetcode_agent.types import Candidate, Problem, ProblemRef, SampleCase


def candidate(code, tests):
    return Candidate(code, "摘要", "思路", "O(n)", "O(1)", tests)


def test_runs_json_compatible_cases():
    result = check_candidate(
        candidate(
            "class Solution:\n    def twoSum(self, nums, target):\n        return [0, 1]\n",
            [SampleCase(args=[[2, 7], 9], expected=[0, 1])],
        )
    )
    assert result.passed, result.detail


def test_reports_wrong_result():
    result = check_candidate(
        candidate(
            "class Solution:\n    def twoSum(self, nums, target):\n        return []\n",
            [SampleCase(args=[[2, 7], 9], expected=[0, 1])],
        )
    )
    assert not result.passed
    assert "expected" in result.detail


def test_floating_regression_uses_absolute_tolerance_and_catches_precision_loss():
    args = [-0.9999999968539456, -1669585506]
    expected = 191.0637
    prefix = (
        "class Solution:\n"
        "    def myPow(self, x: float, n: int) -> float:\n"
        "        result = 1.0\n"
        "        base = x\n"
        "        power = abs(n)\n"
        "        while power:\n"
        "            if power & 1:\n"
        "                result *= base\n"
        "            base *= base\n"
        "            power >>= 1\n"
    )
    precise = candidate(prefix + "        return 1.0 / result if n < 0 else result\n",
                        [SampleCase(args=args, expected=expected)])
    early_reciprocal = candidate(prefix.replace("base = x", "base = 1.0 / x if n < 0 else x")
                                 + "        return result\n",
                                 [SampleCase(args=args, expected=expected)])

    assert check_candidate(precise).passed
    assert not check_candidate(early_reciprocal).passed


def test_integer_expected_value_still_requires_exact_match():
    almost_zero = candidate(
        "class Solution:\n    def compute(self):\n        return 0.000001\n",
        [SampleCase(args=[], expected=0)],
    )
    assert not check_candidate(almost_zero).passed


def test_void_in_place_method_checks_modified_first_argument():
    correct = candidate(
        "class Solution:\n    def nextPermutation(self, nums: list[int]) -> None:\n"
        "        nums[0], nums[1] = nums[1], nums[0]\n",
        [SampleCase(args=[[1, 2]], expected=[2, 1])],
    )
    assert check_candidate(correct).passed

    wrong = candidate(
        "class Solution:\n    def nextPermutation(self, nums: list[int]) -> None:\n"
        "        pass\n",
        [SampleCase(args=[[1, 2]], expected=[2, 1])],
    )
    result = check_candidate(wrong)
    assert not result.passed
    assert "expected [2, 1], got [1, 2]" in result.detail


def test_local_process_does_not_inherit_api_key(monkeypatch):
    monkeypatch.setenv("POKE_API_KEY", "test-secret-marker")
    result = check_candidate(
        candidate(
            "import os\nclass Solution:\n    def secret(self):\n        return os.getenv('POKE_API_KEY')\n",
            [SampleCase(args=[], expected=None)],
        )
    )
    assert result.passed, result.detail


def test_linked_list_cases_use_list_nodes_and_compare_serialized_result():
    code = (
        "# Definition for singly-linked list.\n"
        "# class ListNode:\n"
        "class Solution:\n"
        "    def rotateRight(self, head: ListNode | None, k: int) -> ListNode | None:\n"
        "        if not head or not head.next:\n"
        "            return head\n"
        "        tail, length = head, 1\n"
        "        while tail.next:\n"
        "            tail, length = tail.next, length + 1\n"
        "        k %= length\n"
        "        if not k:\n"
        "            return head\n"
        "        split = head\n"
        "        for _ in range(length - k - 1):\n"
        "            split = split.next\n"
        "        result = split.next\n"
        "        split.next = None\n"
        "        tail.next = head\n"
        "        return result\n"
    )
    checks = [
        SampleCase(args=[[1, 2, 3, 4, 5], 2], expected=[4, 5, 1, 2, 3]),
        SampleCase(args=[[], 3], expected=[]),
    ]
    assert check_candidate(candidate(code, checks)).passed


def test_linked_list_metadata_supports_unannotated_method():
    problem = Problem(
        ProblemRef(61, "Rotate List", "rotate-list", "https://leetcode.cn/problems/rotate-list/"),
        "Rotate the linked list", "class Solution: pass",
        meta_data='{"params": [{"name": "head", "type": "ListNode"}], "return": {"type": "ListNode"}}',
    )
    result = check_candidate(candidate(
        "class Solution:\n    def identity(self, head):\n        return head\n",
        [SampleCase(args=[[1, 2]], expected=[1, 2])],
    ), problem)
    assert result.passed, result.detail
