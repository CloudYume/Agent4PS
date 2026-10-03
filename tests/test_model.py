from pathlib import Path
import asyncio
import json
import time

import httpx
import pytest

import leetcode_agent.model as model_module
from leetcode_agent.model import ModelClient
from leetcode_agent.settings import load_settings
from leetcode_agent.types import Candidate, ModelUnavailable, Problem, ProblemImage, ProblemRef, Reference, SampleCase


def test_vision_model_describes_problem_image_for_text_solver(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    client = ModelClient(settings.api, 3)
    calls = []
    monkeypatch.setattr(client, "_chat", lambda system, user, model_override=None:
                        calls.append((user, model_override)) or "Image 1: 1,2,3 becomes 3,1,2.")
    problem = Problem(
        ProblemRef(61, "Rotate List", "rotate-list", "https://leetcode.cn/problems/rotate-list/"),
        "Rotate the linked list. [Image 1]", "class Solution: pass",
        images=(ProblemImage("https://assets.leetcode.com/rotate.jpg"),),
    )

    prepared = client.prepare_problem(problem)

    assert calls[0][1] == "deepseek-v4-flash-vision"
    assert calls[0][0][-1] == {
        "type": "image_url", "image_url": {"url": "https://assets.leetcode.com/rotate.jpg"},
    }
    assert "Image 1: 1,2,3 becomes 3,1,2." in prepared.visual_notes
    assert prepared.statement == problem.statement


def test_model_request_uses_configured_endpoint_and_model(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    monkeypatch.setenv("INTERN_AI_API_KEY", "test-secret-marker")
    original_client = httpx.AsyncClient
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    monkeypatch.setattr(
        model_module.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    ModelClient(settings.api, 2).ping()

    assert len(requests) == 1
    assert str(requests[0].url) == "https://discovery-api.intern-ai.org.cn/v1/chat/completions"
    assert requests[0].headers["Authorization"] == "Bearer test-secret-marker"
    assert '"model":"deepseek-v4-flash-0731"' in requests[0].content.decode()
    sent = json.loads(requests[0].content)
    assert sent["stream"] is True
    assert sent["thinking"] == {"type": "disabled"}


def test_model_key_uses_user_environment_when_process_has_not_inherited_it(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    monkeypatch.delenv("INTERN_AI_API_KEY", raising=False)
    monkeypatch.setattr(model_module, "_user_environment_value",
                        lambda name: "user-key-marker" if name == "INTERN_AI_API_KEY" else "")

    assert ModelClient(settings.api, 2)._key() == "user-key-marker"


def test_streamed_chat_assembles_content(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    monkeypatch.setenv("INTERN_AI_API_KEY", "test-secret-marker")
    original_client = httpx.AsyncClient
    body = (
        'data: {"choices":[{"delta":{"content":"O"},"finish_reason":null}]}\n\n'
        'data: {"choices":[{"delta":{"content":"K"},"finish_reason":"stop"}]}\n\n'
        'data: {"choices":[],"usage":{"total_tokens":2}}\n\n'
        'data: [DONE]\n\n'
    )
    monkeypatch.setattr(model_module.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=body,
        )), **kwargs,
    ))
    ModelClient(settings.api, 2).ping()


def test_stream_heartbeats_cannot_extend_total_model_deadline(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    short_api = settings.api.model_copy(update={"timeout_seconds": 0.1})
    monkeypatch.setenv("INTERN_AI_API_KEY", "test-secret-marker")
    original_client = httpx.AsyncClient
    requests = []
    notes = []

    class Heartbeats(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.01)
                yield b": still working\n\n"

        async def aclose(self):
            pass

    def handle(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Heartbeats())

    monkeypatch.setattr(model_module.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    started = time.monotonic()
    with pytest.raises(ModelUnavailable, match="exceeded 0.1s total"):
        ModelClient(short_api, 3, on_retry=notes.append).ping()
    assert time.monotonic() - started < 1
    assert len(requests) == 1
    assert any("total timeout" in note for note in notes)
    assert not any("completed" in note for note in notes)


def test_stream_finishes_on_stop_even_if_connection_stays_open(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    short_api = settings.api.model_copy(update={"timeout_seconds": 0.2})
    monkeypatch.setenv("INTERN_AI_API_KEY", "test-secret-marker")
    original_client = httpx.AsyncClient

    class LingeringStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n'
            while True:
                await asyncio.sleep(0.01)
                yield b": still open\n\n"

        async def aclose(self):
            pass

    monkeypatch.setattr(model_module.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=LingeringStream(),
        )), **kwargs,
    ))
    ModelClient(short_api, 2).ping()


def test_reasoning_only_stream_reports_activity_without_using_it_as_answer(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    monkeypatch.setenv("INTERN_AI_API_KEY", "test-secret-marker")
    original_client = httpx.AsyncClient
    notes = []
    body = (
        'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n'
        'data: [DONE]\n\n'
    )
    monkeypatch.setattr(model_module.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=body,
        )), **kwargs,
    ))
    ModelClient(settings.api, 2, on_retry=notes.append).ping()
    assert any("reasoning stream started" in note for note in notes)
    assert not any("thinking" in note for note in notes)


def test_reasoning_only_completion_is_not_used_as_an_answer(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    monkeypatch.setenv("INTERN_AI_API_KEY", "test-secret-marker")
    original_client = httpx.AsyncClient
    body = (
        'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    )
    monkeypatch.setattr(model_module.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=body,
        )), **kwargs,
    ))
    with pytest.raises(ModelUnavailable, match="empty content"):
        ModelClient(settings.api, 2).ping()


def test_fallback_after_primary_headers_stall(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    api = settings.api.model_copy(update={
        "timeout_seconds": 0.5, "first_answer_timeout_seconds": 0.05,
    })
    monkeypatch.setenv("INTERN_AI_API_KEY", "test-secret-marker")
    original_client = httpx.AsyncClient
    models = []
    notes = []

    async def handle(request):
        model = json.loads(request.content)["model"]
        models.append(model)
        if model == api.model:
            await asyncio.sleep(0.2)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    monkeypatch.setattr(model_module.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    client = ModelClient(api, 2, on_retry=notes.append)
    client.ping()
    assert models == [api.model, api.fallback_model]
    assert client.calls == 2
    assert any("switching to fallback model" in note for note in notes)


def test_fallback_after_reasoning_only_stream(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    api = settings.api.model_copy(update={
        "timeout_seconds": 0.5, "first_answer_timeout_seconds": 0.05,
    })
    monkeypatch.setenv("INTERN_AI_API_KEY", "test-secret-marker")
    original_client = httpx.AsyncClient
    models = []

    class ReasoningStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n\n'
                await asyncio.sleep(0.01)

        async def aclose(self):
            pass

    def handle(request):
        model = json.loads(request.content)["model"]
        models.append(model)
        if model == api.model:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ReasoningStream())
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    monkeypatch.setattr(model_module.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    ModelClient(api, 2).ping()
    assert models == [api.model, api.fallback_model]


def test_first_answer_cancels_fallback_deadline(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    api = settings.api.model_copy(update={
        "timeout_seconds": 0.5, "first_answer_timeout_seconds": 0.05,
    })
    monkeypatch.setenv("INTERN_AI_API_KEY", "test-secret-marker")
    original_client = httpx.AsyncClient
    models = []

    class SlowFinish(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"O"}}]}\n\n'
            await asyncio.sleep(0.1)
            yield b'data: {"choices":[{"delta":{"content":"K"},"finish_reason":"stop"}]}\n\n'

        async def aclose(self):
            pass

    def handle(request):
        models.append(json.loads(request.content)["model"])
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=SlowFinish())

    monkeypatch.setattr(model_module.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    ModelClient(api, 2).ping()
    assert models == [api.model]


def test_partial_answer_timeout_does_not_retry_with_fallback(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    api = settings.api.model_copy(update={
        "timeout_seconds": 0.2, "first_answer_timeout_seconds": 0.05,
    })
    monkeypatch.setenv("INTERN_AI_API_KEY", "test-secret-marker")
    original_client = httpx.AsyncClient
    models = []

    class IncompleteStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            await asyncio.sleep(0.3)

        async def aclose(self):
            pass

    def handle(request):
        models.append(json.loads(request.content)["model"])
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=IncompleteStream())

    monkeypatch.setattr(model_module.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    with pytest.raises(ModelUnavailable, match="exceeded 0.2s total"):
        ModelClient(api, 2).ping()
    assert models == [api.model]


def test_truncated_stream_is_rejected_and_reports_finish_reason(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    monkeypatch.setenv("INTERN_AI_API_KEY", "test-secret-marker")
    original_client = httpx.AsyncClient
    body = (
        'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":"length"}]}\n\n'
        'data: [DONE]\n\n'
    )
    monkeypatch.setattr(model_module.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=body,
        )), **kwargs,
    ))
    notes = []
    with pytest.raises(ModelUnavailable, match="finish_reason=length"):
        ModelClient(settings.api, 2, on_retry=notes.append).ping()
    assert any("finish_reason=length" in note for note in notes)
    assert not any("completed" in note for note in notes)
    assert all("partial" not in note and "test-secret-marker" not in note for note in notes)


def test_incomplete_stream_retries_without_using_partial_answer(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    monkeypatch.setenv("INTERN_AI_API_KEY", "test-secret-marker")
    monkeypatch.setattr(model_module.time, "sleep", lambda _: None)
    original_client = httpx.AsyncClient
    requests = []
    notes = []

    def handle(request):
        requests.append(request)
        finish = "data: [DONE]\n\n" if len(requests) == 2 else ""
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=(
            'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":null}]}\n\n' + finish
        ))

    monkeypatch.setattr(model_module.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    ModelClient(settings.api, 3, on_retry=notes.append).ping()
    assert len(requests) == 2
    assert notes[0].startswith("Model API request 1: headers in ")
    assert notes[1].startswith("Model API request 1: first output in ")
    assert "RemoteProtocolError" in notes[2]
    assert "retrying request 2/3" in notes[2]
    assert notes[-1].startswith("Model API request 2: completed in ")
    assert "2 chars, finish=unknown" in notes[-1]


def test_config_error_does_not_echo_accidentally_pasted_key(tmp_path):
    secret = "sk-test-sensitive-value"
    config = tmp_path / "config.yaml"
    config.write_text(f"api:\n  key_env: {secret}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="environment variable name") as error:
        load_settings(config)
    assert secret not in str(error.value)


def test_fallback_settings_require_distinct_model_and_shorter_deadline():
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    api = settings.api
    with pytest.raises(ValueError, match="configured together"):
        api.model_validate({**api.model_dump(), "fallback_model": None})
    with pytest.raises(ValueError, match="must differ"):
        api.model_validate({**api.model_dump(), "fallback_model": api.model})
    with pytest.raises(ValueError, match="must be less"):
        api.model_validate({**api.model_dump(), "first_answer_timeout_seconds": api.timeout_seconds})


def test_repair_prompt_includes_complete_labeled_judge_feedback(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    client = ModelClient(settings.api, 2)
    captured = []
    candidate = Candidate(
        "class Solution: pass", "summary", "approach", "O(1)", "O(1)",
        [SampleCase(args=[["."]], expected=[["1"]])],
    )
    problem = Problem(ProblemRef(37, "Sudoku", "sudoku-solver", "https://leetcode.cn/problems/sudoku-solver/"),
                      "Solve Sudoku", "class Solution: pass")
    monkeypatch.setattr(client, "_validated_chat", lambda system, user, parse, **kwargs: captured.append((system, user)) or candidate)
    feedback = "Failing input:\nboard = []\nActual output:\n[]\nExpected output:\n[1]" + "x" * 5000 + "END"

    reference = Reference("source", "https://example.com/solution",
                          "## Description\nrepeated problem statement\n## Solutions\nUse backtracking")
    assert client.repair(problem, candidate, feedback, [reference]) is candidate
    system, user = captured[0]
    assert "regression case" in system
    assert feedback in user
    assert '"args": [["."]], "expected": [["1"]]' in user
    assert "## Solutions\nUse backtracking" in user
    assert "repeated problem statement" not in user


def test_repair_retries_unchanged_python_logic(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    client = ModelClient(settings.api, 3)
    failed = Candidate("class Solution:\n    def f(self):\n        return 1\n", "old", "old", "O(1)", "O(1)")
    responses = iter([
        json.dumps({"code": "# revised\n" + failed.code, "summary": "new"}),
        json.dumps({"code": failed.code.replace("return 1", "return 2")}),
    ])
    prompts = []

    def chat(system, user, model_override=None):
        prompts.append((user, model_override))
        return next(responses)

    monkeypatch.setattr(client, "_chat", chat)
    problem = Problem(ProblemRef(37, "Sudoku", "sudoku-solver", "https://leetcode.cn/problems/sudoku-solver/"),
                      "Return 2", "class Solution: pass")
    repaired = client.repair(problem, failed, "expected 2, got 1", [])

    assert "return 2" in repaired.code
    assert len(prompts) == 2
    assert "same Python logic" in prompts[1][0]
    assert "Change the code logic" in prompts[1][0]
    assert prompts[0][1] is None
    assert prompts[1][1] == settings.api.fallback_model


def test_repair_rejects_twice_unchanged_python_logic(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    client = ModelClient(settings.api, 3)
    failed = Candidate("class Solution: pass", "old", "old", "O(1)", "O(1)")
    calls = []

    def chat(system, user, model_override=None):
        calls.append(model_override)
        return json.dumps({"code": "class Solution: pass", "summary": "new"})

    monkeypatch.setattr(client, "_chat", chat)
    problem = Problem(ProblemRef(37, "Sudoku", "sudoku-solver", "https://leetcode.cn/problems/sudoku-solver/"),
                      "Solve Sudoku", "class Solution: pass")
    with pytest.raises(ModelUnavailable, match="same Python logic"):
        client.repair(problem, failed, "wrong answer", [])
    assert calls == [None, settings.api.fallback_model]


def test_repair_retries_changed_code_that_still_fails_saved_case(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    client = ModelClient(settings.api, 3)
    failed = Candidate(
        "class Solution:\n    def compute(self, x):\n        return 0\n",
        "old", "old", "O(1)", "O(1)",
        [SampleCase(args=[1], expected=2)],
    )
    responses = iter([
        json.dumps({"code": failed.code.replace("return 0", "return 1")}),
        json.dumps({"code": failed.code.replace("return 0", "return 2")}),
    ])
    prompts = []

    def chat(system, user, model_override=None):
        prompts.append((user, model_override))
        return next(responses)

    monkeypatch.setattr(client, "_chat", chat)
    problem = Problem(ProblemRef(50, "Pow", "powx-n", "https://leetcode.cn/problems/powx-n/"),
                      "Return 2", "class Solution: pass")
    repaired = client.repair(problem, failed, "expected 2, got 0", [])

    assert "return 2" in repaired.code
    assert "repair failed saved regression cases" in prompts[1][0]
    assert prompts[1][1] == settings.api.fallback_model


def test_model_401_is_not_reported_as_success(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    monkeypatch.setenv("INTERN_AI_API_KEY", "test-secret-marker")
    original_client = httpx.AsyncClient
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(401)

    monkeypatch.setattr(
        model_module.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(handle), **kwargs
        ),
    )

    with pytest.raises(ModelUnavailable, match="HTTP 401") as error:
        ModelClient(settings.api, 2).ping()
    assert len(requests) == 1
    assert "test-secret-marker" not in str(error.value)


def test_model_call_limit_counts_http_retries(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    monkeypatch.setenv("INTERN_AI_API_KEY", "test-secret-marker")
    monkeypatch.setattr(model_module.time, "sleep", lambda _: None)
    original_client = httpx.AsyncClient
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(503)

    monkeypatch.setattr(
        model_module.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    with pytest.raises(ModelUnavailable, match="call limit reached"):
        ModelClient(settings.api, 1).ping()
    assert len(requests) == 1


def test_remote_disconnect_retries_and_reports_progress(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    monkeypatch.setenv("INTERN_AI_API_KEY", "test-secret-marker")
    monkeypatch.setattr(model_module.time, "sleep", lambda _: None)
    original_client = httpx.AsyncClient
    requests = []
    notes = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.RemoteProtocolError("server disconnected", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    monkeypatch.setattr(
        model_module.httpx, "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    client = ModelClient(settings.api, 3, on_retry=notes.append)
    client.ping()
    assert client.calls == len(requests) == 2
    assert "RemoteProtocolError" in notes[0]
    assert "retrying request 2/3" in notes[0]
    assert notes[1].startswith("Model API request 2: headers in ")
    assert notes[2].startswith("Model API request 2: first output in ")
    assert notes[3].startswith("Model API request 2: completed in ")
    assert all("test-secret-marker" not in note for note in notes)


def test_remote_disconnect_respects_model_call_limit(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    monkeypatch.setenv("INTERN_AI_API_KEY", "test-secret-marker")
    monkeypatch.setattr(model_module.time, "sleep", lambda _: None)
    original_client = httpx.AsyncClient
    requests = []

    def handle(request):
        requests.append(request)
        raise httpx.RemoteProtocolError("server disconnected", request=request)

    monkeypatch.setattr(
        model_module.httpx, "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    with pytest.raises(ModelUnavailable, match="call limit reached"):
        ModelClient(settings.api, 2).ping()
    assert len(requests) == 2


def test_remote_disconnect_stops_after_three_attempts(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    monkeypatch.setenv("INTERN_AI_API_KEY", "test-secret-marker")
    monkeypatch.setattr(model_module.time, "sleep", lambda _: None)
    original_client = httpx.AsyncClient
    requests = []

    def handle(request):
        requests.append(request)
        raise httpx.RemoteProtocolError("server disconnected", request=request)

    monkeypatch.setattr(
        model_module.httpx, "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    with pytest.raises(ModelUnavailable, match="after 3 attempts: RemoteProtocolError"):
        ModelClient(settings.api, 10).ping()
    assert len(requests) == 3


def test_solution_repairs_invalid_code_once_and_requests_efficient_algorithm(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    client = ModelClient(settings.api, 3)
    responses = iter([
        '{"code": "class Solution:\\n def broken("}',
        '{"code": "class Solution: pass", "summary": "summary", "approach": "approach", '
        '"time_complexity": "O(n)", "space_complexity": "O(1)", "tests": []}',
    ])
    prompts = []

    def chat(system, user, model_override=None):
        prompts.append((system, user, model_override))
        return next(responses)

    monkeypatch.setattr(client, "_chat", chat)
    problem = Problem(
        ProblemRef(3, "test", "test", "https://leetcode.cn/problems/test/"),
        "Find the answer", "class Solution: pass",
    )
    candidate = client.solve(problem)

    assert candidate.time_complexity == "O(n)"
    assert len(prompts) == 2
    assert "auxiliary memory" in prompts[0][0]
    assert "invalid Python syntax" in prompts[1][1]
    assert prompts[1][2] == settings.api.fallback_model


def test_optional_explanations_and_bad_tests_do_not_trigger_new_request(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    client = ModelClient(settings.api, 3)
    calls = []

    def chat(system, user):
        calls.append(1)
        return json.dumps({
            "code": "class Solution: pass",
            "tests": [{"args": "wrong", "expected": 0}] + [
                {"args": [index], "expected": index} for index in range(5)
            ],
        })

    monkeypatch.setattr(client, "_chat", chat)
    problem = Problem(
        ProblemRef(3, "test", "test", "https://leetcode.cn/problems/test/"),
        "Find the answer", "class Solution: pass",
    )
    candidate = client.solve(problem)
    assert len(calls) == 1
    assert candidate.time_complexity == "未评估"
    assert len(candidate.tests) == 3
    assert [test.args for test in candidate.tests] == [[0], [1], [2]]


def test_invalid_json_has_only_one_format_repair(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    notes = []
    client = ModelClient(settings.api, 3, on_retry=notes.append)
    calls = []

    def chat(system, user, model_override=None):
        calls.append((user, model_override))
        return "not json"

    monkeypatch.setattr(client, "_chat", chat)
    problem = Problem(
        ProblemRef(3, "test", "test", "https://leetcode.cn/problems/test/"),
        "Find the answer", "class Solution: pass",
    )
    with pytest.raises(ModelUnavailable, match="not a JSON object"):
        client.solve(problem)
    assert len(calls) == 2
    assert "Model API format repair 1/1" in notes[0]
    assert "switching to deepseek-v4-pro-0813" in notes[0]
    assert calls[1][1] == settings.api.fallback_model


@pytest.mark.parametrize("response", [
    "class Solution:\n    def solve(self):\n        memo = {}\n        return memo\n",
    "Here is the solution:\n```python\nclass Solution:\n    def solve(self):\n        return 1\n```",
])
def test_candidate_accepts_unambiguous_python_code_response(response):
    candidate = ModelClient._candidate(response)
    assert "class Solution:" in candidate.code
    assert candidate.tests == []


def test_candidate_accepts_json_fence_and_literal_newline_in_code():
    response = (
        "Here is the JSON:\n```json\n"
        '{"code": "class Solution:\n    def solve(self):\n        return 1", "tests": []}'
        "\n```"
    )
    candidate = ModelClient._candidate(response)
    assert "return 1" in candidate.code


def test_candidate_rejects_ambiguous_or_non_solution_response():
    with pytest.raises(ModelUnavailable, match="not a JSON object"):
        ModelClient._candidate('{"code": "class Solution: pass"} {"code": "class Solution: pass"}')
    with pytest.raises(ModelUnavailable, match="not a JSON object"):
        ModelClient._candidate("Here is an explanation without executable code.")


def test_review_retries_invalid_json(monkeypatch):
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    client = ModelClient(settings.api, 3)
    responses = iter(["not json", '{"approved": true, "issues": []}'])
    monkeypatch.setattr(client, "_chat", lambda system, user, model_override=None: next(responses))
    problem = Problem(
        ProblemRef(3, "test", "test", "https://leetcode.cn/problems/test/"),
        "Find the answer", "class Solution: pass",
    )
    candidate = client._candidate(
        '{"code": "class Solution: pass", "summary": "summary", "approach": "approach", '
        '"time_complexity": "O(n)", "space_complexity": "O(1)"}'
    )
    assert client.review(problem, candidate) == (True, [])
