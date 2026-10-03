from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .types import Candidate, CheckResult, JudgeFeedback, Problem, ProblemRef, Reference, SampleCase


_PENDING_SUBMISSION = {"submit_intent", "submission_unconfirmed"}
_FINISHED = {"accepted", "already_accepted", "user_reported_accepted", "skipped", "needs_review"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProgressStore:
    def __init__(self, path: Path, start_id: int):
        self.path = path
        self.start_id = start_id

    def load(self) -> dict:
        state = (json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists()
                 else {"version": 1, "next_id": self.start_id, "records": {}})
        if state.get("version") != 1 or not isinstance(state.get("records"), dict):
            raise ValueError("unsupported progress file")
        if not isinstance(state.get("next_id"), int) or state["next_id"] < 1:
            raise ValueError("invalid next_id in progress file")
        records = {}
        for shard in sorted(self._history_dir().glob("*.json")):
            archived = json.loads(shard.read_text(encoding="utf-8"))
            if not isinstance(archived, dict):
                raise ValueError(f"invalid progress history shard: {shard}")
            records.update(archived)
        for number, record in state["records"].items():
            archived = records.get(number)
            if (archived and archived.get("status") in _FINISHED
                    and int(number) >= state["next_id"]
                    and record.get("status") not in _FINISHED):
                continue
            records[number] = record
        state["records"] = records
        while state["records"].get(str(state["next_id"]), {}).get("status") in _FINISHED:
            state["next_id"] += 1
        return state

    def compact(self) -> None:
        if self.path.exists():
            self._write(self.load())

    def _history_dir(self) -> Path:
        return self.path.parent / "progress-history"

    def _history_path(self, number: int) -> Path:
        first = number // 100 * 100
        return self._history_dir() / f"{first:04d}-{first + 99:04d}.json"

    def advance(self, number: int, status: str, **details: object) -> dict:
        state = self.load()
        if state["next_id"] != number:
            raise ValueError(f"progress expected problem {state['next_id']}, got {number}")
        if state["records"].get(str(number), {}).get("status") in _FINISHED:
            raise ValueError(f"problem {number} is already finished")
        state["records"][str(number)] = {
            "status": status,
            "updated_at": _now(),
            **details,
        }
        state["next_id"] = number + 1
        self._write(state)
        return state

    def anchor(self, number: int) -> dict:
        if number < 1:
            raise ValueError("current problem number must be positive")
        state = self.load()
        if any(record.get("status") in _PENDING_SUBMISSION for record in state["records"].values()):
            raise ValueError("resolve the unconfirmed submission before starting another problem")
        if any(int(key) >= number and record.get("status") in _FINISHED
               for key, record in state["records"].items()):
            raise ValueError("cannot start at or before an already finished problem")
        current = state["records"].get(str(state["next_id"]), {})
        if number != state["next_id"] and current.get("status") in {"candidate_ready", "run_verified", "needs_repair"}:
            raise ValueError("cannot abandon a saved candidate with --start-current")
        state["next_id"] = number
        self._write(state)
        return state

    def mark_current(self, number: int, status: str, **details: object) -> dict:
        state = self.load()
        if state["next_id"] != number:
            raise ValueError(f"progress expected problem {state['next_id']}, got {number}")
        if state["records"].get(str(number), {}).get("status") in _FINISHED:
            raise ValueError(f"problem {number} is already finished")
        state["records"][str(number)] = {
            "status": status,
            "updated_at": _now(),
            **details,
        }
        self._write(state)
        return state

    def clear_current(self, number: int) -> dict:
        state = self.load()
        if state["next_id"] != number:
            raise ValueError(f"progress expected problem {state['next_id']}, got {number}")
        state["records"].pop(str(number), None)
        self._write(state)
        return state

    def resolve_unconfirmed(self, resolution: str, verified: bool = False) -> dict:
        if resolution not in {"accepted", "retry"}:
            raise ValueError("resolution must be accepted or retry")
        state = self.load()
        number = state["next_id"]
        record = state["records"].get(str(number))
        if not record or record.get("status") not in _PENDING_SUBMISSION:
            raise ValueError("there is no unconfirmed submission for the current problem")
        if resolution == "accepted":
            if verified and not record.get("submission_id"):
                raise ValueError("verified resolution requires a recorded submission ID")
            record["status"] = "accepted" if verified else "user_reported_accepted"
            record["updated_at"] = _now()
            if verified:
                record["confirmed_by_judge"] = True
            else:
                record["confirmed_by_user"] = True
            state["next_id"] = number + 1
        else:
            del state["records"][str(number)]
        self._write(state)
        return state

    def _write(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        history: dict[Path, dict] = {}
        current = {}
        for number, record in state["records"].items():
            if record.get("status") in _FINISHED:
                history.setdefault(self._history_path(int(number)), {})[number] = {
                    key: value for key, value in record.items() if key != "snapshot"
                }
            else:
                current[number] = record
        for path, records in history.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and json.loads(path.read_text(encoding="utf-8")) == records:
                continue
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            os.replace(temporary, path)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({**state, "records": current}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


class ArtifactStore:
    def __init__(self, root: Path, enabled: bool = True):
        self.root = root
        self.enabled = enabled
        self.snapshot: dict | None = None

    def save(
        self,
        problem: Problem,
        candidate: Candidate | None,
        status: str,
        checks: list[CheckResult],
        references: list[Reference],
        attempts: int,
        retrospective: str | None = None,
    ) -> Path:
        folder = self._folder(problem)
        snapshot = self._snapshot(candidate, checks, references)
        if not self.enabled:
            self.snapshot = snapshot
            return folder
        folder.mkdir(parents=True, exist_ok=True)
        if problem.draft_code and problem.draft_code.strip() != problem.starter_code.strip():
            draft_path = folder / "browser-draft.txt"
            if not draft_path.exists():
                draft_path.write_text(problem.draft_code.rstrip() + "\n", encoding="utf-8")
        if candidate:
            attempt_folder = folder / "attempts"
            attempt_folder.mkdir(exist_ok=True)
            legacy = folder / "solution.py"
            if legacy.exists() and not any(attempt_folder.iterdir()):
                shutil.copy2(legacy, attempt_folder / "00-legacy.py")
                legacy_readme = folder / "README.md"
                if legacy_readme.exists():
                    shutil.copy2(legacy_readme, attempt_folder / "00-legacy.md")
            legacy.write_text(candidate.code.rstrip() + "\n", encoding="utf-8")
            (attempt_folder / f"{attempts:02d}.py").write_text(candidate.code.rstrip() + "\n", encoding="utf-8")
            (attempt_folder / f"{attempts:02d}.json").write_text(
                json.dumps({
                    "number": problem.ref.number,
                    "slug": problem.ref.slug,
                    "code_sha256": _digest(candidate.code),
                    "candidate": {key: value for key, value in snapshot["candidate"].items()
                                  if key != "code"},
                    "status": status,
                    "checks": snapshot["checks"],
                    "references": snapshot["references"],
                }, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        details = [
            f"# {problem.ref.number}. {problem.ref.title}",
            "",
            f"- 题目：{problem.ref.url}",
            f"- 状态：{status}",
            f"- 候选版本：{attempts}",
            "",
            "## 题意摘要",
            "",
            candidate.summary if candidate else "尚未生成解法。",
            "",
            "## 解法",
            "",
            candidate.approach if candidate else "尚未生成解法。",
            "",
            "## 复杂度",
            "",
            f"- 时间：{candidate.time_complexity if candidate else '未评估'}",
            f"- 空间：{candidate.space_complexity if candidate else '未评估'}",
            "",
            "## 验证与提交",
            "",
        ]
        details.extend(f"- {'通过' if check.passed else '失败'}：{check.detail}" for check in checks)
        if not checks:
            details.append("- 尚未验证")
        details.extend(["", "## 参考资料", ""])
        details.extend(f"- [{ref.title}]({ref.url})" for ref in references)
        if not references:
            details.append("- 未使用站外资料")
        if retrospective:
            details.extend(["", "## 复盘", "", retrospective])
        (folder / "README.md").write_text("\n".join(details) + "\n", encoding="utf-8")
        return folder

    @staticmethod
    def _snapshot(candidate: Candidate | None, checks: list[CheckResult],
                  references: list[Reference]) -> dict:
        return {
            "candidate": {
                "code": candidate.code,
                "summary": candidate.summary,
                "approach": candidate.approach,
                "time_complexity": candidate.time_complexity,
                "space_complexity": candidate.space_complexity,
                "tests": [{"args": test.args, "expected": test.expected} for test in candidate.tests],
            } if candidate else None,
            "checks": [
                {
                    "passed": item.passed,
                    "detail": item.detail,
                    **({"judge_feedback": {
                        "testcase": item.judge_feedback.testcase,
                        "actual_output": item.judge_feedback.actual_output,
                        "expected_output": item.judge_feedback.expected_output,
                    }} if item.judge_feedback else {}),
                }
                for item in checks
            ],
            "references": [{"title": item.title, "url": item.url, "excerpt": item.excerpt}
                           for item in references],
        }

    def load_candidate(self, problem: Problem, record: dict) -> Candidate | None:
        if record.get("status") not in {"candidate_ready", "run_verified", "needs_repair"}:
            return None
        if record.get("slug") != problem.ref.slug:
            raise ValueError("saved candidate belongs to a different problem")
        if "snapshot" in record:
            data = record["snapshot"]["candidate"]
            if not isinstance(data, dict) or record.get("code_sha256") != _digest(data["code"]):
                raise ValueError("saved candidate snapshot does not match the code")
            return Candidate(data["code"], data["summary"], data["approach"],
                             data["time_complexity"], data["space_complexity"],
                             [SampleCase(item["args"], item["expected"]) for item in data["tests"]])
        folder = Path(record["folder"])
        expected = self._folder(problem)
        if folder.resolve() != expected.resolve():
            raise ValueError("saved candidate folder does not match the problem")
        attempt = record.get("attempts")
        if not isinstance(attempt, int) or attempt < 1:
            raise ValueError("saved candidate attempt is invalid")
        code = (folder / "attempts" / f"{attempt:02d}.py").read_text(encoding="utf-8")
        if (folder / "solution.py").read_text(encoding="utf-8") != code:
            raise ValueError("saved candidate files differ")
        metadata = json.loads((folder / "attempts" / f"{attempt:02d}.json").read_text(encoding="utf-8"))
        if (metadata.get("number") != problem.ref.number or metadata.get("slug") != problem.ref.slug
                or metadata.get("code_sha256") != _digest(code)
                or record.get("code_sha256") != _digest(code)):
            raise ValueError("saved candidate metadata does not match the code")
        data = metadata["candidate"]
        return Candidate(code, data["summary"], data["approach"], data["time_complexity"],
                         data["space_complexity"], [SampleCase(item["args"], item["expected"])
                                                     for item in data["tests"]])

    def load_attempt_context(self, record: dict) -> tuple[list[CheckResult], list[Reference]]:
        if "snapshot" in record:
            metadata = record["snapshot"]
        else:
            folder = Path(record["folder"])
            attempt = record["attempts"]
            metadata = json.loads((folder / "attempts" / f"{attempt:02d}.json").read_text(encoding="utf-8"))
        checks = [
            CheckResult(
                item["passed"], item["detail"],
                JudgeFeedback(**item["judge_feedback"]) if item.get("judge_feedback") else None,
            )
            for item in metadata.get("checks", [])
        ]
        references = [Reference(item["title"], item["url"], item.get("excerpt", ""))
                      for item in metadata.get("references", [])]
        return checks, references

    def migrate_problem_eight(self, problem: Problem) -> Candidate | None:
        if problem.ref.number != 8 or problem.ref.slug != "string-to-integer-atoi":
            return None
        folder = self._folder(problem)
        source = folder / "attempts" / "01.py"
        solution = folder / "solution.py"
        readme = folder / "README.md"
        metadata = folder / "attempts" / "01.json"
        if not all(path.is_file() for path in (source, solution, readme, metadata)):
            return None
        code = source.read_text(encoding="utf-8")
        text = readme.read_text(encoding="utf-8")
        old = json.loads(metadata.read_text(encoding="utf-8"))
        if (code != solution.read_text(encoding="utf-8")
                or f"- 题目：{problem.ref.url}" not in text
                or "- 状态：submission_unconfirmed" not in text
                or old.get("status") != "submission_unconfirmed"
                or not any(check.get("passed") and "站内运行" in check.get("detail", "")
                           for check in old.get("checks", []))):
            return None

        def section(start: str, end: str) -> str:
            return text.split(start, 1)[1].split(end, 1)[0].strip()

        try:
            summary = section("## 题意摘要", "## 解法")
            approach = section("## 解法", "## 复杂度")
            complexity = section("## 复杂度", "## 验证与提交")
            time_value = complexity.split("- 时间：", 1)[1].splitlines()[0].strip()
            space_value = complexity.split("- 空间：", 1)[1].splitlines()[0].strip()
        except (IndexError, ValueError):
            return None
        if not all((summary, approach, time_value, space_value)):
            return None
        if self.enabled:
            for path in (readme, metadata):
                backup = folder / "attempts" / f"01-legacy{path.suffix}"
                if not backup.exists():
                    shutil.copy2(path, backup)
        return Candidate(code, summary, approach, time_value, space_value)

    def reconcile_legacy_acceptance(self, problem: Problem) -> Path | None:
        if not self.enabled:
            return None
        if self.migrate_problem_eight(problem) is None:
            return None
        folder = self._folder(problem)
        readme = folder / "README.md"
        content = readme.read_text(encoding="utf-8")
        readme.write_text(content.replace(
            "- 状态：submission_unconfirmed",
            "- 状态：already_accepted（站内已有 AC，未确认本程序提交 ID）", 1,
        ), encoding="utf-8")
        return folder

    def reconcile_saved_acceptance(self, problem: Problem, record: dict) -> Path | None:
        self.load_candidate(problem, record)
        if not self.enabled or "snapshot" in record:
            return None
        folder = Path(record["folder"])
        readme = folder / "README.md"
        content = readme.read_text(encoding="utf-8")
        if f"- 题目：{problem.ref.url}" not in content:
            raise ValueError("saved candidate README belongs to a different problem")
        updated, replacements = re.subn(
            r"^- 状态：.+$",
            "- 状态：already_accepted（站内已有 AC，未确认本程序提交 ID）",
            content, count=1, flags=re.MULTILINE,
        )
        if replacements != 1:
            raise ValueError("saved candidate README has no status line")
        backup = folder / "attempts" / f"{record['attempts']:02d}-before-external-ac.md"
        if not backup.exists():
            shutil.copy2(readme, backup)
        readme.write_text(updated, encoding="utf-8")
        return folder

    def _folder(self, problem: Problem) -> Path:
        slug = re.sub(r"[^a-z0-9-]", "-", problem.ref.slug.lower()).strip("-") or "problem"
        return self.root / f"{problem.ref.number:04d}-{slug}"

    @staticmethod
    def confirm_submission(folder: Path, detail: str | None = None) -> None:
        path = folder / "README.md"
        content = path.read_text(encoding="utf-8")
        if "- 状态：accepted" in content and detail:
            return
        old = next((value for value in ("- 状态：submission_unconfirmed", "- 状态：submit_intent")
                    if value in content), None)
        if old is None:
            raise ValueError("artifact is not awaiting submission confirmation")
        label = "accepted（站内提交 ID 确认）" if detail else "accepted（人工确认）"
        content = content.replace(old, f"- 状态：{label}", 1)
        content += f"\n- {detail or '提交结果已由用户在力扣提交记录中确认。'}\n"
        path.write_text(content, encoding="utf-8")


def record_ref(ref: ProblemRef) -> dict:
    return {"title": ref.title, "slug": ref.slug, "url": ref.url}


def _digest(code: str) -> str:
    import hashlib
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()
