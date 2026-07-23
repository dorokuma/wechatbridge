# WeChatBridge

[English](README.md) | [简体中文](README.zh-CN.md)

![license](https://img.shields.io/badge/license-MIT-blue.svg)
![python](https://img.shields.io/badge/python-3.10+-blue.svg)

WeChatBridge connects a WeChat bot to [agy](#prerequisites) — Google's Antigravity CLI — so you can read files, run commands, fetch the web, and receive generated files back, all from a WeChat conversation.

```
WeChat (phone)  ⇄  iLink bot API  ⇄  WeChatBridge  ⇄  agy CLI
                                     (this project)    (runs tools)
```

The bridge long-polls the iLink bot API for incoming messages, spawns an `agy` subprocess per user, and returns the reply. Files agy generates are uploaded back to WeChat over the CDN.

## Features

- **Text, image, file, and voice** messages from WeChat forwarded to agy
- **Generated files returned** — documents, images, and code agy produces are sent back to WeChat
- **Per-user sessions** — each WeChat user gets an isolated agy workspace
- **Slash commands** for runtime control (`/model`, `/clear`, `/fast`, `/persona`, ...)
- **Dangerous prompt gate** — delete / format / `rm -rf` prompts ask for confirmation before running
- **Sender whitelist** — restrict access to specific WeChat IDs
- **MCP and subagent guidance** via `/mcp` and `/agent`
- **AES-128-ECB encrypted media** transfer over the WeChat CDN
- **systemd unit** with auto-restart included

## Prerequisites

- **agy** (Google's Antigravity CLI) installed and authenticated (`agy` in `PATH`, or set `AGY_BIN_PATH`). Antigravity CLI is Google's terminal-first agentic coding tool — it understands your codebase, makes edits with your permission, and runs commands from the terminal. It is the official successor to Gemini CLI.
- A WeChat account with a [ClawBot / iLink](https://ilinkai.weixin.qq.com) bot to bind via QR code.
- Python 3.10+.

## Install

```bash
git clone https://github.com/dorokuma/wechatbridge.git
cd wechatbridge
pip install -r requirements.txt
```

Or install as a package:

```bash
pip install -e .
```

## Configure

Copy the example environment file and adjust:

```bash
cp deploy/wechatbridge.env.example .env
```

Key variables (all have defaults):

| Variable | Default | Purpose |
|---|---|---|
| `AGY_BIN_PATH` | `agy` | path to the agy binary |
| `WECHATBRIDGE_ALLOWED_SENDERS` | _empty_ | comma-separated WeChat IDs allowed to use the bridge (empty = allow all) |
| `AGY_TIMEOUT` | `180` | agy execution timeout in seconds |
| `WECHATBRIDGE_MAX_OUTBOUND_BYTES` | `104857600` | max file size sent back to WeChat (100 MB) |

See [`deploy/wechatbridge.env.example`](deploy/wechatbridge.env.example) for the full list.

## Run

```bash
python -m wechatbridge
```

On first run the bridge prints a QR code. Scan it with WeChat to bind the bot, after which it long-polls for messages.

## Deploy with systemd

```bash
sudo cp deploy/wechatbridge.service /etc/systemd/system/
# edit WorkingDirectory and add an EnvironmentFile= line to the unit
sudo systemctl enable --now wechatbridge
```

## Slash commands

| Command | Action |
|---|---|
| `/help` | list supported commands |
| `/clear` or `/new` | reset the session |
| `/model <name>` | switch agy model (see `/models`) |
| `/models` | list available models |
| `/fast` | toggle fast mode (low reasoning effort) |
| `/planning` | toggle planning mode |
| `/add-dir <path>` | add a working directory |
| `/agents` | list available agents |
| `/persona <text>` | set your persona document (supports `show` / `clear` / `reset`) |
| `/mcp` | MCP tool usage guidance |
| `/agent <name> <task>` | invoke a subagent for a task |

Other `/` commands are passed through to agy unchanged.

## Limitations

- Requires agy — this is not a standalone agent.
- Voice accuracy is capped at WeChat's speech-to-text; there is no on-device ASR.
- No video send or receive. agy has no native video understanding — parsing video content needs third-party tooling, which is out of scope.
- No native voice bubble output (silk encoding is not implemented).
- One bot per process — run two instances for two WeChat accounts.
- agy runs with `--dangerously-skip-permissions` (auto-approves every tool call). Restrict access with the sender whitelist and only deploy for trusted users.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). The project follows Semantic Versioning from 1.0.0; record every change in [`CHANGELOG.md`](CHANGELOG.md).

## License

MIT — see [`LICENSE`](LICENSE).