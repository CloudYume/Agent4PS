from io import StringIO

from rich.console import Console

from leetcode_agent.terminal_ui import TerminalUI
from leetcode_agent.types import ProgressEvent


def test_terminal_progress_resets_for_next_problem_and_logs_stages():
    output = StringIO()
    ui = TerminalUI(Console(file=output, force_terminal=False))
    ui.start()
    ui.update(ProgressEvent(5, "solve", "Problem 5: generating"))
    ui.update(ProgressEvent(5, "submit", "Problem 5: submitting"))
    assert ui.progress.tasks[0].completed == 7
    ui.update(ProgressEvent(6, "select", "Problem 6: reading"))
    assert ui.progress.tasks[0].completed == 1
    ui.close()
    text = output.getvalue()
    assert "Problem 5: solve took" in text
    assert "Problem 5: submitting" in text
    assert "Problem 6: reading" in text
