import hashlib

import pytest

from leetcode_agent.__main__ import _recover_submission, _select_start
from leetcode_agent.progress import ArtifactStore, ProgressStore
from leetcode_agent.types import Candidate, CheckResult, Problem, ProblemRef, SiteUnavailable


CODE = "class Solution:\n    def twoSum(self, nums, target):\n        return [0, 1]\n"


def pending(tmp_path, digest=None):
    progress = ProgressStore(tmp_path / "Output/progress.json", 1)
    artifacts = ArtifactStore(tmp_path / "Output")
    problem = Problem(ProblemRef(1, "Two Sum", "two-sum", "https://leetcode.cn/problems/two-sum/"), "statement", CODE)
    candidate = Candidate(CODE, "summary", "approach", "O(n)", "O(n)")
    folder = artifacts.save(problem, candidate, "submission_unconfirmed", [], [], 1)
    progress.mark_current(
        1, "submission_unconfirmed", folder=str(folder), submission_id="123",
        code_sha256=digest or hashlib.sha256(CODE.strip().encode()).hexdigest(), question_id="1",
    )
    return progress, artifacts, folder


def test_recovers_saved_submission_after_browser_moves_to_next_problem(tmp_path):
    progress, artifacts, folder = pending(tmp_path)

    class Browser:
        def check_submission(self, receipt):
            assert receipt.submission_id == "123"
            assert receipt.question_id == "1"
            return CheckResult(True, "Accepted")

    _recover_submission(progress, artifacts, Browser(), lambda message: None)
    state = progress.load()
    assert state["next_id"] == 2
    assert state["records"]["1"]["confirmed_by_judge"] is True
    assert "accepted（站内提交 ID 确认）" in (folder / "README.md").read_text(encoding="utf-8")


def test_recovers_unconfirmed_submission_without_artifact_files(tmp_path):
    progress = ProgressStore(tmp_path / "Output/progress.json", 1)
    artifacts = ArtifactStore(tmp_path / "Output", enabled=False)
    problem = Problem(ProblemRef(1, "Two Sum", "two-sum", "https://leetcode.cn/problems/two-sum/"),
                      "statement", CODE)
    candidate = Candidate(CODE, "summary", "approach", "O(n)", "O(n)")
    artifacts.save(problem, candidate, "submission_unconfirmed", [], [], 1)
    progress.mark_current(
        1, "submission_unconfirmed", slug="two-sum", submission_id="123",
        code_sha256=hashlib.sha256(CODE.strip().encode()).hexdigest(),
        question_id="1", snapshot=artifacts.snapshot,
    )

    class Browser:
        def check_submission(self, receipt):
            assert receipt.submission_id == "123"
            return CheckResult(True, "Accepted")

    _recover_submission(progress, artifacts, Browser(), lambda message: None)
    assert progress.load()["next_id"] == 2
    assert sorted(path.name for path in (tmp_path / "Output").iterdir()) == ["progress-history", "progress.json"]


def test_recovery_does_not_update_old_artifacts_while_paused(tmp_path):
    progress, _, folder = pending(tmp_path)
    readme = folder / "README.md"
    before = readme.read_bytes()

    class Browser:
        def check_submission(self, receipt):
            return CheckResult(True, "Accepted")

    _recover_submission(progress, ArtifactStore(tmp_path / "Output", enabled=False),
                        Browser(), lambda message: None)
    assert progress.load()["next_id"] == 2
    assert readme.read_bytes() == before


def test_recovery_refuses_mismatched_saved_code(tmp_path):
    progress, artifacts, _ = pending(tmp_path, "bad-hash")

    class Browser:
        def check_submission(self, receipt):
            pytest.fail("must not check a receipt for altered code")

    with pytest.raises(SiteUnavailable, match="no longer matches"):
        _recover_submission(progress, artifacts, Browser(), lambda message: None)
    assert progress.load()["next_id"] == 1


def test_recovers_missing_id_only_from_code_matched_new_submission(tmp_path):
    progress, artifacts, folder = pending(tmp_path)
    record = progress.load()["records"]["1"]
    progress.mark_current(1, "submission_unconfirmed", folder=str(folder), slug="two-sum",
                          baseline_id="100", code_sha256=record["code_sha256"], question_id="1")

    class Browser:
        def recover_submission(self, slug, digest, baseline):
            assert (slug, digest, baseline) == ("two-sum", record["code_sha256"], "100")
            from leetcode_agent.types import SubmissionReceipt
            return SubmissionReceipt("101", digest, "1")

        def check_submission(self, receipt):
            assert receipt.submission_id == "101"
            return CheckResult(True, "Accepted")

    _recover_submission(progress, artifacts, Browser(), lambda message: None)
    assert progress.load()["records"]["1"]["submission_id"] == "101"
    assert progress.load()["next_id"] == 2


def test_submit_intent_recovers_without_clicking_again(tmp_path):
    progress, artifacts, folder = pending(tmp_path)
    record = progress.load()["records"]["1"]
    progress.mark_current(1, "submit_intent", folder=str(folder), slug="two-sum",
                          baseline_id="100", code_sha256=record["code_sha256"], question_id="1")

    class Browser:
        def recover_submission(self, slug, digest, baseline):
            from leetcode_agent.types import SubmissionReceipt
            assert (slug, baseline) == ("two-sum", "100")
            return SubmissionReceipt("101", digest, "1")

        def check_submission(self, receipt):
            return CheckResult(True, "Accepted")

    _recover_submission(progress, artifacts, Browser(), lambda message: None)
    assert progress.load()["records"]["1"]["status"] == "accepted"


def test_saved_cursor_wins_after_recovered_ac_even_if_edge_is_on_old_problem(tmp_path):
    progress, artifacts, _ = pending(tmp_path)

    class Browser:
        def check_submission(self, receipt):
            return CheckResult(True, "Accepted")

    _recover_submission(progress, artifacts, Browser(), lambda message: None)
    assert _select_start(progress, current_number=1, start_current=False) == 2
    assert progress.load()["next_id"] == 2
    with pytest.raises(ValueError, match="already finished"):
        _select_start(progress, current_number=1, start_current=True)
