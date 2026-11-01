(() => {
  const QUESTION = `query AgentProblem($slug: String!) {
    userStatus { isSignedIn }
    question(titleSlug: $slug) {
      questionId questionFrontendId titleSlug translatedTitle title translatedContent content status isPaidOnly
      sampleTestCase exampleTestcases metaData enableRunCode
      codeSnippets { langSlug code }
    }
  }`;
  const QUESTION_STATUS = `query AgentProblemStatus($slug: String!) {
    userStatus { isSignedIn }
    question(titleSlug: $slug) { questionId questionFrontendId titleSlug status }
  }`;
  const POLL_MS = 500;
  const HEARTBEAT_MS = 2000;
  let pollMs = POLL_MS;
  const PROTOCOL_VERSION = 5;
  const CAPABILITIES = ["command_phase", "execution_heartbeat", "submission_recovery", "write_authorization", "problem_status", "captured_submission_verification"];
  const READ_ONLY_COMMANDS = new Set(["check_submission", "submission_baseline", "recover_submission", "problem_status", "verify_submission"]);
  const DOCUMENT_ID = crypto.randomUUID();
  const SUBMISSIONS = `query AgentSubmissions($slug: String!) {
    userStatus { isSignedIn }
    submissionList(offset: 0, limit: 20, questionSlug: $slug) {
      submissions { id }
    }
  }`;
  const SUBMISSION_DETAIL = `query AgentSubmissionDetail($id: ID!) {
    submissionDetail(submissionId: $id) { code }
  }`;
  let cachedSlug = null;
  let cachedProblem = null;
  let fetching = null;
  let polling = false;
  let heartbeating = false;
  let action = null;

  function singleAnswer(value) {
    if (typeof value === "string") return value;
    if (!Array.isArray(value) || value.length !== 1) return "";
    return typeof value[0] === "string" ? value[0] : JSON.stringify(value[0]);
  }

  function inputText(value) {
    return typeof value === "string" ? value : value == null ? "" : JSON.stringify(value);
  }

  function pageState(slug, problem, ready) {
    return {
      slug, url: `https://leetcode.cn/problems/${slug}/`, document_id: DOCUMENT_ID,
      protocol_version: PROTOCOL_VERSION, capabilities: CAPABILITIES, ready,
      visible: document.visibilityState === "visible", problem,
    };
  }

  function within(promise, milliseconds, message) {
    let timer;
    return Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(message)), milliseconds);
      }),
    ]).finally(() => clearTimeout(timer));
  }

  function sendResult(result) {
    return within(
      chrome.runtime.sendMessage({ type: "RESULT", result }),
      4000, "browser result acknowledgement timed out",
    );
  }

  function commandPhase(command, phase, details = {}) {
    return within(chrome.runtime.sendMessage({ type: "PHASE", phase: {
      command_id: command.id, kind: command.kind, slug: command.target_slug,
      document_id: DOCUMENT_ID, phase, ...details,
    } }), 4000, "browser phase acknowledgement timed out");
  }

  function emitPhase(command, phase, details = {}) {
    commandPhase(command, phase, details).catch(() => {});
  }

  function slugFromUrl() {
    const match = location.pathname.match(/^\/problems\/([a-z0-9-]+)\/?/);
    return match?.[1] || null;
  }

  function hasChallenge() {
    const text = (document.body?.innerText || "").slice(0, 3000).toLowerCase();
    return ["人机验证", "安全验证", "请输入验证码", "滑动验证", "verify you are human", "captcha"]
      .some((word) => text.includes(word)) || !!document.querySelector('iframe[src*="captcha"]');
  }

  function normalizedStatus(value) {
    const rawStatus = String(value || "").toUpperCase();
    return ["AC", "SOLVED", "ACCEPTED"].includes(rawStatus) ? "AC"
      : ["NOTAC", "TRIED"].includes(rawStatus) ? "TRIED"
      : rawStatus === "" ? "NOT_STARTED" : rawStatus;
  }

  async function loadProblem(slug) {
    const response = await fetch("https://leetcode.cn/graphql/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ query: QUESTION, operationName: "AgentProblem", variables: { slug } }),
    });
    if (!response.ok) throw new Error(`LeetCode GraphQL HTTP ${response.status}`);
    const body = await response.json();
    if (body.errors?.length) throw new Error(body.errors[0].message);
    const question = body.data?.question;
    if (!question || question.titleSlug !== slug) throw new Error("Problem metadata unavailable");
    const starter = question.codeSnippets?.find((item) => item.langSlug === "python3")?.code || "";
    return {
      number: Number(question.questionFrontendId),
      slug,
      title: question.translatedTitle || question.title || slug,
      content: question.translatedContent || question.content || "",
      starter,
      question_id: String(question.questionId || ""),
      sample_testcase: question.sampleTestCase || "",
      example_testcases: question.exampleTestcases || "",
      meta_data: question.metaData || "",
      enable_run_code: question.enableRunCode !== false,
      status: normalizedStatus(question.status),
      is_paid: !!question.isPaidOnly,
      logged_in: body.data?.userStatus?.isSignedIn === true,
      challenge: hasChallenge(),
    };
  }

  async function problemFor(slug) {
    if (slug !== cachedSlug) {
      cachedSlug = slug;
      cachedProblem = null;
      fetching = null;
    }
    if (cachedProblem) return cachedProblem;
    if (!fetching) fetching = loadProblem(slug).then((value) => {
      cachedProblem = value;
      return value;
    }).finally(() => { fetching = null; });
    return fetching;
  }

  async function ensurePython3() {
    const buttons = [...document.querySelectorAll('button[aria-haspopup="dialog"]')];
    const language = buttons.find((button) => /^(C\+\+|Java|Python3|Python|JavaScript|TypeScript|Go)$/.test(button.textContent.trim()));
    if (!language) throw new Error("Language selector unavailable");
    if (language.textContent.trim() === "Python3") return;
    language.click();
    for (let attempt = 0; attempt < 30; attempt++) {
      const option = [...document.querySelectorAll('[role="option"], [role="menuitem"], button, div, span')]
        .filter((element) => element.textContent.trim() === "Python3" && element.getClientRects().length)
        .at(-1);
      if (option) {
        option.click();
        await new Promise((resolve) => setTimeout(resolve, 300));
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    throw new Error("Python3 could not be selected");
  }

  function actionButton(kind) {
    const locator = kind === "run" ? "console-run-button" : "console-submit-button";
    const button = document.querySelector(`button[data-e2e-locator="${locator}"]`);
    if (button?.getClientRects().length && !button.disabled && button.getAttribute("aria-disabled") !== "true") {
      return button;
    }
    return null;
  }

  async function activeTab() {
    const result = await chrome.runtime.sendMessage({ type: "ACTIVE" });
    return result?.active === true && document.visibilityState === "visible";
  }

  async function sha256(text) {
    const bytes = new TextEncoder().encode(text.trim());
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  async function graphql(query, operationName, variables, signal) {
    const response = await fetch("https://leetcode.cn/graphql/", {
      method: "POST", headers: { "Content-Type": "application/json" },
      credentials: "include", cache: "no-store", signal,
      body: JSON.stringify({ query, operationName, variables }),
    });
    const body = await response.json().catch(() => null);
    if (body?.errors?.length) throw new Error(String(body.errors[0].message || "GraphQL failed"));
    if (!response.ok) throw new Error(`LeetCode GraphQL HTTP ${response.status}`);
    return body?.data;
  }

  async function submissionIds(slug) {
    const data = await graphql(SUBMISSIONS, "AgentSubmissions", { slug });
    if (data?.userStatus?.isSignedIn !== true) throw new Error("LeetCode login is required for submission history");
    const submissions = data?.submissionList?.submissions;
    if (!Array.isArray(submissions)) throw new Error("submission list is unavailable");
    return submissions.map((entry) => String(entry.id)).filter((id) => /^\d+$/.test(id));
  }

  async function matchingSubmission(slug, baseline, digest) {
    const ids = await submissionIds(slug);
    if (baseline && !ids.includes(baseline)) {
      throw new Error("submission baseline is outside the latest 20 records");
    }
    const newer = baseline ? ids.slice(0, ids.indexOf(baseline)) : ids;
    const matches = [];
    for (const id of newer) {
      const data = await graphql(SUBMISSION_DETAIL, "AgentSubmissionDetail", { id });
      const code = data?.submissionDetail?.code;
      if (typeof code === "string" && await sha256(code) === digest) matches.push(id);
    }
    if (matches.length > 1) throw new Error("multiple new submissions have the candidate code");
    return matches[0] || null;
  }

  async function verifySubmissionCode(id, digest) {
    if (!/^\d+$/.test(String(id)) || !/^[a-f0-9]{64}$/.test(digest || "")) {
      throw new Error("captured submission identity is invalid");
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 7000);
    try {
      for (let attempt = 0; attempt < 3; attempt++) {
        const data = await graphql(SUBMISSION_DETAIL, "AgentSubmissionDetail", {
          id: String(id),
        }, controller.signal);
        const code = data?.submissionDetail?.code;
        if (typeof code === "string") {
          if (await sha256(code) !== digest) {
            throw new Error("captured submission code does not match the candidate");
          }
          return;
        }
        if (attempt < 2) await new Promise((resolve) => setTimeout(resolve, 400));
      }
      throw new Error("captured submission code is unavailable; result unconfirmed");
    } finally {
      clearTimeout(timer);
    }
  }

  function waitForAction(kind, expectedCode, timeoutMs, command) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        const detail = action?.requestSeen
          ? action.codeMatched ? "request seen, submission ID missing" : "request code did not match the editor"
          : "no matching site request observed";
        action = null;
        reject(new Error(`${kind} response did not arrive: ${detail}`));
      }, timeoutMs);
      action = { kind, command, expectedCode, requestSeen: false, codeMatched: false, submissionId: null, resolve: async (value) => {
        clearTimeout(timer);
        action = null;
        if (kind === "submit") {
          try {
            await verifySubmissionCode(value.submission_id, command.payload?.code_sha256);
          } catch (error) {
            error.submission_id = value.submission_id;
            reject(error);
            return;
          }
          try {
            const ack = await sendResult({
              command_id: command.id, kind: command.kind, slug: command.target_slug,
              command_kind: command.kind, document_id: DOCUMENT_ID, ok: true, ...value,
            });
            if (ack?.ok) {
              resolve({ ...value, reported: true });
              return;
            }
          } catch (_) {}
        }
        resolve(value);
      } };
    });
  }

  window.addEventListener("agent4ps-network", (event) => {
    let data;
    try { data = JSON.parse(event.detail); } catch (_) { return; }
    if (!action) return;
    const detail = data.detail || {};
    if (data.type === "request" && detail.kind === action.kind) {
      action.requestSeen = true;
      const requestedCode = typeof detail.code === "string" ? detail.code.trim() : "";
      action.codeMatched = requestedCode === action.expectedCode.trim()
        || (action.kind === "submit" && !requestedCode);
      emitPhase(action.command, "request_seen");
    } else if (data.type === "issued" && action.codeMatched && detail.kind === action.kind && detail.submission_id) {
      if (action.kind === "submit" && !/^\d+$/.test(String(detail.submission_id))) return;
      action.submissionId = String(detail.submission_id);
      emitPhase(action.command, "id_seen", action.kind === "submit" ? { submission_id: action.submissionId } : {});
      if (action.kind === "submit") {
        action.resolve({ kind: "submit", submission_id: action.submissionId, code: action.expectedCode });
      }
    } else if (data.type === "terminal" && action.codeMatched &&
        action.submissionId === String(detail.submission_id)) {
      emitPhase(action.command, "terminal");
      action.resolve({ ...detail, kind: action.kind, code: action.expectedCode });
    }
  });

  function pressShortcut(key, code) {
    const target = document.activeElement?.isConnected ? document.activeElement : document;
    target.dispatchEvent(new KeyboardEvent("keydown", {
      key, code, ctrlKey: true, bubbles: true, cancelable: true,
    }));
  }

  async function execute(command) {
    const result = {
      command_id: command.id, kind: command.kind, slug: command.target_slug,
      command_kind: command.kind,
      document_id: DOCUMENT_ID, ok: false,
    };
    try {
      if (!/^[a-f0-9]{24}$/.test(command.id || "") ||
          !["navigate", "read_draft", "check_submission", "submission_baseline", "recover_submission", "verify_submission", "problem_status", "run", "submit"].includes(command.kind) ||
          command.document_id !== DOCUMENT_ID || !/^[a-z0-9-]+$/.test(command.target_slug || "")) {
        throw new Error("invalid browser command envelope; reload the extension and problem tab");
      }
      if (slugFromUrl() !== command.target_slug ||
          !READ_ONLY_COMMANDS.has(command.kind) && !(await activeTab())) {
        throw new Error("bound tab is no longer active on the expected problem");
      }
      const started = await commandPhase(command, "started");
      if (!started?.ok) throw new Error("browser command acknowledgement failed");
      if (command.kind === "navigate") {
        const url = command.payload?.url;
        if (!/^https:\/\/leetcode\.cn\/problems\/[a-z0-9-]+\/$/.test(url || "")) throw new Error("invalid next problem URL");
        const ack = await sendResult({ ...result, ok: true });
        if (!ack?.ok || !(await activeTab())) return;
        if (command.payload?.trigger === "shortcut") {
          pressShortcut("ArrowRight", "ArrowRight");
        } else {
          location.assign(url);
        }
        return;
      }
      if (command.kind === "read_draft") {
        let code;
        for (let attempt = 0; attempt < 60; attempt++) {
          if (slugFromUrl() !== command.target_slug || !(await activeTab())) {
            throw new Error("bound tab changed while waiting for the editor");
          }
          const current = await chrome.runtime.sendMessage({ type: "READ_CODE", any: true });
          if (typeof current?.code === "string") {
            code = current.code;
            break;
          }
          if (attempt < 59) await new Promise((resolve) => setTimeout(resolve, 250));
        }
        if (typeof code !== "string") throw new Error("editor draft is unavailable after 15s");
        await sendResult({ ...result, ok: true, code });
        return;
      }
      if (command.kind === "check_submission") {
        const id = String(command.payload?.submission_id || "");
        if (!/^\d+$/.test(id)) throw new Error("invalid submission ID");
        if (hasChallenge() || cachedProblem?.logged_in !== true) throw new Error("LeetCode login or verification is required");
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 20000);
        let data;
        try {
          const response = await fetch(`https://leetcode.cn/submissions/detail/${id}/check/`, {
            credentials: "include", cache: "no-store", signal: controller.signal,
          });
          if (!response.ok) throw new Error(`LeetCode check HTTP ${response.status}`);
          data = await response.json();
        } catch (error) {
          if (controller.signal.aborted) throw new Error("submission check timed out");
          throw error;
        } finally {
          clearTimeout(timer);
        }
        const pending = (data.finished === false ||
          ["PENDING", "STARTED", "PROCESSING"].includes(String(data.state || "").toUpperCase()) ||
          data.finished !== true && data.status_code == null);
        let lastTestcase = inputText(data.last_testcase || data.input_formatted || data.input);
        if (Number(data.status_code) === 14 && !data.expected_output && !singleAnswer(data.expected_code_answer) &&
            lastTestcase.length > 6000) {
          lastTestcase = `${lastTestcase.slice(0, 3000)}\n... (${lastTestcase.length - 6000} characters omitted) ...\n${lastTestcase.slice(-3000)}`;
        }
        await sendResult({
          ...result, ok: true, kind: "submit", submission_id: id, pending,
          finished: data.finished ?? null, state: data.state || "",
          question_id: data.question_id == null ? null : String(data.question_id),
          status_code: data.status_code == null ? null : Number(data.status_code),
          status_msg: data.status_msg || "", status_runtime: data.status_runtime || "",
          status_memory: data.status_memory || "", runtime_percentile: data.runtime_percentile ?? null,
          memory_percentile: data.memory_percentile ?? null,
          total_correct: data.total_correct ?? null, total_testcases: data.total_testcases ?? null,
          error: data.full_compile_error || data.full_runtime_error || data.runtime_error || data.compile_error || "",
          last_testcase: lastTestcase,
          expected_output: data.expected_output || singleAnswer(data.expected_code_answer),
          code_output: data.code_output || singleAnswer(data.code_answer),
          code_answers: data.code_answer ?? null,
          expected_code_answers: data.expected_code_answer ?? null,
        });
        return;
      }
      if (command.kind === "submission_baseline") {
        if (hasChallenge() || cachedProblem?.logged_in !== true) throw new Error("LeetCode login or verification is required");
        const ids = await submissionIds(command.target_slug);
        await sendResult({
          ...result, ok: true, latest_id: ids[0] || null,
        });
        return;
      }
      if (command.kind === "problem_status") {
        if (hasChallenge() || cachedProblem?.logged_in !== true) throw new Error("LeetCode login or verification is required");
        const slug = command.payload?.question_slug;
        if (!/^[a-z0-9-]+$/.test(slug || "")) throw new Error("invalid problem status request");
        const data = await graphql(QUESTION_STATUS, "AgentProblemStatus", { slug });
        if (data?.userStatus?.isSignedIn !== true || data?.question?.titleSlug !== slug) {
          throw new Error("problem status could not be confirmed");
        }
        await sendResult({
          ...result, ok: true, question_slug: slug,
          question_id: String(data.question.questionId || ""),
          number: Number(data.question.questionFrontendId),
          status: normalizedStatus(data.question.status),
        });
        return;
      }
      if (command.kind === "recover_submission") {
        if (hasChallenge() || cachedProblem?.logged_in !== true) throw new Error("LeetCode login or verification is required");
        const slug = command.payload?.question_slug;
        const baseline = command.payload?.baseline_id;
        const digest = command.payload?.code_sha256;
        if (!/^[a-z0-9-]+$/.test(slug || "") ||
            !(baseline === null || /^\d+$/.test(String(baseline))) ||
            !/^[a-f0-9]{64}$/.test(digest || "")) throw new Error("invalid submission recovery request");
        const id = await matchingSubmission(slug, baseline, digest);
        await sendResult({
          ...result, ok: true, submission_id: id,
        });
        return;
      }
      if (command.kind === "verify_submission") {
        if (hasChallenge() || cachedProblem?.logged_in !== true) throw new Error("LeetCode login or verification is required");
        const slug = command.payload?.question_slug;
        const id = String(command.payload?.submission_id || "");
        const digest = command.payload?.code_sha256;
        if (!/^[a-z0-9-]+$/.test(slug || "") || !/^\d+$/.test(id) ||
            !/^[a-f0-9]{64}$/.test(digest || "")) throw new Error("invalid captured submission verification request");
        const ids = await submissionIds(slug);
        if (!ids.includes(id)) throw new Error("captured submission ID is not in this problem's recent submissions");
        await verifySubmissionCode(id, digest);
        await sendResult({ ...result, ok: true, verified: true });
        return;
      }
      await ensurePython3();
      if (!(await activeTab()) || slugFromUrl() !== command.target_slug) {
        throw new Error("bound tab became inactive before editing code");
      }
      let code;
      if (command.kind === "run") {
        code = command.payload?.code;
        if (typeof code !== "string" || !code.trim()) throw new Error("candidate code is empty");
        const written = await chrome.runtime.sendMessage({
          type: "WRITE_CODE", code, command_id: command.id,
          slug: command.target_slug, document_id: DOCUMENT_ID,
        });
        if (!written?.ok) throw new Error(written?.error || "Monaco write failed");
      } else if (command.kind === "submit") {
        const current = await chrome.runtime.sendMessage({ type: "READ_CODE" });
        code = current?.code;
        if (typeof code !== "string" || await sha256(code) !== command.payload?.code_sha256) {
          throw new Error("editor code changed after the successful run");
        }
      } else {
        throw new Error("unknown browser command");
      }
      if (!(await activeTab())) throw new Error("bound tab became inactive");
      const editorReady = await commandPhase(command, "editor_ready");
      if (!editorReady?.ok) throw new Error("editor readiness could not be acknowledged");
      const shortcut = command.kind === "submit" && command.payload?.trigger === "shortcut";
      const button = shortcut ? null : actionButton(command.kind);
      if (!shortcut && !button) throw new Error("LeetCode action button is unavailable");
      const triggerReady = await commandPhase(command, "triggering");
      if (!triggerReady?.ok || !(await activeTab()) || slugFromUrl() !== command.target_slug) {
        throw new Error("site action was not triggered because the tab changed or bridge disconnected");
      }
      const judged = waitForAction(command.kind, code, command.kind === "run" ? 120000 : shortcut ? 15000 : 45000, command);
      if (shortcut) pressShortcut("Enter", "Enter");
      else button.click();
      const outcome = await judged;
      if (outcome.reported) return;
      Object.assign(result, outcome, { ok: true });
    } catch (error) {
      result.error = String(error?.message || error);
      if (command.kind === "submit" && /^\d+$/.test(String(error?.submission_id || ""))) {
        result.submission_id = String(error.submission_id);
      }
    }
    await sendResult(result);
  }

  async function poll() {
    if (polling) return;
    const slug = slugFromUrl();
    if (!slug) return;
    polling = true;
    try {
      const problem = await problemFor(slug);
      problem.challenge = hasChallenge();
      const response = await chrome.runtime.sendMessage({
        type: "HELLO",
        page: pageState(slug, problem, true),
      });
      if (Number.isInteger(response?.poll_interval_ms)
          && response.poll_interval_ms >= 250 && response.poll_interval_ms <= 10000) {
        pollMs = response.poll_interval_ms;
      }
      if (response?.command) await execute(response.command);
    } catch (_) {
      cachedProblem = null;
    } finally {
      polling = false;
    }
  }

  async function heartbeat() {
    if (heartbeating || !polling || !cachedProblem) return;
    const slug = slugFromUrl();
    if (!slug) return;
    heartbeating = true;
    try {
      const response = await chrome.runtime.sendMessage({
        type: "HELLO", page: pageState(slug, { ...cachedProblem, challenge: hasChallenge() }, false),
      });
      if (response?.command && READ_ONLY_COMMANDS.has(response.command.kind)) {
        await execute(response.command);
      }
    } catch (_) {
      // The bridge timeout reports the last acknowledged command phase.
    } finally {
      heartbeating = false;
    }
  }

  async function pollLoop() {
    await poll();
    setTimeout(pollLoop, pollMs);
  }

  setInterval(heartbeat, HEARTBEAT_MS);
  pollLoop();
})();
