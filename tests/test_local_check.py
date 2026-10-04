from leetcode_agent.local_check import check_candidate, normalize_platform_node_classes
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


def test_sparse_tree_cases_use_leetcode_level_order_nodes_and_typing_annotations():
    code = (
        "class Solution:\n"
        "    def inorderTraversal(self, root: Optional[TreeNode]) -> List[int]:\n"
        "        result, stack = [], []\n"
        "        while root or stack:\n"
        "            while root:\n"
        "                stack.append(root)\n"
        "                root = root.left\n"
        "            root = stack.pop()\n"
        "            result.append(root.val)\n"
        "            root = root.right\n"
        "        return result\n"
    )
    result = check_candidate(candidate(code, [
        SampleCase(args=[[1, None, 2, 3]], expected=[1, 3, 2]),
        SampleCase(args=[[]], expected=[]),
        SampleCase(args=[[1]], expected=[1]),
    ]))
    assert result.passed, result.detail


def test_tree_metadata_supports_unannotated_input_and_tree_output():
    problem = Problem(
        ProblemRef(94, "Binary Tree", "binary-tree", "https://leetcode.cn/problems/binary-tree/"),
        "Return the same tree", "class Solution: pass",
        meta_data='{"params": [{"name": "root", "type": "TreeNode"}], "return": {"type": "TreeNode"}}',
    )
    result = check_candidate(candidate(
        "class Solution:\n    def identity(self, root):\n        return root\n",
        [SampleCase(args=[[1, None, 2, 3]], expected=[1, None, 2, 3]),
         SampleCase(args=[[]], expected=[])],
    ), problem)
    assert result.passed, result.detail


def test_unsupported_custom_node_cases_are_left_to_site_runner():
    problem = Problem(
        ProblemRef(117, "Connect", "connect", "https://leetcode.cn/problems/connect/"),
        "Connect nodes", "class Solution: pass",
        meta_data='{"params": [{"name": "root", "type": "Node"}], "return": {"type": "Node"}}',
    )
    result = check_candidate(candidate(
        "class Solution:\n    def connect(self, root: Optional[Node]) -> Optional[Node]:\n        return root\n",
        [SampleCase(args=[[1, 2, 3]], expected=[1, 2, 3])],
    ), problem)
    assert result.passed
    assert "skipped for unsupported Node" in result.detail


def test_manual_judge_skips_misleading_list_node_metadata():
    problem = Problem(
        ProblemRef(138, "Copy List", "copy-list-with-random-pointer", "https://leetcode.cn/problems/copy-list-with-random-pointer/"),
        "Copy list with random pointers", "class Solution: pass",
        meta_data='{"manual": true, "params": [{"name": "head", "type": "ListNode"}]}',
    )
    result = check_candidate(candidate(
        "class Solution:\n    def copyRandomList(self, head):\n        raise RuntimeError('site only')\n",
        [SampleCase(args=[[[1, None]]], expected=[[1, None]])],
    ), problem)
    assert result.passed
    assert "manual judge" in result.detail


def test_nested_nodes_and_unknown_judge_interfaces_skip_local_cases():
    for type_name in ("list<NestedInteger>", "list<TreeNode>", "TreeNode[]", "MountainArray", "BinaryMatrix"):
        problem = Problem(
            ProblemRef(1, "Custom", "custom", "https://leetcode.cn/problems/custom/"),
            "Custom input", "class Solution: pass",
            meta_data='{"params": [{"name": "value", "type": "' + type_name + '"}]}',
        )
        result = check_candidate(candidate(
            "class Solution:\n    def solve(self, value):\n        raise RuntimeError('site only')\n",
            [SampleCase(args=[[1, 2]], expected=3)],
        ), problem)
        assert result.passed, type_name
        assert "skipped for unsupported " + type_name in result.detail


def test_custom_annotation_skips_even_when_metadata_claims_primitive():
    problem = Problem(
        ProblemRef(1, "Reader", "reader", "https://leetcode.cn/problems/reader/"),
        "Read input", "class Solution: pass",
        meta_data='{"params": [{"name": "reader", "type": "integer"}]}',
    )
    result = check_candidate(candidate(
        "class Solution:\n    def solve(self, reader: ArrayReader) -> int:\n        return reader.get(0)\n",
        [SampleCase(args=[[1]], expected=1)],
    ), problem)
    assert result.passed
    assert "unsupported ArrayReader" in result.detail


def test_non_json_python_container_annotations_skip_local_cases():
    for annotation in ("set[int]", "tuple[int, int]", "Deque[int]", "List['TreeNode']"):
        result = check_candidate(candidate(
            "class Solution:\n    def solve(self, value: " + annotation + ") -> int:\n"
            "        raise RuntimeError('site only')\n",
            [SampleCase(args=[[1, 2]], expected=3)],
        ))
        assert result.passed, annotation
        assert "skipped for unsupported " + annotation in result.detail


def test_builtin_judge_api_and_base_class_skip_local_cases():
    api = check_candidate(candidate(
        "class Solution:\n    def guessNumber(self, n: int) -> int:\n        return guess(n)\n",
        [SampleCase(args=[5], expected=5)],
    ))
    assert api.passed
    assert "judge API guess" in api.detail

    base = check_candidate(candidate(
        "class Solution(VersionControl):\n"
        "    def firstBadVersion(self, n: int) -> int:\n"
        "        return self.isBadVersion(n)\n",
        [SampleCase(args=[5], expected=5)],
    ))
    assert base.passed
    assert "base class" in base.detail


def test_design_problem_without_solution_class_skips_local_cases():
    problem = Problem(
        ProblemRef(146, "LRU Cache", "lru-cache", "https://leetcode.cn/problems/lru-cache/"),
        "Design a cache", "class LRUCache:\n    pass\n",
    )
    result = check_candidate(candidate(
        "class LRUCache:\n    def get(self, key):\n        return -1\n",
        [SampleCase(args=[1], expected=-1)],
    ), problem)
    assert result.passed
    assert "design problem" in result.detail


def test_plain_matrix_and_qualified_typing_annotations_still_run():
    problem = Problem(
        ProblemRef(85, "Maximal Rectangle", "maximal-rectangle", "https://leetcode.cn/problems/maximal-rectangle/"),
        "Find rectangle", "class Solution: pass",
        meta_data='{"params": [{"name": "matrix", "type": "character[][]"}]}',
    )
    result = check_candidate(candidate(
        "class Solution:\n    def area(self, matrix: typing.List[typing.List[str]]) -> int:\n"
        "        return len(matrix)\n",
        [SampleCase(args=[[["1", "0"], ["1", "1"]]], expected=3)],
    ), problem)
    assert not result.passed
    assert "expected 3, got 2" in result.detail


def test_void_node_methods_compare_serialized_mutated_input():
    list_result = check_candidate(candidate(
        "class Solution:\n    def change(self, head: Optional[ListNode]) -> None:\n"
        "        head.val = 9\n",
        [SampleCase(args=[[1, 2]], expected=[9, 2])],
    ))
    tree_result = check_candidate(candidate(
        "class Solution:\n    def change(self, root: typing.Optional[TreeNode]) -> None:\n"
        "        root.left.val = 9\n",
        [SampleCase(args=[[1, 2, 3]], expected=[1, 9, 3])],
    ))
    assert list_result.passed, list_result.detail
    assert tree_result.passed, tree_result.detail


def test_missing_runtime_import_is_still_reported():
    result = check_candidate(candidate(
        "class Solution:\n    def count(self, values: List[int]) -> int:\n"
        "        return Counter(values)[1]\n",
        [SampleCase(args=[[1]], expected=1)],
    ))
    assert not result.passed
    assert "NameError: name 'Counter' is not defined" in result.detail


def test_submission_method_matches_official_python_starter_without_local_cases():
    problem = Problem(
        ProblemRef(126, "Word Ladder II", "word-ladder-ii", "https://leetcode.cn/problems/word-ladder-ii/"),
        "Return shortest paths.",
        "class Solution:\n"
        "    def findLadders(self, beginWord: str, endWord: str, wordList: list[str]) -> list[list[str]]:\n"
        "        pass\n",
    )
    wrong_name = candidate(
        "class Solution:\n    def ladder(self, beginWord, endWord, wordList):\n        return []\n", []
    )
    wrong_arity = candidate(
        "class Solution:\n    def findLadders(self, beginWord, endWord):\n        return []\n", []
    )
    assert "Solution.findLadders" in check_candidate(wrong_name, problem).detail
    assert "signature" in check_candidate(wrong_arity, problem).detail

    helper_first = candidate(
        "class Solution:\n"
        "    def helper(self):\n        return None\n"
        "    def findLadders(self, beginWord, endWord, wordList):\n"
        "        return [[beginWord, endWord]]\n",
        [SampleCase(args=["a", "c", ["c"]], expected=[["a", "c"]])],
    )
    assert check_candidate(helper_first, problem).passed


def test_design_submission_uses_class_and_methods_from_starter():
    problem = Problem(
        ProblemRef(146, "LRU Cache", "lru-cache", "https://leetcode.cn/problems/lru-cache/"),
        "Design a cache.",
        "class LRUCache:\n"
        "    def __init__(self, capacity: int):\n        pass\n"
        "    def get(self, key: int) -> int:\n        pass\n"
        "    def put(self, key: int, value: int) -> None:\n        pass\n",
    )
    wrong = candidate("class Solution:\n    def get(self, key):\n        return -1\n", [])
    assert "class LRUCache" in check_candidate(wrong, problem).detail
    correct = candidate(
        "class LRUCache:\n"
        "    def __init__(self, capacity):\n        self.values = {}\n"
        "    def get(self, key):\n        return self.values.get(key, -1)\n"
        "    def put(self, key, value):\n        self.values[key] = value\n", []
    )
    assert check_candidate(correct, problem).passed


def test_standard_platform_node_class_is_removed_before_site_run():
    problem = Problem(
        ProblemRef(106, "Build Tree", "construct-binary-tree-from-inorder-and-postorder-traversal",
                   "https://leetcode.cn/problems/construct-binary-tree-from-inorder-and-postorder-traversal/"),
        "Build a tree", "# class TreeNode:\nclass Solution: pass",
        meta_data='{"params": [{"type": "integer[]"}, {"type": "integer[]"}], '
                  '"return": {"type": "TreeNode"}}',
    )
    original = candidate(
        "class TreeNode:\n"
        "    def __init__(self, val=0, left=None, right=None):\n"
        "        self.val = val\n"
        "        self.left = left\n"
        "        self.right = right\n\n"
        "class Solution:\n"
        "    def buildTree(self, inorder: list[int], postorder: list[int]) -> TreeNode | None:\n"
        "        return TreeNode(postorder[-1]) if postorder else None\n",
        [SampleCase(args=[[1], [1]], expected=[1])],
    )
    normalized = normalize_platform_node_classes(original, problem)
    assert "class TreeNode:" not in normalized.code
    assert original.code.startswith("class TreeNode:")
    assert not check_candidate(original, problem).passed
    assert check_candidate(normalized, problem).passed


def test_custom_platform_node_class_is_not_silently_removed():
    problem = Problem(
        ProblemRef(106, "Build Tree", "build-tree", "https://leetcode.cn/problems/build-tree/"),
        "Build a tree", "# class TreeNode:\nclass Solution: pass",
        meta_data='{"return": {"type": "TreeNode"}}',
    )
    original = candidate(
        "class TreeNode:\n"
        "    def __init__(self, val=0, left=None, right=None):\n"
        "        self.val, self.left, self.right = val, left, right\n"
        "    def extra(self):\n        return self.val\n\n"
        "class Solution:\n    def buildTree(self) -> TreeNode:\n        return TreeNode(1)\n",
        [],
    )
    assert normalize_platform_node_classes(original, problem) is original
    result = check_candidate(original, problem)
    assert not result.passed
    assert "remove the top-level class TreeNode" in result.detail
