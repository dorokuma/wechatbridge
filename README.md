# WeChatBridge

[English](README.md) | [简体中文](README.zh-CN.md)

![license](https://img.shields.io/badge/license-MIT-blue.svg)
![python](https://img.shields.io/badge/python-3.10+-blue.svg)

WeChatBridge puts your terminal coding agent in your pocket. Bring your own CLI — agy, grok, codex, or dsh — and talk to it from WeChat: text, images, files, and voice (transcribed to text). You get replies back, and generated files can come back to you over the WeChat CDN. Each user can switch backends with `/backend`, no restart needed.

```
WeChat (phone)  ⇄  iLink bot API  ⇄  WeChatBridge  ⇄  agy / grok / codex / dsh CLI
                                       (the bridge)   (runs tools)
```

WeChatBridge runs in the background and keeps asking WeChat for new messages. When you send something that needs the AI, it starts the CLI once, waits for the answer, then closes it again. Nothing stays running in between except the bridge itself: CLI children do not linger; the bridge keeps polling WeChat in the background.

Commands like `/help`, `/backend`, or `/persona` are answered by the bridge itself and never start a CLI. A generated file only comes back to WeChat if the bridge can see it under your allowed session paths.

## Quick start

Five steps, about 10 minutes from zero to your first reply in WeChat.

1. **Install one CLI backend and sign in.** I recommend [agy](https://antigravity.google/docs/cli) (Google Antigravity), since that is the backend I run daily. [grok](https://docs.x.ai/build/overview), [codex](https://developers.openai.com/codex/cli), and [dsh](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/cli/README.md) also work, but check the status notes under **CLI Backends** before you pick one. After installing, log in once in that CLI (for example `grok login`, `codex login`, or `dsh login`).

2. **Install WeChatBridge:**

   ```bash
   pipx install wechatbridge-cli
   wechatbridge --version
   ```

3. **Write a minimal config:**

   ```bash
   mkdir -p ~/.config/wechatbridge
   curl -o ~/.config/wechatbridge/.env https://raw.githubusercontent.com/dorokuma/wechatbridge/main/deploy/wechatbridge.env.example
   ```

   Then edit `~/.config/wechatbridge/.env` and, for the first run, use the open mode:

   ```bash
   WECHATBRIDGE_ALLOWED_SENDERS=*
   ```

   Why `*`: the bridge fails closed. With both `WECHATBRIDGE_ALLOWED_SENDERS` and `WECHATBRIDGE_ADMINS` empty it denies everyone, and you don't know your own `wxid` yet.

   If the backend you installed in step 1 is not `agy`, also set the global default backend in `.env`; `agy` is the default and can be omitted:

   ```bash
   WECHATBRIDGE_BACKEND=grok   # or codex / dsh
   ```

4. **Start it and scan the QR code to bind.** The ClawBot / iLink channel is Tencent's official, free personal-account bot channel: no application, review, qualification, or payment required — and you don't need to install anything extra: the bridge handles binding itself. Run `wechatbridge`; it prints a QR code in the terminal, and you scan it with WeChat on your phone. The bridge waits for a scan with a default poll timeout of 180 seconds; on timeout it automatically fetches a fresh QR code and keeps waiting — the process does not exit and does not need a restart. Only if you stop it yourself do you rerun `wechatbridge`.

5. **Get your own wxid and tighten the whitelist.** In WeChat, send the bot any message, then find that message's sender ID in the bridge log. It appears as `from=<id>`. Put that ID into `WECHATBRIDGE_ALLOWED_SENDERS` in place of `*` and restart. `*` is for first-run debugging only; tighten it for long-term use.

See **Configure** below for every variable, and **Deploy** for running it as a service.

## Features

- You can send text, images, files, and voice (transcribed by WeChat's servers) to the active backend: `agy`, `grok`, `codex`, or `dsh`. See **CLI Backends** for how mature each one is.
- Files the CLI produces inside your allowed directory can come back to WeChat, up to a size cap. Not every file it touches gets sent.
- Each WeChat user gets their own workspace. Model, effort, and mode settings are remembered per backend.
- You can switch backends at runtime with `/backend agy`, `/backend grok`, `/backend codex`, or `/backend dsh`. Switching clears that backend's continuation state (the agy/grok continuation flag and the codex `thread_id`/resume state), so the next CLI turn starts a fresh session. History files on disk are not wiped right away.
- Slash commands for model, session reset, persona, and more (see below).
- A dangerous-prompt gate checks messages against a keyword list of destructive patterns and asks for confirmation first.
- Sender whitelist via `WECHATBRIDGE_ALLOWED_SENDERS`: non-empty means listed users only, `*` means everyone, empty means admins only (nobody if there are no admins either).
- `/mcp` returns short usage text. `/agent` rewrites your request into a natural-language subagent prompt for the CLI; it is not a native MCP bridge.
- Media go over the WeChat CDN with AES-128-ECB encrypt/decrypt.
- Multi-instance: one codebase, set `WECHATBRIDGE_INSTANCE` per process, and state / session / QR paths derive from it.
- Deploy templates for systemd (Linux), launchd (macOS), and Task Scheduler (Windows).

## Platform Support

- **Linux**: primary (systemd unit included)
- **macOS**: supported (launchd plist included)
- **Windows**: supported (Task Scheduler guide included)

Default data paths expand from `~` (for example `~/.local/share/wechatbridge/<instance>/`).

## CLI Backends

agy is the backend I use every day. The other three are implemented and tested, but I have not run them in production for long. Verify a non-agy backend in your own setup before you rely on it.

- **agy** (default): Google Antigravity CLI. **Status: this is the backend the author uses daily.**
- **grok**: xAI Grok Build CLI. **Status: implemented and tested, but not battle-tested by the author — please verify it in your own setup before relying on it.**
- **codex**: OpenAI Codex CLI. **Status: implemented and tested, but not battle-tested by the author — please verify it in your own setup before relying on it.**
- **dsh**: DeepSeek Harness CLI (one-shot `headless` profile). **Status: contributed by [@KOP-XIAO](https://github.com/KOP-XIAO) in #3 and #4, and verified on real hardware by the contributor (macOS, including cross-turn memory). The repo author has not run dsh in long-term production use — verify it in your own setup before relying on it.**

You switch per user with `/backend agy`, `/backend grok`, `/backend codex`, or `/backend dsh`. Each backend keeps its own model, effort, mode, and persona files. `WECHATBRIDGE_BACKEND` sets the global default.

### dsh backend notes

- **Status: contributed by [@KOP-XIAO](https://github.com/KOP-XIAO) in #3 and #4, and verified on real hardware by the contributor (macOS, including cross-turn memory). The repo author has not run dsh in long-term production use — verify it in your own setup before relying on it.**
- **What dsh is:** the DeepSeek Harness CLI (`dsh`), running one-shot tasks through its `headless` profile.
- **Enable or switch:** set `WECHATBRIDGE_BACKEND=dsh` in `.env`, or send `/backend dsh` in WeChat.
- **PyYAML dependency:** dsh needs `PyYAML` to parse profile plugin configurations. Without it, `/backend dsh` shows a notice and keeps your current backend. Install it with `pip install PyYAML` or `pipx inject wechatbridge-cli PyYAML`.
- **Window memory (default):** `headless` starts a fresh session (`session-<uuid>`) on every call, so the bridge keeps the conversation memory instead. Successful turns are appended to `dsh_memory.jsonl`; on each call the bridge reads the most recent turns (10 pairs by default, capped at `WECHATBRIDGE_DSH_MEMORY_CHARS` chars) and injects them into the prompt as context — that cap limits the injected read window, not the file size on disk. `/clear` or `/new` wipes this memory to start fresh.
- **Persistent resume mode:** set `WECHATBRIDGE_DSH_RESUME=true` for real persistent sessions, codex-style. This needs the `dsh-bridge-runner` plugin (`dsh_bridge_runner.py`, which reads `DSH_BRIDGE_SESSION_ID` and `DSH_BRIDGE_TASK` from the environment) mounted in your headless profile bundle. Context then accumulates natively across turns, with no windowing. `/clear` or `/new` deletes the stored session ID to start a fresh session. Window memory injection is skipped while resume mode is on.
- **Workspace and state isolation:** in the default windowed mode, dsh runs as `dsh --profile headless -- <prompt>` with `cwd` set to your per-user session directory (`WECHATBRIDGE_SESSION_DIR/<user_id>`). In resume mode there is no `-- <prompt>` argv: the task is passed via the `DSH_BRIDGE_TASK` environment variable (and the profile stays configurable via `DSH_PROFILE`). Bridge-private state (`dsh_memory.jsonl` and the persistent session ID) lives under `WECHATBRIDGE_DSH_STATE_DIR` (`~/.local/share/wechatbridge/<instance>/dsh_state/`), outside the child's session tree. That blocks one-level `../` traversal into private state.
- **Who can hurt whom:** every dsh child runs as the same Unix user as the bridge, with no container in between. One-level `../` cannot reach private state, but two-level traversal (`../../dsh_state/<user_id>`) still can on the filesystem, and that is accepted on purpose. Only let people you trust use this.
- **Attachments:** images and files are merged into the prompt text as `@/absolute/path` mentions. I checked dsh v0.1.1-rc.2: `headless` does not open or inline `@`-mentioned files itself. It passes your prompt text straight to the model. The model gets system guidance about @-paths and calls its `read` tool when it needs a file. The bridge rewrites out-of-bounds mentions (`@/abs`, `@~/x`, `@file://`) to `[blocked-path]`. That filter only edits prompt text. It is not a security boundary.
- **Auth and profiles are machine-wide.** Resolution order: `WECHATBRIDGE_DSH_HOME` > `WECHATBRIDGE_HOST_HOME/.dsh` > `~/.dsh`, the same host-fallback model as grok and codex. The child's `HOME` points at the per-user session dir, so the bridge always passes `DSH_HOME` explicitly. Set `WECHATBRIDGE_DSH_HOME` for a dedicated service home with automatic session retention cleanup. Leave it unset and it falls back to `$WECHATBRIDGE_HOST_HOME/.dsh` (if set) or `~/.dsh`, with no automatic cleanup; the operator manages sessions. If the bridge runs as a dedicated system user (for example `wechatbridge`), set `WECHATBRIDGE_HOST_HOME` to the operator's home directory. That way CLI credentials and profiles are found. `DSH_BIN_PATH`, `DSH_PROFILE`, and `DSH_TIMEOUT` are all configurable.
- **Testing caveat:** like codex, the tests ran against a fake CLI; on top of that, the contributor verified the real `dsh` CLI on his own hardware (macOS, including cross-turn memory resume). The author still has not run `dsh` in long-term production use — verify it in your own setup before relying on it.
- **Plugin files:** resume mode needs the `dsh-bridge-runner` plugin mounted in your own dsh profile bundle. This repository does not ship plugin files.

### Grok backend notes

- **Status: implemented and tested, but not battle-tested by the author — please verify it in your own setup before relying on it.**
- **Isolation:** each WeChat user runs with `HOME` pointed at their own session directory. Conversation state stays there; login is machine-wide.
- **Auth:** the session links to the host `~/.grok/auth.json` (copied as a fallback), so it reuses your host `grok login`. The grok CLI rewrites `auth.json` with a temp file plus rename, which turns the session symlink into a regular file. The bridge copies such session files back to the host atomically and re-links them, so refreshed tokens never get overwritten by the revoked host copy. You can also set `XAI_API_KEY` in the bridge process environment; the grok child gets that key after env sanitizing. Being signed in to a grok-remote TUI session is not the same as the host CLI login. No key or token values are stored in this repository.

### Codex backend notes

- **Status: implemented and tested, but not battle-tested by the author — please verify it in your own setup before relying on it.**
- Each turn runs `codex exec --json`. Continuation uses `codex exec resume <thread_id> <prompt>`, with the thread id persisted per user.
- **Isolation:** each WeChat user runs with `HOME` pointed at their own per-user session directory (`session_dir`), and `CODEX_HOME` pointed at `session_dir/.codex`. Sessions, logs, and caches never cross users.
- **Auth:** the per-user session links to the host `~/.codex/auth.json` (copied as a fallback), so it reuses your host `codex login`. The codex CLI rewrites `auth.json` with a temp file plus rename, which turns the session symlink into a regular file. The bridge copies such session files back to the host atomically and re-links them, so refreshed tokens never get overwritten by the revoked host copy. You can also set `CODEX_API_KEY` in the bridge process environment to authenticate. No key or token values are stored in this repository.
- **Testing caveat:** I do not have a real Codex subscription or CLI to test against. This backend was built from source research, a JSONL fixture, and tests against a fake CLI, which pass. Final acceptance needs a real user running it against the actual Codex CLI.

## Prerequisites

- At least one CLI installed and signed in:
  - `agy` on `PATH`, or set `AGY_BIN_PATH`
  - and/or `grok` on `PATH`, or set `GROK_BIN_PATH`
  - and/or `codex` on `PATH`, or set `CODEX_BIN_PATH`
  - and/or `dsh` on `PATH`, or set `DSH_BIN_PATH` (DeepSeek Harness; run `dsh login` first)
  - Antigravity is Google's terminal coding agent (the successor to Gemini CLI). Grok Build is xAI's equivalent, Codex is OpenAI's, and dsh is DeepSeek Harness' CLI.
- A WeChat account with a [ClawBot / iLink](https://ilinkai.weixin.qq.com) bot (QR bind on first run)
- Python 3.10+

## Install

The recommended way is [pipx](https://pypa.github.io/pipx/) (Python >= 3.10 required):

```bash
pipx install wechatbridge-cli
```

After installation, verify:

```bash
wechatbridge --version
```

### Install pipx

**Debian / Ubuntu:**

```bash
sudo apt install pipx
```

**Other systems (or to get the latest version):**

```bash
python3 -m pip install --user pipx && python3 -m pipx ensurepath
```

Then start a new shell or re-source your shell config so `pipx` is on `PATH`.

### Developers

If you want to work on the source:

```bash
git clone https://github.com/dorokuma/wechatbridge.git
cd wechatbridge
pip install -e .
```

## Configure

WeChatBridge loads config from the first location it finds:

1. `$WECHATBRIDGE_ENV_FILE`, an explicit path
2. `$XDG_CONFIG_HOME/wechatbridge/<instance>.env` (defaults to `~/.config/wechatbridge/<instance>.env`)
3. `$XDG_CONFIG_HOME/wechatbridge/.env` (defaults to `~/.config/wechatbridge/.env`)
4. `.env` in the repository root: **deprecated** (prints a warning on startup)

The instance name defaults to `default`; override it with `WECHATBRIDGE_INSTANCE`.

Get the example config:

```bash
mkdir -p ~/.config/wechatbridge
curl -o ~/.config/wechatbridge/.env https://raw.githubusercontent.com/dorokuma/wechatbridge/main/deploy/wechatbridge.env.example
```

Then edit `~/.config/wechatbridge/.env` with your settings. **First install:** if you leave both `WECHATBRIDGE_ALLOWED_SENDERS` and `WECHATBRIDGE_ADMINS` empty, the bridge denies every user (fail-closed). You cannot know your own `wxid` before the first run, so start with `WECHATBRIDGE_ALLOWED_SENDERS=*`. Then send the bot a message, read your sender ID (`from=<id>`) in the bridge log, and replace `*` with that wxid. Optionally set `WECHATBRIDGE_ADMINS=<your wxid>` too, and restart.

Key variables (all have defaults):

| Variable | Default | Purpose |
|---|---|---|
| `AGY_BIN_PATH` | `agy` | path to the agy binary |
| `GROK_BIN_PATH` | `grok` | path to the grok binary |
| `CODEX_BIN_PATH` | `codex` | path to the codex binary |
| `DSH_BIN_PATH` | `dsh` | path to the dsh binary |
| `DSH_PROFILE` | `headless` | dsh profile booted for one-shot tasks |
| `DSH_TIMEOUT` | `600` | dsh CLI run timeout in seconds |
| `WECHATBRIDGE_DSH_MEMORY_TURNS` | `10` | dsh backend: recent user+assistant turns injected as context |
| `WECHATBRIDGE_DSH_MEMORY_CHARS` | `6000` | dsh backend: max chars of injected memory context |
| `WECHATBRIDGE_DSH_RESUME` | `false` | dsh backend: true persistent-session mode, one dsh session per user resumed on every message (codex-style); requires the dsh-bridge-runner plugin in the headless profile; skips windowed memory when enabled |
| `WECHATBRIDGE_DSH_HOME` | _empty_ | explicit `DSH_HOME` passed to the dsh child. Explicitly set = dedicated home + auto session cleanup; unset = fallback to `WECHATBRIDGE_HOST_HOME/.dsh` or `~/.dsh` without auto cleanup (precedence: `WECHATBRIDGE_DSH_HOME` > `WECHATBRIDGE_HOST_HOME/.dsh` > `~/.dsh`) |
| `WECHATBRIDGE_HOST_HOME` | _empty_ | host home directory override for service user deployments; used as fallback base for grok (`~/.grok`), codex (`~/.codex`), and dsh (`~/.dsh`) |
| `WECHATBRIDGE_DSH_STATE_DIR` | _derived_ | bridge-private dsh state directory (memory JSONL and persistent session IDs; defaults to `~/.local/share/wechatbridge/<instance>/dsh_state`) |
| `WECHATBRIDGE_BACKEND` | `agy` | global default backend (`agy` / `grok` / `codex` / `dsh`; overridable per user via `/backend`) |
| `WECHATBRIDGE_INSTANCE` | `default` | instance name; state / session / QR paths derive from it |
| `WECHATBRIDGE_ALLOWED_SENDERS` | _empty_ | comma-separated WeChat IDs; `*` = allow all; empty = only `WECHATBRIDGE_ADMINS` users (deny all if that is empty too) |
| `AGY_TIMEOUT` | `600` | CLI run timeout in seconds (agy / grok / codex backends) |
| `WECHATBRIDGE_MAX_OUTBOUND_BYTES` | `104857600` | max file size sent back to WeChat (100 MB) |
| `WECHATBRIDGE_MAX_INBOUND_BYTES` | `20971520` | max inbound image/file after download (20 MB) |
| `WECHATBRIDGE_MAX_CONCURRENT` | `4` | global concurrent process slots; same user serial (queue does not hold a slot); extras get a busy reply (on small RAM hosts, lower this to avoid concurrent CLI memory spikes; see [Memory limits](#memory-limits)) |
| `WECHATBRIDGE_QUEUE_NOTICE` | `true` | one-shot "queued" notice when the user's previous message is still running; set `0`/`false`/`no` to disable |
| `WECHATBRIDGE_PROGRESS_NOTICE` | `true` | one-shot "still working" notice after a message runs past `WECHATBRIDGE_PROGRESS_NOTICE_DELAY`; set `0`/`false`/`no` to disable |
| `WECHATBRIDGE_PROGRESS_NOTICE_DELAY` | `30` | seconds before the progress notice fires |
| `WECHATBRIDGE_CONFIRM_TOKEN` | `y` | reply this token to approve a gated dangerous prompt |
| `WECHATBRIDGE_ENABLE_MCP` | `true` | enable the `/mcp` help text command |
| `WECHATBRIDGE_ENABLE_SUBAGENT` | `true` | enable the `/agent` prompt-rewrite command |
| `WECHATBRIDGE_ADMINS` | _empty_ | comma-separated wxid list; admins receive WeChat notification when a new version is detected |
| `WECHATBRIDGE_UPDATE_CHECK` | `true` | check PyPI for new versions on startup and every 24h; failures are silent |
| `WECHATBRIDGE_UPDATE_CHECK_INTERVAL` | `86400` | update check interval in seconds |

Full list: [`deploy/wechatbridge.env.example`](deploy/wechatbridge.env.example).

> **Why the new config location?** With pipx the package is installed globally, so a `.env` next to the source no longer makes sense. The XDG base directory layout keeps your config separate and instance-aware.

## Run

```bash
wechatbridge
```

On first run the bridge prints a QR code (and saves a PNG under the instance data dir). Scan it with WeChat to bind, then it starts polling for messages.

## Upgrading

```bash
pipx upgrade wechatbridge-cli
sudo systemctl restart wechatbridge
```

Or use the upgrade script. No clone needed; fetch it with curl:

```bash
curl -fsSL https://raw.githubusercontent.com/dorokuma/wechatbridge/main/deploy/update.sh | sudo bash
```

The script upgrades the pipx install and restarts the service. If the service runs as a dedicated system user (for example `wechatbridge`), running it as root automatically runs pipx as that user. Override with `WECHATBRIDGE_USER=<user>`.

Your data lives under `~/.local/share/wechatbridge/<instance>/` (sessions, SQLite history, QR codes, login state), and the upgrade does not touch it. Your bots stay logged in and conversations survive.

Before a **major** or **minor** upgrade (for example 1.2 → 1.3), check [`CHANGELOG.md`](CHANGELOG.md) for breaking changes and migration steps.

> **Before upgrading to 1.8.0:** set `WECHATBRIDGE_ADMINS` or `WECHATBRIDGE_ALLOWED_SENDERS` in your `.env`. Since 1.8.0 the whitelist denies everyone when both are empty. Set `WECHATBRIDGE_ALLOWED_SENDERS=*` if you want to keep allowing all users.

## Deploy

### Linux (systemd)

First, install the bridge under the `wechatbridge` system user:

```bash
sudo -u wechatbridge pipx install wechatbridge-cli
```

Then deploy the service unit:

```bash
sudo cp deploy/wechatbridge.service /etc/systemd/system/
sudo systemctl enable --now wechatbridge
```

**Multi-instance:** copy the template `deploy/wechatbridge@.service` and enable instances:

```bash
sudo cp deploy/wechatbridge@.service /etc/systemd/system/
sudo systemctl enable --now wechatbridge@bot2
sudo systemctl enable --now wechatbridge@bot3
```

Each instance reads its own config file (`~/.config/wechatbridge/bot2.env`) and keeps state under its own data directory (`~/.local/share/wechatbridge/bot2/`).

#### Memory limits

The multi-instance template (`deploy/wechatbridge@.service`) sets cgroup memory limits:

```ini
MemoryAccounting=yes
MemoryHigh=450M
MemoryMax=512M
```

- **Why these numbers:** backend CLI children (like `agy`) can reach ~276 MB+ RSS in a single turn. Image recognition or long sessions spike even higher. On a ~1 GB RAM host, a hard cap of 300 MB causes heavy page thrashing and deep swap latency. It also breaks streaming (`subscriber fell behind updates`). `450M` (`MemoryHigh`) reclaims memory gently; `512M` (`MemoryMax`) is the hard ceiling that prevents a full-host OOM.
- **Override per instance with a drop-in**, without editing the base template:
  ```bash
  sudo mkdir -p /etc/systemd/system/wechatbridge@bot2.service.d/
  cat << 'EOF' | sudo tee /etc/systemd/system/wechatbridge@bot2.service.d/memory.conf
  [Service]
  # for hosts with >= 2 GB RAM
  MemoryHigh=600M
  MemoryMax=768M
  EOF
  sudo systemctl daemon-reload
  sudo systemctl restart wechatbridge@bot2
  ```
  Check the active limits:
  ```bash
  systemctl show wechatbridge@bot2 -p MemoryHigh,MemoryMax
  ```
- **Sizing:** the defaults are tuned for small hosts (~1 GB RAM). On bigger hosts you can relax them. Each instance runs in its own service cgroup, so budget for the worst case of N × MemoryMax across all running instances.
- **Concurrency interplay:** several heavy turns at once (for example multiple users sending images) multiply backend CLI memory use. On small hosts, lower `WECHATBRIDGE_MAX_CONCURRENT` (to `2` or `1`) before you raise the memory caps.
- **Swap:** do not tighten `MemorySwapMax`. Swap headroom is a safety buffer: it slows the process under pressure instead of letting the cgroup OOM-kill it right away.
- **Single-instance deployments:** `deploy/wechatbridge.service` ships the same built-in limits (`MemoryHigh=450M` and `MemoryMax=512M`). Adjust them with a drop-in under `/etc/systemd/system/wechatbridge.service.d/`, and use `wechatbridge` as the unit name in the `systemctl` commands above. The limits take effect when the unit file is installed or re-installed; `update.sh` upgrades do not touch unit files.
- **Platform scope:** these cgroup limits only exist on Linux systemd. The macOS (launchd) and Windows (Task Scheduler) paths have no equivalent per-instance cap.

### macOS (launchd)

```bash
cp deploy/wechatbridge.plist ~/Library/LaunchAgents/com.wechatbridge.plist
# edit WorkingDirectory and ProgramArguments in the plist
launchctl load ~/Library/LaunchAgents/com.wechatbridge.plist
```

### Windows (Task Scheduler)

See [`deploy/wechatbridge-windows.md`](deploy/wechatbridge-windows.md).

## Slash commands

| Command | Action |
|---|---|
| `/help` | list supported commands for the active backend |
| `/backend <agy\|grok\|codex\|dsh>` | switch CLI backend for this WeChat user (on a real change it clears that backend's continuation state: agy/grok flag, codex `thread_id`/resume, or dsh memory/session ID, so the next turn starts a fresh session; history files may remain until retention cleanup) |
| `/clear` or `/new` | start a new conversation (agy/grok: drops continue flag; codex: clears thread ID; dsh: clears window memory or persistent session ID) |
| `/model <name>` | set model (all backends validate against a live list: agy/grok via CLI `models`; codex via `codex debug models` then `--bundled`; unknown names or list-fetch failures are refused and nothing is written; see `/models`) |
| `/models` | list models (agy/grok/codex all query the live CLI; codex uses `debug models` and falls back to a built-in reference note only if the live list cannot be fetched) |
| `/fast` | fast-mode toggle: low reasoning effort (accepts `on`/`off`; a second bare `/fast` turns it off) |
| `/planning` | planning-mode toggle (accepts `on`/`off`; a second bare `/planning` turns it off) |
| `/add-dir <path>` | **agy/codex:** pass `--add-dir` on later runs if path is allowed. **grok:** recorded only; not passed to the CLI yet. No-arg shows the current list, `/add-dir clear` empties it (dsh unsupported) |
| `/agents` | list agents via the active CLI |
| `/persona <text>` | set persona (`show` / `clear` / `reset` subcommands) |
| `/price <link/token/keyword>` | JD/Taobao coupon & price lookup (requires `JDUNION_*` or `DATAOKE_*` keys) |
| `/version` | show current version, instance name, and backend; if a newer version is available, show upgrade hint |
| `/mcp` | short MCP **usage hint** text (can disable with `WECHATBRIDGE_ENABLE_MCP`) |
| `/agent <name> <task>` | craft a "invoke subagent …" prompt and run the CLI (can disable with `WECHATBRIDGE_ENABLE_SUBAGENT`) |

Other `/…` commands are either rejected (for example `/exit`), reported as unsupported on WeChat (TUI-only panels), or passed through to the active CLI.

`/add-dir` only accepts paths under the user's session directory or roots listed in `WECHATBRIDGE_ADD_DIR_ROOTS`.

## Ops & security (what the bridge actually enforces)

- **Whitelist first.** Empty `WECHATBRIDGE_ALLOWED_SENDERS` means only `WECHATBRIDGE_ADMINS` members can use the bridge. `*` means everyone. Both empty means nobody.
- **Auto-approve CLIs.** agy runs with `--dangerously-skip-permissions`, grok with `--always-approve` (unless planning mode is on), and dsh tools run without host path restrictions. Only give this bot to people you trust; it is not a multi-tenant sandbox.
- **The danger gate matches keywords.** It does not understand intent. The defaults look for concrete patterns: `rm -rf /`, pipe-to-shell, `mkfs`, `format c:`, and a few heavy Chinese phrases. Everyday wording like a bare "delete" is **not** gated. Override the list with `WECHATBRIDGE_CONFIRM_KEYWORDS`. You confirm with `WECHATBRIDGE_CONFIRM_TOKEN` (default `y`), within `WECHATBRIDGE_PENDING_TTL`.
- **Inbound media** are size-capped (default 20 MB), streamed, and CDN hosts are allowlisted. A missing `aes_key` returns a clear error.
- **Outbound artifacts** can only leave from the allowed per-user tree (agy: session scratch; grok and dsh: under the session dir). The bridge `realpath`-checks them first and caps them at `WECHATBRIDGE_MAX_OUTBOUND_BYTES`.
- **Concurrency:** a global process-slot cap (`WECHATBRIDGE_MAX_CONCURRENT`, default 4). One user's messages run serially and do **not** hold a global slot while waiting on a previous message. Different users run in parallel up to the cap. Heavy concurrent tasks multiply subprocess memory; see [Memory limits](#memory-limits).
- **Long replies** get split into chunks (`WECHATBRIDGE_MESSAGE_CHUNK`, default 2000 characters).
- **Data layout:** instance data lives under `~/.local/share/wechatbridge/<instance>/` (override with env). Runtime dirs prefer `0700`; token and QR files prefer `0600` (on Unix; Windows relies on NTFS ACLs).
- **Retention:** session temps and dialogue history use separate TTLs (`WECHATBRIDGE_SESSION_RETENTION_DAYS`, `WECHATBRIDGE_HISTORY_RETENTION_DAYS`). Prefs and auth are kept.
- **Child env** is sanitized (common secret-style variable names are stripped), and `HOME` (plus `USERPROFILE` on Windows) points at the per-user session dir.

## Limitations

- Not a standalone agent. You need agy and/or grok and/or codex and/or dsh.
- **codex** is implemented and tested, but not battle-tested by me. I have no real Codex subscription or CLI to check against. I built it from source research, a JSONL fixture, and tests against a fake CLI, which pass. Confirm it in your own setup before you rely on it.
- **dsh** has both windowed memory (default, last `WECHATBRIDGE_DSH_MEMORY_TURNS` turns) and true persistent sessions (`WECHATBRIDGE_DSH_RESUME=true`). Tests cover the logic, but I have not run it long-term either. Resume mode also needs you to mount the `dsh-bridge-runner` plugin in your own dsh profile; this repo does not ship plugin files.
- On dsh, the model/effort/mode/persona slash commands are not wired yet. `/model`, `/fast`, `/planning`, `/persona`, and `/add-dir` return a "not supported" notice.
- Voice is WeChat speech-to-text only; no local ASR. An empty transcript gets a "type instead" reply.
- No video send or receive, and no native WeChat voice-bubble replies (no silk encode).
- One WeChat binding per process. Multiple accounts need multiple instances (`WECHATBRIDGE_INSTANCE`).
- Artifact send-back is best-effort: files detected under allowed paths get sent, not every file the CLI created anywhere.
- `/mcp` and `/agent` do not run MCP protocol or process supervisors inside the bridge. They only guide or rephrase for the CLI.
- Keep it behind a whitelist for trusted users.

## Contributors

- [@KOP-XIAO](https://github.com/KOP-XIAO) — the dsh backend ([#3](https://github.com/dorokuma/wechatbridge/pull/3)) and its long-term memory plus resume support ([#4](https://github.com/dorokuma/wechatbridge/pull/4)), verified on real hardware.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Versions follow SemVer starting at 1.0.0; changes go into [`CHANGELOG.md`](CHANGELOG.md).

## License

MIT. See [`LICENSE`](LICENSE).
