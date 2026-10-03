from __future__ import annotations

import argparse
import errno
import hashlib
import os
import sys
import threading
from pathlib import Path

from pydantic import ValidationError

from .bridge import BrowserBridge, make_server
from .catalog import ProblemCatalog
from .extension_browser import ExtensionBrowser
from .model import ModelClient
from .orchestrator import Orchestrator
from .progress import ArtifactStore, ProgressStore
from .search import ReferenceSearch
from .settings import Settings, load_settings
from .terminal_ui import TerminalUI
from .types import AgentError, SiteUnavailable, SubmissionReceipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Solve LeetCode CN problems in the main Edge window")
    parser.add_argument("command", choices=("doctor", "login", "run", "status", "resolve"))
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--resolution", choices=("accepted", "retry"))
    parser.add_argument("--start-current", action="store_true",
                        help="explicitly begin at the active Edge problem instead of the saved cursor")
    return parser


def _edge_installed() -> bool:
    return any(
        (Path(os.environ.get(name, "")) / "Microsoft/Edge/Application/msedge.exe").is_file()
        for name in ("PROGRAMFILES(X86)", "PROGRAMFILES")
    )


def _recover_submission(
    progress: ProgressStore, artifacts: ArtifactStore, browser: ExtensionBrowser, note,
) -> None:
    state = progress.load()
    pending = [(int(number), record) for number, record in state["records"].items()
               if record.get("status") in {"submit_intent", "submission_unconfirmed"}]
    if not pending:
        return
    if len(pending) != 1 or pending[0][0] != state["next_id"]:
        raise SiteUnavailable("multiple or misplaced unconfirmed submissions need manual review")
    number, record = pending[0]
    submission_id = record.get("submission_id")
    digest = record.get("code_sha256")
    if not digest:
        raise SiteUnavailable(
            f"problem {number} has no captured submission ID; check its submission record, then use resolve accepted or retry"
        )
    snapshot = record.get("snapshot")
    if snapshot is not None:
        code = snapshot.get("candidate", {}).get("code")
        if not isinstance(code, str):
            raise SiteUnavailable("saved candidate snapshot has no code")
    else:
        folder = Path(record["folder"])
        code = (folder / "solution.py").read_text(encoding="utf-8")
    if hashlib.sha256(code.strip().encode("utf-8")).hexdigest() != digest:
        raise SiteUnavailable("saved candidate no longer matches the recorded submission receipt")
    if not submission_id:
        if "baseline_id" not in record or not record.get("slug"):
            raise SiteUnavailable(
                f"problem {number} has no captured submission ID or baseline; check its submission record, then use resolve accepted or retry"
            )
        note(f"Checking new submissions for problem {number} against the saved code.")
        receipt = browser.recover_submission(record["slug"], digest, record["baseline_id"])
        if receipt is None:
            raise SiteUnavailable(f"problem {number} has no new submission matching the saved code; use resolve retry only after checking LeetCode")
        submission_id = receipt.submission_id
        progress.mark_current(number, "submission_unconfirmed", **{
            key: value for key, value in record.items() if key not in {"status", "updated_at", "submission_id"}
        }, submission_id=submission_id)
    note(f"Checking saved submission {submission_id} for problem {number}.")
    result = browser.check_submission(SubmissionReceipt(str(submission_id), digest, record.get("question_id")))
    if not result.passed:
        raise SiteUnavailable(f"saved submission {submission_id} did not pass: {result.detail}")
    if snapshot is None and artifacts.enabled:
        artifacts.confirm_submission(folder, result.detail)
    progress.resolve_unconfirmed("accepted", verified=True)
    note(f"Problem {number}: saved submission {submission_id} confirmed Accepted.")


def _select_start(progress: ProgressStore, current_number: int, start_current: bool) -> int:
    state = progress.load()
    saved = state["records"].get(str(state["next_id"]), {})
    recoverable = {"candidate_ready", "run_verified", "needs_repair", "submit_intent", "submission_unconfirmed"}
    if (start_current or not progress.path.exists()
            or current_number > state["next_id"] and saved.get("status") not in recoverable):
        progress.anchor(current_number)
    return progress.load()["next_id"]


def _run(settings: Settings, root: Path, progress: ProgressStore, start_current: bool = False) -> int:
    bridge = BrowserBridge(root / ".local" / "bridge-token", settings.browser.poll_interval_seconds)
    try:
        server = make_server(settings.browser.host, settings.browser.port, bridge)
    except OSError as exc:
        if exc.errno in {errno.EADDRINUSE, 10048} or getattr(exc, "winerror", None) == 10048:
            raise SiteUnavailable(
                f"Port {settings.browser.port} is already in use; stop the other leetcode_agent run process"
            ) from exc
        raise
    ui = TerminalUI()
    bridge.on_phase = lambda kind, phase, command_id: ui.note(
        f"Browser {kind}: {phase.replace('_', ' ')}"
    ) if phase in {"triggering", "request_seen", "id_seen"} else None
    ui.start()
    ui.note(f"Edge extension pairing code: {bridge.pair_code} (valid for 10 minutes)")
    ui.note("Waiting for the active leetcode.cn problem tab. Press Ctrl+C to stop.")
    outcome = {"code": 0}

    def work() -> None:
        try:
            browser = ExtensionBrowser(
                bridge, ProblemCatalog(str(settings.leetcode.base_url)),
                settings.browser.submit_trigger, settings.browser.navigation_trigger,
                on_pause=ui.note,
            )
            number = browser.current_number()
            artifacts = ArtifactStore(root / "Output", enabled=settings.output.save_artifacts)
            _recover_submission(progress, artifacts, browser, ui.note)
            saved_number = _select_start(progress, number, start_current)
            ui.note(f"Active Edge tab: problem {number}; next saved problem: {saved_number}.")
            agent = Orchestrator(
                settings,
                progress,
                artifacts,
                browser,
                ModelClient(settings.api, settings.workflow.max_model_calls, on_retry=ui.note),
                ReferenceSearch(settings.search.max_pages),
                on_progress=ui.update,
            )
            while not bridge.stopped:
                result = agent.run_one()
                destination = f" -> {result['folder']}" if result.get("folder") else ""
                ui.note(f"Problem {result['number']}: {result['status']}{destination}")
                agent.model.calls = 0
        except (AgentError, ValueError, OSError) as exc:
            outcome["code"] = 1
            ui.note(f"Stopped: {exc}")
        except Exception as exc:
            outcome["code"] = 1
            ui.note(f"Stopped on unexpected {type(exc).__name__}")
        finally:
            bridge.stop()
            server.shutdown()

    worker = threading.Thread(target=work, name="leetcode-worker", daemon=True)
    worker.start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        ui.note("Stopping after the current action.")
        bridge.stop()
    finally:
        server.server_close()
        worker.join(timeout=3)
        ui.close()
    return outcome["code"]


def main() -> int:
    args = _parser().parse_args()
    root = args.config.resolve().parent
    try:
        if args.start_current and args.command != "run":
            raise ValueError("--start-current is only valid with run")
        settings = load_settings(args.config)
        progress = ProgressStore(root / "Output" / "progress.json", settings.leetcode.start_id)
        progress.compact()
        if args.command == "status":
            state = progress.load()
            print(f"Next problem: {state['next_id']}")
            for number, record in sorted(state["records"].items(), key=lambda item: int(item[0])):
                print(f"{number}: {record['status']} {record.get('title', '')}")
            return 0
        if args.command == "resolve":
            if not args.resolution:
                raise ValueError("resolve requires --resolution accepted or --resolution retry")
            state = progress.load()
            record = state["records"].get(str(state["next_id"]), {})
            if (args.resolution == "accepted" and record.get("status") in {"submit_intent", "submission_unconfirmed"}
                    and "snapshot" not in record and settings.output.save_artifacts):
                ArtifactStore.confirm_submission(Path(record["folder"]))
            state = progress.resolve_unconfirmed(args.resolution)
            print(f"Submission resolved as {args.resolution}; next problem: {state['next_id']}")
            return 0
        if args.command == "login":
            print("Log in to https://leetcode.cn in the main Edge window, then open a problem page.")
            return 0
        if args.command == "doctor":
            print(f"Python: {sys.version.split()[0]}")
            print(f"Edge: {'found' if _edge_installed() else 'missing'}")
            print(f"Extension: {'found' if (root / 'extension' / 'manifest.json').is_file() else 'missing'}")
            ref = ProblemCatalog(str(settings.leetcode.base_url)).find(1)
            if not ref:
                raise AgentError("LeetCode algorithm catalog is unavailable")
            print(f"Catalog: problem 1 is {ref.slug}")
            ModelClient(settings.api, settings.workflow.max_model_calls).ping()
            print(f"Model: {settings.api.model} responded via configured endpoint")
            return 0 if _edge_installed() else 1
        return _run(settings, root, progress, args.start_current)
    except (AgentError, ValidationError, ValueError, FileNotFoundError, OSError) as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
