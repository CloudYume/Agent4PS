# Troubleshooting

[Project overview](../README.md) · [Setup](setup.md) · [Recovery](workflow.md)

| Symptom | What to check |
| --- | --- |
| `invalid pairing code` | Use the current `run` process's six-digit code. Check the popup and configured port. Codes expire and have an attempt limit. An identity change or deleted bridge token can require pairing again. |
| Waiting for the active problem tab | Activate a `leetcode.cn/problems/.../` page in the paired Edge profile and refresh it. Check the popup. After extension updates, reload both the extension and the page. |
| Active tab detected, but catalog lookup continues | The algorithm catalog excludes some IDs, such as SQL problems. The runner skips an absent ID once the ordered catalog has passed it. Catalog lookup displays the current number. |
| Protocol version mismatch | Reload Agent4PS at `edge://extensions`, refresh the problem tab, and restart the Python runner. |
| Extension missing after updating | The source directory is now `browser-extension/`. Load that directory rather than the previous `extension/` path, then verify pairing. |
| `LeetCode GraphQL HTTP 400` | Refresh the problem page and check login. Persistent failures may indicate a site/API change; inspect extension logs. The cursor is retained. |
| Model has no answer text or times out | Check endpoint, model ID, key, and provider status. Review the request deadlines. Failure preserves the current problem. |
| `model response was not a JSON object` | The client accepts complete Python submission code and its legacy JSON format. If neither can be parsed, it tries the configured fallback once and retains the cursor on failure. |
| Repair repeats the failed logic | Inspect whether saved feedback came from a site run or submission. Site failing input/output is passed to repair. An unchanged repair triggers fallback; another unchanged result stops with `needs_repair`. |
| `submission_unconfirmed` | Inspect the site's submission history first. Restart performs read-only recovery. If still ambiguous, manually verify the problem, code, and result before using `resolve`. |
| Site access-limit message | After submission, the runner can navigate forward, wait 60 seconds, and recheck the saved ID or code. It does not repeat an unconfirmed submission. Resume after the site recovers. |
| `check_submission result could not be confirmed` | The submission ID and candidate remain saved. Reload an outdated extension and restart; recovery queries the same submission, then repairs if a failure is confirmed. |
| Next problem times out after AC | Keep the bound tab active and verify the target page opens. The next cursor is already saved; restart from it. |
| Manual AC and next-page navigation still stop the runner | Reload the extension and page. Recovery must verify the original problem's AC and that the new page is its immediate successor. Unconfirmed saved submissions still follow the ID-based recovery flow. |

`doctor` makes catalog and model requests. For an offline usage check, use `python -m leetcode_agent --help`. Do not use a model's confidence, a passed local case, or the editor's current contents as proof of an Accepted submission.
