import json

import pytest

from leetcode_agent.__main__ import _select_start
from leetcode_agent.progress import ArtifactStore, ProgressStore, _digest
from leetcode_agent.types import Candidate, Problem, ProblemRef, SampleCase


def test_progress_advances_atomically(tmp_path):
    path = tmp_path / "Output/progress.json"
    store = ProgressStore(path, 7)
    assert store.load()["next_id"] == 7

    store.advance(7, "skipped", reason="not algorithm")

    content = json.loads(path.read_text(encoding="utf-8"))
    assert content["next_id"] == 8
    assert content["records"] == {}
    assert store.load()["records"]["7"]["reason"] == "not algorithm"
    assert (path.parent / "progress-history/0000-0099.json").exists()
    assert not path.with_suffix(".json.tmp").exists()
    with pytest.raises(ValueError, match="expected problem 8"):
        store.advance(7, "accepted")


def test_retry_clears_only_current_unconfirmed_record(tmp_path):
    store = ProgressStore(tmp_path / "Output/progress.json", 1)
    store.mark_current(1, "submission_unconfirmed", url="https://leetcode.cn/problems/two-sum/")

    store.resolve_unconfirmed("retry")

    assert store.load() == {"version": 1, "next_id": 1, "records": {}}


def test_compact_migrates_legacy_history_without_losing_current_snapshot(tmp_path):
    path = tmp_path / "Output/progress.json"
    path.parent.mkdir()
    snapshot = {"candidate": {"code": "class Solution: pass"}}
    path.write_text(json.dumps({
        "version": 1, "next_id": 100,
        "records": {
            "1": {"status": "accepted", "title": "one", "snapshot": snapshot},
            "100": {"status": "needs_repair", "snapshot": snapshot},
        },
    }), encoding="utf-8")
    store = ProgressStore(path, 1)

    store.compact()

    compacted = json.loads(path.read_text(encoding="utf-8"))
    assert list(compacted["records"]) == ["100"]
    assert compacted["records"]["100"]["snapshot"] == snapshot
    assert store.load()["records"]["1"]["title"] == "one"
    assert "snapshot" not in store.load()["records"]["1"]
    store.compact()
    assert store.load()["next_id"] == 100


def test_load_recovers_when_history_write_precedes_cursor_write(tmp_path):
    path = tmp_path / "Output/progress.json"
    store = ProgressStore(path, 1)
    store.mark_current(1, "submission_unconfirmed", snapshot={"candidate": {"code": "old"}})
    old_cursor = path.read_bytes()
    store.resolve_unconfirmed("accepted")
    path.write_bytes(old_cursor)

    recovered = store.load()
    assert recovered["next_id"] == 2
    assert recovered["records"]["1"]["status"] == "user_reported_accepted"
    store.compact()
    assert json.loads(path.read_text(encoding="utf-8"))["records"] == {}


def test_anchor_preserves_prior_records_and_rejects_unconfirmed(tmp_path):
    store = ProgressStore(tmp_path / "Output/progress.json", 1)
    store.advance(1, "accepted", title="one")
    store.anchor(42)
    assert store.load()["next_id"] == 42
    assert store.load()["records"]["1"]["status"] == "accepted"
    store.mark_current(42, "submission_unconfirmed")
    with pytest.raises(ValueError, match="unconfirmed"):
        store.anchor(43)


def test_manual_confirmation_is_distinct_from_judge_verified_acceptance(tmp_path):
    store = ProgressStore(tmp_path / "Output/progress.json", 5)
    store.mark_current(5, "submission_unconfirmed")
    state = store.resolve_unconfirmed("accepted")
    assert state["records"]["5"]["status"] == "user_reported_accepted"
    assert state["records"]["5"]["confirmed_by_user"] is True

    store.mark_current(6, "submission_unconfirmed", submission_id="123")
    state = store.resolve_unconfirmed("accepted", verified=True)
    assert state["records"]["6"]["status"] == "accepted"
    assert state["records"]["6"]["confirmed_by_judge"] is True


def test_anchor_rejects_pending_intent_and_finished_problem(tmp_path):
    store = ProgressStore(tmp_path / "Output/progress.json", 1)
    store.mark_current(1, "submit_intent", baseline_id="100")
    with pytest.raises(ValueError, match="unconfirmed"):
        store.anchor(2)
    store.resolve_unconfirmed("accepted")
    with pytest.raises(ValueError, match="already finished"):
        store.anchor(1)


def test_anchor_cannot_abandon_candidate_and_advance_cannot_rewrite_finished(tmp_path):
    store = ProgressStore(tmp_path / "Output/progress.json", 1)
    store.mark_current(1, "candidate_ready", folder="candidate")
    with pytest.raises(ValueError, match="saved candidate"):
        store.anchor(2)
    store.advance(1, "accepted")
    with pytest.raises(ValueError, match="already finished"):
        store.anchor(1)


def test_run_starts_at_active_problem_unless_saved_work_needs_recovery(tmp_path):
    store = ProgressStore(tmp_path / "Output/progress.json", 1)
    assert _select_start(store, 29, False) == 29
    assert store.load()["next_id"] == 29

    store.mark_current(29, "candidate_ready", folder="saved-candidate")
    assert _select_start(store, 30, False) == 29
    assert store.load()["next_id"] == 29

    store.clear_current(29)
    assert _select_start(store, 30, False) == 30
    assert store.load()["next_id"] == 30


def test_structured_candidate_can_be_loaded_only_with_matching_files(tmp_path):
    artifacts = ArtifactStore(tmp_path / "Output")
    problem = Problem(ProblemRef(8, "atoi", "string-to-integer-atoi",
                                 "https://leetcode.cn/problems/string-to-integer-atoi/"),
                      "statement", "class Solution:\n    pass")
    candidate = Candidate("class Solution:\n    pass\n", "summary", "approach", "O(n)", "O(1)",
                          [SampleCase(["42"], 42)])
    folder = artifacts.save(problem, candidate, "candidate_ready", [], [], 1)
    record = {"status": "candidate_ready", "slug": problem.ref.slug, "folder": str(folder),
              "attempts": 1, "code_sha256": _digest(candidate.code)}
    assert artifacts.load_candidate(problem, record) == candidate
    (folder / "solution.py").write_text("other code", encoding="utf-8")
    with pytest.raises(ValueError, match="files differ"):
        artifacts.load_candidate(problem, record)


def test_problem_eight_legacy_migration_preserves_original_metadata(tmp_path):
    artifacts = ArtifactStore(tmp_path / "Output")
    problem = Problem(ProblemRef(8, "atoi", "string-to-integer-atoi",
                                 "https://leetcode.cn/problems/string-to-integer-atoi/"),
                      "statement", "class Solution:\n    pass")
    folder = tmp_path / "Output" / "0008-string-to-integer-atoi"
    (folder / "attempts").mkdir(parents=True)
    code = "class Solution:\n    pass\n"
    (folder / "solution.py").write_text(code, encoding="utf-8")
    (folder / "attempts" / "01.py").write_text(code, encoding="utf-8")
    (folder / "attempts" / "01.json").write_text(
        json.dumps({"status": "submission_unconfirmed", "checks": [
            {"passed": True, "detail": "站内运行: Accepted"}]}), encoding="utf-8")
    (folder / "README.md").write_text(
        "# 8. atoi\n- 题目：https://leetcode.cn/problems/string-to-integer-atoi/\n"
        "- 状态：submission_unconfirmed\n## 题意摘要\nsummary\n## 解法\napproach\n"
        "## 复杂度\n- 时间：O(n)\n- 空间：O(1)\n## 验证与提交\n", encoding="utf-8")
    assert artifacts.migrate_problem_eight(problem).code == code
    assert (folder / "attempts" / "01-legacy.json").exists()
    assert (folder / "attempts" / "01-legacy.md").exists()
    (folder / "attempts" / "01.py").write_text("mismatch", encoding="utf-8")
    assert artifacts.migrate_problem_eight(problem) is None
