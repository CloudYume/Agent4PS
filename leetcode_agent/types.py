from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class AgentError(Exception):
    """A run cannot proceed without changing the current problem."""


class SiteUnavailable(AgentError):
    """The site, login, challenge, or result state needs human attention."""


class ModelUnavailable(AgentError):
    """The configured model could not produce a usable response."""


@dataclass(frozen=True)
class ProblemRef:
    number: int
    title: str
    slug: str
    url: str


@dataclass(frozen=True)
class ProblemImage:
    url: str
    alt: str = ""


@dataclass(frozen=True)
class Problem:
    ref: ProblemRef
    statement: str
    starter_code: str
    site_status: str | None = None
    draft_code: str | None = None
    site_question_id: str | None = None
    sample_testcase: str = ""
    example_testcases: str = ""
    meta_data: str = ""
    images: tuple[ProblemImage, ...] = ()
    visual_notes: str = ""


@dataclass(frozen=True)
class SampleCase:
    args: list[Any]
    expected: Any


@dataclass(frozen=True)
class Candidate:
    code: str
    summary: str
    approach: str
    time_complexity: str
    space_complexity: str
    tests: list[SampleCase] = field(default_factory=list)


@dataclass(frozen=True)
class JudgeFeedback:
    testcase: str
    actual_output: str
    expected_output: str


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    detail: str
    judge_feedback: JudgeFeedback | None = None


@dataclass(frozen=True)
class SubmissionReceipt:
    submission_id: str
    code_sha256: str
    question_id: str | None = None


@dataclass(frozen=True)
class ProgressEvent:
    number: int
    stage: str
    message: str
    attempt: int = 1


@dataclass(frozen=True)
class Reference:
    title: str
    url: str
    excerpt: str

