"""Prepare config.toml for running locally on a Claude subscription.

Run through ``./claude-local.sh`` (or ``uv run python scripts/claude_local_setup.py``).

What it does, all idempotent:
  1. Creates config.toml from config.example.toml if it is missing.
  2. Sets ``llm_provider = "claude_code"`` and binds the API to 127.0.0.1.
  3. Applies optional overrides: --model, --pexels-key (or PEXELS_API_KEY).
  4. Checks that the ``claude`` CLI is installed and new enough.
  5. Unless --skip-login-check is given, sends one tiny prompt through the
     same code path video generation uses, so an expired login fails here
     instead of halfway through a video.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _version_tuple(text: str) -> tuple[int, ...]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(part) for part in match.groups()) if match else ()


def check_cli(cli_path: str, min_version: str) -> str:
    resolved = shutil.which(cli_path) or (cli_path if os.path.isfile(cli_path) else "")
    if not resolved:
        raise SystemExit(
            "The `claude` CLI was not found.\n"
            "  Install it:  npm install -g @anthropic-ai/claude-code\n"
            "  Then sign in once by running `claude` and following the prompts."
        )
    try:
        output = subprocess.run(
            [resolved, "--version"], capture_output=True, text=True, timeout=30
        ).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"Could not run `{resolved} --version`: {exc}")
    found = _version_tuple(output)
    if found and found < _version_tuple(min_version):
        raise SystemExit(
            f"claude CLI {'.'.join(map(str, found))} is too old; "
            f"{min_version} or newer is required.\n"
            "  Upgrade it:  npm install -g @anthropic-ai/claude-code@latest"
        )
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", help="Claude model, e.g. claude-sonnet-5-5 (blank = CLI default)")
    parser.add_argument("--pexels-key", default=os.environ.get("PEXELS_API_KEY", ""))
    parser.add_argument("--skip-login-check", action="store_true")
    args = parser.parse_args(argv)

    # Importing the config module creates config.toml from the example file
    # when it does not exist yet, using the app's own safe initialisation.
    from app.config import config
    from app.services import llm

    app_cfg = config.app
    app_cfg["llm_provider"] = "claude_code"
    app_cfg.pop("listen_host", None)
    # listen_host is a top-level key, not part of [app].
    config._cfg["listen_host"] = "127.0.0.1"
    if args.model is not None:
        app_cfg["claude_code_model_name"] = args.model.strip()

    pexels = [key for key in app_cfg.get("pexels_api_keys") or [] if str(key).strip()]
    if args.pexels_key.strip():
        pexels = [args.pexels_key.strip()]
        app_cfg["pexels_api_keys"] = pexels
    config.save_config()
    print(f"config.toml ready: {config.config_file}")
    print("  llm_provider = claude_code, API bound to 127.0.0.1")

    cli = check_cli(
        (app_cfg.get("claude_code_cli_path") or "").strip() or "claude",
        llm.CLAUDE_CODE_MIN_CLI_VERSION,
    )
    print(f"  claude CLI: {cli}")

    if not args.skip_login_check:
        print("Checking your Claude login with one short prompt...")
        ok, error, elapsed = llm.test_connection()
        if not ok:
            raise SystemExit(
                f"Claude check failed: {error}\n"
                "  Sign in by running `claude` once (or `claude setup-token` for Docker)."
            )
        print(f"  Claude answered in {elapsed:.1f}s")

    if not pexels:
        print(
            "\nNote: no Pexels key yet. Stock footage needs one (free):\n"
            "  https://www.pexels.com/api/  then rerun with PEXELS_API_KEY=... or\n"
            "  paste it in the WebUI under Basic Settings."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
