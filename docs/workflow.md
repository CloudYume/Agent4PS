# Workflow and Recovery

[Project overview](../README.md) · [Troubleshooting](troubleshooting.md)

## One problem at a time

The orchestrator passes `Problem`, `Candidate`, and `CheckResult` records between stages and applies the configured repair/model-call budgets.

1. **Read.** Fetch `translatedContent` through the problem's GraphQL response, falling back to `content`, and use the official Python3 `codeSnippets` template. Parse the problem HTML, not the page's discussions, solutions, related problems, or footer. Long descriptions prioritize the opening statement and final constraints.
2. **Generate and review.** Request complete submission code. Repair candidates are independently reviewed; the configured review mode controls first-candidate review. Images remain in context, with optional vision-model descriptions.
3. **Check.** Syntax and the official class/method interface must be valid before a site run. Parsed site examples may support local diagnosis; model-invented examples and local output mismatches do not replace the site's judgment.
4. **Run and submit.** Write code into the active bound tab, run the site's examples, and submit when configured. A successful example run is not a final Accepted result.
5. **Advance, repair, or stop.** Correlate the submission ID, problem, code digest, and judge result. Site failures feed repair; ambiguous ownership, exhausted budgets, and unrecoverable service failures preserve state for review.

For a site Wrong Answer, repair requests include the failing input, actual output, and expected output when available. Runtime/compile failures provide the site's error text. If a proposed repair repeats the failed Python logic, the client tries the fallback model and stops with saved `needs_repair` state if it still cannot obtain a changed candidate.

## Progress and artifacts

`Output/progress.json` stores the cursor and current unfinished work. Completed records are partitioned into `Output/progress-history/`. Startup compacts older records while retaining recoverable work, and `status` reads the combined history.

With the checked-in `output.save_artifacts: false`, candidate code, checks, and references remain in the current progress snapshot instead of creating per-problem `solution.py`, `README.md`, or `attempts/` files. Enabling artifact saving writes those per-problem records. Existing artifact directories are not deleted. Inspect progress files before sharing: they can contain full candidate code and feedback.

| Status | Restart behavior |
| --- | --- |
| `candidate_ready` / `run_verified` | Revalidate the saved candidate and continue the same problem. |
| `needs_repair` | Restore failure feedback and repair; legacy local-case failures are rechecked without invented model cases. |
| `submit_intent` / `submission_unconfirmed` | Recover or query the existing submission read-only; do not click Submit again while ownership/result remains unresolved. |
| `accepted` | A matching submission and successful judge result were confirmed automatically. |
| `already_accepted` | Site acceptance was confirmed, including supported manual-completion recovery, without claiming a verified agent submission. |
| `user_reported_accepted` | The user resolved a submission manually; it is distinguished from automatic judge verification. |

## Submission recovery

Before submission, record the latest site submission ID as a baseline and hash the candidate code. After the click, capture the new ID and verify its site code. If the detail endpoint is rate-limited, the runner can navigate to the next page, wait 60 seconds, and recheck the old submission before solving another problem. Continued throttling backs off read-only queries rather than repeating the submission.

If a receipt is lost, recovery queries submissions after the baseline and looks for matching code. Restarting after a confirmed WA, TLE, or runtime error restores the feedback and continues repair. If the site independently confirms the original problem is already Accepted, the runner can record `already_accepted` while preserving an unresolved agent submission ID as audit information.

Older records without a recoverable ID or baseline require manual inspection of the problem number, code, and result on LeetCode. Only then choose one:

```powershell
python -m leetcode_agent resolve --resolution accepted
python -m leetcode_agent resolve --resolution retry
```

Use `accepted` only after checking the actual submission history. An editor's current code or a passed example run is insufficient.

## Active tab and navigation

Browser actions require the bound problem tab to be active. Switching away makes the runner wait and recheck the problem number when the tab returns. After acceptance, the default navigation uses the next problem's URL. A navigation timeout triggers extension/activity checks and retries; unresolved navigation stops with the cursor at the next problem.

If you manually obtain Accepted and navigate to the immediately following problem in the bound tab, the runner can verify the original number/slug and site AC status read-only before continuing. It does not treat that event as proof that its own saved submission succeeded.

With no progress, the active problem supplies the starting point. Existing progress can advance to a later active problem only when no saved work needs recovery. `--start-current` cannot discard a pending candidate, skip an unconfirmed submission, or return to finished work.

Local checks convert supported linked-list and binary-tree examples to `ListNode`/`TreeNode` objects and normalize returned nodes. Interactive problems, custom containers, manual judges, and examples that cannot be represented reliably as JSON are left to site-run validation. Vision calls count toward `max_model_calls`; unreadable required images stop the current task.
