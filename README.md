# WeChatBridge

[English](README.md) | [简体中文](README.zh-CN.md)

![license](https://img.shields.io/badge/license-MIT-blue.svg)
![python](https://img.shields.io/badge/python-3.10+-blue.svg)

WeChatBridge connects a WeChat bot to agy (Google's Antigravity CLI). You can read files, run commands, fetch web pages, and get generated files back, all from a WeChat chat.

```
WeChat (phone)  ⇄  iLink bot API  ⇄  WeChatBridge  ⇄  agy CLI
                                     (this project)    (runs tools)
```

The bridge long-polls iLink for incoming messages, runs an `agy` subprocess per user, and sends the reply back. Files agy creates go back to WeChat through the CDN.

## Features

- Text, image, file, and voice messages from WeChat all go to agy
- Files agy generates (documents, images, code) get sent back to WeChat
- Each WeChat user gets an isolated agy workspace
- Slash commands for runtime control: `/model`, `/clear`, `/fast`, `/persona`, and more
- Dangerous prompts (delete, format, `rm -rf`) ask for confirmation before running
- Sender whitelist to restrict access to specific WeChat IDs
- `/mcp` and `/agent` guide agy's MCP tools and subagents
- Media is encrypted with AES-128-ECB over the WeChat CDN
- systemd unit with auto-restart included

## Prerequisites

- **agy** (Google's Antigravity CLI), installed and authenticated. Either have `agy` in `PATH` or set `AGY_BIN_PATH`. Antigravity CLI is Google's terminal-first agentic coding tool: it understands your codebase, edits files with your permission, and runs commands from the terminal. It's the official successor to Gemini CLI.
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

Other `/` commands pass through to agy unchanged.

## Limitations

- Requires agy. This is not a standalone agent.
- Voice accuracy is capped at WeChat's speech-to-text. There's no on-device ASR.
- No video send or receive. agy has no native video understanding, and parsing video content needs third-party tooling that's out of scope.
- No native voice bubble output (silk encoding isn't implemented).
- One bot per process. Run two instances for two WeChat accounts.
- agy runs with `--dangerously-skip-permissions` (auto-approves every tool call). Restrict access with the sender whitelist and only deploy for trusted users.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). The project follows Semantic Versioning from 1.0.0; record every change in [`CHANGELOG.md`](CHANGELOG.md).

## License

MIT. See [`LICENSE`](LICENSE).