import hashlib

import pytest

from leetcode_agent.__main__ import (_handle_rate_limited_submission, _recover_submission,
                                     _recover_with_cooldown, _select_start)
from leetcode_agent.progress import ArtifactStore, ProgressStore
from leetcode_agent.types import Candidate, CheckResult, JudgeFeedback, Problem, ProblemRef, SiteUnavailable


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
        def check_submission(self, receipt, timeout=180):
            assert receipt.submission_id == "123"
            assert receipt.question_id == "1"
            return CheckResult(True, "Accepted")

    _recover_submission(progress, artifacts, Browser(), lambda message: None)
    state = progress.load()
    assert state["next_id"] == 2
    assert state["records"]["1"]["confirmed_by_judge"] is True
    assert "accepted（站内提交 ID 确认）" in (folder / "README.md").read_text(encoding="utf-8")


def test_captured_id_requires_problem_and_code_verification_before_judge(tmp_path):
    progress, artifacts, folder = pending(tmp_path)
    record = progress.load()["records"]["1"]
    progress.mark_current(1, "submission_unconfirmed", **{
        **{key: value for key, value in record.items() if key not in {"status", "updated_at"}},
        "slug": "two-sum", "submission_verified": False,
    })

    class Browser:
        def __init__(self):
            self.verified = False

        def verify_captured_submission(self, slug, submission_id, digest):
            assert (slug, submission_id, digest) == ("two-sum", "123", record["code_sha256"])
            self.verified = True

        def check_submission(self, receipt, timeout=180):
            assert self.verified
            assert progress.load()["records"]["1"]["submission_verified"] is True
            return CheckResult(True, "Accepted")

    browser = Browser()
    _recover_submission(progress, artifacts, browser, lambda message: None)
    assert browser.verified
    assert progress.load()["records"]["1"]["confirmed_by_judge"] is True


def test_captured_id_mismatch_keeps_pending_state(tmp_path):
    progress, artifacts, _ = pending(tmp_path)
    record = progress.load()["records"]["1"]
    progress.mark_current(1, "submission_unconfirmed", **{
        **{key: value for key, value in record.items() if key not in {"status", "updated_at"}},
        "slug": "two-sum", "submission_verified": False,
    })

    class Browser:
        def verify_captured_submission(self, slug, submission_id, digest):
            raise SiteUnavailable("captured submission code does not match the candidate")

        def check_submission(self, receipt, timeout=180):
            pytest.fail("unverified ID must not reach the judge")

    with pytest.raises(SiteUnavailable, match="does not match"):
        _recover_submission(progress, artifacts, Browser(), lambda message: None)
    assert progress.load()["records"]["1"]["submission_verified"] is False
    assert progress.load()["next_id"] == 1


def test_captured_id_rate_limit_can_fall_back_to_confirmed_site_ac(tmp_path):
    progress, artifacts, _ = pending(tmp_path)
    record = progress.load()["records"]["1"]
    progress.mark_current(1, "submission_unconfirmed", **{
        **{key: value for key, value in record.items() if key not in {"status", "updated_at"}},
        "title": "Two Sum", "slug": "two-sum", "attempts": 1, "submission_verified": False,
    })

    class Browser:
        def verify_captured_submission(self, slug, submission_id, digest):
            raise SiteUnavailable("超出访问限制，请稍后再试")

        def pending_problem_accepted(self, problem):
            return True

        def check_submission(self, receipt, timeout=180):
            pytest.fail("unverified ID must not reach the judge")

    _recover_submission(progress, artifacts, Browser(), lambda message: None)
    state = progress.load()
    assert state["records"]["1"]["status"] == "already_accepted"
    assert state["records"]["1"]["unconfirmed_submission_id"] == "123"
    assert state["next_id"] == 2


def test_rate_limit_recovery_waits_then_only_rechecks(tmp_path, monkeypatch):
    attempts = []
    waits = []
    def recover(*args):
        attempts.append(1)
        if len(attempts) < 3:
            raise SiteUnavailable("超出访问限制，请稍后再试")

    monkeypatch.setattr("leetcode_agent.__main__._recover_submission", recover)
    monkeypatch.setattr("leetcode_agent.__main__._wait_seconds", lambda seconds, stopped: waits.append(seconds))
    _recover_with_cooldown(None, None, None, lambda message: None, initial_rate_limit=True)
    assert waits == [60, 60, 120]
    assert len(attempts) == 3


def test_rate_limit_navigates_before_cooling_down_and_recovery(tmp_path, monkeypatch):
    progress, artifacts, _ = pending(tmp_path)
    record = progress.load()["records"]["1"]
    progress.mark_current(1, "submission_unconfirmed", slug="two-sum", folder=record["folder"],
                          submission_id="123", code_sha256=record["code_sha256"])
    events = []

    class Browser:
        def navigate_after_rate_limit(self, number, slug):
            events.append(("navigate", number, slug))

    def recover(*args, **kwargs):
        events.append(("recover", kwargs["initial_rate_limit"]))

    monkeypatch.setattr("leetcode_agent.__main__._recover_with_cooldown", recover)
    _handle_rate_limited_submission(progress, artifacts, Browser(), lambda message: None)
    assert events == [("navigate", 1, "two-sum"), ("recover", True)]


def test_rate_limit_navigation_failure_still_cools_down_and_recovers(tmp_path, monkeypatch):
    progress, artifacts, _ = pending(tmp_path)
    record = progress.load()["records"]["1"]
    progress.mark_current(1, "submission_unconfirmed", slug="two-sum", folder=record["folder"],
                          submission_id="123", code_sha256=record["code_sha256"])
    calls = []

    class Browser:
        def navigate_after_rate_limit(self, number, slug):
            raise SiteUnavailable("next page is unavailable")

    monkeypatch.setattr("leetcode_agent.__main__._recover_with_cooldown",
                        lambda *args, **kwargs: calls.append(kwargs["initial_rate_limit"]))
    _handle_rate_limited_submission(progress, artifacts, Browser(), lambda message: None)
    assert calls == [True]
    assert progress.load()["next_id"] == 1


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
        def check_submission(self, receipt, timeout=180):
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
        def check_submission(self, receipt, timeout=180):
            return CheckResult(True, "Accepted")

    _recover_submission(progress, ArtifactStore(tmp_path / "Output", enabled=False),
                        Browser(), lambda message: None)
    assert progress.load()["next_id"] == 2
    assert readme.read_bytes() == before


def test_recovery_refuses_mismatched_saved_code(tmp_path):
    progress, artifacts, _ = pending(tmp_path, "bad-hash")

    class Browser:
        def check_submission(self, receipt, timeout=180):
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

        def check_submission(self, receipt, timeout=180):
            assert receipt.submission_id == "101"
            return CheckResult(True, "Accepted")

    _recover_submission(progress, artifacts, Browser(), lambda message: None)
    assert progress.load()["records"]["1"]["submission_id"] == "101"
    assert progress.load()["next_id"] == 2


def test_missing_id_with_site_ac_on_original_page_advances_without_claiming_agent_submit(tmp_path):
    progress, artifacts, folder = pending(tmp_path)
    record = progress.load()["records"]["1"]
    progress.mark_current(1, "submission_unconfirmed", folder=str(folder), title="Two Sum",
                          slug="two-sum", attempts=1, code_sha256=record["code_sha256"], question_id="1")

    class Browser:
        def pending_problem_accepted(self, problem):
            assert problem.ref.number == 1
            return True

        def recover_submission(self, slug, digest, baseline):
            pytest.fail("site AC should resolve the missing ID without querying submission history")

    _recover_submission(progress, artifacts, Browser(), lambda message: None)
    state = progress.load()
    assert state["next_id"] == 2
    assert state["records"]["1"]["status"] == "already_accepted"
    assert not state["records"]["1"].get("confirmed_by_judge")


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

        def check_submission(self, receipt, timeout=180):
            return CheckResult(True, "Accepted")

    _recover_submission(progress, artifacts, Browser(), lambda message: None)
    assert progress.load()["records"]["1"]["status"] == "accepted"


def test_saved_cursor_wins_after_recovered_ac_even_if_edge_is_on_old_problem(tmp_path):
    progress, artifacts, _ = pending(tmp_path)

    class Browser:
        def check_submission(self, receipt, timeout=180):
            return CheckResult(True, "Accepted")

    _recover_submission(progress, artifacts, Browser(), lambda message: None)
    assert _select_start(progress, current_number=1, start_current=False) == 2
    assert progress.load()["next_id"] == 2
    with pytest.raises(ValueError, match="already finished"):
        _select_start(progress, current_number=1, start_current=True)


@pytest.mark.parametrize("save_artifacts", [True, False])
def test_failed_saved_submission_resumes_repair_without_resubmitting(tmp_path, save_artifacts):
    progress = ProgressStore(tmp_path / "Output/progress.json", 1)
    artifacts = ArtifactStore(tmp_path / "Output", enabled=save_artifacts)
    ref = ProblemRef(1, "Two Sum", "two-sum", "https://leetcode.cn/problems/two-sum/")
    problem = Problem(ref, "statement", CODE)
    candidate = Candidate(CODE, "summary", "approach", "O(n)", "O(n)")
    folder = artifacts.save(problem, candidate, "submission_unconfirmed", [], [], 1)
    digest = hashlib.sha256(CODE.strip().encode()).hexdigest()
    progress.mark_current(1, "submission_unconfirmed", title=ref.title, slug=ref.slug, url=ref.url,
                          attempts=1, submission_id="123", code_sha256=digest, question_id="1",
                          **({"folder": str(folder)} if save_artifacts else {"snapshot": artifacts.snapshot}))

    class Browser:
        def check_submission(self, receipt, timeout=180):
            assert receipt.submission_id == "123"
            return CheckResult(False, "站内提交 123: 超出时间限制 | cases=44/47",
                               JudgeFeedback("nums = [1,1,1]\nk = 2", "", ""))

    notes = []
    _recover_submission(progress, artifacts, Browser(), notes.append)
    state = progress.load()
    record = state["records"]["1"]
    assert state["next_id"] == 1
    assert record["status"] == "needs_repair"
    assert record["validation_failures"] == 1
    assert "超出时间限制" in record["last_error"]
    assert "nums = [1,1,1]" in record["last_error"]
    assert "submission_id" not in record
    checks, _ = artifacts.load_attempt_context(record)
    assert checks[-1].judge_feedback.testcase == "nums = [1,1,1]\nk = 2"
    if save_artifacts:
        assert "- 状态：needs_repair" in (folder / "README.md").read_text(encoding="utf-8")
    else:
        assert record["snapshot"]["candidate"]["code"] == CODE
    assert notes[-1].endswith("repairing from judge feedback.")


@pytest.mark.parametrize("save_artifacts", [True, False])
def test_manual_ac_on_next_page_supersedes_unconfirmed_old_submission(tmp_path, save_artifacts):
    progress = ProgressStore(tmp_path / "Output/progress.json", 1)
    artifacts = ArtifactStore(tmp_path / "Output", enabled=save_artifacts)
    ref = ProblemRef(1, "Two Sum", "two-sum", "https://leetcode.cn/problems/two-sum/")
    problem = Problem(ref, "statement", CODE, site_question_id="1")
    candidate = Candidate(CODE, "summary", "approach", "O(n)", "O(n)")
    folder = artifacts.save(problem, candidate, "submission_unconfirmed", [], [], 1)
    progress.mark_current(
        1, "submission_unconfirmed", title=ref.title, slug=ref.slug, url=ref.url,
        attempts=1, submission_id="123", code_sha256=hashlib.sha256(CODE.strip().encode()).hexdigest(),
        question_id="1", **({"folder": str(folder)} if save_artifacts else {"snapshot": artifacts.snapshot}),
    )

    class Browser:
        def next_page_after_manual_acceptance(self, current):
            assert current.ref == ref
            assert current.site_question_id == "1"
            return True

        def check_submission(self, receipt, timeout=180):
            pytest.fail("site AC on the next page should resolve the old pending result first")

    notes = []
    _recover_submission(progress, artifacts, Browser(), notes.append)
    state = progress.load()
    assert state["next_id"] == 2
    assert state["records"]["1"]["status"] == "already_accepted"
    assert state["records"]["1"]["unconfirmed_submission_id"] == "123"
    assert "submission_id" not in state["records"]["1"]
    if save_artifacts:
        assert "- 状态：already_accepted" in (folder / "README.md").read_text(encoding="utf-8")
    assert notes[-1].endswith("continuing.")


def test_manual_ac_detected_after_old_submission_fails(tmp_path):
    progress = ProgressStore(tmp_path / "Output/progress.json", 1)
    artifacts = ArtifactStore(tmp_path / "Output", enabled=False)
    ref = ProblemRef(1, "Two Sum", "two-sum", "https://leetcode.cn/problems/two-sum/")
    problem = Problem(ref, "statement", CODE)
    candidate = Candidate(CODE, "summary", "approach", "O(n)", "O(n)")
    artifacts.save(problem, candidate, "submission_unconfirmed", [], [], 1)
    progress.mark_current(1, "submission_unconfirmed", title=ref.title, slug=ref.slug, url=ref.url,
                          attempts=1, submission_id="123", code_sha256=hashlib.sha256(CODE.strip().encode()).hexdigest(),
                          snapshot=artifacts.snapshot)

    class Browser:
        probes = 0

        def next_page_after_manual_acceptance(self, current):
            self.probes += 1
            return self.probes == 2

        def check_submission(self, receipt, timeout=180):
            return CheckResult(False, "Time Limit Exceeded")

    _recover_submission(progress, artifacts, Browser(), lambda message: None)
    assert progress.load()["records"]["1"]["status"] == "already_accepted"


def test_manual_ac_detected_after_one_saved_submission_query_times_out(tmp_path):
    progress = ProgressStore(tmp_path / "Output/progress.json", 1)
    artifacts = ArtifactStore(tmp_path / "Output", enabled=False)
    ref = ProblemRef(1, "Two Sum", "two-sum", "https://leetcode.cn/problems/two-sum/")
    problem = Problem(ref, "statement", CODE)
    candidate = Candidate(CODE, "summary", "approach", "O(n)", "O(n)")
    artifacts.save(problem, candidate, "submission_unconfirmed", [], [], 1)
    progress.mark_current(1, "submission_unconfirmed", title=ref.title, slug=ref.slug, url=ref.url,
                          attempts=1, submission_id="123", code_sha256=hashlib.sha256(CODE.strip().encode()).hexdigest(),
                          snapshot=artifacts.snapshot)

    class Browser:
        probes = 0
        queries = 0

        def next_page_after_manual_acceptance(self, current):
            self.probes += 1
            return self.probes == 2

        def check_submission(self, receipt, timeout=180):
            self.queries += 1
            assert timeout <= 30
            raise SiteUnavailable("submission 123 result could not be confirmed")

    browser = Browser()
    _recover_submission(progress, artifacts, browser, lambda message: None)
    assert browser.queries == 1
    assert progress.load()["records"]["1"]["status"] == "already_accepted"
