from concurrent.futures import ThreadPoolExecutor
from threading import Thread
from time import sleep

import httpx
import pytest

from leetcode_agent.bridge import BrowserBridge, make_server
from leetcode_agent.extension_browser import ExtensionBrowser, _statement_with_images
from leetcode_agent.types import (CapturedSubmissionUnavailable, JudgeFeedback, Problem,
                                  ProblemRef, SiteUnavailable, SubmissionReceipt)


def page(tab_id=7, active=True):
    return {
        "tab_id": tab_id,
        "active": active,
        "slug": "two-sum",
        "url": "https://leetcode.cn/problems/two-sum/",
        "problem": {"number": 1, "slug": "two-sum", "logged_in": True},
    }


def test_problem_html_keeps_image_order_and_resolves_urls():
    statement, images = _statement_with_images(
        '<p>Before</p><img src="https://assets.leetcode.com/rotate.jpg" alt="rotation steps">'
        '<p>After</p><img data-src="/images/example.png">',
        "https://leetcode.cn/problems/rotate-list/",
    )
    assert statement.index("Before") < statement.index("[Image 1: rotation steps]") < statement.index("After")
    assert "[Image 2]" in statement
    assert [image.url for image in images] == [
        "https://assets.leetcode.com/rotate.jpg", "https://leetcode.cn/images/example.png",
    ]


def versioned_page(tab_id=7, active=True, ready=True):
    return {
        **page(tab_id, active), "document_id": "document-1", "protocol_version": 5,
        "capabilities": ["command_phase", "execution_heartbeat", "submission_recovery", "write_authorization", "problem_status",
                         "captured_submission_verification"],
        "ready": ready,
    }


def test_old_content_script_is_rejected_before_solving(tmp_path):
    bridge = BrowserBridge(tmp_path / "token")
    bridge.hello(page())
    browser = ExtensionBrowser(bridge, None)
    with pytest.raises(SiteUnavailable, match="reload Agent4PS"):
        browser.current_number()
    bridge.hello(versioned_page())
    assert browser.current_number() == 1


def test_heartbeat_does_not_consume_command_and_phases_are_bound_to_document(tmp_path):
    bridge = BrowserBridge(tmp_path / "token")
    bridge.hello(versioned_page())
    phases = []
    bridge.on_phase = lambda kind, phase, command_id: phases.append((kind, phase, command_id))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(bridge.call, "submit", "two-sum", {}, 3)
        for _ in range(100):
            if bridge.pending:
                break
            sleep(0.01)
        assert bridge.hello(versioned_page(ready=False))["command"] is None
        command = bridge.hello(versioned_page())["command"]
        assert command["document_id"] == "document-1"
        phase = {
            "tab_id": 7, "command_id": command["id"], "kind": "submit",
            "slug": "two-sum", "document_id": "document-1", "phase": "request_seen",
        }
        with pytest.raises(ValueError, match="invalid command phase"):
            bridge.phase({**phase, "document_id": "other-document"})
        bridge.phase(phase)
        assert bridge.status()["pending_action"][0]["phase"] == "request_seen"
        with pytest.raises(ValueError, match="does not match"):
            bridge.result({"tab_id": 7, "command_id": command["id"], "kind": "run", "ok": True})
        bridge.result({
            "tab_id": 7, "command_id": command["id"], "command_kind": "submit",
            "slug": "two-sum", "document_id": "document-1", "ok": True,
        })
        assert future.result(timeout=3)["ok"] is True
        assert phases == [("submit", "request_seen", command["id"])]


def test_bridge_preserves_unverified_id_when_submit_detail_is_rate_limited(tmp_path):
    bridge = BrowserBridge(tmp_path / "token")
    bridge.hello(versioned_page())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(bridge.call, "submit", "two-sum", {"code_sha256": "a" * 64}, 3)
        for _ in range(100):
            if bridge.pending:
                break
            sleep(0.01)
        command = bridge.hello(versioned_page())["command"]
        identity = {"tab_id": 7, "command_id": command["id"], "kind": "submit",
                    "slug": "two-sum", "document_id": "document-1"}
        bridge.phase({**identity, "phase": "id_seen", "submission_id": "42"})
        bridge.result({**identity, "ok": False, "error": "🐸☕超出访问限制，请稍后再试"})
        with pytest.raises(CapturedSubmissionUnavailable, match="超出访问限制") as error:
            future.result(timeout=3)
        assert error.value.submission_id == "42"


def test_bound_inactive_tab_can_deliver_read_only_check_from_heartbeat(tmp_path):
    bridge = BrowserBridge(tmp_path / "token")
    bridge.hello(versioned_page())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(bridge.call, "check_submission", "two-sum", {"submission_id": "42"}, 3)
        for _ in range(100):
            if bridge.pending:
                break
            sleep(0.01)
        heartbeat = bridge.hello(versioned_page(active=False, ready=False))
        command = heartbeat["command"]
        assert command["kind"] == "check_submission"
        assert bridge.wait_for_page(timeout=1, require_active=False)["active"] is False
        bridge.result({
            "tab_id": 7, "command_id": command["id"], "command_kind": "check_submission",
            "slug": "two-sum", "document_id": "document-1", "ok": True,
            "kind": "submit", "submission_id": "42", "status_code": 10,
        })
        assert future.result(timeout=3)["status_code"] == 10


def test_problem_status_is_read_only_on_inactive_next_page(tmp_path):
    bridge = BrowserBridge(tmp_path / "token")
    next_page = versioned_page(active=False, ready=False)
    next_page.update(slug="add-two-numbers", url="https://leetcode.cn/problems/add-two-numbers/",
                     problem={"number": 2, "slug": "add-two-numbers", "logged_in": True})
    bridge.hello(versioned_page())
    bridge.hello(next_page)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(bridge.call, "problem_status", "add-two-numbers",
                             {"question_slug": "two-sum"}, 3)
        for _ in range(100):
            if bridge.pending:
                break
            sleep(0.01)
        command = bridge.hello(next_page)["command"]
        assert command["kind"] == "problem_status"
        bridge.result({"tab_id": 7, "command_id": command["id"], "kind": "problem_status",
                       "slug": "add-two-numbers", "document_id": "document-1", "ok": True,
                       "question_slug": "two-sum", "question_id": "1", "number": 1, "status": "AC"})
        assert future.result(timeout=3)["status"] == "AC"


@pytest.mark.parametrize("next_number,status,expected", [
    (2, "AC", True), (2, "TRIED", False), (3, "AC", False),
])
def test_manual_acceptance_requires_immediate_next_page_and_original_ac(next_number, status, expected):
    class FakeBridge:
        def __init__(self):
            self.calls = 0

        def wait_for_page(self, timeout, require_active):
            assert timeout == 5 and require_active is False
            return {"slug": "add-two-numbers", "problem": {
                "slug": "add-two-numbers", "number": next_number, "logged_in": True,
            }}

        def call(self, kind, slug, payload, timeout):
            self.calls += 1
            assert (kind, slug, payload, timeout) == (
                "problem_status", "add-two-numbers", {"question_slug": "two-sum"}, 20,
            )
            return {"question_slug": "two-sum", "question_id": "1", "number": 1, "status": status}

    bridge = FakeBridge()
    browser = ExtensionBrowser(bridge, None)
    problem = Problem(ProblemRef(1, "one", "two-sum", "https://leetcode.cn/problems/two-sum/"),
                      "statement", "starter", site_question_id="1")
    assert browser.next_page_after_manual_acceptance(problem) is expected
    assert bridge.calls == (0 if next_number != 2 else 1)


def test_manual_acceptance_rejects_status_for_another_problem():
    class FakeBridge:
        def wait_for_page(self, timeout, require_active):
            return {"slug": "add-two-numbers", "problem": {
                "slug": "add-two-numbers", "number": 2, "logged_in": True,
            }}

        def call(self, kind, slug, payload, timeout):
            return {"question_slug": "two-sum", "question_id": "other", "number": 1, "status": "AC"}

    browser = ExtensionBrowser(FakeBridge(), None)
    problem = Problem(ProblemRef(1, "one", "two-sum", "https://leetcode.cn/problems/two-sum/"),
                      "statement", "starter", site_question_id="1")
    with pytest.raises(SiteUnavailable, match="another problem"):
        browser.next_page_after_manual_acceptance(problem)


@pytest.mark.parametrize("status,number,expected", [
    ("AC", 1, True), ("TRIED", 1, False), ("AC", 3, False),
])
def test_pending_acceptance_can_be_confirmed_on_original_page(status, number, expected):
    class FakeBridge:
        def wait_for_page(self, timeout, require_active):
            return {"slug": "two-sum", "problem": {
                "slug": "two-sum", "number": number, "logged_in": True,
            }}

        def call(self, kind, slug, payload, timeout):
            assert (kind, slug, payload) == ("problem_status", "two-sum", {"question_slug": "two-sum"})
            return {"question_slug": "two-sum", "question_id": "1", "number": 1, "status": status}

    browser = ExtensionBrowser(FakeBridge(), None)
    problem = Problem(ProblemRef(1, "one", "two-sum", "https://leetcode.cn/problems/two-sum/"),
                      "statement", "starter", site_question_id="1")
    assert browser.pending_problem_accepted(problem) is expected


def test_rate_limit_navigation_requests_next_page_without_confirming_old_submission():
    next_ref = ProblemRef(2, "two", "add-two-numbers", "https://leetcode.cn/problems/add-two-numbers/")

    class Catalog:
        def find(self, number):
            assert number == 2
            return next_ref

    class FakeBridge:
        def __init__(self):
            self.calls = []

        def wait_for_page(self, timeout, require_active):
            return {"slug": "two-sum", "problem": {"slug": "two-sum", "number": 1}}

        def call(self, kind, slug, payload, timeout):
            self.calls.append((kind, slug, payload, timeout))
            return {"ok": True}

    bridge = FakeBridge()
    ExtensionBrowser(bridge, Catalog()).navigate_after_rate_limit(1, "two-sum")
    assert bridge.calls == [("navigate", "two-sum", {"url": next_ref.url, "trigger": "url"}, 20)]


def test_switching_tabs_during_run_keeps_command_until_result(tmp_path):
    bridge = BrowserBridge(tmp_path / "token")
    bridge.hello(versioned_page())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(bridge.call, "run", "two-sum", {}, 3)
        for _ in range(100):
            if bridge.pending:
                break
            sleep(0.01)
        command = bridge.hello(versioned_page())["command"]
        assert command["kind"] == "run"
        identity = {"tab_id": 7, "command_id": command["id"], "slug": "two-sum",
                    "document_id": "document-1"}
        assert bridge.command_active(identity)
        bridge.hello(versioned_page(active=False, ready=False))
        assert not future.done()
        bridge.hello(versioned_page())
        bridge.result({**identity, "kind": "run", "ok": True})
        assert future.result(timeout=3)["ok"] is True
        assert not bridge.command_active(identity)


def test_expired_or_reloaded_run_cannot_write_code(tmp_path):
    bridge = BrowserBridge(tmp_path / "token")
    bridge.hello(versioned_page())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(bridge.call, "run", "two-sum", {}, 0.2)
        for _ in range(100):
            if bridge.pending:
                break
            sleep(0.001)
        command = bridge.hello(versioned_page())["command"]
        identity = {"tab_id": 7, "command_id": command["id"], "slug": "two-sum",
                    "document_id": "document-1"}
        assert bridge.command_active(identity)
        assert not bridge.command_active({**identity, "document_id": "other"})
        bridge.hello({**versioned_page(), "document_id": "document-2"})
        assert not bridge.command_active(identity)
        with pytest.raises(SiteUnavailable, match="could not be confirmed"):
            future.result(timeout=3)
        assert not bridge.command_active(identity)


def test_switching_tabs_still_interrupts_submit(tmp_path):
    bridge = BrowserBridge(tmp_path / "token")
    bridge.hello(versioned_page())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(bridge.call, "submit", "two-sum", {}, 3)
        for _ in range(100):
            if bridge.pending:
                break
            sleep(0.01)
        assert bridge.hello(versioned_page())["command"]["kind"] == "submit"
        bridge.hello(versioned_page(active=False, ready=False))
        with pytest.raises(SiteUnavailable, match="inactive"):
            future.result(timeout=3)


def test_bridge_binds_active_tab_and_delivers_command_once(tmp_path):
    bridge = BrowserBridge(tmp_path / "token")
    first = bridge.hello(page())
    assert first["bound"] is True
    assert first["poll_interval_ms"] == 500

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(bridge.call, "run", "two-sum", {"code": "x"}, 3)
        for _ in range(100):
            if bridge.pending:
                break
            sleep(0.01)
        assert bridge.hello(page(8))["bound"] is False
        assert bridge.hello(page(active=False))["command"] is None
        delivered = bridge.hello(page())["command"]
        assert delivered["kind"] == "run"
        assert bridge.hello(page())["command"] is None
        bridge.result({"tab_id": 7, "command_id": delivered["id"], "ok": True, "status_code": 10})
        assert future.result(timeout=3)["status_code"] == 10


def test_bridge_stops_waiting_when_bound_tab_leaves_problem(tmp_path):
    bridge = BrowserBridge(tmp_path / "token")
    bridge.hello(page())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(bridge.call, "run", "two-sum", {"code": "x"}, 3)
        for _ in range(100):
            if bridge.pending:
                break
            sleep(0.01)
        moved = page()
        moved.update(slug="add-two-numbers", url="https://leetcode.cn/problems/add-two-numbers/")
        bridge.hello(moved)
        with pytest.raises(SiteUnavailable, match="left two-sum"):
            future.result(timeout=3)


def test_submit_wait_ends_when_page_reloads(tmp_path):
    bridge = BrowserBridge(tmp_path / "token")
    first = {**page(), "document_id": "before"}
    bridge.hello(first)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(bridge.call, "submit", "two-sum", {}, 30)
        for _ in range(100):
            if bridge.pending:
                break
            sleep(0.01)
        assert bridge.hello(first)["command"]["kind"] == "submit"
        bridge.hello({**page(), "document_id": "after"})
        with pytest.raises(SiteUnavailable, match="page reloaded"):
            future.result(timeout=3)


def test_pairing_code_is_single_use(tmp_path):
    bridge = BrowserBridge(tmp_path / "token")
    assert bridge.pair(bridge.pair_code) == bridge.token
    assert bridge.pair("") is None


def test_bridge_port_rejects_second_runner(tmp_path):
    first = make_server("127.0.0.1", 0, BrowserBridge(tmp_path / "first-token"))
    try:
        with pytest.raises(OSError):
            make_server("127.0.0.1", first.server_port, BrowserBridge(tmp_path / "second-token"))
    finally:
        first.server_close()


def test_http_bridge_requires_pairing_and_authentication(tmp_path):
    bridge = BrowserBridge(tmp_path / "token")
    server = make_server("127.0.0.1", 0, bridge)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with httpx.Client(base_url=base) as client:
            assert client.post("/v1/hello", json=page()).status_code == 401
            response = client.post("/v1/pair", json={"code": bridge.pair_code})
            token = response.json()["token"]
            assert client.post("/v1/pair", json={"code": ""}).status_code == 403
            response = client.post("/v1/hello", json=page(), headers={"Authorization": f"Bearer {token}"})
            assert response.json()["bound"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_reconnect_requires_previously_paired_extension_origin(tmp_path):
    bridge = BrowserBridge(tmp_path / "token")
    server = make_server("127.0.0.1", 0, bridge)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = "chrome-extension://" + "a" * 32
    other_origin = "chrome-extension://" + "b" * 32
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}") as client:
            assert client.post("/v1/reconnect", json={}, headers={"Origin": origin}).status_code == 403
            assert client.post("/v1/reconnect", json={}).status_code == 403
            paired = client.post("/v1/pair", json={"code": bridge.pair_code}, headers={"Origin": origin})
            assert paired.status_code == 200
            token = paired.json()["token"]
            restored = client.post("/v1/reconnect", json={}, headers={"Origin": origin})
            assert restored.status_code == 200
            assert restored.json()["token"] == token
            assert client.post("/v1/reconnect", json={}, headers={"Origin": other_origin}).status_code == 403
            assert client.post("/v1/reconnect", json={}, headers={"Origin": "https://leetcode.cn"}).status_code == 403
        restarted = BrowserBridge(tmp_path / "token")
        assert restarted.reconnect(origin) == token
        assert restarted.reconnect(other_origin) is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_existing_token_registers_extension_origin_without_new_pairing(tmp_path):
    bridge = BrowserBridge(tmp_path / "token")
    server = make_server("127.0.0.1", 0, bridge)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = "chrome-extension://" + "c" * 32
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}") as client:
            response = client.get("/v1/status", headers={"Authorization": f"Bearer {bridge.token}",
                                                         "Origin": origin})
            assert response.status_code == 200
            assert client.post("/v1/reconnect", json={}, headers={"Origin": origin}).json()["token"] == bridge.token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_deleting_bridge_token_revokes_automatic_reconnect(tmp_path):
    path = tmp_path / "token"
    origin = "chrome-extension://" + "d" * 32
    original = BrowserBridge(path)
    assert original.pair(original.pair_code, origin) == original.token
    path.unlink()

    replacement = BrowserBridge(path)
    assert replacement.reconnect(origin) is None
    assert replacement.token != original.token
    assert not replacement.origin_path.exists()


def test_extension_browser_rejects_unmatched_result(tmp_path):
    class FakeBridge:
        def call(self, kind, slug, payload, timeout):
            return {
                "kind": kind,
                "submission_id": "runcode_12" if kind == "run" else "13",
                "status_code": 10,
                "status_msg": "Accepted",
                "code": "different code",
            }

    browser = ExtensionBrowser(FakeBridge(), None)
    ref = ProblemRef(1, "两数之和", "two-sum", "https://leetcode.cn/problems/two-sum/")
    browser.current = Problem(ref, "statement", "class Solution: pass")
    with pytest.raises(SiteUnavailable, match="does not match"):
        browser.run_code("class Solution: pass")


def test_run_retries_after_inactive_write_only_on_same_problem():
    class FakeBridge:
        def __init__(self, resumed_slug="two-sum"):
            self.calls = 0
            self.resumed_slug = resumed_slug

        def call(self, kind, slug, payload, timeout):
            assert kind == "run" and slug == "two-sum"
            self.calls += 1
            if self.calls == 1:
                raise SiteUnavailable("bound tab became inactive before code write (document hidden)")
            return {"kind": "run", "submission_id": "runcode_1", "status_code": 10,
                    "status_msg": "Accepted", "code": payload["code"]}

        def wait_for_page(self, timeout, since):
            assert timeout == 86400
            assert since > 0
            return {"slug": self.resumed_slug, "problem": {
                "number": 1, "question_id": "1", "logged_in": True,
            }}

    notes = []
    bridge = FakeBridge()
    browser = ExtensionBrowser(bridge, None, on_pause=notes.append)
    browser.current = Problem(ProblemRef(1, "one", "two-sum", "https://leetcode.cn/problems/two-sum/"),
                              "statement", "starter", site_question_id="1")
    assert browser.run_code("class Solution: pass").passed
    assert bridge.calls == 2
    assert len(notes) == 1

    changed = FakeBridge(resumed_slug="add-two-numbers")
    browser = ExtensionBrowser(changed, None)
    browser.current = Problem(ProblemRef(1, "one", "two-sum", "https://leetcode.cn/problems/two-sum/"),
                              "statement", "starter", site_question_id="1")
    with pytest.raises(SiteUnavailable, match="changed problems"):
        browser.run_code("class Solution: pass")
    assert changed.calls == 1


def test_run_uses_accepted_status_and_submit_needs_real_id():
    class FakeBridge:
        def __init__(self):
            self.compare = None
            self.submission_id = "runcode_2"

        def call(self, kind, slug, payload, timeout):
            return {
                "kind": kind,
                "submission_id": self.submission_id,
                "status_code": 10,
                "status_msg": "Accepted",
                "compare_result": self.compare,
                "code": "code",
            }

    bridge = FakeBridge()
    browser = ExtensionBrowser(bridge, None)
    browser.current = Problem(ProblemRef(1, "one", "two-sum", "https://leetcode.cn/problems/two-sum/"), "statement", "starter")
    assert browser.run_code("code").passed
    bridge.compare = "judge-specific-format"
    assert browser.run_code("code").passed
    with pytest.raises(SiteUnavailable, match="submit receipt does not match"):
        browser.submit_code()
    bridge.submission_id = "12345"
    assert browser.submit_code().submission_id == "12345"


def test_submit_recovers_matching_new_id_without_retrying_click():
    class FakeBridge:
        def __init__(self):
            self.calls = []

        def call(self, kind, slug, payload=None, timeout=120):
            self.calls.append(kind)
            if kind == "submit":
                raise SiteUnavailable("submit page reloaded before the receipt arrived")
            assert kind == "recover_submission"
            assert payload["baseline_id"] == "100"
            return {"submission_id": "101"}

        def wait_for_page(self, timeout=180, require_active=True):
            assert require_active is False
            return {"slug": "two-sum", "problem": {"logged_in": True}}

    bridge = FakeBridge()
    browser = ExtensionBrowser(bridge, None)
    browser.current = Problem(ProblemRef(1, "one", "two-sum", "https://leetcode.cn/problems/two-sum/"),
                              "statement", "starter", site_question_id="1")
    browser.last_code = "class Solution: pass"
    receipt = browser.submit_code("100")
    assert receipt.submission_id == "101"
    assert receipt.question_id == "1"
    assert bridge.calls == ["submit", "recover_submission"]


def test_submit_does_not_query_history_after_rate_limit_or_captured_id():
    class FakeBridge:
        def __init__(self, error):
            self.error = error
            self.calls = []

        def call(self, kind, slug, payload=None, timeout=120):
            self.calls.append(kind)
            raise self.error

    for error in (SiteUnavailable("超出访问限制，请稍后再试"),
                  CapturedSubmissionUnavailable("LeetCode GraphQL HTTP 429", "42")):
        bridge = FakeBridge(error)
        browser = ExtensionBrowser(bridge, None)
        browser.current = Problem(ProblemRef(1, "one", "two-sum", "https://leetcode.cn/problems/two-sum/"),
                                  "statement", "starter")
        browser.last_code = "class Solution: pass"
        with pytest.raises(SiteUnavailable):
            browser.submit_code()
        assert bridge.calls == ["submit"]


def test_submission_detail_includes_performance_metrics():
    assert ExtensionBrowser._detail({
        "status_msg": "Accepted",
        "status_runtime": "7 ms",
        "status_memory": "19.20 MB",
        "runtime_percentile": 29.54,
        "memory_percentile": 36.48,
    }) == "Accepted | runtime=7 ms | memory=19.20 MB | runtime percentile=29.54% | memory percentile=36.48%"


def test_statement_keeps_examples_and_constraints_without_page_footer():
    statement, images = _statement_with_images(
        "<p>Find all shortest paths.</p>"
        "<p>Example 1: input a, output b.</p>"
        "<p>Constraints: words are unique.</p>"
        "<footer>Related Topics and comments</footer><script>tracking()</script>",
        "https://leetcode.cn/problems/word-ladder-ii/",
    )
    assert statement.index("Find all") < statement.index("Example 1") < statement.index("Constraints")
    assert "Related Topics" not in statement
    assert "tracking" not in statement
    assert not images


def test_failed_submission_keeps_raw_judge_input_actual_and_expected():
    class FakeBridge:
        def wait_for_page(self, timeout, require_active=True):
            return {"slug": "two-sum", "problem": {"logged_in": True}}

        def call(self, kind, slug, payload, timeout):
            return {
                "kind": "submit", "submission_id": "42", "status_code": 11,
                "status_msg": "Wrong Answer", "last_testcase": "[1,2]\n3",
                "code_output": "-1", "expected_output": "0",
            }

    result = ExtensionBrowser(FakeBridge(), None).check_submission(SubmissionReceipt("42", "hash"))
    assert not result.passed
    assert result.judge_feedback == JudgeFeedback("[1,2]\n3", "-1", "0")


def test_runtime_error_detail_keeps_final_exception_after_long_traceback():
    detail = ExtensionBrowser._detail({
        "status_msg": "Runtime Error",
        "error": "Traceback\n" + "helper frame\n" * 160
                 + "TypeError: custom TreeNode is not valid for expected TreeNode",
    })
    assert "Traceback" in detail
    assert "TypeError: custom TreeNode" in detail


def test_multiple_judge_outputs_are_context_not_a_false_regression_case():
    result = {
        "status_msg": "Wrong Answer", "last_testcase": "[1,2]",
        "code_answers": ["1", "2"], "expected_code_answers": ["1", "3"],
    }
    assert "actual outputs=[\"1\", \"2\"]" in ExtensionBrowser._detail(result)
    assert ExtensionBrowser._judge_feedback(result) == JudgeFeedback("[1,2]", "", "")


def test_submission_check_waits_for_terminal_result_and_matches_problem(monkeypatch):
    monkeypatch.setattr("leetcode_agent.extension_browser.time.sleep", lambda _: None)

    class FakeBridge:
        def __init__(self):
            self.results = [
                {"kind": "submit", "submission_id": "42", "pending": True},
                {"kind": "submit", "submission_id": "42", "question_id": "1", "status_code": 10, "status_msg": "Accepted"},
            ]

        def wait_for_page(self, timeout, require_active=True):
            assert require_active is False
            return {"slug": "next-problem", "problem": {"logged_in": True}}

        def call(self, kind, slug, payload, timeout):
            assert kind == "check_submission"
            assert slug == "next-problem"
            assert payload["submission_id"] == "42"
            return self.results.pop(0)

    bridge = FakeBridge()
    browser = ExtensionBrowser(bridge, None)
    assert browser.check_submission(SubmissionReceipt("42", "hash", "1")).passed
    assert not bridge.results


def test_submission_check_retries_slow_queries_for_the_same_receipt(monkeypatch):
    monkeypatch.setattr("leetcode_agent.extension_browser.time.sleep", lambda _: None)

    class FakeBridge:
        def __init__(self):
            self.calls = []
            self.responses = [
                SiteUnavailable("check_submission result could not be confirmed (last phase: started)"),
                SiteUnavailable("submission check timed out"),
                {"kind": "submit", "submission_id": "42", "question_id": "1",
                 "status_code": 10, "status_msg": "Accepted"},
            ]

        def wait_for_page(self, timeout, require_active=True):
            return {"slug": "next-problem", "problem": {"logged_in": True}}

        def call(self, kind, slug, payload, timeout):
            self.calls.append((kind, slug, payload["submission_id"]))
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

    bridge = FakeBridge()
    notes = []
    browser = ExtensionBrowser(bridge, None, on_pause=notes.append)
    assert browser.check_submission(SubmissionReceipt("42", "hash", "1")).passed
    assert bridge.calls == [("check_submission", "next-problem", "42")] * 3
    assert len(notes) == 2


def test_submission_check_passes_rate_limit_to_navigation_handler():
    class FakeBridge:
        def wait_for_page(self, timeout, require_active=True):
            return {"slug": "two-sum", "problem": {"logged_in": True}}

        def call(self, kind, slug, payload, timeout):
            raise SiteUnavailable("LeetCode check HTTP 429")

    browser = ExtensionBrowser(FakeBridge(), None)
    with pytest.raises(SiteUnavailable, match="429"):
        browser.check_submission(SubmissionReceipt("42", "hash", "1"))


def test_time_limit_feedback_keeps_case_shape_without_saving_huge_input():
    testcase = "nums = [" + "1," * 30000 + "1]\nk = 50000"
    feedback = ExtensionBrowser._judge_feedback({
        "status_code": 14, "status_msg": "Time Limit Exceeded", "last_testcase": testcase,
    })
    assert feedback is not None
    assert len(feedback.testcase) < 6100
    assert feedback.testcase.startswith("nums = [1,1,")
    assert feedback.testcase.endswith("k = 50000")
    assert "characters omitted" in feedback.testcase


def test_submission_check_rejects_other_question():
    class FakeBridge:
        def wait_for_page(self, timeout, require_active=True):
            assert require_active is False
            return {"slug": "next-problem", "problem": {"logged_in": True}}

        def call(self, kind, slug, payload, timeout):
            return {"kind": "submit", "submission_id": "42", "question_id": "2", "status_code": 10}

    with pytest.raises(SiteUnavailable, match="another problem"):
        ExtensionBrowser(FakeBridge(), None).check_submission(SubmissionReceipt("42", "hash", "1"))


def test_next_shortcut_falls_back_to_exact_problem_after_wrong_navigation():
    target = ProblemRef(2, "Two", "add-two-numbers", "https://leetcode.cn/problems/add-two-numbers/")

    class Catalog:
        def is_paid(self, number):
            return False

    class FakeBridge:
        def __init__(self):
            self.calls = []
            self.waits = 0

        def wait_for_page(self, slug=None, timeout=180):
            self.waits += 1
            if self.waits == 1:
                return {"slug": "two-sum", "problem": {"number": 1}}
            if self.waits == 2:
                assert slug == target.slug and timeout == 8
                raise SiteUnavailable("waiting for an active LeetCode problem tab timed out")
            if self.waits == 3:
                return {"slug": "wrong-problem", "problem": {"number": 3}}
            assert slug == target.slug
            return {"slug": target.slug, "problem": {
                "number": 2, "slug": target.slug, "logged_in": True, "status": "AC",
                "starter": "class Solution:\n    def solve(self):\n        pass",
                "content": "<p>Find the sum of the two given linked lists.</p>",
            }}

        def call(self, kind, slug, payload, timeout):
            self.calls.append((kind, slug, payload))
            return {"ok": True}

    bridge = FakeBridge()
    problem = ExtensionBrowser(bridge, Catalog(), navigation_trigger="shortcut").open_problem(target)
    assert problem.site_status == "AC"
    assert [call[2]["trigger"] for call in bridge.calls] == ["shortcut", "url"]


@pytest.mark.parametrize("inactive", [False, True])
def test_url_navigation_waits_for_slow_or_inactive_target(inactive):
    target = ProblemRef(45, "Jump Game II", "jump-game-ii",
                        "https://leetcode.cn/problems/jump-game-ii/")
    old_page = {"slug": "wildcard-matching", "problem": {"number": 44}}
    target_page = {"slug": target.slug, "problem": {
        "number": 45, "slug": target.slug, "logged_in": True, "status": "AC",
        "starter": "class Solution:\n    def jump(self, nums):\n        pass",
        "content": "<p>Find the minimum number of jumps needed to reach the last index.</p>",
    }}

    class Catalog:
        def is_paid(self, number):
            return False

    class FakeBridge:
        def __init__(self):
            self.waits = []
            self.calls = []

        def wait_for_page(self, slug=None, timeout=180):
            self.waits.append((slug, timeout))
            if len(self.waits) == 1:
                return old_page
            if len(self.waits) == 2:
                raise SiteUnavailable("waiting for an active LeetCode problem tab timed out")
            return target_page

        def status(self):
            return {"connected": inactive, "active": False, "slug": target.slug}

        def call(self, kind, slug, payload, timeout):
            self.calls.append((kind, slug, payload))
            return {"ok": True}

    bridge = FakeBridge()
    pauses = []
    problem = ExtensionBrowser(bridge, Catalog(), on_pause=pauses.append).open_problem(target)

    assert problem.site_status == "AC"
    assert bridge.calls == [("navigate", "wildcard-matching",
                             {"url": target.url, "trigger": "url"})]
    assert bridge.waits[1] == (target.slug, 30)
    assert bridge.waits[2:] == ([(None, 86400), (target.slug, 60)] if inactive
                                else [(target.slug, 60)])
    assert pauses
