from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from .judge_feedback import format_failure, with_judge_cases
from .local_check import check_candidate, normalize_platform_node_classes
from .progress import ArtifactStore, ProgressStore, _digest, record_ref
from .settings import Settings
from .types import Candidate, CheckResult, Problem, ProgressEvent, Reference, SiteUnavailable


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        progress: ProgressStore,
        artifacts: ArtifactStore,
        browser: object,
        model: object,
        search: object,
        local_check=check_candidate,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ):
        self.settings = settings
        self.progress = progress
        self.artifacts = artifacts
        self.browser = browser
        self.model = model
        self.search = search
        self.local_check = local_check
        self.on_progress = on_progress or (lambda event: None)

    def _report(self, number: int, stage: str, message: str, attempt: int = 1) -> None:
        self.on_progress(ProgressEvent(number, stage, message, attempt))

    def _mark_current(self, number: int, status: str, **details: object) -> None:
        if not self.artifacts.enabled:
            if self.artifacts.snapshot is None:
                raise ValueError("candidate snapshot is unavailable")
            details.pop("folder", None)
            details["snapshot"] = self.artifacts.snapshot
        self.progress.mark_current(number, status, **details)

    def _check_local(self, candidate: Candidate, problem: Problem) -> CheckResult:
        return (self.local_check(candidate, problem) if self.local_check is check_candidate
                else self.local_check(candidate))

    def run_one(self) -> dict:
        state = self.progress.load()
        number = state["next_id"]
        current = state["records"].get(str(number), {})
        if any(record.get("status") in {"submit_intent", "submission_unconfirmed"}
               for record in state["records"].values() if record is not current):
            raise SiteUnavailable("another submission result needs confirmation before continuing")
        if current.get("status") in {"submit_intent", "submission_unconfirmed"}:
            raise SiteUnavailable("submission result needs confirmation; use resolve accepted or resolve retry")
        self.browser.require_login()
        for _ in range(1000):
            ref = self.browser.find_problem(number)
            if ref is None:
                if current.get("status") in {"candidate_ready", "run_verified", "needs_repair"}:
                    raise SiteUnavailable(f"saved candidate for problem {number} is missing from the catalog")
                self.progress.advance(number, "skipped", reason="not in algorithm list")
                number += 1
                continue
            self._report(number, "select", f"Problem {number}: reading {ref.title}")
            problem = self.browser.open_problem(ref)
            if problem is None:
                if current.get("status") in {"candidate_ready", "run_verified", "needs_repair"}:
                    raise SiteUnavailable(f"saved candidate for problem {number} is unavailable on LeetCode")
                self._report(number, "skip", f"Problem {number}: premium or unavailable; skipping")
                self.progress.advance(number, "skipped", **record_ref(ref), reason="premium or unavailable")
                number += 1
                continue
            if problem.site_status == "AC":
                self._report(number, "skip", f"Problem {number}: already accepted on LeetCode; skipping")
                if current.get("status") in {"candidate_ready", "run_verified", "needs_repair"}:
                    folder = self.artifacts.reconcile_saved_acceptance(problem, current)
                else:
                    folder = self.artifacts.reconcile_legacy_acceptance(problem)
                details = {"folder": str(folder)} if folder else {}
                self.progress.advance(number, "already_accepted", **record_ref(ref),
                                      reason="accepted on LeetCode; agent submission ID not confirmed",
                                      **details)
                number += 1
                current = {}
                continue
            return self._solve(problem)
        raise SiteUnavailable("more than 1000 consecutive problems were skipped")

    def _solve(self, problem: Problem) -> dict:
        number = problem.ref.number
        visual_prepared = False

        def prepare_for_model() -> None:
            nonlocal problem, visual_prepared
            if visual_prepared:
                return
            prepare = getattr(self.model, "prepare_problem", None)
            if callable(prepare):
                problem = prepare(problem)
            visual_prepared = True

        saved = self.progress.load()["records"].get(str(number), {})
        candidate = self.artifacts.load_candidate(problem, saved) if saved else None
        if candidate is None and not saved:
            candidate = self.artifacts.migrate_problem_eight(problem)
            if candidate is not None:
                folder = self.artifacts.save(problem, candidate, "candidate_ready", [], [], 1)
                self._mark_current(number, "candidate_ready", **record_ref(problem.ref),
                                           attempts=1, folder=str(folder), code_sha256=_digest(candidate.code),
                                           question_id=problem.site_question_id)
        if candidate is not None:
            self._report(number, "resume", f"Problem {number}: rechecking saved candidate before submission")
        else:
            self._report(number, "solve", f"Problem {number}: generating an efficient solution")
            prepare_for_model()
            candidate = self.model.solve(problem)
        first_attempt = saved.get("attempts", 1) if saved else 1
        checks: list[CheckResult] = []
        references: list[Reference] = []
        validation_failures = 0
        searched = False
        if saved.get("status") in {"needs_repair", "candidate_ready", "run_verified"}:
            checks, references = self.artifacts.load_attempt_context(saved)
            validation_failures = saved.get("validation_failures", 0)
            searched = saved.get("searched", False)
        # Model-generated expected outputs are suggestions; only the site judge can confirm them.
        candidate = replace(candidate, tests=[])
        normalized = normalize_platform_node_classes(candidate, problem)
        if normalized.code != candidate.code and saved.get("status") == "needs_repair":
            saved = {**saved, "status": "candidate_ready"}
            validation_failures = max(0, validation_failures - 1)
        candidate = normalized
        if saved.get("status") == "needs_repair":
            feedback = saved.get("last_error")
            last_failed = next((check for check in reversed(checks) if not check.passed), None)
            if last_failed and last_failed.detail.startswith("local cases:"):
                self._report(number, "local", f"Problem {number}: rechecking saved local failure", first_attempt)
                recheck = self._check_local(candidate, problem)
                if recheck.passed:
                    saved = {**saved, "status": "candidate_ready"}
                    validation_failures = max(0, validation_failures - 1)
                elif recheck.detail != last_failed.detail:
                    checks.append(recheck)
                    feedback = recheck.detail
            if saved.get("status") == "needs_repair":
                if last_failed and last_failed.judge_feedback:
                    feedback = format_failure(last_failed)
                if not isinstance(feedback, str) or not feedback:
                    raise ValueError("saved repair feedback is missing")
                if first_attempt >= self.settings.workflow.max_repairs + 1:
                    return self._record_needs_review(problem, candidate, checks, references,
                                                     first_attempt, feedback)
                if validation_failures >= self.settings.search.fallback_after_failures and not searched:
                    self._report(number, "search", f"Problem {number}: searching public explanations", first_attempt)
                    references = self.search.search(problem)
                    searched = True
                self._report(number, "repair", f"Problem {number}: repairing from saved feedback for attempt {first_attempt + 1}", first_attempt + 1)
                candidate = with_judge_cases(problem, candidate, checks)
                prepare_for_model()
                candidate = self.model.repair(problem, candidate, feedback, references)
                first_attempt += 1
        for repair_number in range(first_attempt - 1, self.settings.workflow.max_repairs + 1):
            attempt = repair_number + 1
            candidate = replace(candidate, tests=[])
            candidate = normalize_platform_node_classes(candidate, problem)
            candidate = with_judge_cases(problem, candidate, checks)
            folder = self.artifacts.save(problem, candidate, "candidate_ready", checks, references, attempt)
            self._mark_current(number, "candidate_ready", **record_ref(problem.ref),
                                       attempts=attempt, folder=str(folder), code_sha256=_digest(candidate.code),
                                       question_id=problem.site_question_id)
            self._report(problem.ref.number, "local", f"Problem {problem.ref.number}, attempt {attempt}: checking local cases", attempt)
            result = self._check_local(candidate, problem)
            local_advisory = not result.passed and result.detail.startswith((
                "local cases:", "local cases exceeded", "local process failed:",
            ))
            if local_advisory:
                checks.append(CheckResult(True, f"local cases inconclusive; site will verify: {result.detail}"))
                self._report(problem.ref.number, "local",
                             f"Problem {problem.ref.number}: local case disagreed; checking on LeetCode", attempt)
            else:
                checks.append(result)
            local_passed = result.passed or local_advisory
            needs_review = self.settings.workflow.review_mode == "always" or repair_number > 0
            if local_passed and needs_review:
                self._report(problem.ref.number, "review", f"Problem {problem.ref.number}, attempt {attempt}: reviewing correctness and complexity", attempt)
                prepare_for_model()
                approved, issues = self.model.review(problem, candidate)
            else:
                approved, issues = local_passed, []
            if approved:
                self._report(problem.ref.number, "run", f"Problem {problem.ref.number}, attempt {attempt}: running on LeetCode", attempt)
                result = self.browser.run_code(candidate.code)
                checks.append(result)
                if result.passed:
                    self.artifacts.save(problem, candidate, "run_verified", checks, references, attempt)
                    self._mark_current(number, "run_verified", **record_ref(problem.ref),
                                               attempts=attempt, folder=str(folder),
                                               code_sha256=_digest(candidate.code),
                                               question_id=problem.site_question_id)
                    if not self.settings.workflow.auto_submit:
                        self.artifacts.save(problem, candidate, "awaiting_submission", checks, references, attempt)
                        raise SiteUnavailable("automatic submission is disabled")
                    self._report(number, "prepare", f"Problem {number}, attempt {attempt}: checking submission baseline", attempt)
                    prepare = getattr(self.browser, "prepare_submission", None)
                    baseline_id = prepare() if callable(prepare) else None
                    self.artifacts.save(problem, candidate, "submit_intent", checks, references, attempt)
                    self._mark_current(
                        problem.ref.number,
                        "submit_intent",
                        **record_ref(problem.ref),
                        attempts=attempt,
                        folder=str(folder),
                        baseline_id=baseline_id,
                        code_sha256=_digest(candidate.code),
                        question_id=problem.site_question_id,
                    )
                    self._report(problem.ref.number, "submit", f"Problem {problem.ref.number}, attempt {attempt}: submitting on LeetCode", attempt)
                    try:
                        receipt = self.browser.submit_code(baseline_id)
                    except SiteUnavailable:
                        self.artifacts.save(problem, candidate, "submission_unconfirmed", checks, references, attempt)
                        self._mark_current(
                            number, "submission_unconfirmed", **record_ref(problem.ref),
                            attempts=attempt, folder=str(folder), baseline_id=baseline_id,
                            code_sha256=_digest(candidate.code), question_id=problem.site_question_id,
                        )
                        raise
                    self.artifacts.save(problem, candidate, "submission_unconfirmed", checks, references, attempt)
                    self._mark_current(
                        problem.ref.number,
                        "submission_unconfirmed",
                        **record_ref(problem.ref),
                        attempts=attempt,
                        folder=str(folder),
                        baseline_id=baseline_id,
                        submission_id=receipt.submission_id,
                        code_sha256=receipt.code_sha256,
                        question_id=receipt.question_id,
                    )
                    self._report(problem.ref.number, "judge", f"Problem {problem.ref.number}, attempt {attempt}: checking submission {receipt.submission_id}", attempt)
                    result = self.browser.check_submission(receipt)
                    checks.append(result)
                    if result.passed:
                        archive_message = ("saving solution and review" if self.artifacts.enabled
                                           else "updating progress")
                        self._report(problem.ref.number, "archive",
                                     f"Problem {problem.ref.number}: Accepted; {archive_message}", attempt)
                        retrospective = "\n".join(
                            f"- {check.detail}" for check in checks if not check.passed
                        ) or "本次未记录失败尝试。"
                        self.artifacts.save(problem, candidate, "accepted", checks, references, attempt, retrospective)
                        self.progress.advance(
                            problem.ref.number,
                            "accepted",
                            **record_ref(problem.ref),
                            attempts=attempt,
                            **({"folder": str(folder)} if self.artifacts.enabled else {}),
                            baseline_id=baseline_id,
                            submission_id=receipt.submission_id,
                        )
                        self._report(number, "navigate", f"Problem {number}: Accepted; navigating to problem {number + 1}", attempt)
                        return {"number": problem.ref.number, "status": "accepted",
                                "folder": str(folder) if self.artifacts.enabled else None}
            if not approved and local_passed:
                feedback = "review rejected: " + "; ".join(issues or ["unspecified issue"])
                checks.append(CheckResult(False, feedback))
            elif not result.passed:
                feedback = format_failure(result)
                validation_failures += 1
            self.artifacts.save(problem, candidate, "needs_repair", checks, references, attempt)
            self._mark_current(number, "needs_repair", **record_ref(problem.ref),
                               attempts=attempt, folder=str(folder), code_sha256=_digest(candidate.code),
                               question_id=problem.site_question_id, last_error=feedback,
                               validation_failures=validation_failures, searched=searched)
            if repair_number >= self.settings.workflow.max_repairs:
                break
            if validation_failures >= self.settings.search.fallback_after_failures and not searched:
                self._report(problem.ref.number, "search", f"Problem {problem.ref.number}: searching public explanations", attempt)
                references = self.search.search(problem)
                searched = True
                self.artifacts.save(problem, candidate, "needs_repair", checks, references, attempt)
                self._mark_current(number, "needs_repair", **record_ref(problem.ref),
                                           attempts=attempt, folder=str(folder), code_sha256=_digest(candidate.code),
                                           question_id=problem.site_question_id, last_error=feedback,
                                           validation_failures=validation_failures, searched=True)
            self._report(problem.ref.number, "repair", f"Problem {problem.ref.number}: repairing from feedback for attempt {attempt + 1}", attempt + 1)
            candidate = with_judge_cases(problem, candidate, checks)
            prepare_for_model()
            candidate = self.model.repair(problem, candidate, feedback, references)
        return self._record_needs_review(problem, candidate, checks, references, attempt, feedback)

    def _record_needs_review(self, problem: Problem, candidate: Candidate,
                             checks: list[CheckResult], references: list[Reference],
                             attempt: int, feedback: str) -> dict:
        folder = self.artifacts.save(problem, candidate, "needs_review", checks, references, attempt)
        self.progress.advance(
            problem.ref.number,
            "needs_review",
            **record_ref(problem.ref),
            attempts=attempt,
            **({"folder": str(folder)} if self.artifacts.enabled else {}),
            last_error=feedback[:1200],
        )
        return {"number": problem.ref.number, "status": "needs_review",
                "folder": str(folder) if self.artifacts.enabled else None}

