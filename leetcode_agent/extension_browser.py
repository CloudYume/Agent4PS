from __future__ import annotations

import hashlib
import ipaddress
import time
from collections.abc import Callable
from html import unescape
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .bridge import BrowserBridge
from .catalog import ProblemCatalog
from .types import CheckResult, JudgeFeedback, Problem, ProblemImage, ProblemRef, SiteUnavailable, SubmissionReceipt


def _statement_with_images(content: str, base_url: str) -> tuple[str, tuple[ProblemImage, ...]]:
    soup = BeautifulSoup(content, "html.parser")
    images = []
    for tag in soup.find_all("img"):
        source = tag.get("data-src") or tag.get("src") or ""
        if not source and tag.get("srcset"):
            source = tag["srcset"].split(",", 1)[0].split()[0]
        url = urljoin(base_url, source)
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        try:
            public_host = ipaddress.ip_address(host).is_global
        except ValueError:
            public_host = bool(host) and host != "localhost" and not host.endswith(".local")
        if parsed.scheme != "https" or not public_host or parsed.username or parsed.password:
            tag.replace_with("[Image unavailable]")
            continue
        alt = unescape(str(tag.get("alt") or tag.get("title") or "")).strip()
        images.append(ProblemImage(url, alt))
        label = f"[Image {len(images)}" + (f": {alt}" if alt else "") + "]"
        tag.replace_with(label)
    return unescape(soup.get_text("\n", strip=True)), tuple(images)


class ExtensionBrowser:
    def __init__(
        self, bridge: BrowserBridge, catalog: ProblemCatalog,
        submit_trigger: str = "button", navigation_trigger: str = "url",
        on_pause: Callable[[str], None] | None = None,
    ):
        self.bridge = bridge
        self.catalog = catalog
        self.submit_trigger = submit_trigger
        self.navigation_trigger = navigation_trigger
        self.current: Problem | None = None
        self.last_code: str | None = None
        self.on_pause = on_pause or (lambda message: None)

    def current_number(self) -> int:
        page = self.bridge.wait_for_page(timeout=86400)
        self._require_protocol(page)
        problem = page["problem"]
        if problem.get("challenge") is True:
            raise SiteUnavailable("LeetCode requested a verification challenge")
        if problem.get("logged_in") is not True:
            raise SiteUnavailable("Log in to leetcode.cn in the main Edge window")
        number = problem.get("number")
        if not isinstance(number, int) or number < 1:
            raise SiteUnavailable("current Edge tab is not a numbered algorithm problem")
        return number

    def require_login(self) -> None:
        page = self.bridge.wait_for_page()
        self._require_protocol(page)
        if page["problem"].get("challenge") is True:
            raise SiteUnavailable("LeetCode requested a verification challenge")
        if page["problem"].get("logged_in") is not True:
            raise SiteUnavailable("LeetCode login is required in the main Edge window")

    def find_problem(self, number: int) -> ProblemRef | None:
        return self.catalog.find(number)

    def open_problem(self, ref: ProblemRef) -> Problem | None:
        if self.catalog.is_paid(ref.number):
            return None
        page = self.bridge.wait_for_page()
        self._require_protocol(page)
        if page["slug"] != ref.slug:
            adjacent = page["problem"].get("number") == ref.number - 1
            trigger = "shortcut" if self.navigation_trigger == "shortcut" and adjacent else "url"
            self.bridge.call("navigate", page["slug"], {"url": ref.url, "trigger": trigger}, timeout=45)
            if trigger == "shortcut":
                try:
                    page = self.bridge.wait_for_page(ref.slug, timeout=8)
                except SiteUnavailable as exc:
                    if "timed out" not in str(exc):
                        raise
                    actual = self.bridge.wait_for_page(timeout=10)
                    if actual["slug"] != ref.slug:
                        self.bridge.call(
                            "navigate", actual["slug"], {"url": ref.url, "trigger": "url"}, timeout=45,
                        )
                    page = self._wait_for_url_page(ref, actual["slug"])
            else:
                page = self._wait_for_url_page(ref, page["slug"])
        self._require_protocol(page)
        meta = page["problem"]
        if meta.get("challenge") is True:
            raise SiteUnavailable("LeetCode requested a verification challenge")
        if meta.get("logged_in") is not True:
            raise SiteUnavailable("LeetCode login expired in the main Edge window")
        if meta.get("number") != ref.number or meta.get("slug") != ref.slug:
            raise SiteUnavailable("browser problem metadata does not match the catalog")
        if meta.get("is_paid") is True:
            return None
        if meta.get("enable_run_code") is False:
            return None
        status = meta.get("status")
        if status not in {"AC", "NOT_STARTED", "TRIED"}:
            raise SiteUnavailable(f"unknown LeetCode problem status: {status!r}")
        starter = meta.get("starter")
        content = meta.get("content")
        if not isinstance(starter, str) or "def " not in starter:
            raise SiteUnavailable("Python3 starter code is unavailable")
        if not isinstance(content, str):
            raise SiteUnavailable("problem description is unavailable")
        statement, images = _statement_with_images(content, ref.url)
        if len(statement) < 30:
            raise SiteUnavailable("problem description is incomplete")
        draft = None
        if status != "AC":
            captured = self.bridge.call("read_draft", ref.slug, timeout=45)
            if isinstance(captured.get("code"), str):
                draft = captured["code"]
        self.current = Problem(
            ref, statement, starter, site_status=status, draft_code=draft,
            site_question_id=meta.get("question_id") or None,
            sample_testcase=meta.get("sample_testcase") or "",
            example_testcases=meta.get("example_testcases") or "",
            meta_data=meta.get("meta_data") or "",
            images=images,
        )
        return self.current

    def _wait_for_url_page(self, ref: ProblemRef, previous_slug: str) -> dict:
        try:
            return self.bridge.wait_for_page(ref.slug, timeout=30)
        except SiteUnavailable as exc:
            if "timed out" not in str(exc):
                raise
        status_method = getattr(self.bridge, "status", None)
        status = status_method() if callable(status_method) else {}
        if status.get("connected") and not status.get("active"):
            self.on_pause(f"Problem {ref.number}: waiting for the bound Edge tab to become active")
            page = self.bridge.wait_for_page(timeout=86400)
            if page["slug"] != ref.slug:
                self.bridge.call("navigate", page["slug"],
                                 {"url": ref.url, "trigger": "url"}, timeout=45)
        elif status.get("connected") and status.get("slug") == previous_slug:
            self.on_pause(f"Problem {ref.number}: retrying navigation from the previous problem")
            self.bridge.call("navigate", previous_slug,
                             {"url": ref.url, "trigger": "url"}, timeout=45)
        else:
            self.on_pause(f"Problem {ref.number}: waiting for the new problem page to load")
        return self.bridge.wait_for_page(ref.slug, timeout=60)

    def run_code(self, code: str) -> CheckResult:
        problem = self._current()
        self.last_code = code
        while True:
            try:
                result = self.bridge.call("run", problem.ref.slug, {"code": code}, timeout=150)
                break
            except SiteUnavailable as exc:
                reason = str(exc)
                if not (reason.startswith("bound tab became inactive before code write")
                        or reason in {"bound tab became inactive before editing code",
                                      "bound tab became inactive",
                                      "bound tab is no longer active on the expected problem"}
                        or reason == "run result could not be confirmed (last phase: queued)"):
                    raise
                self.on_pause(f"Problem {problem.ref.number}: waiting for the bound Edge tab to become active")
                page = self.bridge.wait_for_page(timeout=86400, since=time.monotonic())
                self._require_protocol(page)
                metadata = page["problem"]
                if (page["slug"] != problem.ref.slug
                        or metadata.get("number") != problem.ref.number
                        or problem.site_question_id and metadata.get("question_id") != problem.site_question_id):
                    raise SiteUnavailable("bound Edge tab changed problems before the run could resume") from exc
                if metadata.get("challenge") is True or metadata.get("logged_in") is not True:
                    raise SiteUnavailable("LeetCode login or verification is required before resuming the run") from exc
        self._validate_result(result, "run", code)
        detail = self._detail(result)
        if result.get("status_code") == 10:
            # LeetCode's Accepted status is authoritative; compare_result varies by judge mode.
            return CheckResult(True, f"站内运行: {detail}")
        return CheckResult(False, f"站内运行: {detail}", self._judge_feedback(result))

    def prepare_submission(self) -> str | None:
        problem = self._current()
        result = self.bridge.call("submission_baseline", problem.ref.slug, timeout=15)
        latest = result.get("latest_id")
        if latest is not None and (not isinstance(latest, str) or not latest.isdecimal()):
            raise SiteUnavailable("submission baseline is invalid")
        return latest

    def recover_submission(self, slug: str, digest: str, baseline_id: str | None) -> SubmissionReceipt | None:
        page = self.bridge.wait_for_page(timeout=8, require_active=False)
        if page["problem"].get("challenge") is True or page["problem"].get("logged_in") is not True:
            raise SiteUnavailable("LeetCode login or verification is required for submission recovery")
        result = self.bridge.call(
            "recover_submission", page["slug"],
            {"question_slug": slug, "baseline_id": baseline_id, "code_sha256": digest}, timeout=20,
        )
        submission_id = result.get("submission_id")
        if submission_id is None:
            return None
        if not isinstance(submission_id, str) or not submission_id.isdecimal():
            raise SiteUnavailable("recovered submission ID is invalid")
        return SubmissionReceipt(submission_id, digest, self.current.site_question_id if self.current else None)

    def submit_code(self, baseline_id: str | None = None) -> SubmissionReceipt:
        problem = self._current()
        if self.last_code is None:
            raise SiteUnavailable("no verified code is ready for submission")
        digest = hashlib.sha256(self.last_code.strip().encode("utf-8")).hexdigest()
        try:
            result = self.bridge.call(
                "submit", problem.ref.slug,
                {"code_sha256": digest, "trigger": self.submit_trigger}, timeout=20,
            )
        except SiteUnavailable as original:
            for retry in range(3):
                try:
                    receipt = self.recover_submission(problem.ref.slug, digest, baseline_id)
                except SiteUnavailable:
                    raise original
                if receipt:
                    return receipt
                if retry < 2:
                    time.sleep(1)
            raise SiteUnavailable(f"{original}; no matching new submission found") from original
        submission_id = str(result.get("submission_id") or "")
        if (result.get("kind") != "submit" or not submission_id.isdecimal()
                or str(result.get("code", "")).strip() != self.last_code.strip()):
            raise SiteUnavailable("submit receipt does not match the candidate code")
        return SubmissionReceipt(submission_id, digest, problem.site_question_id)

    def check_submission(self, receipt: SubmissionReceipt, timeout: float = 180) -> CheckResult:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            page = self.bridge.wait_for_page(
                timeout=min(30, max(1, deadline - time.monotonic())), require_active=False,
            )
            if page["problem"].get("challenge") is True or page["problem"].get("logged_in") is not True:
                raise SiteUnavailable("LeetCode login or verification is required to check the submission")
            try:
                result = self.bridge.call(
                    "check_submission", page["slug"], {"submission_id": receipt.submission_id}, timeout=25,
                )
            except SiteUnavailable as exc:
                if str(exc).startswith("bound Edge tab left ") or str(exc) == "check_submission result could not be confirmed":
                    continue
                raise
            if result.get("kind") != "submit" or str(result.get("submission_id")) != receipt.submission_id:
                raise SiteUnavailable("LeetCode check result does not match the submission ID")
            question_id = result.get("question_id")
            if receipt.question_id and question_id and str(question_id) != receipt.question_id:
                raise SiteUnavailable("LeetCode check result belongs to another problem")
            if result.get("pending") is True:
                time.sleep(2)
                continue
            if not isinstance(result.get("status_code"), int):
                raise SiteUnavailable("LeetCode check result has no terminal status")
            passed = result["status_code"] == 10
            return CheckResult(
                passed, f"站内提交 {receipt.submission_id}: {self._detail(result)}",
                None if passed else self._judge_feedback(result),
            )
        raise SiteUnavailable(f"submission {receipt.submission_id} result could not be confirmed")

    def _current(self) -> Problem:
        if self.current is None:
            raise SiteUnavailable("no current problem is open")
        return self.current

    @staticmethod
    def _judge_feedback(result: dict) -> JudgeFeedback | None:
        fields = (result.get("last_testcase"), result.get("code_output"), result.get("expected_output"))
        if not any(isinstance(value, str) and value for value in fields):
            return None
        return JudgeFeedback(*(value if isinstance(value, str) else "" for value in fields))

    def _require_protocol(self, page: dict) -> None:
        if isinstance(self.bridge, BrowserBridge):
            self.bridge.require_protocol(page)

    @staticmethod
    def _detail(result: dict) -> str:
        fields = [result.get("status_msg")]
        for label, key in (("runtime", "status_runtime"), ("memory", "status_memory")):
            if result.get(key):
                fields.append(f"{label}={result[key]}")
        for label, key in (("runtime percentile", "runtime_percentile"), ("memory percentile", "memory_percentile")):
            if result.get(key) is not None:
                fields.append(f"{label}={str(result[key]).rstrip('%')}%")
        if isinstance(result.get("total_correct"), int) and isinstance(result.get("total_testcases"), int):
            fields.append(f"cases={result['total_correct']}/{result['total_testcases']}")
        fields.extend((result.get("error"), result.get("last_testcase"), result.get("expected_output"), result.get("code_output")))
        return " | ".join(str(value)[:500] for value in fields if value)[:1200] or f"status_code={result.get('status_code')}"

    @staticmethod
    def _validate_result(result: dict, kind: str, code: str) -> None:
        submission_id = result.get("submission_id")
        if result.get("kind") != kind or not submission_id or not isinstance(result.get("status_code"), int):
            raise SiteUnavailable("LeetCode result is missing a matching submission ID or status")
        if kind == "submit" and str(submission_id).startswith("runcode_"):
            raise SiteUnavailable("run result cannot confirm a submission")
        if kind == "run" and not str(submission_id).startswith("runcode_"):
            raise SiteUnavailable("submission result cannot confirm a run")
        if str(result.get("code", "")).strip() != code.strip():
            raise SiteUnavailable("LeetCode executed code does not match the candidate")
