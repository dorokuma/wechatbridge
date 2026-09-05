# WeChatBridge

[English](README.md) | [简体中文](README.zh-CN.md)

![license](https://img.shields.io/badge/license-MIT-blue.svg)
![python](https://img.shields.io/badge/python-3.10+-blue.svg)

WeChatBridge connects a WeChat bot to agentic coding CLIs (Google's agy / Antigravity, xAI's Grok Build, OpenAI's Codex, or DeepSeek Harness' dsh). From WeChat you can send text, images, files, and voice-as-text to the active CLI, get replies back, and receive certain generated files over the WeChat CDN. Switch backends per user with `/backend` — no restart.

```
WeChat (phone)  ⇄  iLink bot API  ⇄  WeChatBridge  ⇄  agy / grok / codex / dsh CLI
                                     (this project)    (runs tools)
```

The bridge process stays up and long-polls iLink. For prompts that go to a CLI, it spawns one `agy` / `grok` / `codex` / `dsh` child (single-turn) and exits that child when done — the child does not stay resident. Many slash commands (`/help`, `/backend`, `/persona`, …) are handled inside the bridge and never start a CLI. Only artifacts the bridge can detect under the user's allowed session paths are pushed back via CDN.

## Features

- Text, image, file, and voice (WeChat server-side transcription only) go to the **active** backend (`agy`, `grok`, `codex`, or `dsh`)
- Detected CLI artifacts under the per-user allowed tree can be sent back (size-capped); not every file the CLI touches
- Each WeChat user gets an isolated workspace; model / effort / mode are remembered **per backend**
- Runtime backend switch: `/backend agy`, `/backend grok`, `/backend codex`, or `/backend dsh` (clears that backend's continuation state — the agy/grok continuation flag and the codex `thread_id`/resume state — so the next CLI turn starts a fresh session; history files on disk are not wiped immediately)
- Slash commands for model, session reset, persona, and more (see below)
- Dangerous-prompt gate: a **keyword list** of concrete destructive patterns asks for confirmation before run
- Sender whitelist (`WECHATBRIDGE_ALLOWED_SENDERS`; empty = allow all)
- `/mcp` returns short usage text; `/agent` rewrites into a natural-language subagent prompt for the CLI (not a native MCP bridge)
- Media over WeChat CDN with AES-128-ECB encrypt/decrypt
- Multi-instance: one codebase, set `WECHATBRIDGE_INSTANCE` per process (state / session / QR paths derive from it)
- Deploy templates: systemd (Linux), launchd (macOS), Task Scheduler notes (Windows)

## Platform Support

- **Linux** — primary (systemd unit included)
- **macOS** — supported (launchd plist included)
- **Windows** — supported (Task Scheduler guide included)

Default data paths expand from `~` (e.g. `~/.local/share/wechatbridge/<instance>/`).

## CLI Backends

- **agy** (default) — Google Antigravity CLI
- **grok** — xAI Grok Build CLI
- **codex** — OpenAI Codex CLI
- **dsh** — DeepSeek Harness CLI (one-shot `headless` profile)

Per-user switch: `/backend agy`, `/backend grok`, `/backend codex`, or `/backend dsh`. Each backend keeps its own model / effort / mode memory and persona file layout. Global default is `WECHATBRIDGE_BACKEND`.

### dsh backend notes

- **What is dsh:** DeepSeek Harness CLI (`dsh`), running one-shot tasks via the `headless` profile.
- **Enable / Switch:** Set `WECHATBRIDGE_BACKEND=dsh` globally in `.env`, or switch per-user in WeChat via `/backend dsh`.
- **Dependency (PyYAML):** The dsh backend requires `PyYAML` to parse profile plugin configurations. If missing, `/backend dsh` returns a user-visible notice and preserves existing backend preferences. Install with `pip install PyYAML` or `pipx inject wechatbridge-cli PyYAML`.
- **Window memory (default):** The `headless` profile creates a fresh session per invocation (`session-<uuid>`), so the bridge manages long-term conversation memory: recent turns (default 10 pairs, up to `WECHATBRIDGE_DSH_MEMORY_CHARS` chars) are saved to `dsh_memory.jsonl` and injected into every prompt. `/clear` or `/new` wipes this memory to start fresh.
- **Persistent resume mode:** Set `WECHATBRIDGE_DSH_RESUME=true` for true persistent sessions (codex-style resume). Requires the headless profile bundle to mount the `dsh-bridge-runner` plugin (`dsh_bridge_runner.py`, which reads `DSH_BRIDGE_SESSION_ID` and `DSH_BRIDGE_TASK` from env). Context accumulates natively across turns without windowing. `/clear` or `/new` deletes the stored session ID to start a fresh session. Window memory injection is skipped in resume mode.
- **Workspace & State Isolation:** Runs `dsh --profile headless -- <prompt>` with `cwd` = per-user session directory (`WECHATBRIDGE_SESSION_DIR/<user_id>`). Bridge-private state files (`dsh_memory.jsonl` and persistent session ID) are stored under `WECHATBRIDGE_DSH_STATE_DIR` (`~/.local/share/wechatbridge/<instance>/dsh_state/`), outside the child's `session_dir` tree to eliminate direct 1-level parent/sibling relative traversal (`../`).
- **Threat Model:** Child processes run under the same host UID without container sandbox isolation. While 1-level relative traversal (`../`) cannot reach private state, 2-level traversal (`../../dsh_state/<user_id>`) remains reachable on the filesystem and is accepted. Treat as trusted-user tooling.
- Image and file attachments are merged into the prompt text as `@/absolute/path` mentions. Verified against dsh v0.1.1-rc.2 source: `headless` profile does not pre-read or inline @mention files at the CLI/runtime level, but passes prompt text directly to the model; the model receives system guidance on @-paths and invokes file tools (`read`) as needed. The bridge filters out-of-bounds mentions (`@/abs`, `@~/x`, `@file://`, replaced with `[blocked-path]`), which is a prompt text layer best-effort filter, not a sandbox boundary.
- Auth / profiles are **machine-wide** (resolved via precedence `WECHATBRIDGE_DSH_HOME` > `WECHATBRIDGE_HOST_HOME/.dsh` > `~/.dsh`, same host-fallback model as grok/codex): the child's `HOME` points at the per-user session dir, so the bridge always passes `DSH_HOME` explicitly. Set `WECHATBRIDGE_DSH_HOME` to configure a dedicated service home with automatic session retention cleanup; when unset, it falls back to `$WECHATBRIDGE_HOST_HOME/.dsh` (if set) or `~/.dsh` without automatic session cleanup (managed by the operator). If the bridge runs as a dedicated system user (e.g. `wechatbridge`), set `WECHATBRIDGE_HOST_HOME` to point to the operator's home directory so CLI credentials and profiles are discovered. `DSH_BIN_PATH`, `DSH_PROFILE`, and `DSH_TIMEOUT` are configurable.
- **Status:** Implemented against the published dsh CLI contract and covered by the test suite (fake CLI). The persistent resume mode requires users to configure and mount the `dsh-bridge-runner` plugin in their dsh profile bundle; plugin files are not bundled with this repository.

### Grok backend notes

- Isolation: each WeChat user runs with `HOME` pointed at their own session directory. Conversation state stays there; login is machine-wide.
- Auth: the session links to the host `~/.grok/auth.json` (copied as a fallback), reusing the host `grok login`. The grok CLI rewrites `auth.json` via temp-file + rename, which replaces the session symlink with a regular file; the bridge promotes such session files back to the host (atomic copy) and re-links, so refreshed tokens never get overwritten by the revoked host copy. Alternatively, set `XAI_API_KEY` in the bridge process environment (the grok child is given that key after env sanitizing). A grok-remote TUI session being signed in is not the same as the host CLI login. No key or token values are stored in this repository.

### Codex backend notes

- Runs `codex exec --json` for each single turn; conversation continuation uses `codex exec resume <thread_id> <prompt>` with the thread id persisted per user.
- Isolation: each WeChat user runs with `HOME` and `CODEX_HOME` pointed at their own per-user session directory (`session_dir/.codex`), so sessions, logs, and caches never cross users.
- Auth: the per-user session links to the host `~/.codex/auth.json` (copied as a fallback), reusing the host `codex login`. The codex CLI rewrites `auth.json` via temp-file + rename, which replaces the session symlink with a regular file; the bridge promotes such session files back to the host (atomic copy) and re-links, so refreshed tokens never get overwritten by the revoked host copy. Alternatively, set `CODEX_API_KEY` in the bridge process environment to authenticate. No key or token values are stored in this repository.
- **Status:** there is currently no real Codex subscription or CLI available for live testing. The codex backend is implemented from source research, a JSONL fixture, and a fake CLI used by the test suite (which passes). Final acceptance depends on a real user running it against the actual Codex CLI.

## Prerequisites

- At least one CLI installed and signed in:
  - **agy** on `PATH`, or set `AGY_BIN_PATH`
  - **and/or grok** on `PATH`, or set `GROK_BIN_PATH`
  - **and/or codex** on `PATH`, or set `CODEX_BIN_PATH`
  - **and/or dsh** on `PATH`, or set `DSH_BIN_PATH` (DeepSeek Harness, `dsh login` required)
  - Antigravity is Google's terminal agentic coding CLI (successor to Gemini CLI). Grok Build is xAI's counterpart; Codex is OpenAI's terminal agentic coding CLI; dsh is DeepSeek Harness' CLI.
- A WeChat account with a [ClawBot / iLink](https://ilinkai.weixin.qq.com) bot (QR bind on first run)
- Python 3.10+

## Install

The recommended way is with [pipx](https://pypa.github.io/pipx/) (Python >= 3.10 required):

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

If you want to hack on the source:

```bash
git clone https://github.com/dorokuma/wechatbridge.git
cd wechatbridge
pip install -e .
```

## Configure

Configuration is loaded from the first location found:

1. `$WECHATBRIDGE_ENV_FILE` — explicit path
2. `$XDG_CONFIG_HOME/wechatbridge/<instance>.env` (defaults to `~/.config/wechatbridge/<instance>.env`)
3. `$XDG_CONFIG_HOME/wechatbridge/.env` (defaults to `~/.config/wechatbridge/.env`)
4. `.env` in the repository root — **deprecated** (prints a warning on startup)

The instance name defaults to `default`; override with `WECHATBRIDGE_INSTANCE`.

Get the example config:

```bash
mkdir -p ~/.config/wechatbridge
curl -o ~/.config/wechatbridge/.env https://raw.githubusercontent.com/dorokuma/wechatbridge/main/deploy/wechatbridge.env.example
```

Then edit `~/.config/wechatbridge/.env` with your settings.

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
| `WECHATBRIDGE_DSH_RESUME` | `false` | dsh backend: true persistent-session mode — one dsh session per user resumed on every message (codex-style); requires the dsh-bridge-runner plugin in the headless profile; skips windowed memory when enabled |
| `WECHATBRIDGE_DSH_HOME` | _empty_ | explicit `DSH_HOME` passed to the dsh child. Explicitly set = dedicated home + auto session cleanup; unset = fallback to `WECHATBRIDGE_HOST_HOME/.dsh` or `~/.dsh` without auto cleanup (precedence: `WECHATBRIDGE_DSH_HOME` > `WECHATBRIDGE_HOST_HOME/.dsh` > `~/.dsh`) |
| `WECHATBRIDGE_HOST_HOME` | _empty_ | host home directory override for service user deployments; used as fallback base for grok (`~/.grok`), codex (`~/.codex`), and dsh (`~/.dsh`) |
| `WECHATBRIDGE_DSH_STATE_DIR` | _derived_ | bridge-private dsh state directory (memory JSONL and persistent session IDs; defaults to `~/.local/share/wechatbridge/<instance>/dsh_state`) |
| `WECHATBRIDGE_BACKEND` | `agy` | global default backend (`agy` / `grok` / `codex` / `dsh`; overridable per user via `/backend`) |
| `WECHATBRIDGE_INSTANCE` | `default` | instance name; state / session / QR paths derive from it |
| `WECHATBRIDGE_ALLOWED_SENDERS` | _empty_ | comma-separated WeChat IDs (empty = allow all) |
| `AGY_TIMEOUT` | `600` | CLI run timeout in seconds (agy / grok / codex backends) |
| `WECHATBRIDGE_MAX_OUTBOUND_BYTES` | `104857600` | max file size sent back to WeChat (100 MB) |
| `WECHATBRIDGE_MAX_INBOUND_BYTES` | `20971520` | max inbound image/file after download (20 MB) |
| `WECHATBRIDGE_MAX_CONCURRENT` | `4` | global concurrent process slots; same user serial (queue does not hold a slot); extras get a busy reply (on small RAM hosts, lower this to avoid concurrent CLI memory spikes; see [Memory limits](#memory-limits)) |
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

On first run the bridge prints a QR code (and saves PNG under the instance data dir). Scan with WeChat to bind, then it long-polls for messages.

## Upgrading

```bash
pipx upgrade wechatbridge-cli
sudo systemctl restart wechatbridge
```

Or run the upgrade script (no clone needed — fetch it with curl):

```bash
curl -fsSL https://raw.githubusercontent.com/dorokuma/wechatbridge/main/deploy/update.sh | sudo bash
```

The script upgrades the pipx installation and restarts the service. If the service runs as a dedicated system user (e.g. `wechatbridge`), running as root automatically runs pipx as that user (override with `WECHATBRIDGE_USER=<user>`).

Data lives under `~/.local/share/wechatbridge/<instance>/` (sessions, SQLite history, QR codes, login state) and is **not** touched during upgrade — your bots stay logged in and conversations are preserved.

Before upgrading a **major** or **minor** version (e.g. 1.2 → 1.3), check the corresponding section in [`CHANGELOG.md`](CHANGELOG.md) for breaking changes and migration steps.

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

The multi-instance template (`deploy/wechatbridge@.service`) configures cgroup memory protection:

```ini
MemoryAccounting=yes
MemoryHigh=450M
MemoryMax=512M
```

- **Why these defaults?** Backend CLI child processes (such as `agy`) can reach ~276 MB+ RSS during a single turn, and spike even higher during image recognition or long conversational sessions. A tight hard cap (e.g. 300 MB) on ~1 GB RAM hosts triggers aggressive page thrashing, deep swap latency, and stream interruptions (`subscriber fell behind updates`). The `450M` soft limit (`MemoryHigh`) triggers gentle reclaim, while `512M` (`MemoryMax`) provides a hard ceiling preventing full-host OOM.
- **Overriding per-instance via drop-in:** Override memory limits without modifying the base unit template:
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
  Verify active limits:
  ```bash
  systemctl show wechatbridge@bot2 -p MemoryHigh,MemoryMax
  ```
- **Sizing guidance:** The defaults are tuned for small hosts (~1 GB RAM). On larger hosts with ample headroom, limits can be relaxed. When deploying multiple instances, note that each instance runs in its own service cgroup — budget for the worst-case combined ceiling of N × MemoryMax across running instances.
- **Interaction with concurrency:** Concurrent heavy turns (e.g. multiple users sending images at once) multiply backend CLI memory consumption. On memory-constrained hosts, prioritize lowering `WECHATBRIDGE_MAX_CONCURRENT` (e.g. to `2` or `1`) before increasing memory caps.
- **Swap configuration:** Avoid tightening `MemorySwapMax` — swap margin serves as a vital safety buffer that slows execution under pressure instead of triggering an immediate cgroup OOM kill.
- **Single-instance deployments:** The single-instance unit `deploy/wechatbridge.service` includes the same built-in memory limits (`MemoryHigh=450M` and `MemoryMax=512M`). To adjust them, override via a drop-in under `/etc/systemd/system/wechatbridge.service.d/` and replace the unit name in the `systemctl` commands above with `wechatbridge`. Takes effect when the unit file is installed or re-installed; `update.sh` upgrades do not touch unit files.
- **Platform scope:** These cgroup memory limits apply only to Linux systemd deployments; the macOS (launchd) and Windows (Task Scheduler) paths have no equivalent per-instance cap.

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
| `/backend <agy\|grok\|codex\|dsh>` | switch CLI backend for this WeChat user (on real change: clears that backend's continuation state — agy/grok flag, codex `thread_id`/resume, or dsh memory/session ID — so the next turn starts a fresh session; history files may remain until retention cleanup) |
| `/clear` or `/new` | start a new conversation (agy/grok: drops continue flag; codex: clears thread ID; dsh: clears window memory or persistent session ID) |
| `/model <name>` | set model (all backends validate against a live list: agy/grok via CLI `models`; codex via `codex debug models` [then `--bundled`]; unknown name or list-fetch failure refuse and do not write prefs; see `/models`) |
| `/models` | list models — agy/grok/codex all query the live CLI (codex: `debug models`; falls back to a built-in reference note only if the live list cannot be fetched) |
| `/fast` | set low reasoning effort (**on only** — not a toggle; no “off” command) |
| `/planning` | set planning mode (**on only** — not a toggle) |
| `/add-dir <path>` | **agy:** pass `--add-dir` on later runs if path is allowed. **grok:** recorded only; not passed to the CLI yet |
| `/agents` | list agents via the active CLI |
| `/persona <text>` | set persona (`show` / `clear` / `reset` subcommands) |
| `/version` | show current version, instance name, and backend; if a newer version is available, show upgrade hint |
| `/mcp` | short MCP **usage hint** text (can disable with `WECHATBRIDGE_ENABLE_MCP`) |
| `/agent <name> <task>` | craft a "invoke subagent …" prompt and run the CLI (can disable with `WECHATBRIDGE_ENABLE_SUBAGENT`) |

Other `/…` commands are either rejected (e.g. `/exit`), reported as unsupported on WeChat (TUI-only panels), or passed through to the active CLI.

`/add-dir` only accepts paths under the user's session directory or roots listed in `WECHATBRIDGE_ADD_DIR_ROOTS`.

## Ops & security (what the bridge actually enforces)

- **Whitelist first.** Empty `WECHATBRIDGE_ALLOWED_SENDERS` means anyone who can message the bot can use it.
- **Auto-approve CLIs.** agy runs with `--dangerously-skip-permissions`; grok with `--always-approve` (unless planning mode); dsh tools run without host path restrictions. Treat this as trusted-user tooling, not a multi-tenant sandbox.
- **Danger gate is keyword-based**, not full intent understanding. Defaults target concrete patterns (`rm -rf /`, pipe-to-shell, `mkfs`, `format c:`, a few heavy Chinese phrases, …). Everyday wording like bare “delete” is **not** gated. Override list via `WECHATBRIDGE_CONFIRM_KEYWORDS`; approve with `WECHATBRIDGE_CONFIRM_TOKEN` (default `y`), TTL `WECHATBRIDGE_PENDING_TTL`.
- **Inbound media** is size-capped (default 20 MB), streamed, and CDN hosts are allowlisted. Missing `aes_key` returns a clear error.
- **Outbound artifacts** only leave the allowed per-user tree (agy: session scratch; grok: under session dir; dsh: under session dir), after `realpath` checks, and only if under `WECHATBRIDGE_MAX_OUTBOUND_BYTES`.
- **Concurrency:** global process-slot cap (`WECHATBRIDGE_MAX_CONCURRENT`, default 4). Same user is serialized and does **not** hold a global slot while waiting on their previous message; different users can run in parallel up to the cap (heavy concurrent tasks multiply subprocess memory; see [Memory limits](#memory-limits)).
- **Long replies** are split into chunks (`WECHATBRIDGE_MESSAGE_CHUNK`, default 2000 characters).
- **Data layout:** instance data under `~/.local/share/wechatbridge/<instance>/` (override with env). Runtime dirs prefer `0700`; token/QR files prefer `0600` (Unix; Windows relies on NTFS ACLs).
- **Retention:** session temps vs dialogue history use separate TTLs (`WECHATBRIDGE_SESSION_RETENTION_DAYS`, `WECHATBRIDGE_HISTORY_RETENTION_DAYS`). Prefs/auth are kept.
- **Child env** is sanitized (strips common secret-style variable names) and points `HOME` (and `USERPROFILE` on Windows) at the per-user session dir.

## Limitations

- Not a standalone agent — requires agy and/or grok and/or codex and/or dsh.
- The **codex** backend is not yet verified against a real Codex subscription/CLI; it is validated by source research, a JSONL fixture, and a fake CLI in tests. Treat it as community-tested until a real user confirms.
- The **dsh** backend supports both windowed memory (default, last `WECHATBRIDGE_DSH_MEMORY_TURNS` turns) and true persistent sessions (`WECHATBRIDGE_DSH_RESUME=true`). Covered by the test suite; persistent resume mode requires users to configure and mount their own `dsh-bridge-runner` plugin (this repository does not bundle plugin files).
- dsh model/effort/mode/persona slash commands are not wired yet; `/model`, `/fast`, `/planning`, `/persona`, `/add-dir` return a "not supported" notice on the dsh backend.
- Voice is WeChat speech-to-text only; no local ASR; empty transcript → “type instead”.
- No video send/receive; no native WeChat voice-bubble replies (no silk encode).
- One WeChat binding per process; multiple accounts need multiple instances (`WECHATBRIDGE_INSTANCE`).
- Artifact send-back is best-effort detection under allowed paths, not “every file the CLI created anywhere”.
- `/mcp` / `/agent` do not implement MCP protocol or spawn process supervisors inside the bridge — they only guide or rephrase for the CLI.
- Deploy only for trusted users behind a whitelist when possible.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Semantic Versioning from 1.0.0; record changes in [`CHANGELOG.md`](CHANGELOG.md).

## License

MIT. See [`LICENSE`](LICENSE).
