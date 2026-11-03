#!/usr/bin/env python3
"""Fake `codex` CLI for integration tests of wechatbridge.codex.

Simulates `codex exec [options] [resume <thread_id>] <prompt>` with a JSONL
event stream on stdout. Behaviour is controlled by FAKE_CODEX_MODE (read from
the environment each invocation):

  ok            (default) first run emits a thread + reply and writes a rollout
                           file; resume emits a continuation reply.
  resume_fail   first run succeeds (writes rollout); resume exits 1 with a
                           REAL codex wording message ("no rollout found for
                           thread id <id>", from codex-rs read_thread.rs) ->
                           triggers fallback retry (which is a fresh first run
                           and therefore succeeds).
  resume_fail_stdout  like resume_fail but the error is only in a JSONL error
                           event on stdout (stderr has an unrelated warning),
                           using the REAL codex wording "no rollout found for
                           thread id <id>". NOTE: this is a DEFENSIVE/
                           future-proofing test only, NOT the typical real
                           path. Real Codex emits the resume-not-found error to
                           STDERR *before* the JSONL stream starts; this mode
                           only verifies the bridge's resilience against an
                           abnormal/out-of-band JSONL error event on stdout.
  ok_emoji      first run emits a thread + a normal agent_message whose TEXT
                           starts with ❌ but exits 0 -> the bridge must treat
                           this as a SUCCESS and persist thread_id (regression
                           for the "never judge failure by ❌ display prefix"
                           rule).
  ok_turn_failed first run emits thread.started THEN a structured turn.failed
                           (model overloaded) and exits 0 -> the bridge must
                           treat this as a FAILURE even with zero exit, and
                           never persist thread_id.
  ok_error      first run emits thread.started THEN a structured error event
                           (model overloaded) and exits 0 -> the bridge must
                           treat this as a FAILURE even with zero exit, and
                           never persist thread_id.
  resume_turn_fail   resume emits thread.started THEN turn.failed("rate limit
                           exceeded") and exits 1. This is a *normal* turn
                           failure (rate limit), NOT a resume/session error,
                           so the bridge must NOT fallback and must keep the
                           old thread_id. Invoked exactly once.
  resume_missing_credentials  like resume_turn_fail but with a "missing
                           credentials" turn failure. NOT a session error ->
                           no fallback, old thread_id kept, invoked once.
  resume_file_not_found  resume exits 1 with stderr "file not found". NOT a
                           session error -> no fallback, invoked once.
  resume_permission_denied  like resume_turn_fail but with a "permission
                           denied" turn failure. NOT a session error -> no
                           fallback, old thread_id kept, invoked once.
  fail_first    first run exits 1 with an error message (no resume involved).
  turn_fail     first run emits thread.started then turn.failed("model is
                           currently overloaded") and exits 1 (plain turn
                           failure, no fallback since it's a first run).
  ok_shell_files  first run emits normal thread+reply JSONL AND writes two
                           regular files to session_dir (result.txt created during
                           execution, out.md pre-existing old file).  Tests that
                           fallback scan collects only the new file.
  ok_shell_and_file_change  first run emits file_change events AND writes an
                           additional file via shell.  Tests deduplication.
  ok_shell_mixed_docs  file_change for helper.py AND a shell-written report.pdf.
                           Tests merge: structured + extra document.
  ok_shell_old_files  first run writes two regular files to session_dir but
                           both have old mtimes (before t0).  Tests that the
                           fallback scan does NOT collect old files.
  ok_shell_internal_metadata  first run writes files to session_dir/.codex and
                           session_dir/.git/  Tests that internal metadata dirs
                           are excluded.
  ok_shell_escape   first run creates a symlink in session_dir pointing to
                           /tmp (outside allowed roots).  Tests symlink escape
                           exclusion.
  timeout       sleeps far longer than the bridge timeout so the caller times out.
  models_fail   ``debug models`` (and --bundled) exit 1 — list fetch fails.
  models_empty  ``debug models`` exits 0 with empty models array.
  models_plain_fail  plain ``debug models`` fails; ``--bundled`` succeeds.

If FAKE_CODEX_LOG is set (path), every invocation appends one line so tests
can assert exactly how many times (and with what args) the CLI was launched.

Rollout files are written under <cwd>/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl
so the bridge's precheck (filename contains thread_id) can locate them.
"""

import datetime
import json
import os
import sys
import time
import uuid


def _emit_thread(uuid_str, text):
    lines = [
        json.dumps({"type": "thread.started", "thread_id": uuid_str}),
        json.dumps({"type": "turn.started"}),
        json.dumps(
            {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": text}}
        ),
        json.dumps({"type": "turn.completed", "usage": {}}),
    ]
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _write_rollout(session_dir, uuid_str):
    now = datetime.datetime.now()
    ts = now.strftime("%Y-%m-%dT%H-%M-%S")
    rel = os.path.join(".codex", "sessions", now.strftime("%Y"), now.strftime("%m"), now.strftime("%d"))
    rollout_dir = os.path.join(session_dir, rel)
    os.makedirs(rollout_dir, exist_ok=True)
    path = os.path.join(rollout_dir, f"rollout-{ts}-{uuid_str}.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        # meta line (real codex writes a SessionMeta line first)
        f.write(json.dumps({"type": "session_meta", "payload": {"id": uuid_str}}) + "\n")
        f.write(json.dumps({"type": "thread.started", "thread_id": uuid_str}) + "\n")


def _emit_debug_models(bundled=False):
    """Emit a JSON model list for ``codex debug models`` [/ ``--bundled``].

    Controlled by FAKE_CODEX_MODE:
      models_fail   → exit 1 (both plain and --bundled)
      models_empty  → exit 0 with empty models array
      models_plain_fail → plain fails, --bundled succeeds (tests fallback)
      (default / others) → exit 0 with a few gpt-* slugs
    """
    mode = os.environ.get("FAKE_CODEX_MODE", "ok")
    if mode == "models_fail":
        sys.stderr.write("error: failed to list models\n")
        sys.stderr.flush()
        return 1
    if mode == "models_empty":
        sys.stdout.write(json.dumps({"models": []}) + "\n")
        sys.stdout.flush()
        return 0
    if mode == "models_plain_fail" and not bundled:
        sys.stderr.write("error: network unavailable\n")
        sys.stderr.flush()
        return 1

    payload = {
        "models": [
            {"slug": "gpt-5.1-codex", "display_name": "GPT-5.1 Codex"},
            {"slug": "gpt-5.1", "display_name": "GPT-5.1"},
            {"slug": "gpt-5-codex", "display_name": "GPT-5 Codex"},
            {"slug": "gpt-5", "display_name": "GPT-5"},
            {"slug": "gpt-4.1", "display_name": "GPT-4.1"},
        ]
    }
    if bundled:
        # Distinct marker so tests can tell --bundled path was used when desired
        payload["source"] = "bundled"
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()
    return 0


def main():
    args = sys.argv[1:]
    mode = os.environ.get("FAKE_CODEX_MODE", "ok")

    # 调用日志：每个 invocation 追加一行，供测试精确断言调用次数/参数。
    log_path = os.environ.get("FAKE_CODEX_LOG")
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write("invoked mode=%s args=%s\n" % (mode, " ".join(args)))
        except OSError:
            pass

    # ``codex debug models`` / ``codex debug models --bundled`` (no ``exec``)
    if "debug" in args and "models" in args:
        bundled = "--bundled" in args
        return _emit_debug_models(bundled=bundled)

    # "--" separates options from the positional prompt (bridge always sends
    # it so '-…' prompts are not parsed as flags). Drop only the FIRST one —
    # the prompt itself may be a literal "--" and must survive.
    if "--" in args:
        i = args.index("--")
        args = args[:i] + args[i + 1:]

    if "exec" in args:
        args = args[args.index("exec") + 1:]

    session_dir = os.getcwd()

    if "resume" in args:
        idx = args.index("resume")
        # format: resume <thread_id> <prompt>  (prompt is a single positional)
        thread_id = args[idx + 1] if len(args) > idx + 1 else ""
        prompt = args[idx + 2] if len(args) > idx + 2 else ""

        if mode == "resume_fail":
            # 真实 Codex 文案（codex-rs/thread-store/.../read_thread.rs）：
            # "no rollout found for thread id {thread_id}"
            sys.stderr.write(
                "Error: no rollout found for thread id %s\n" % thread_id
            )
            sys.stderr.flush()
            return 1

        if mode == "resume_fail_stdout":
            # 防御性测试：真实 Codex 的 resume-not-found 主要在 JSONL 启动前写
            # stderr（不是 stdout JSONL error）。本模式只验证 bridge 对未来/异常
            # 的 stdout JSONL error 事件的防御性兼容，不得声称是典型真实路径。
            # stderr 只有无关 warning（不含 resume/session/thread/not found 关键词），
            # 真正的错误在 stdout 的 JSONL error 事件里，使用真实 Codex 文案
            # （codex-rs）："no rollout found for thread id {thread_id}"，returncode=1。
            # 验证 run_codex 从 JSONL 抽取结构化错误后仍能识别并降级首轮重试。
            sys.stderr.write(
                "warning: deprecation: option --json will change in a future release\n"
            )
            sys.stderr.flush()
            sys.stdout.write(
                json.dumps({
                    "type": "error",
                    "message": "no rollout found for thread id %s" % thread_id,
                }) + "\n"
            )
            sys.stdout.flush()
            return 1

        if mode == "resume_turn_fail":
            # 续轮时线程其实找到了（先 emit thread.started），随后 turn 失败
            # （限流）。这不是 resume/session missing 错误，bridge 不得 fallback，
            # 旧 thread_id 必须保留，且只运行这一次（无重试）。
            sys.stdout.write(
                json.dumps({"type": "thread.started", "thread_id": thread_id}) + "\n"
            )
            sys.stdout.flush()
            sys.stdout.write(
                json.dumps({
                    "type": "turn.failed",
                    "error": {"message": "rate limit exceeded, please retry later"},
                }) + "\n"
            )
            sys.stdout.flush()
            return 1

        if mode == "resume_missing_credentials":
            # 续轮时线程其实找到了（先 emit thread.started），随后 turn 失败
            # （缺失凭据）。这是普通错误，不是 resume/session missing，bridge
            # 不得 fallback，旧 thread_id 必须保留，且只运行这一次（无重试）。
            sys.stdout.write(
                json.dumps({"type": "thread.started", "thread_id": thread_id}) + "\n"
            )
            sys.stdout.flush()
            sys.stdout.write(
                json.dumps({
                    "type": "turn.failed",
                    "error": {"message": "missing credentials for this request"},
                }) + "\n"
            )
            sys.stdout.flush()
            return 1

        if mode == "resume_file_not_found":
            # stderr 报 file not found，无 JSONL 错误事件。这不是 session 错误，
            # bridge 不得 fallback，旧 thread_id 保留，只运行一次。
            sys.stderr.write("error: file not found: ./config.yaml\n")
            sys.stderr.flush()
            return 1

        if mode == "resume_permission_denied":
            # 续轮找到了线程（emit thread.started），随后 turn 失败（权限不足）。
            # 不是 resume/session missing 错误，bridge 不得 fallback。
            sys.stdout.write(
                json.dumps({"type": "thread.started", "thread_id": thread_id}) + "\n"
            )
            sys.stdout.flush()
            sys.stdout.write(
                json.dumps({
                    "type": "turn.failed",
                    "error": {"message": "permission denied: cannot read /etc/shadow"},
                }) + "\n"
            )
            sys.stdout.flush()
            return 1

        _emit_thread(thread_id, "resumed(%s): %s" % (thread_id[:8], prompt))
        return 0

    # first run
    prompt = args[-1] if args else ""
    if mode == "ok_emoji":
        # 零退出 + agent_message 文本以 ❌ 开头：必须是成功（正常 agent 回复
        # 碰巧以 ❌ 起头），bridge 应视为成功并落盘 thread_id。
        new_id = str(uuid.uuid4())
        _write_rollout(session_dir, new_id)
        _emit_thread(new_id, "❌ something looks off, but the task actually completed")
        return 0
    if mode == "ok_turn_failed":
        # 零退出但结构化 turn.failed（模型过载）：即使零退出也必须判定为失败，
        # 且本就是首轮，不得写 thread_id / 不得 fallback。
        new_id = str(uuid.uuid4())
        sys.stdout.write(
            json.dumps({"type": "thread.started", "thread_id": new_id}) + "\n"
        )
        sys.stdout.flush()
        sys.stdout.write(
            json.dumps({
                "type": "turn.failed",
                "error": {"message": "model is currently overloaded, try again soon"},
            }) + "\n"
        )
        sys.stdout.flush()
        return 0
    if mode == "ok_error":
        # 零退出但结构化 error 事件（非 turn.failed）：即使零退出也必须判定为
        # 失败，且本就是首轮，不得写 thread_id / 不得 fallback。
        new_id = str(uuid.uuid4())
        sys.stdout.write(
            json.dumps({"type": "thread.started", "thread_id": new_id}) + "\n"
        )
        sys.stdout.flush()
        sys.stdout.write(
            json.dumps({
                "type": "error",
                "message": "model is currently overloaded, try again soon",
            }) + "\n"
        )
        sys.stdout.flush()
        return 0
    if mode == "timeout":
        time.sleep(60)
        return 0
    if mode == "fail_first":
        sys.stderr.write("boom: something went wrong\n")
        sys.stderr.flush()
        return 1
    if mode == "turn_fail":
        # 首轮普通 turn.failed（模型过载），非 resume/session 错误；bridge 不得
        # fallback（本就是首轮），返回 ❌ 错误且不写 thread_id。
        sys.stdout.write(
            json.dumps({"type": "thread.started", "thread_id": str(uuid.uuid4())}) + "\n"
        )
        sys.stdout.flush()
        sys.stdout.write(
            json.dumps({
                "type": "turn.failed",
                "error": {"message": "model is currently overloaded, try again soon"},
            }) + "\n"
        )
        sys.stdout.flush()
        return 1

    if mode == "ok_shell_files":
        # Fallback scan test: emit normal success JSONL AND write files to session_dir.
        # result.txt is "new" (not in snapshot_before) -> should be collected.
        # out.md has old mtime -> should NOT be collected.
        new_id = str(uuid.uuid4())
        _write_rollout(session_dir, new_id)
        _emit_thread(new_id, "first(%s): %s" % (new_id[:8], prompt))
        # New file created during execution
        with open(os.path.join(session_dir, "result.txt"), "w") as f:
            f.write("generated content")
        # Old pre-existing file (mtime before typical t0)
        old_path = os.path.join(session_dir, "out.md")
        with open(old_path, "w") as f:
            f.write("old content")
        os.utime(old_path, (1000000, 1000000))
        return 0

    if mode == "ok_shell_and_file_change":
        # Dedup test: emit file_change for result.txt AND write it via shell.
        # The file_change should be the authoritative source; fallback should
        # not add a duplicate.
        new_id = str(uuid.uuid4())
        _write_rollout(session_dir, new_id)
        lines = [
            json.dumps({"type": "thread.started", "thread_id": new_id}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "done"}}),
            json.dumps({"type": "item.completed", "item": {
                "id": "i2", "type": "file_change", "status": "completed",
                "changes": [{"kind": "add", "path": "result.txt"}]
            }}),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
        with open(os.path.join(session_dir, "result.txt"), "w") as f:
            f.write("generated content")
        return 0

    if mode == "ok_shell_mixed_docs":
        # Mixed: file_change reports helper.py; the real document is only
        # written via the shell. Both must come back.
        new_id = str(uuid.uuid4())
        _write_rollout(session_dir, new_id)
        lines = [
            json.dumps({"type": "thread.started", "thread_id": new_id}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "done"}}),
            json.dumps({"type": "item.completed", "item": {
                "id": "i2", "type": "file_change", "status": "completed",
                "changes": [{"kind": "add", "path": "helper.py"}]
            }}),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
        with open(os.path.join(session_dir, "helper.py"), "w") as f:
            f.write("print('ok')\n")
        with open(os.path.join(session_dir, "report.pdf"), "wb") as f:
            f.write(b"%PDF-1.4")
        return 0

    if mode == "ok_shell_old_files":
        # Old files test: write files with old mtimes. Fallback scan should
        # not collect them because mtime < t0.
        new_id = str(uuid.uuid4())
        _write_rollout(session_dir, new_id)
        _emit_thread(new_id, "first(%s): %s" % (new_id[:8], prompt))
        for name in ("old_result.txt", "old_output.md"):
            p = os.path.join(session_dir, name)
            with open(p, "w") as f:
                f.write("old content")
            os.utime(p, (1000000, 1000000))
        return 0

    if mode == "ok_shell_internal_metadata":
        # Internal metadata test: write files to .codex/ and .git/ dirs.
        # These should be excluded by _EXCLUDED_SCAN_NAMES.
        new_id = str(uuid.uuid4())
        _write_rollout(session_dir, new_id)
        _emit_thread(new_id, "first(%s): %s" % (new_id[:8], prompt))
        codex_meta = os.path.join(session_dir, ".codex", "meta.json")
        os.makedirs(os.path.dirname(codex_meta), exist_ok=True)
        with open(codex_meta, "w") as f:
            f.write("{}")
        git_dir = os.path.join(session_dir, ".git", "config")
        os.makedirs(os.path.dirname(git_dir), exist_ok=True)
        with open(git_dir, "w") as f:
            f.write("[core]")
        # Also a real file that should be collected
        with open(os.path.join(session_dir, "real_output.txt"), "w") as f:
            f.write("real output")
        return 0

    if mode == "ok_shell_escape":
        # Symlink escape test: create a symlink to /tmp (outside allowed roots).
        # The fallback scan should NOT follow it.
        new_id = str(uuid.uuid4())
        _write_rollout(session_dir, new_id)
        _emit_thread(new_id, "first(%s): %s" % (new_id[:8], prompt))
        # Create target file outside session dir
        target = "/tmp/_codex_escape_test_target.txt"
        with open(target, "w") as f:
            f.write("escaped content")
        # Create symlink inside session dir pointing to target
        link_path = os.path.join(session_dir, "escaped.txt")
        try:
            os.symlink(target, link_path)
        except OSError:
            pass  # If symlink fails, test still passes (no file to collect)
        return 0

    new_id = str(uuid.uuid4())
    _write_rollout(session_dir, new_id)
    _emit_thread(new_id, "first(%s): %s" % (new_id[:8], prompt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
