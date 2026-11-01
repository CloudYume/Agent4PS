(() => {
  if (window.__AGENT4PS_HOOK__) return;
  window.__AGENT4PS_HOOK__ = true;
  const post = (type, detail) => window.dispatchEvent(new CustomEvent(
    "agent4ps-network", { detail: JSON.stringify({ type, detail }) },
  ));

  function kindFor(url, body) {
    const lower = String(url || "").toLowerCase();
    if (lower.includes("/interpret_solution")) return "run";
    if (/\/problems\/[^/]+\/submit(?:\/|\?|$)/.test(lower)) return "submit";
    if (!lower.includes("/graphql")) return null;
    const operation = String(body?.operationName || "");
    const query = String(body?.query || "");
    if (/submit/i.test(operation) || (/\bmutation\b/i.test(query) && /\bsubmit\w*/i.test(query))) return "submit";
    return null;
  }

  function parseBody(value) {
    if (!value) return null;
    if (typeof value === "string") {
      try { return JSON.parse(value); } catch (_) {
        const params = new URLSearchParams(value);
        return Object.fromEntries(params);
      }
    }
    if (value instanceof URLSearchParams || (typeof FormData !== "undefined" && value instanceof FormData)) {
      return Object.fromEntries(value);
    }
    return null;
  }

  function codeFrom(value, depth = 0) {
    if (!value || depth > 5) return "";
    if (typeof value === "string") {
      if (!value.trim().startsWith("{")) return "";
      try { return codeFrom(JSON.parse(value), depth + 1); } catch (_) { return ""; }
    }
    if (typeof value !== "object") return "";
    for (const key of ["typedCode", "typed_code", "user_code", "source_code", "code"]) {
      if (typeof value[key] === "string" && value[key]) return value[key];
    }
    for (const nested of Object.values(value)) {
      const code = codeFrom(nested, depth + 1);
      if (code) return code;
    }
    return "";
  }

  function findId(value, depth = 0) {
    if (!value || typeof value !== "object" || depth > 4) return null;
    for (const key of ["submission_id", "submissionId", "interpret_id", "interpretId"]) {
      if (value[key] != null) return String(value[key]);
    }
    for (const child of Object.values(value)) {
      const id = findId(child, depth + 1);
      if (id) return id;
    }
    return null;
  }

  function requestEvent(url, body) {
    const kind = kindFor(url, body);
    if (kind) post("request", { kind, code: codeFrom(body), url: String(url) });
    return kind;
  }

  function responseEvent(url, data, kind) {
    const lower = String(url).toLowerCase();
    if (lower.includes("/submissions/detail/") && lower.includes("/check")) {
      if (data?.status_code == null || data.finished === false ||
          ["PENDING", "STARTED", "PROCESSING"].includes(String(data.state || "").toUpperCase())) return;
      const match = lower.match(/\/submissions\/detail\/([^/]+)\/check/);
      post("terminal", {
        submission_id: String(data.submission_id || match?.[1] || ""),
        status_code: Number(data.status_code),
        status_msg: data.status_msg || "",
        finished: data.finished ?? null,
        state: data.state || "",
        question_id: data.question_id == null ? null : String(data.question_id),
        total_correct: data.total_correct ?? null,
        total_testcases: data.total_testcases ?? null,
        status_runtime: data.status_runtime || "",
        status_memory: data.status_memory || "",
        runtime_percentile: data.runtime_percentile ?? null,
        memory_percentile: data.memory_percentile ?? null,
        compare_result: data.compare_result ?? null,
        error: data.full_compile_error || data.full_runtime_error || data.runtime_error || data.compile_error || "",
        last_testcase: inputText(data.last_testcase || data.input_formatted || data.input),
        expected_output: data.expected_output || singleAnswer(data.expected_code_answer),
        code_output: data.code_output || singleAnswer(data.code_answer),
        code_answers: data.code_answer ?? null,
        expected_code_answers: data.expected_code_answer ?? null,
      });
      return;
    }
    if (kind) {
      const id = findId(data);
      if (id) post("issued", { kind, submission_id: id });
    }
  }

  function singleAnswer(value) {
    if (typeof value === "string") return value;
    if (!Array.isArray(value) || value.length !== 1) return "";
    return typeof value[0] === "string" ? value[0] : JSON.stringify(value[0]);
  }

  function inputText(value) {
    return typeof value === "string" ? value : value == null ? "" : JSON.stringify(value);
  }

  const originalFetch = window.fetch;
  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : input?.url || input?.href || "";
    const body = parseBody(init?.body);
    const kind = requestEvent(url, body);
    const requestKind = !body && input instanceof Request
      ? input.clone().text().then((text) => requestEvent(url, parseBody(text))).catch(() => null)
      : Promise.resolve(kind);
    const promise = originalFetch.apply(this, arguments);
    if (kind || input instanceof Request || /\/submissions\/detail\/[^/]+\/check/.test(String(url))) {
      promise.then((response) => {
        const relevant = /\/submissions\/detail\/[^/]+\/check/.test(String(url));
        const resolvedKind = requestKind.then((value) => kind || value);
        const originalJson = response.json.bind(response);
        response.json = async () => {
          const data = await originalJson();
          const requestType = await resolvedKind;
          if (requestType || relevant) responseEvent(url, data, requestType);
          return data;
        };
        resolvedKind.then((requestType) => {
          if (requestType || relevant) {
            response.clone().json().then((data) => responseEvent(url, data, requestType)).catch(() => {});
          }
        }).catch(() => {});
      }).catch(() => {});
    }
    return promise;
  };

  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__agent4psUrl = url;
    return originalOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    const url = this.__agent4psUrl || "";
    const kind = requestEvent(url, parseBody(body));
    if (kind || /\/submissions\/detail\/[^/]+\/check/.test(String(url))) {
      this.addEventListener("load", () => {
        try { responseEvent(url, JSON.parse(this.responseText), kind); } catch (_) {}
      });
    }
    return originalSend.apply(this, arguments);
  };
})();
