from pathlib import Path
import json

import pytest

from leetcode_agent.local_check import check_candidate
from leetcode_agent.orchestrator import Orchestrator
from leetcode_agent.progress import ArtifactStore, ProgressStore, _digest
from leetcode_agent.settings import load_settings
from leetcode_agent.types import Candidate, CheckResult, JudgeFeedback, ModelUnavailable, Problem, ProblemRef, Reference, SampleCase, SiteUnavailable, SubmissionReceipt


REF = ProblemRef(2, "两数相加", "add-two-numbers", "https://leetcode.cn/problems/add-two-numbers/")
PROBLEM = Problem(REF, "给定两个链表，返回它们的和。", "class Solution:\n    def addTwoNumbers(self):\n        pass\n")
CANDIDATE = Candidate("class Solution:\n    def addTwoNumbers(self):\n        return 1\n", "摘要", "思路", "O(n)", "O(1)")


class FakeBrowser:
    def __init__(self, runs: list[CheckResult], submissions: list[CheckResult] | None = None):
        self.runs = runs
        self.submissions = submissions or []
        self.require_login_called = False
        self.submit_calls = 0
        self.check_calls = 0
        self.baseline_calls = 0

    def require_login(self):
        self.require_login_called = True

    def find_problem(self, number: int):
        return REF if number == 2 else None

    def open_problem(self, ref):
        assert ref == REF
        return PROBLEM

    def run_code(self, code):
        return self.runs.pop(0)

    def prepare_submission(self):
        self.baseline_calls += 1
        return "100"

    def submit_code(self, baseline_id=None):
        assert baseline_id == "100"
        self.submit_calls += 1
        return SubmissionReceipt(str(100 + self.submit_calls), "candidate-hash", "2")

    def check_submission(self, receipt):
        self.check_calls += 1
        assert receipt.submission_id == str(100 + self.submit_calls)
        return self.submissions.pop(0)


class FakeModel:
    def __init__(self):
        self.repairs = 0

    def solve(self, problem):
        return CANDIDATE

    def review(self, problem, candidate):
        return True, []

    def repair(self, problem, candidate, feedback, references):
        self.repairs += 1
        return CANDIDATE


class FakeSearch:
    def __init__(self):
        self.calls = 0

    def search(self, problem):
        self.calls += 1
        return [Reference("参考", "https://example.com/solution", "参考内容")]


def make_agent(tmp_path: Path, browser, model, search, on_progress=None, save_artifacts=True):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    progress = ProgressStore(tmp_path / "Output/progress.json", 1)
    agent = Orchestrator(
        settings,
        progress,
        ArtifactStore(tmp_path / "Output", enabled=save_artifacts),
        browser,
        model,
        search,
        local_check=lambda candidate: CheckResult(True, "local pass"),
        on_progress=on_progress,
    )
    return agent, progress


def test_skips_gap_then_accepts_one_problem(tmp_path):
    browser = FakeBrowser([CheckResult(True, "site run passed")], [CheckResult(True, "accepted")])
    messages = []
    agent, progress = make_agent(tmp_path, browser, FakeModel(), FakeSearch(), messages.append)

    result = agent.run_one()

    state = progress.load()
    assert result["status"] == "accepted"
    assert state["next_id"] == 3
    assert state["records"]["1"]["status"] == "skipped"
    assert state["records"]["2"]["status"] == "accepted"
    folder = tmp_path / "Output/0002-add-two-numbers"
    assert (folder / "solution.py").read_text(encoding="utf-8") == CANDIDATE.code
    assert "site run passed" in (folder / "README.md").read_text(encoding="utf-8")
    assert any("running on LeetCode" in event.message for event in messages)
    assert any("submitting on LeetCode" in event.message for event in messages)
    assert browser.submit_calls == browser.check_calls == 1
    assert state["records"]["2"]["submission_id"] == "101"
    assert state["records"]["2"]["baseline_id"] == "100"


def test_first_attempt_skips_remote_review_in_fast_mode(tmp_path):
    class CountingModel(FakeModel):
        def __init__(self):
            super().__init__()
            self.reviews = 0

        def review(self, problem, candidate):
            self.reviews += 1
            return True, []

    model = CountingModel()
    agent, _ = make_agent(
        tmp_path, FakeBrowser([CheckResult(True, "run passed")], [CheckResult(True, "accepted")]),
        model, FakeSearch(),
    )
    agent.run_one()
    assert model.reviews == 0


def test_saved_local_failure_rechecks_before_requesting_repair(tmp_path):
    browser = FakeBrowser([CheckResult(True, "run passed")], [CheckResult(True, "accepted")])
    class TrackingModel(FakeModel):
        def __init__(self):
            super().__init__()
            self.preparations = 0

        def prepare_problem(self, problem):
            self.preparations += 1
            return problem

    model = TrackingModel()
    agent, progress = make_agent(tmp_path, browser, model, FakeSearch())
    progress.anchor(2)
    folder = agent.artifacts.save(PROBLEM, CANDIDATE, "needs_repair", [
        CheckResult(False, "local cases: NameError: ListNode is not defined"),
    ], [], 1)
    progress.mark_current(
        2, "needs_repair", slug=REF.slug, attempts=1, folder=str(folder),
        code_sha256=_digest(CANDIDATE.code), last_error="local cases: NameError: ListNode is not defined",
        validation_failures=1,
    )

    assert agent.run_one()["status"] == "accepted"
    assert model.repairs == 0
    assert model.preparations == 0
    assert browser.submit_calls == 1


def test_failed_local_case_repairs_before_remote_review_or_browser_run(tmp_path):
    events = []
    corrected = Candidate(CANDIDATE.code.replace("return 1", "return 2"),
                          "摘要", "思路", "O(n)", "O(1)")

    class TrackingModel(FakeModel):
        def repair(self, problem, candidate, feedback, references):
            assert "expected 2, got 1" in feedback
            return corrected

        def review(self, problem, candidate):
            assert events == ["local failed", "local passed"]
            events.append("review")
            return True, []

    class TrackingBrowser(FakeBrowser):
        def run_code(self, code):
            assert code == corrected.code
            assert events[-1] == "review"
            events.append("browser run")
            return super().run_code(code)

    browser = TrackingBrowser([CheckResult(True, "run passed")], [CheckResult(True, "accepted")])
    agent, _ = make_agent(tmp_path, browser, TrackingModel(), FakeSearch())

    def local_check(candidate):
        if candidate.code == CANDIDATE.code:
            events.append("local failed")
            return CheckResult(False, "case 1: expected 2, got 1")
        events.append("local passed")
        return CheckResult(True, "local pass")

    agent.local_check = local_check
    assert agent.run_one()["status"] == "accepted"
    assert events == ["local failed", "local passed", "review", "browser run"]
    assert browser.submit_calls == 1


def test_accepted_moves_to_next_problem_without_another_model_request(tmp_path):
    fallback = Candidate(CANDIDATE.code, "解法已生成；模型未提供摘要。", "详见代码实现。", "未评估", "未评估")

    class MetadataModel(FakeModel):
        def solve(self, problem):
            return fallback

        def complete_metadata(self, problem, candidate):
            pytest.fail("documentation must not block the next problem")

        def summarize(self, problem, candidate, checks, references):
            pytest.fail("retrospective must not block the next problem")

    agent, progress = make_agent(
        tmp_path, FakeBrowser([CheckResult(True, "run passed")], [CheckResult(True, "accepted")]),
        MetadataModel(), FakeSearch(),
    )
    assert agent.run_one()["status"] == "accepted"
    folder = tmp_path / "Output/0002-add-two-numbers"
    readme = (folder / "README.md").read_text(encoding="utf-8")
    assert (folder / "solution.py").read_text(encoding="utf-8") == fallback.code
    assert fallback.summary in readme
    assert "## 复盘" in readme
    assert progress.load()["records"]["2"]["submission_id"] == "101"


def test_three_repairs_then_records_failure_and_search(tmp_path):
    browser = FakeBrowser([CheckResult(False, "wrong answer")] * 4)
    model = FakeModel()
    search = FakeSearch()
    agent, progress = make_agent(tmp_path, browser, model, search)

    result = agent.run_one()

    assert result["status"] == "needs_review"
    assert progress.load()["next_id"] == 3
    assert progress.load()["records"]["2"]["attempts"] == 4
    assert model.repairs == 3
    assert search.calls == 1


def test_review_rejection_does_not_trigger_reference_search(tmp_path):
    class RejectingModel(FakeModel):
        def review(self, problem, candidate):
            return False, ["signature mismatch"]

    model = RejectingModel()
    search = FakeSearch()
    agent, progress = make_agent(tmp_path, FakeBrowser([]), model, search)
    agent.settings.workflow.review_mode = "always"

    result = agent.run_one()

    assert result["status"] == "needs_review"
    assert model.repairs == 3
    assert search.calls == 0
    assert progress.load()["next_id"] == 3


def test_site_failure_keeps_cursor(tmp_path):
    class BlockedBrowser(FakeBrowser):
        def run_code(self, code):
            raise SiteUnavailable("login expired")

    agent, progress = make_agent(tmp_path, BlockedBrowser([]), FakeModel(), FakeSearch())

    with pytest.raises(SiteUnavailable, match="login expired"):
        agent.run_one()

    assert progress.load()["next_id"] == 2
    assert progress.load()["records"]["2"]["status"] == "candidate_ready"


def test_model_failure_keeps_cursor_before_any_submission(tmp_path):
    class DisconnectedModel(FakeModel):
        def solve(self, problem):
            raise ModelUnavailable("model API connection failed")

    browser = FakeBrowser([])
    agent, progress = make_agent(tmp_path, browser, DisconnectedModel(), FakeSearch())
    with pytest.raises(ModelUnavailable, match="connection failed"):
        agent.run_one()
    assert progress.load()["next_id"] == 2
    assert "2" not in progress.load()["records"]
    assert browser.submit_calls == 0


def test_unconfirmed_submission_blocks_duplicate_run(tmp_path):
    class UnconfirmedBrowser(FakeBrowser):
        def submit_code(self, baseline_id=None):
            raise SiteUnavailable("submission result unknown")

    browser = UnconfirmedBrowser([CheckResult(True, "site run passed")])
    agent, progress = make_agent(tmp_path, browser, FakeModel(), FakeSearch())

    with pytest.raises(SiteUnavailable, match="submission result unknown"):
        agent.run_one()

    state = progress.load()
    assert state["next_id"] == 2
    assert state["records"]["2"]["status"] == "submission_unconfirmed"
    with pytest.raises(SiteUnavailable, match="needs confirmation"):
        agent.run_one()

    progress.resolve_unconfirmed("accepted")
    assert progress.load()["next_id"] == 3


def test_unknown_judge_result_preserves_receipt_without_resubmitting(tmp_path):
    class PendingBrowser(FakeBrowser):
        def check_submission(self, receipt):
            self.check_calls += 1
            raise SiteUnavailable("submission 101 result could not be confirmed")

    browser = PendingBrowser([CheckResult(True, "site run passed")])
    agent, progress = make_agent(tmp_path, browser, FakeModel(), FakeSearch())
    with pytest.raises(SiteUnavailable, match="could not be confirmed"):
        agent.run_one()
    record = progress.load()["records"]["2"]
    assert record["submission_id"] == "101"
    assert record["code_sha256"] == "candidate-hash"
    with pytest.raises(SiteUnavailable, match="needs confirmation"):
        agent.run_one()
    assert browser.submit_calls == browser.check_calls == 1


def test_existing_ac_is_skipped_without_generating_solution(tmp_path):
    class AcceptedBrowser(FakeBrowser):
        def open_problem(self, ref):
            return Problem(PROBLEM.ref, PROBLEM.statement, PROBLEM.starter_code, site_status="AC")

        def find_problem(self, number):
            if number == 2:
                return REF
            raise SiteUnavailable("stop after skip")

    agent, progress = make_agent(tmp_path, AcceptedBrowser([]), FakeModel(), FakeSearch())
    progress.anchor(2)
    with pytest.raises(SiteUnavailable, match="stop after skip"):
        agent.run_one()
    assert progress.load()["records"]["2"]["status"] == "already_accepted"
    assert not (tmp_path / "Output/0002-add-two-numbers").exists()


def test_saved_candidate_rechecks_locally_and_on_site_without_solving(tmp_path):
    browser = FakeBrowser([CheckResult(True, "fresh run passed")], [CheckResult(True, "accepted")])
    model = FakeModel()
    agent, progress = make_agent(tmp_path, browser, model, FakeSearch())
    progress.anchor(2)
    folder = agent.artifacts.save(PROBLEM, CANDIDATE, "candidate_ready", [], [], 1)
    from leetcode_agent.progress import _digest
    progress.mark_current(2, "candidate_ready", **{
        "slug": REF.slug, "folder": str(folder), "attempts": 1,
        "code_sha256": _digest(CANDIDATE.code),
    })

    def cannot_solve(problem):
        pytest.fail("saved candidate must be reused")

    model.solve = cannot_solve
    assert agent.run_one()["status"] == "accepted"
    assert browser.baseline_calls == browser.submit_calls == 1
    assert "fresh run passed" in (folder / "README.md").read_text(encoding="utf-8")


def test_baseline_failure_preserves_verified_run_without_submission_intent(tmp_path):
    class BaselineFailure(FakeBrowser):
        def prepare_submission(self):
            raise SiteUnavailable("old extension lacks submission_baseline")

    browser = BaselineFailure([CheckResult(True, "site run passed")])
    agent, progress = make_agent(tmp_path, browser, FakeModel(), FakeSearch())
    with pytest.raises(SiteUnavailable, match="submission_baseline"):
        agent.run_one()
    record = progress.load()["records"]["2"]
    assert record["status"] == "run_verified"
    assert "baseline_id" not in record
    assert browser.submit_calls == 0


def test_submit_intent_is_durable_before_browser_action(tmp_path):
    class InspectingBrowser(FakeBrowser):
        def submit_code(self, baseline_id=None):
            record = progress.load()["records"]["2"]
            assert record["status"] == "submit_intent"
            assert record["baseline_id"] == "100"
            assert record["code_sha256"]
            raise SiteUnavailable("receipt lost")

    browser = InspectingBrowser([CheckResult(True, "site run passed")])
    agent, progress = make_agent(tmp_path, browser, FakeModel(), FakeSearch())
    with pytest.raises(SiteUnavailable, match="receipt lost"):
        agent.run_one()
    assert progress.load()["records"]["2"]["status"] == "submission_unconfirmed"
    with pytest.raises(SiteUnavailable, match="needs confirmation"):
        agent.run_one()


def test_problem_eight_legacy_candidate_reaches_next_cursor_without_solve(tmp_path):
    ref = ProblemRef(8, "atoi", "string-to-integer-atoi",
                     "https://leetcode.cn/problems/string-to-integer-atoi/")
    problem = Problem(ref, "Parse an integer from a string.", "class Solution:\n    def myAtoi(self):\n        pass")

    class Browser(FakeBrowser):
        def find_problem(self, number):
            return ref if number == 8 else None

        def open_problem(self, found):
            assert found == ref
            return problem

    browser = Browser([CheckResult(True, "fresh site run")], [CheckResult(True, "accepted")])
    model = FakeModel()
    agent, progress = make_agent(tmp_path, browser, model, FakeSearch())
    progress.anchor(8)
    folder = tmp_path / "Output" / "0008-string-to-integer-atoi"
    (folder / "attempts").mkdir(parents=True)
    (folder / "solution.py").write_text(CANDIDATE.code, encoding="utf-8")
    (folder / "attempts" / "01.py").write_text(CANDIDATE.code, encoding="utf-8")
    (folder / "attempts" / "01.json").write_text(json.dumps({
        "status": "submission_unconfirmed",
        "checks": [{"passed": True, "detail": "站内运行: Accepted"}],
    }), encoding="utf-8")
    (folder / "README.md").write_text(
        "# 8. atoi\n- 题目：https://leetcode.cn/problems/string-to-integer-atoi/\n"
        "- 状态：submission_unconfirmed\n## 题意摘要\nsummary\n## 解法\napproach\n"
        "## 复杂度\n- 时间：O(n)\n- 空间：O(1)\n## 验证与提交\n", encoding="utf-8")
    model.solve = lambda problem: pytest.fail("legacy candidate should be reused")

    assert agent.run_one()["status"] == "accepted"
    assert progress.load()["next_id"] == 9
    assert browser.submit_calls == 1
    assert (folder / "attempts" / "01-legacy.md").exists()


def test_problem_eight_external_ac_reconciles_legacy_readme_without_claiming_agent_submit(tmp_path):
    ref = ProblemRef(8, "atoi", "string-to-integer-atoi",
                     "https://leetcode.cn/problems/string-to-integer-atoi/")
    problem = Problem(ref, "Parse an integer from a string.",
                      "class Solution:\n    def myAtoi(self):\n        pass", site_status="AC")

    class AcceptedBrowser(FakeBrowser):
        def find_problem(self, number):
            if number == 8:
                return ref
            raise SiteUnavailable("stop after accepted skip")

        def open_problem(self, found):
            return problem

    agent, progress = make_agent(tmp_path, AcceptedBrowser([]), FakeModel(), FakeSearch())
    progress.anchor(8)
    folder = tmp_path / "Output" / "0008-string-to-integer-atoi"
    (folder / "attempts").mkdir(parents=True)
    (folder / "solution.py").write_text(CANDIDATE.code, encoding="utf-8")
    (folder / "attempts" / "01.py").write_text(CANDIDATE.code, encoding="utf-8")
    metadata = json.dumps({"status": "submission_unconfirmed", "checks": [
        {"passed": True, "detail": "站内运行: Accepted"}]})
    (folder / "attempts" / "01.json").write_text(metadata, encoding="utf-8")
    old_readme = (
        "# 8. atoi\n- 题目：https://leetcode.cn/problems/string-to-integer-atoi/\n"
        "- 状态：submission_unconfirmed\n## 题意摘要\nsummary\n## 解法\napproach\n"
        "## 复杂度\n- 时间：O(n)\n- 空间：O(1)\n## 验证与提交\n"
    )
    (folder / "README.md").write_text(old_readme, encoding="utf-8")

    with pytest.raises(SiteUnavailable, match="stop after accepted skip"):
        agent.run_one()

    assert progress.load()["next_id"] == 9
    assert progress.load()["records"]["8"]["status"] == "already_accepted"
    assert "未确认本程序提交 ID" in (folder / "README.md").read_text(encoding="utf-8")
    assert (folder / "attempts" / "01-legacy.md").read_text(encoding="utf-8") == old_readme
    assert (folder / "attempts" / "01.json").read_text(encoding="utf-8") == metadata
    assert (folder / "solution.py").read_text(encoding="utf-8") == CANDIDATE.code


def test_judged_failure_resumes_repair_without_resubmitting_old_code(tmp_path):
    repaired = Candidate(CANDIDATE.code.replace("return 1", "return 2"),
                         "摘要", "思路", "O(n)", "O(1)")

    class TrackingBrowser(FakeBrowser):
        def __init__(self):
            super().__init__([CheckResult(True, "run passed")] * 2,
                             [CheckResult(False, "Wrong Answer"), CheckResult(True, "Accepted")])
            self.run_codes = []

        def run_code(self, code):
            self.run_codes.append(code)
            return super().run_code(code)

    class InterruptingModel(FakeModel):
        def __init__(self):
            super().__init__()
            self.fail_repair = True

        def repair(self, problem, candidate, feedback, references):
            assert candidate.code == CANDIDATE.code
            assert "Wrong Answer" in feedback
            if self.fail_repair:
                raise ModelUnavailable("repair API unavailable")
            return repaired

    browser = TrackingBrowser()
    model = InterruptingModel()
    agent, progress = make_agent(tmp_path, browser, model, FakeSearch())
    with pytest.raises(ModelUnavailable, match="repair API unavailable"):
        agent.run_one()
    assert progress.load()["records"]["2"]["status"] == "needs_repair"
    assert browser.submit_calls == 1

    model.fail_repair = False
    model.solve = lambda problem: pytest.fail("resume must repair the saved candidate")
    resumed, _ = make_agent(tmp_path, browser, model, FakeSearch())
    assert resumed.run_one()["status"] == "accepted"
    assert browser.run_codes == [CANDIDATE.code, repaired.code]
    assert browser.submit_calls == 2


def test_paused_artifacts_keep_repair_snapshot_in_progress_only(tmp_path):
    corrected = Candidate(CANDIDATE.code.replace("return 1", "return 2"),
                          "摘要", "思路", "O(n)", "O(1)")

    class Model(FakeModel):
        fail_repair = True

        def repair(self, problem, candidate, feedback, references):
            assert candidate.code == CANDIDATE.code
            assert "Wrong Answer" in feedback
            if self.fail_repair:
                raise ModelUnavailable("temporary model failure")
            return corrected

    browser = FakeBrowser(
        [CheckResult(True, "run passed")] * 2,
        [CheckResult(False, "Wrong Answer", JudgeFeedback("1", "1", "2")),
         CheckResult(True, "Accepted")],
    )
    model = Model()
    agent, progress = make_agent(tmp_path, browser, model, FakeSearch(), save_artifacts=False)
    with pytest.raises(ModelUnavailable, match="temporary model failure"):
        agent.run_one()

    saved = progress.load()["records"]["2"]
    assert saved["status"] == "needs_repair"
    assert saved["snapshot"]["candidate"]["code"] == CANDIDATE.code
    assert saved["snapshot"]["checks"][-1]["judge_feedback"]["expected_output"] == "2"
    assert not (tmp_path / "Output/0002-add-two-numbers").exists()

    model.fail_repair = False
    resumed, _ = make_agent(tmp_path, browser, model, FakeSearch(), save_artifacts=False)
    assert resumed.run_one()["status"] == "accepted"
    assert progress.load()["next_id"] == 3
    assert browser.submit_calls == 2
    assert sorted(path.name for path in (tmp_path / "Output").iterdir()) == ["progress-history", "progress.json"]


def test_judge_case_reaches_repair_and_local_check_after_restart(tmp_path):
    problem = Problem(
        REF, "Return the next integer.", "class Solution:\n    def compute(self, x: int) -> int:\n        pass\n",
        meta_data=json.dumps({"params": [{"name": "x", "type": "integer"}]}),
    )
    original = Candidate(
        "class Solution:\n    def compute(self, x: int) -> int:\n        return x\n",
        "summary", "approach", "O(1)", "O(1)",
    )
    corrected = Candidate(original.code.replace("return x", "return x + 1"), "summary", "approach", "O(1)", "O(1)")
    failure = JudgeFeedback("x = 1", "1", "2")

    class Browser(FakeBrowser):
        def open_problem(self, ref):
            return problem

    class Model(FakeModel):
        fail_repair = True
        feedback = ""

        def solve(self, current):
            return original

        def repair(self, current, candidate, feedback, references):
            self.feedback = feedback
            assert candidate.tests[0] == SampleCase(args=[1], expected=2)
            if self.fail_repair:
                raise ModelUnavailable("repair API unavailable")
            return corrected

    browser = Browser(
        [CheckResult(True, "run passed")] * 2,
        [CheckResult(False, "Wrong Answer", failure), CheckResult(True, "Accepted")],
    )
    model = Model()
    agent, progress = make_agent(tmp_path, browser, model, FakeSearch())
    agent.local_check = check_candidate
    with pytest.raises(ModelUnavailable, match="repair API unavailable"):
        agent.run_one()
    assert progress.load()["records"]["2"]["status"] == "needs_repair"

    model.fail_repair = False
    resumed, _ = make_agent(tmp_path, browser, model, FakeSearch())
    resumed.local_check = check_candidate
    assert resumed.run_one()["status"] == "accepted"
    assert "Failing input:\nx = 1" in model.feedback
    assert "Actual output:\n1" in model.feedback
    assert "Expected output:\n2" in model.feedback
    attempt = json.loads((tmp_path / "Output/0002-add-two-numbers/attempts/02.json").read_text(encoding="utf-8"))
    assert attempt["candidate"]["tests"][0] == {"args": [1], "expected": 2}


@pytest.mark.parametrize("missing_in_catalog", [True, False])
def test_saved_candidate_is_not_skipped_when_catalog_or_page_is_unavailable(tmp_path, missing_in_catalog):
    class UnavailableBrowser(FakeBrowser):
        def find_problem(self, number):
            return None if missing_in_catalog else REF

        def open_problem(self, ref):
            return None

    agent, progress = make_agent(tmp_path, UnavailableBrowser([]), FakeModel(), FakeSearch())
    progress.anchor(2)
    folder = agent.artifacts.save(PROBLEM, CANDIDATE, "candidate_ready", [], [], 1)
    from leetcode_agent.progress import _digest
    progress.mark_current(2, "candidate_ready", slug=REF.slug, folder=str(folder), attempts=1,
                          code_sha256=_digest(CANDIDATE.code))
    with pytest.raises(SiteUnavailable, match="saved candidate"):
        agent.run_one()
    assert progress.load()["next_id"] == 2
    assert progress.load()["records"]["2"]["status"] == "candidate_ready"


def test_site_ac_reconciles_saved_candidate_without_claiming_agent_submission(tmp_path):
    class AcceptedBrowser(FakeBrowser):
        def open_problem(self, ref):
            return Problem(REF, PROBLEM.statement, PROBLEM.starter_code, site_status="AC")

        def find_problem(self, number):
            if number == 2:
                return REF
            raise SiteUnavailable("stop after skip")

    browser = AcceptedBrowser([])
    agent, progress = make_agent(tmp_path, browser, FakeModel(), FakeSearch())
    progress.anchor(2)
    folder = agent.artifacts.save(PROBLEM, CANDIDATE, "candidate_ready", [], [], 1)
    from leetcode_agent.progress import _digest
    progress.mark_current(2, "candidate_ready", slug=REF.slug, folder=str(folder), attempts=1,
                          code_sha256=_digest(CANDIDATE.code))
    old_readme = (folder / "README.md").read_text(encoding="utf-8")

    with pytest.raises(SiteUnavailable, match="stop after skip"):
        agent.run_one()
    assert browser.submit_calls == 0
    assert progress.load()["next_id"] == 3
    assert progress.load()["records"]["2"]["status"] == "already_accepted"
    assert "未确认本程序提交 ID" in (folder / "README.md").read_text(encoding="utf-8")
    assert (folder / "attempts" / "01-before-external-ac.md").read_text(encoding="utf-8") == old_readme
