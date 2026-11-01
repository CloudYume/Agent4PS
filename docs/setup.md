# Setup and Commands

[Project overview](../README.md) · [Configuration](configuration.md)

## Environment

Use Windows, Python 3.12+, Microsoft Edge, and network access to `leetcode.cn` and your model provider. VS Code is a convenient terminal host, not a runtime dependency. Node.js is needed only for extension development tests.

From the repository root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\Activate.ps1
```

Activation makes the installed `run` shortcut available. Without activation, use `.\.venv\Scripts\python.exe -m leetcode_agent run`. All commands read `config.yaml` from the current directory unless `--config PATH` is supplied. State and extension discovery are resolved relative to that configuration file's directory.

## Model credentials

Configure the endpoint, model IDs, and API-key environment-variable name in [`config.yaml`](../config.yaml). For the checked-in `INTERN_AI_API_KEY` setting, enter the secret without adding it to shell history:

```powershell
$secret = Read-Host 'Model API key' -AsSecureString
$env:INTERN_AI_API_KEY = [System.Net.NetworkCredential]::new('', $secret).Password
Remove-Variable secret
```

The client also checks the Windows user environment if the terminal has not inherited that variable. Keep the value out of command arguments, configuration files, progress files, and commits. Template model names and endpoints depend on your actual provider's support.

## Load and pair the extension

1. Open `edge://extensions`, enable **Developer mode**, and select **Load unpacked**. Choose the repository's [`browser-extension/`](../browser-extension/) directory.
2. Sign in to LeetCode China in the same Edge profile and open an algorithm problem page. Keep this tab active.
3. Run `python -m leetcode_agent doctor`. It checks Edge, extension files, the remote catalog, and the configured model API; it is not an offline configuration-only check.
4. Run `run`. In the extension popup, enter the six-digit code displayed by that process. The code expires after ten minutes and has a limited attempt budget.
5. Confirm that the popup's local port matches `browser.port` (default `8765`). Pairing refreshes the active problem tab; the saved token is then reused.

The bridge binds only to `127.0.0.1`. Tokens are stored in `.local/bridge-token` and extension local storage; `.local/bridge-token.origin` records the extension identity allowed to recover pairing. The manifest's public key fixes the extension ID. This is an extension identity key, not an API credential.

The extension source now lives in `browser-extension/`. If updating an unpacked installation loaded from the old `extension/` path, load the renamed directory and verify pairing before starting another run.

## Commands

| Command | Purpose |
| --- | --- |
| `python -m leetcode_agent doctor` | Check Edge, extension files, catalog access, and a model response. |
| `run` | Start the bridge and continue from saved progress. |
| `run --start-current` | Explicitly select the active problem, subject to pending-work protections. |
| `python -m leetcode_agent status` | Show the next problem and saved statuses. |
| `python -m leetcode_agent resolve --resolution accepted` | Record a manually checked successful submission. |
| `python -m leetcode_agent resolve --resolution retry` | Allow another attempt after checking the unresolved submission. |
| `python -m leetcode_agent login` | Print browser login instructions; it does not perform login. |

The installed shortcut also accepts `run --config PATH`. The module form is `python -m leetcode_agent run --config PATH`. Read [recovery](workflow.md) before using `resolve`.

Press `Ctrl+C` in the running terminal to stop. Restart the Python process after changing Python code or configuration. After changing extension files, reload the extension and refresh the problem page as well.
