# Configuration

[Project overview](../README.md) · [Template](../config.yaml)

All commands read the selected YAML file. `--config PATH` changes both the configuration and the directory used for local state. The tracked file contains a key's environment-variable name, not its value.

| Setting | Checked-in value | Behavior |
| --- | --- | --- |
| `api.endpoint` | `https://discovery-api.intern-ai.org.cn/v1/chat/completions` | Chat Completions endpoint; replace it with a service you can access. |
| `api.model` | `deepseek-v4-flash-0731` | Primary solver model. |
| `api.fallback_model` | `deepseek-v4-pro-0813` | Used for supported no-answer, format, completion-timeout, or rejected-repair fallbacks. |
| `api.vision_model` | `deepseek-v4-flash-vision` | Describes problem images; the provider must be able to access their public URLs. |
| `api.key_env` | `INTERN_AI_API_KEY` | Environment-variable name holding the secret. |
| `api.first_answer_timeout_seconds` | `15` | Primary-model deadline for first answer text, not for reasoning-stream activity. |
| `api.primary_completion_timeout_seconds` | `60` | Primary-model completion deadline; fallback uses the remaining total budget. |
| `api.timeout_seconds` | `180` | Total request deadline. |
| `api.stream` / `api.thinking` | `true` / `disabled` | Request streaming without thinking mode; gateways may still emit reasoning events. |
| `leetcode.base_url` / `leetcode.language` | `https://leetcode.cn` / `python3` | The supported site and submission language. |
| `leetcode.start_id` | `1` | Initial cursor when no saved/active start overrides it. |
| `browser.mode` / `browser.host` | `edge_extension` / `127.0.0.1` | Local paired extension transport. |
| `browser.port` | `8765` | Must match the extension popup. |
| `browser.poll_interval_seconds` | `0.5` | Extension command polling interval. |
| `browser.submit_trigger` / `browser.navigation_trigger` | `button` / `url` | Default site submission and navigation mechanisms. |
| `workflow.max_repairs` | `3` | Repair budget for one problem. |
| `workflow.max_model_calls` | `10` | Model-call budget, including required vision calls. |
| `workflow.review_mode` | `on_repair` | Review repairs; use `always` to also review first candidates. |
| `workflow.auto_submit` | `true` | Submit candidates automatically after the required checks. |
| `search.provider` | `bing_html` | Public reference search provider. |
| `search.fallback_after_failures` / `search.max_pages` | `2` / `3` | Search activation threshold and maximum reference pages. |
| `output.save_artifacts` | `false` | Keep resumable state/history while disabling new per-problem artifact files. |

These are template values, not guarantees that a model or endpoint is available. Use IDs supported by your account. `fallback_model` and `first_answer_timeout_seconds` must be configured together, with distinct primary/fallback models and deadlines inside the total timeout. The optional primary completion deadline must exceed the first-answer deadline.

The client uses only completed model streams. A broken stream, retryable HTTP 429/5xx response, or connection failure is retried within the available budget. Receiving answer text prevents the first-answer timer from switching models; exceeding the primary completion deadline discards partial output and passes the remaining budget to fallback. An unresolved total timeout leaves the problem resumable.

`browser.submit_trigger` and `browser.navigation_trigger` also accept `shortcut`, but whether synthetic keyboard shortcuts work depends on the current site. `review_mode` supports `on_repair` and `always`; it has no `off` value in the current settings model. Setting `auto_submit: false` stops before submission with the candidate saved, rather than completing the problem.
