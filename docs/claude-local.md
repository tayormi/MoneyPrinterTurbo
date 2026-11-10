# Running locally on a Claude subscription

This fork uses Claude for script writing and keyword extraction. It calls the
`claude_code` provider, which runs your signed-in `claude` CLI (Pro, Max or
Team plan) in headless mode. That means no Anthropic API key and no other LLM
account is needed.

## One-time prerequisites

1. **uv**: `curl -LsSf https://astral.sh/uv/install.sh | sh`
2. **Claude Code CLI**, version 2.1.260 or newer, signed in once:
   ```bash
   npm install -g @anthropic-ai/claude-code
   claude            # follow the sign-in prompts, then exit
   ```
3. **Pexels API key** (free) for stock footage: https://www.pexels.com/api/.
   You can skip this if you only use your own clips (video source "Local").

The default voice is Edge TTS, which is free and needs no key. FFmpeg comes
bundled with the Python dependencies.

## Start it

```bash
git clone https://github.com/tayormi/MoneyPrinterTurbo.git
cd MoneyPrinterTurbo
PEXELS_API_KEY=your-key ./claude-local.sh
```

The script:

1. Installs the locked dependencies (`uv sync --frozen`).
2. Creates `config.toml` if it's missing, sets `llm_provider = "claude_code"`,
   and binds the API to `127.0.0.1`.
3. Sends one short prompt through Claude to confirm the login works.
4. Starts the API (http://127.0.0.1:8080/docs) and the WebUI
   (http://127.0.0.1:8501). Press Ctrl+C to stop both.

Options:

| Want to | Run |
|---|---|
| Pick a model | `CLAUDE_MODEL=claude-sonnet-5-5 ./claude-local.sh` (leave unset for the CLI default) |
| Only set up and check, don't start | `./claude-local.sh --setup-only` |
| Re-check the Claude login | `uv run python scripts/claude_local_setup.py` |

On later runs, `./claude-local.sh` alone is enough. Settings you change in the
WebUI are kept in `config.toml`, which git ignores.

## Docker instead

The container can't use your keychain login, so it needs a long-lived token:

```bash
export CLAUDE_CODE_OAUTH_TOKEN="$(claude setup-token)"
docker compose -f docker-compose.claude.yml up -d --build
```

## Troubleshooting

- **"claude CLI not found"**: install it as shown above, or set
  `claude_code_cli_path` in `config.toml` to its full path.
- **Login or usage errors**: run `claude` once in a terminal to sign in again.
  These calls count against your subscription's usage limits.
- **`ANTHROPIC_API_KEY` set in your shell**: the provider removes it (and any
  other conflicting `ANTHROPIC_*` or `CLAUDE_CODE_USE_*` variables) before
  calling the CLI, so usage always goes through the subscription.
