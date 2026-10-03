from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
from threading import Thread

import pytest

from leetcode_agent.bridge import BrowserBridge, make_server


HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<button aria-haspopup="dialog">Python3</button>
<textarea aria-label="代码编辑器">class Solution:\n    def twoSum(self):\n        pass\n</textarea>
<button id="run" data-e2e-locator="console-run-button" aria-label="运行"><svg></svg></button>
<button id="submit" data-e2e-locator="console-submit-button" aria-label="提交">提交</button>
<script>
  window.submitShortcutCount = 0;
  document.addEventListener('keydown', (event) => {
    if (event.ctrlKey && event.key === 'Enter') {
      window.submitShortcutCount++;
      document.getElementById('submit').click();
    } else if (event.ctrlKey && event.key === 'ArrowRight') {
      sessionStorage.setItem('nextShortcutCount', String(Number(sessionStorage.getItem('nextShortcutCount') || 0) + 1));
      if (!window.ignoreNextShortcut) location.assign('/problems/add-two-numbers/');
    }
  });
  let value = document.querySelector('textarea').value;
  const model = {
    getLanguageId: () => 'python3',
    getValue: () => value,
    setValue: (next) => { value = next; document.querySelector('textarea').value = next; },
  };
  const editorDelay = Number(new URLSearchParams(location.search).get('editorDelay') || 0);
  setTimeout(() => { window.monaco = { editor: { getModels: () => [model] } }; }, editorDelay);
  document.getElementById('run').onclick = async () => {
    if (window.runDelayMs) await new Promise((resolve) => setTimeout(resolve, window.runDelayMs));
    const response = await fetch('/problems/two-sum/interpret_solution/', {
      method: 'POST', body: JSON.stringify({ typed_code: value, lang: 'python3' }),
    });
    const issued = await response.json();
    await fetch(`/submissions/detail/${issued.interpret_id}/check/`);
  };
  document.getElementById('submit').onclick = async () => {
    const response = await fetch('/problems/two-sum/submit/', {
      method: 'POST', body: JSON.stringify(window.submitWithoutCode
        ? { lang: 'python3' } : { typed_code: value, lang: 'python3' }),
    });
    const issued = await response.json();
    if (window.reloadAfterSubmit) {
      location.reload();
      return;
    }
    if (window.redirectAfterSubmit) {
      location.assign('/problems/add-two-numbers/');
      return;
    }
    await fetch(`/submissions/detail/${window.checkOldSubmission ? '41' : issued.submission_id}/check/`);
  };
</script></body></html>"""


def _await_command(page, future, polls=60):
    for _ in range(polls):
        if future.done():
            return future.result()
        page.wait_for_timeout(250)
    return future.result(timeout=1)


@pytest.mark.parametrize("trigger,missing_request_code,redirect_after_submit,ignore_next_shortcut,lost_receipt,mismatched_detail,stray_terminal,editor_delay", [
    ("button", False, False, False, False, False, False, 0), ("shortcut", False, False, True, False, False, False, 0),
    ("button", True, False, False, False, False, False, 0), ("button", False, True, False, False, False, False, 0),
    ("shortcut", False, False, False, True, False, False, 0),
    ("button", True, False, False, False, True, False, 0),
    ("shortcut", False, False, False, False, False, True, 0),
    ("button", False, False, False, False, False, False, 1500),
])
def test_edge_extension_writes_runs_and_correlates_submission(
    tmp_path, trigger, missing_request_code, redirect_after_submit, ignore_next_shortcut,
    lost_receipt, mismatched_detail, stray_terminal, editor_delay,
):
    playwright = pytest.importorskip("playwright.sync_api")
    bridge = BrowserBridge(tmp_path / "bridge-token")
    server = make_server("127.0.0.1", 0, bridge)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    extension = str((Path(__file__).resolve().parents[1] / "extension").resolve())
    try:
        with playwright.sync_playwright() as engine:
            context = engine.chromium.launch_persistent_context(
                str(tmp_path / "edge-profile"), channel="msedge", headless=True,
                args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}"],
            )
            try:
                worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker")
                worker.evaluate("settings => chrome.storage.local.set(settings)", {
                    "bridgeToken": bridge.token, "bridgePort": server.server_port,
                })
                submissions = ["41"] if stray_terminal else []
                code = "class Solution:\n    def twoSum(self):\n        return [0, 1]\n"

                def route(request):
                    url = request.request.url
                    if url.endswith("/graphql/"):
                        operation = request.request.post_data_json.get("operationName")
                        if operation == "AgentSubmissions":
                            request.fulfill(json={"data": {"userStatus": {"isSignedIn": True}, "submissionList": {
                                "submissions": [{"id": item} for item in submissions],
                            }}})
                            return
                        if operation == "AgentSubmissionDetail":
                            assert "$id: ID!" in request.request.post_data_json["query"]
                            assert isinstance(request.request.post_data_json["variables"]["id"], str)
                            submitted_code = "class Solution:\n    pass\n" if mismatched_detail else code
                            request.fulfill(json={"data": {"submissionDetail": {"code": submitted_code}}})
                            return
                        slug = request.request.post_data_json.get("variables", {}).get("slug", "two-sum")
                        assert "translatedContent" in request.request.post_data_json["query"]
                        request.fulfill(json={"data": {
                            "userStatus": {"isSignedIn": True},
                            "question": {
                                "questionId": "1" if slug == "two-sum" else "2",
                                "questionFrontendId": "1" if slug == "two-sum" else "2",
                                "titleSlug": slug, "translatedTitle": "Two Sum",
                                "content": "<p>Given an array of integers, find two numbers that add up to a target.</p>",
                                "translatedContent": "<p>给你一个整数数组，请找出和为目标值的两个数。</p>",
                                "status": None, "isPaidOnly": False,
                                "codeSnippets": [{"langSlug": "python3", "code": "class Solution:\n    def twoSum(self):\n        pass"}],
                            },
                        }})
                    elif url.endswith("/interpret_solution/"):
                        request.fulfill(json={"interpret_id": "runcode_1"})
                    elif url.endswith("/submit/"):
                        if not stray_terminal:
                            submissions.insert(0, "42")
                        request.fulfill(json={} if lost_receipt or stray_terminal else {"submission_id": "42"})
                    elif "runcode_1/check" in url:
                        request.fulfill(json={"submission_id": "runcode_1", "status_code": 10, "compare_result": "1,1", "status_msg": "Accepted"})
                    elif "/42/check" in url:
                        request.fulfill(json={"submission_id": "42", "question_id": "1", "finished": True, "status_code": 10, "status_msg": "Accepted"})
                    elif "/41/check" in url:
                        request.fulfill(json={"submission_id": "41", "question_id": "1", "finished": True, "status_code": 10, "status_msg": "Accepted"})
                    else:
                        request.fulfill(status=200, content_type="text/html; charset=utf-8", body=HTML)

                context.route("https://leetcode.cn/**", route)
                page = context.new_page()
                page.goto(f"https://leetcode.cn/problems/two-sum/?editorDelay={editor_delay}")
                for attempt in range(2):
                    for _ in range(40):
                        if bridge.status()["connected"]:
                            break
                        page.wait_for_timeout(250)
                    if bridge.status()["connected"]:
                        break
                    # Edge may finish registering static content scripts after its first startup tab.
                    if attempt == 0:
                        page.reload()
                assert bridge.status()["connected"]
                initial_page = bridge.wait_for_page("two-sum")
                assert initial_page["problem"]["status"] == "NOT_STARTED"
                assert "给你一个整数数组" in initial_page["problem"]["content"]
                bridge.require_protocol(initial_page)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    draft = pool.submit(bridge.call, "read_draft", "two-sum", {}, 20)
                    assert "class Solution" in _await_command(page, draft)["code"]
                    check_heartbeat = trigger == "button" and not any((
                        missing_request_code, redirect_after_submit, ignore_next_shortcut,
                        lost_receipt, mismatched_detail, stray_terminal,
                    ))
                    if check_heartbeat:
                        page.evaluate("window.runDelayMs = 3500")
                    run = pool.submit(bridge.call, "run", "two-sum", {"code": code}, 30)
                    if check_heartbeat:
                        for _ in range(50):
                            pending = bridge.status()["pending_action"]
                            if pending and pending[0]["phase"] == "triggering":
                                break
                            page.wait_for_timeout(100)
                        assert bridge.status()["pending_action"][0]["phase"] == "triggering"
                        before_heartbeat = bridge.current["seen_at"]
                        background_check = pool.submit(
                            bridge.call, "check_submission", "two-sum", {"submission_id": "42"}, 20,
                        )
                        page.wait_for_timeout(2500)
                        assert bridge.current["seen_at"] > before_heartbeat
                        assert _await_command(page, background_check)["status_code"] == 10
                    run_result = _await_command(page, run)
                    assert run_result["submission_id"] == "runcode_1"
                    assert run_result["code"].strip() == code.strip()
                    if check_heartbeat:
                        other_tab = context.new_page()
                        try:
                            for _ in range(20):
                                if bridge.status()["active"] is False:
                                    break
                                page.wait_for_timeout(250)
                            assert bridge.status()["active"] is False
                            hidden_check = pool.submit(
                                bridge.call, "check_submission", "two-sum", {"submission_id": "42"}, 20,
                            )
                            assert _await_command(page, hidden_check)["status_code"] == 10
                        finally:
                            other_tab.close()
                    digest = hashlib.sha256(code.strip().encode("utf-8")).hexdigest()
                    baseline = pool.submit(bridge.call, "submission_baseline", "two-sum", {}, 15)
                    assert _await_command(page, baseline)["latest_id"] == ("41" if stray_terminal else None)
                    page.evaluate("value => { window.submitWithoutCode = value; }", missing_request_code)
                    page.evaluate("value => { window.redirectAfterSubmit = value; }", redirect_after_submit)
                    page.evaluate("value => { window.reloadAfterSubmit = value; }", lost_receipt)
                    page.evaluate("value => { window.checkOldSubmission = value; }", stray_terminal)
                    submit = pool.submit(bridge.call, "submit", "two-sum", {"code_sha256": digest, "trigger": trigger}, 30)
                    if stray_terminal:
                        with pytest.raises(Exception, match="submission ID missing"):
                                _await_command(page, submit, polls=120)
                        assert submissions == ["41"]
                        recovered = pool.submit(bridge.call, "recover_submission", "two-sum", {
                            "question_slug": "two-sum", "baseline_id": "41", "code_sha256": digest,
                        }, 20)
                        assert _await_command(page, recovered)["submission_id"] is None
                        return
                    if mismatched_detail:
                        with pytest.raises(Exception, match="captured submission code does not match"):
                            _await_command(page, submit)
                        assert submissions == ["42"]
                        recovered = pool.submit(bridge.call, "recover_submission", "two-sum", {
                            "question_slug": "two-sum", "baseline_id": None, "code_sha256": digest,
                        }, 20)
                        assert _await_command(page, recovered)["submission_id"] is None
                        return
                    if lost_receipt:
                        with pytest.raises(Exception, match="page reloaded before the receipt"):
                            _await_command(page, submit)
                    else:
                        submit_result = _await_command(page, submit)
                        assert submit_result["submission_id"] == "42"
                        assert submit_result["code"].strip() == code.strip()
                    if redirect_after_submit:
                        for _ in range(40):
                            if bridge.status()["slug"] == "add-two-numbers":
                                break
                            page.wait_for_timeout(250)
                    recovery_slug = "add-two-numbers" if redirect_after_submit else "two-sum"
                    recovered = pool.submit(bridge.call, "recover_submission", recovery_slug, {
                        "question_slug": "two-sum", "baseline_id": None, "code_sha256": digest,
                    }, 20)
                    assert _await_command(page, recovered)["submission_id"] == "42"
                    assert submissions == ["42"]
                    old = pool.submit(bridge.call, "recover_submission", recovery_slug, {
                        "question_slug": "two-sum", "baseline_id": "42", "code_sha256": digest,
                    }, 20)
                    assert _await_command(page, old)["submission_id"] is None
                    different = pool.submit(bridge.call, "recover_submission", recovery_slug, {
                        "question_slug": "two-sum", "baseline_id": None, "code_sha256": "0" * 64,
                    }, 20)
                    assert _await_command(page, different)["submission_id"] is None
                    if not redirect_after_submit:
                        if not lost_receipt:
                            assert page.evaluate("window.submitShortcutCount") == (1 if trigger == "shortcut" else 0)
                        page.reload()
                    else:
                        for _ in range(40):
                            if bridge.status()["slug"] == "add-two-numbers":
                                break
                            page.wait_for_timeout(250)
                    check_slug = "add-two-numbers" if redirect_after_submit else "two-sum"
                    checked = pool.submit(bridge.call, "check_submission", check_slug, {"submission_id": "42"}, 30)
                    assert _await_command(page, checked)["status_code"] == 10
                    if not redirect_after_submit:
                        page.evaluate("value => { window.ignoreNextShortcut = value; }", ignore_next_shortcut)
                        navigate = pool.submit(
                            bridge.call, "navigate", "two-sum",
                            {"url": "https://leetcode.cn/problems/add-two-numbers/", "trigger": "shortcut"}, 30,
                        )
                        assert _await_command(page, navigate)["ok"] is True
                        if ignore_next_shortcut:
                            page.wait_for_timeout(2000)
                            assert bridge.status()["slug"] == "two-sum"
                            fallback = pool.submit(
                                bridge.call, "navigate", "two-sum",
                                {"url": "https://leetcode.cn/problems/add-two-numbers/", "trigger": "url"}, 30,
                            )
                            assert _await_command(page, fallback)["ok"] is True
                    for _ in range(40):
                        if bridge.status()["slug"] == "add-two-numbers":
                            break
                        page.wait_for_timeout(250)
                    if not redirect_after_submit:
                        assert page.evaluate("sessionStorage.getItem('nextShortcutCount')") == "1"
                    checked_next_page = pool.submit(bridge.call, "check_submission", "add-two-numbers", {"submission_id": "42"}, 30)
                    assert _await_command(page, checked_next_page)["question_id"] == "1"
            finally:
                context.close()
    finally:
        bridge.stop()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=3)
