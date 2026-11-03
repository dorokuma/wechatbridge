"""Unit + integration tests for wechatbridge.codex (codex CLI backend)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock
from unittest.mock import AsyncMock

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIXTURES = os.path.join(_HERE, "fixtures")
_FAKE_CODEX = os.path.join(_HERE, "fake_codex.py")


def _read_fixture(name: str) -> str:
    with open(os.path.join(_FIXTURES, name), "r", encoding="utf-8") as f:
        return f.read()


class TestBuildCodexCommand(unittest.TestCase):
    def setUp(self):
        from wechatbridge.config import config
        self._patchers = []
        p = mock.patch.object(config, "codex_binary_path", "codex")
        p.start()
        self._patchers.append(p)

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def _cmd(self, prompt="hello", prefs=None, first=True, thread_id=""):
        from wechatbridge.codex import _build_codex_command
        return _build_codex_command(prompt, prefs or {}, first, thread_id)

    def test_first_basic(self):
        cmd = self._cmd()
        self.assertEqual(
            cmd,
            ["codex", "exec", "--json", "--skip-git-repo-check",
             "--dangerously-bypass-approvals-and-sandbox", "--", "hello"],
        )

    def test_prompt_is_last_positional(self):
        cmd = self._cmd(prompt="a b c")
        self.assertEqual(cmd[-1], "a b c")
        self.assertEqual(cmd[-2], "--")
        self.assertNotIn("resume", cmd)

    def test_dash_prompt_after_separator(self):
        # 以 '-' 开头的 prompt 必须跟在 "--" 之后，否则被 codex CLI 当选项。
        cmd = self._cmd(prompt="--help")
        self.assertEqual(cmd[-1], "--help")
        self.assertEqual(cmd[-2], "--")
        # prompt 本身就是 "--" 时也必须保留（fake_codex 只删第一个分隔符）
        cmd = self._cmd(prompt="--")
        self.assertEqual(cmd[-2:], ["--", "--"])

    def test_plan_mode_uses_readonly_sandbox(self):
        cmd = self._cmd(prefs={"mode": "plan"})
        self.assertIn("--sandbox", cmd)
        self.assertIn("read-only", cmd)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)

    def test_model_flag(self):
        cmd = self._cmd(prefs={"model": "gpt-5.1-codex"})
        self.assertIn("-m", cmd)
        self.assertIn("gpt-5.1-codex", cmd)

    def test_effort_flag(self):
        cmd = self._cmd(prefs={"effort": "low"})
        self.assertIn("-c", cmd)
        self.assertIn("model_reasoning_effort=low", cmd)

    def test_model_and_effort(self):
        cmd = self._cmd(prefs={"model": "gpt-5.1-codex", "effort": "medium"})
        self.assertIn("-m", cmd)
        self.assertIn("gpt-5.1-codex", cmd)
        self.assertIn("-c", cmd)
        self.assertIn("model_reasoning_effort=medium", cmd)

    def test_embedded_effort_in_model_plus_explicit(self):
        # 与 grok 对齐：模型名内嵌 -high 且又显式给 effort 时，模型取 base、effort 取显式值
        cmd = self._cmd(prefs={"model": "gpt-5.1-codex-high", "effort": "low"})
        self.assertIn("-m", cmd)
        self.assertIn("gpt-5.1-codex", cmd)
        self.assertNotIn("gpt-5.1-codex-high", cmd)
        self.assertIn("model_reasoning_effort=low", cmd)

    def test_add_dirs(self):
        cmd = self._cmd(prefs={"add_dirs": ["/tmp/a", "/tmp/b"]})
        self.assertIn("--add-dir", cmd)
        self.assertIn("/tmp/a", cmd)
        self.assertIn("/tmp/b", cmd)

    def test_resume_puts_subcommand_before_prompt(self):
        cmd = self._cmd(first=False, thread_id="tid-123")
        self.assertIn("resume", cmd)
        self.assertIn("tid-123", cmd)
        # resume 是子命令，options 在前，"--" 分隔符之后是 prompt
        self.assertEqual(cmd[cmd.index("resume") + 1], "tid-123")
        self.assertEqual(cmd[-1], "hello")
        self.assertEqual(cmd[-2], "--")

    def test_resume_no_yolo_skip_when_plan(self):
        cmd = self._cmd(first=False, thread_id="tid", prefs={"mode": "plan"})
        self.assertIn("--sandbox", cmd)
        self.assertIn("read-only", cmd)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertIn("resume", cmd)

    def test_plan_mode_uses_real_read_only_value(self):
        # 真实 clap 枚举是 kebab-case read-only，不是 readonly。
        cmd = self._cmd(prefs={"mode": "plan"})
        self.assertIn("--sandbox", cmd)
        # sandbox 必须紧跟真实枚举值 read-only
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "read-only")
        self.assertIn("read-only", cmd)
        # 明确断言不存在 readonly 这个值（连作子串也不应出现）
        self.assertNotIn("readonly", cmd)
        # 且不得误带 yolo 标志
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)


class TestParseCodexOutput(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(lambda: _rmtree(self.td))

    def _parse(self, name, since=0.0):
        from wechatbridge.codex import _parse_codex_output
        return _parse_codex_output(_read_fixture(name), self.td, since=since)

    def test_normal_reply(self):
        display, artifacts, tid, parse_failed = self._parse("codex_normal.jsonl")
        self.assertEqual(display, "Hello from codex")
        self.assertEqual(tid, "11111111-1111-1111-1111-111111111111")
        self.assertEqual(artifacts, [])
        self.assertFalse(parse_failed)

    def test_multi_agent_takes_last(self):
        display, _, _, parse_failed = self._parse("codex_multi_agent.jsonl")
        self.assertEqual(display, "final reply")
        self.assertFalse(parse_failed)

    def test_file_change_status_failed_not_collected(self):
        # failed 的 file_change 不得进入 artifacts（其 add/update 文件不应回传）；
        # completed / 缺 status 的正常事件继续收集。所有涉及文件都创建并置于
        # 时间窗口内，以排除 mtime 过滤的干扰，确认 fail 仅因 status=="failed" 被排除。
        now = time.time()
        for name in ("out.md", "keep.py", "fail.md", "fail2.py", "done.md"):
            p = os.path.join(self.td, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write("x")
            os.utime(p, (now, now))

        display, artifacts, _, parse_failed = self._parse("codex_file_change_status.jsonl", since=now - 100)
        self.assertFalse(parse_failed)
        names = {n for n, _ in artifacts}
        # 缺 status（f0）：兼容旧事件，照常收集
        self.assertIn("out.md", names)
        self.assertIn("keep.py", names)
        # completed（f2）：正常收集
        self.assertIn("done.md", names)
        # failed（f1）：不得收集（文件存在且在窗口内仍被排除）
        self.assertNotIn("fail.md", names)
        self.assertNotIn("fail2.py", names)
        # 路径基于 session_dir 且为绝对路径
        for _n, ap in artifacts:
            self.assertTrue(os.path.isabs(ap))
            self.assertTrue(ap.startswith(self.td))

    def test_file_change_extract_and_mtime_filter(self):
        # 近期创建的文件应被收录；过期文件应被过滤
        now = time.time()
        fresh = os.path.join(self.td, "out.md")
        fresh2 = os.path.join(self.td, "keep.py")
        old = os.path.join(self.td, "old.md")
        for p in (fresh, fresh2, old):
            with open(p, "w", encoding="utf-8") as f:
                f.write("x")
        os.utime(fresh, (now, now))
        os.utime(fresh2, (now, now))
        old_mtime = now - 3600
        os.utime(old, (old_mtime, old_mtime))

        display, artifacts, _, parse_failed = self._parse("codex_file_change.jsonl", since=now - 100)
        self.assertFalse(parse_failed)
        names = {n for n, _ in artifacts}
        self.assertIn("out.md", names)
        self.assertIn("keep.py", names)
        self.assertNotIn("old.md", names)          # 过期被过滤
        self.assertNotIn("gone.txt", names)        # delete 类型不收
        # 绝对路径基于 session_dir
        for _n, ap in artifacts:
            self.assertTrue(os.path.isabs(ap))
            self.assertTrue(ap.startswith(self.td))

    def test_turn_failed(self):
        display, artifacts, _, parse_failed = self._parse("codex_turn_failed.jsonl")
        self.assertTrue(display.startswith("❌"))
        self.assertEqual(artifacts, [])
        self.assertTrue(parse_failed)             # 结构化错误 -> parse_failed

    def test_error_event(self):
        display, artifacts, _, parse_failed = self._parse("codex_error.jsonl")
        self.assertTrue(display.startswith("❌"))
        self.assertEqual(artifacts, [])
        self.assertTrue(parse_failed)             # 结构化错误 -> parse_failed

    def test_nonjson_fallback(self):
        display, artifacts, tid, parse_failed = self._parse("codex_nonjson.jsonl")
        self.assertIn("just some plain text output", display)
        self.assertEqual(artifacts, [])
        self.assertEqual(tid, "")
        self.assertFalse(parse_failed)

    def test_empty_output(self):
        from wechatbridge.runner_common import EMPTY_REPLY
        display, artifacts, tid, parse_failed = self._parse("codex_empty.jsonl")
        self.assertEqual(display, EMPTY_REPLY)
        self.assertEqual(artifacts, [])
        self.assertEqual(tid, "")
        self.assertFalse(parse_failed)

    def test_agent_reply_emoji_prefix_is_success(self):
        # 正常 agent 回复以 ❌ 开头（文本恰好带该 emoji），零结构化错误：
        # 必须判定为成功（parse_failed=False），且仍捕获 thread_id / artifacts。
        display, artifacts, tid, parse_failed = self._parse("codex_emoji_reply.jsonl")
        self.assertTrue(display.startswith("❌"))   # 文本确实以 ❌ 开头
        self.assertFalse(parse_failed)             # 关键：不以 ❌ 判失败
        self.assertEqual(tid, "33333333-3333-3333-3333-333333333333")
        self.assertEqual(artifacts, [])


class TestExtractCodexErrorMessages(unittest.TestCase):
    """_extract_codex_error_messages must pull ONLY structured error text from
    JSONL (error.message / turn.failed.error.message), never the full stdout
    blob — so a normal thread.started + rate-limit message is NOT seen as a
    resume/session failure."""

    def _extract(self, text):
        from wechatbridge.codex import _extract_codex_error_messages
        return _extract_codex_error_messages(text)

    def test_pulls_error_message(self):
        out = '{"type":"error","message":"session not found"}\n'
        self.assertEqual(self._extract(out), ["session not found"])

    def test_pulls_turn_failed_object_error(self):
        out = ('{"type":"thread.started","thread_id":"t1"}\n'
               '{"type":"turn.failed","error":{"message":"rate limit exceeded"}}\n')
        # thread.started must NOT be treated as an error
        self.assertEqual(self._extract(out), ["rate limit exceeded"])

    def test_turn_failed_string_error(self):
        out = '{"type":"turn.failed","error":"some string error"}\n'
        self.assertEqual(self._extract(out), ["some string error"])

    def test_skips_normal_text_and_empty(self):
        out = ('{"type":"thread.started","thread_id":"t1"}\n'
               '{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}\n')
        self.assertEqual(self._extract(out), [])

    def test_empty_nonjson(self):
        self.assertEqual(self._extract(""), [])
        self.assertEqual(self._extract("not json at all"), [])


class TestResumeSessionLostDetection(unittest.TestCase):
    """_is_resume_session_lost must trigger ONLY on explicit resume/session
    not-found semantics, never on bare `missing` or bare `not found`.

    Positive (must trigger):
      session not found / conversation not found / no such session /
      unknown session / missing session / resume ... session not found /
      resume ... missing session
    Negative (must NOT trigger):
      missing credentials / missing model / file not found / rate limit /
      network error / permission denied
    """

    def _detect(self, text):
        from wechatbridge.codex import _is_resume_session_lost
        return _is_resume_session_lost(text)

    # --- positive: explicit session-not-found semantics ---
    def test_session_not_found(self):
        self.assertTrue(self._detect("error: session not found"))

    def test_resume_session_not_found(self):
        self.assertTrue(self._detect("error: resume: session not found for abc"))

    def test_conversation_not_found(self):
        self.assertTrue(self._detect("conversation not found: thread expired"))

    def test_no_such_session(self):
        self.assertTrue(self._detect("no such session: abc-123"))

    def test_unknown_session(self):
        self.assertTrue(self._detect("unknown session provided"))

    def test_missing_session(self):
        self.assertTrue(self._detect("missing session state for this id"))

    def test_resume_missing_session(self):
        self.assertTrue(self._detect("resume: missing session abc-123"))

    def test_case_insensitive(self):
        self.assertTrue(self._detect("RESUME: Session Not Found"))

    # --- positive: REAL codex wording (codex-rs source-confirmed) ---
    def test_no_rollout_found_for_thread_id(self):
        # codex-rs/thread-store/src/local/read_thread.rs:
        #   "no rollout found for thread id {thread_id}"
        self.assertTrue(self._detect("no rollout found for thread id abc-123"))

    def test_thread_not_found(self):
        # codex-rs/thread-store/src/local/update_thread_metadata.rs:
        #   "thread not found: {thread_id}"
        self.assertTrue(self._detect("thread not found: abc-123"))

    # --- negative: ordinary (non-session) errors must NOT trigger ---
    def test_normal_errors_negative(self):
        for msg in (
            "rate limit exceeded, please retry later",
            "network error: connection reset by peer",
            "model is currently overloaded, try again soon",
            "an internal server error occurred",
            "file not found: ./config.yaml",
        ):
            self.assertFalse(self._detect(msg), msg)

    # --- negative: bare `missing` / bare `not found` must NOT trigger ---
    def test_missing_credentials(self):
        self.assertFalse(self._detect("missing credentials: CODEX_API_KEY is not set"))

    def test_missing_model(self):
        self.assertFalse(self._detect("missing model: no model selected"))

    def test_file_not_found(self):
        self.assertFalse(self._detect("error: file not found: ./config.yaml"))

    def test_rate_limit(self):
        self.assertFalse(self._detect("rate limit exceeded, please retry later"))

    def test_network_error(self):
        self.assertFalse(self._detect("network error: connection reset by peer"))

    def test_permission_denied(self):
        self.assertFalse(self._detect("permission denied: cannot read /etc/shadow"))

    def test_empty_text(self):
        self.assertFalse(self._detect(""))
        self.assertFalse(self._detect(None))


class TestCodexThreadIdHelpers(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(lambda: _rmtree(self.td))

    def test_roundtrip(self):
        from wechatbridge.codex import (
            _read_codex_thread_id, _write_codex_thread_id, _delete_codex_thread_id,
        )
        tid = "11111111-1111-1111-1111-111111111111"
        self.assertEqual(_read_codex_thread_id(self.td), "")
        _write_codex_thread_id(self.td, tid)
        self.assertEqual(_read_codex_thread_id(self.td), tid)
        _delete_codex_thread_id(self.td)
        self.assertEqual(_read_codex_thread_id(self.td), "")

    def test_read_thread_id_strict_uuid(self):
        from wechatbridge.codex import (
            _read_codex_thread_id, _write_codex_thread_id,
        )
        # 规范（小写 hyphenated）UUID 才接受
        ok = "feedface-0000-0000-0000-000000000000"
        _write_codex_thread_id(self.td, ok)
        self.assertEqual(_read_codex_thread_id(self.td), ok)

        # 无连字符的 32 位十六进制串：拒绝（非规范字符串）
        _write_codex_thread_id(self.td, "feedface00000000000000000000000000")
        self.assertEqual(_read_codex_thread_id(self.td), "")

        # 大写变形：拒绝
        _write_codex_thread_id(self.td, "FEEDFACE-0000-0000-0000-000000000000")
        self.assertEqual(_read_codex_thread_id(self.td), "")

        # 大括号变形：拒绝
        _write_codex_thread_id(self.td, "{feedface-0000-0000-0000-000000000000}")
        self.assertEqual(_read_codex_thread_id(self.td), "")

        # 空白 / 空：拒绝
        _write_codex_thread_id(self.td, "   ")
        self.assertEqual(_read_codex_thread_id(self.td), "")
        _write_codex_thread_id(self.td, "")
        self.assertEqual(_read_codex_thread_id(self.td), "")

        # 超长（拼接污染）：拒绝
        _write_codex_thread_id(self.td, ok + "-extra")
        self.assertEqual(_read_codex_thread_id(self.td), "")

        # 换行污染（多行注入）：拒绝
        _write_codex_thread_id(self.td, ok + "\n" + ok)
        self.assertEqual(_read_codex_thread_id(self.td), "")

        # 完全非 UUID 文本：拒绝
        _write_codex_thread_id(self.td, "abc-123")
        self.assertEqual(_read_codex_thread_id(self.td), "")
        _write_codex_thread_id(self.td, "not-a-uuid-at-all")
        self.assertEqual(_read_codex_thread_id(self.td), "")

    def test_has_session_finds_rollout(self):
        from wechatbridge.codex import _has_codex_session
        tid = "feedface-0000-0000-0000-000000000000"
        day = os.path.join(self.td, ".codex", "sessions", "2025", "01", "05")
        os.makedirs(day)
        self.assertFalse(_has_codex_session(self.td, tid))
        with open(os.path.join(day, f"rollout-2025-01-05T00-00-00-{tid}.jsonl"), "w") as f:
            f.write("x")
        self.assertTrue(_has_codex_session(self.td, tid))
        # 不匹配的 tid 找不到
        self.assertFalse(_has_codex_session(self.td, "deadbeef-0000-0000-0000-000000000000"))

    def test_has_session_finds_rollout_zst(self):
        from wechatbridge.codex import _has_codex_session
        tid = "feedface-0000-0000-0000-000000000000"
        day = os.path.join(self.td, ".codex", "sessions", "2025", "01", "05")
        os.makedirs(day)
        self.assertFalse(_has_codex_session(self.td, tid))
        # 压缩版 .jsonl.zst（feature 默认关闭但需前向兼容）文件名也含 thread_id
        with open(os.path.join(day, f"rollout-2025-01-05T00-00-00-{tid}.jsonl.zst"), "w") as f:
            f.write("x")
        self.assertTrue(_has_codex_session(self.td, tid))
        # 不匹配的 tid 找不到
        self.assertFalse(_has_codex_session(self.td, "deadbeef-0000-0000-0000-000000000000"))


class TestCleanCodexSessionsZst(unittest.TestCase):
    """_clean_codex_sessions must expire old rollout zst files but keep new
    zst and non-rollout files (auth.json / other session files)."""

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(lambda: _rmtree(self.td))

    def test_expired_zst_removed_keeps_new_and_non_rollout(self):
        import datetime
        from wechatbridge.runner_common import _clean_codex_sessions

        from wechatbridge.codex import _has_codex_session
        old_tid = "feedface-0000-0000-0000-000000000000"
        new_tid = "facebeef-0000-0000-0000-000000000000"
        # _clean_codex_sessions 接收 sessions 根目录（不含 .codex 前缀），
        # _has_codex_session 接收 session_dir（查找 session_dir/.codex/sessions），
        # 两者的目录结构前缀不同，需分别对齐。
        now = datetime.datetime.now()
        day = os.path.join(
            self.td, ".codex", "sessions", now.strftime("%Y"),
            now.strftime("%m"), now.strftime("%d"),
        )
        os.makedirs(day)

        # 过期 rollout zst -> 应被删除
        old_zst = os.path.join(day, f"rollout-2024-01-01T00-00-00-{old_tid}.jsonl.zst")
        with open(old_zst, "w", encoding="utf-8") as f:
            f.write("x")
        old_mtime = time.time() - 100 * 86400
        os.utime(old_zst, (old_mtime, old_mtime))

        # 新 rollout zst（文件名含新 thread_id）-> 保留
        new_zst = os.path.join(
            day, f"rollout-{now.strftime('%Y-%m-%dT%H-%M-%S')}-{new_tid}.jsonl.zst"
        )
        with open(new_zst, "w", encoding="utf-8") as f:
            f.write("x")

        # 非 rollout 文件（auth.json）-> 保留，且不影响 _has_codex_session 判定
        auth = os.path.join(day, "auth.json")
        with open(auth, "w", encoding="utf-8") as f:
            f.write("secret")

        cutoff = time.time() - 30 * 86400
        removed = _clean_codex_sessions(os.path.join(self.td, ".codex", "sessions"), cutoff)

        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(old_zst))
        self.assertTrue(os.path.exists(new_zst))
        self.assertTrue(os.path.exists(auth))
        # 过期 rollout 被删后，旧 tid 不再可被 _has_codex_session 找到
        self.assertFalse(_has_codex_session(self.td, old_tid))
        # 新 zst 仍在，新 tid 可找到
        self.assertTrue(_has_codex_session(self.td, new_tid))


class TestCodexSlash(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(lambda: _rmtree(self.td))
        from wechatbridge.config import config
        self._patchers = []
        p = mock.patch.object(config, "session_base_dir", self.td)
        p.start()
        self._patchers.append(p)
        p2 = mock.patch.object(config, "enable_mcp", True)
        p2.start()
        self._patchers.append(p2)

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    async def test_clear_removes_thread_id_and_flag(self):
        from wechatbridge.codex import (
            handle_codex_slash_command, _write_codex_thread_id, _read_codex_thread_id,
            clear_initialized, mark_initialized,
        )
        from wechatbridge.runner_common import get_session_dir
        uid = "u-clear"
        sd = get_session_dir(uid)
        _write_codex_thread_id(sd, "tid-x")
        mark_initialized(sd, backend="codex")
        reply = await handle_codex_slash_command("/clear", uid)
        self.assertIn("对话已重置", reply)
        self.assertEqual(_read_codex_thread_id(sd), "")
        self.assertFalse(os.path.exists(os.path.join(sd, ".initialized.codex")))

    async def test_model_validated_stores(self):
        """Known model in live list → switch succeeds and prefs are written."""
        from wechatbridge.codex import handle_codex_slash_command
        from wechatbridge.runner_common import load_prefs
        from wechatbridge import codex as codex_mod

        with mock.patch.object(
            codex_mod,
            "_fetch_codex_model_list",
            new=AsyncMock(return_value=["gpt-5.1-codex", "gpt-5.1", "gpt-5"]),
        ):
            reply = await handle_codex_slash_command("/model gpt-5.1-codex", "u-model")
        self.assertIn("模型已切换", reply)
        self.assertNotIn("未校验", reply)
        self.assertEqual(load_prefs("u-model")["model"], "gpt-5.1-codex")

    async def test_model_unknown_rejects(self):
        """Unknown model → refuse and prefs unchanged."""
        from wechatbridge.codex import handle_codex_slash_command
        from wechatbridge.runner_common import load_prefs, save_prefs
        from wechatbridge import codex as codex_mod

        uid = "u-model-unknown"
        save_prefs(uid, {"model": "gpt-5.1", "effort": "", "mode": ""})
        with mock.patch.object(
            codex_mod,
            "_fetch_codex_model_list",
            new=AsyncMock(return_value=["gpt-5.1-codex", "gpt-5.1"]),
        ):
            reply = await handle_codex_slash_command("/model not-a-real-model", uid)
        self.assertIn("模型不存在", reply)
        self.assertNotIn("模型已切换", reply)
        self.assertEqual(load_prefs(uid)["model"], "gpt-5.1")

    async def test_model_list_fail_rejects(self):
        """List fetch failure → refuse and do not write prefs (strict)."""
        from wechatbridge.codex import handle_codex_slash_command
        from wechatbridge.runner_common import load_prefs, save_prefs
        from wechatbridge import codex as codex_mod

        uid = "u-model-listfail"
        save_prefs(uid, {"model": "gpt-5", "effort": "", "mode": ""})
        with mock.patch.object(
            codex_mod,
            "_fetch_codex_model_list",
            new=AsyncMock(return_value=None),
        ):
            reply = await handle_codex_slash_command("/model gpt-5.1-codex", uid)
        self.assertIn("无法获取模型列表", reply)
        self.assertNotIn("模型已切换", reply)
        self.assertEqual(load_prefs(uid)["model"], "gpt-5")

    async def test_model_prefix_match(self):
        """Prefix match takes the first hit (same as agy)."""
        from wechatbridge.codex import handle_codex_slash_command
        from wechatbridge.runner_common import load_prefs
        from wechatbridge import codex as codex_mod

        with mock.patch.object(
            codex_mod,
            "_fetch_codex_model_list",
            new=AsyncMock(return_value=["gpt-5.1-codex", "gpt-5.1", "gpt-5"]),
        ):
            reply = await handle_codex_slash_command("/model gpt-5.1", "u-model-prefix")
        # exact "gpt-5.1" is in list → exact match wins over longer prefixes
        self.assertIn("模型已切换", reply)
        self.assertEqual(load_prefs("u-model-prefix")["model"], "gpt-5.1")

        with mock.patch.object(
            codex_mod,
            "_fetch_codex_model_list",
            new=AsyncMock(return_value=["gpt-5.1-codex", "gpt-5-codex"]),
        ):
            reply2 = await handle_codex_slash_command("/model gpt-5.1", "u-model-prefix2")
        self.assertIn("模型已切换", reply2)
        self.assertEqual(load_prefs("u-model-prefix2")["model"], "gpt-5.1-codex")

    async def test_model_empty_shows_current(self):
        from wechatbridge.codex import handle_codex_slash_command
        from wechatbridge.runner_common import save_prefs

        save_prefs("u-model-empty", {"model": "gpt-5.1-codex", "effort": "", "mode": ""})
        reply = await handle_codex_slash_command("/model", "u-model-empty")
        self.assertIn("当前模型", reply)
        self.assertIn("gpt-5.1-codex", reply)
        self.assertNotIn("不会校验", reply)
        self.assertNotIn("未校验", reply)

    async def test_models_live_list(self):
        """/models under mock returns the live list."""
        from wechatbridge.codex import handle_codex_slash_command
        from wechatbridge import codex as codex_mod

        with mock.patch.object(
            codex_mod,
            "_fetch_codex_model_list",
            new=AsyncMock(return_value=["gpt-5.1-codex", "gpt-5.1"]),
        ):
            reply = await handle_codex_slash_command("/models", "u-models")
        self.assertIn("gpt-5.1-codex", reply)
        self.assertIn("gpt-5.1", reply)
        self.assertIn("debug models", reply)

    async def test_models_fallback_builtin(self):
        """/models falls back to built-in note when live list fails."""
        from wechatbridge.codex import handle_codex_slash_command
        from wechatbridge import codex as codex_mod

        with mock.patch.object(
            codex_mod,
            "_fetch_codex_model_list",
            new=AsyncMock(return_value=None),
        ):
            reply = await handle_codex_slash_command("/models", "u-models-fb")
        self.assertIn("参考", reply)
        self.assertIn("gpt-5.1-codex", reply)

    async def test_parse_codex_models_json_and_lines(self):
        from wechatbridge.codex import _parse_codex_models

        j = json.dumps({
            "models": [
                {"slug": "gpt-5.1-codex"},
                {"slug": "gpt-5"},
            ]
        })
        self.assertEqual(_parse_codex_models(j), ["gpt-5.1-codex", "gpt-5"])

        nested = json.dumps({
            "models": [{"slug": "a"}, {"id": "b", "name": "ignored-without-slug-only"}]
        })
        # models[] item with slug + item with id (no slug) both accepted
        self.assertEqual(_parse_codex_models(nested), ["a", "b"])

        # bare string list
        self.assertEqual(_parse_codex_models(json.dumps(["m1", "m2"])), ["m1", "m2"])

        lines = "* gpt-5.1-codex (default)\n- gpt-5\n"
        self.assertEqual(_parse_codex_models(lines), ["gpt-5.1-codex", "gpt-5"])

        self.assertEqual(_parse_codex_models(""), [])
        self.assertEqual(_parse_codex_models("{}"), [])

    async def test_fast_and_planning(self):
        from wechatbridge.codex import handle_codex_slash_command
        from wechatbridge.runner_common import load_prefs
        await handle_codex_slash_command("/fast", "u-fp")
        self.assertEqual(load_prefs("u-fp")["effort"], "low")
        await handle_codex_slash_command("/planning", "u-fp")
        self.assertEqual(load_prefs("u-fp")["mode"], "plan")

    async def test_persona_write_and_show(self):
        from wechatbridge.codex import handle_codex_slash_command, _persona_path
        from wechatbridge.runner_common import get_session_dir
        reply = await handle_codex_slash_command("/persona you are terse", "u-persona")
        self.assertIn("人格文档已更新", reply)
        sd = get_session_dir("u-persona")
        # AGENTS.md 是 codex 自动读取的 persona 文件
        path = _persona_path(sd)
        self.assertTrue(os.path.isfile(path))
        reply2 = await handle_codex_slash_command("/persona show", "u-persona")
        self.assertIn("you are terse", reply2)

    async def test_passthrough_returns_none(self):
        from wechatbridge.codex import handle_codex_slash_command
        self.assertIsNone(await handle_codex_slash_command("/notaslash hi", "u-any"))

    async def test_disabled_cmds(self):
        from wechatbridge.codex import handle_codex_slash_command
        self.assertIn("禁用", await handle_codex_slash_command("/exit", "u"))
        self.assertIn("微信端不支持", await handle_codex_slash_command("/resume", "u"))


class TestCodexRunFakeCli(unittest.IsolatedAsyncioTestCase):
    """Integration: run run_codex against the fake codex CLI.

    Uses a real subprocess, so it exercises command building, thread_id
    persistence, resume, fallback retry, timeout, and /clear end-to-end.
    config.codex_binary_path points at a shim that exec's python on fake_codex.py.
    """

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(lambda: _rmtree(self.td))
        from wechatbridge.config import config
        self._patchers = []
        p = mock.patch.object(config, "session_base_dir", self.td)
        p.start()
        self._patchers.append(p)
        p2 = mock.patch.object(config, "agy_timeout", 60)
        p2.start()
        self._patchers.append(p2)
        # 写一个 shim，subprocess 跑 shim 时 exec 到 python fake_codex.py
        self.shim = os.path.join(self.td, "codex-shim.py")
        with open(self.shim, "w", encoding="utf-8") as f:
            f.write(
                "#!/usr/bin/env python3\n"
                "import sys, os\n"
                f"sys.argv[0] = {_FAKE_CODEX!r}\n"
                "sys.argv.insert(0, sys.executable)\n"
                "os.execv(sys.executable, sys.argv)\n"
            )
        os.chmod(self.shim, 0o755)
        p3 = mock.patch.object(config, "codex_binary_path", self.shim)
        p3.start()
        self._patchers.append(p3)

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def _run(self, prompt, uid, timeout=None, mode="ok", log_path=None):
        import wechatbridge.codex as codex_mod
        env = dict(os.environ)
        env["FAKE_CODEX_MODE"] = mode
        if log_path is not None:
            env["FAKE_CODEX_LOG"] = log_path
        with mock.patch.dict(os.environ, env, clear=False):
            return asyncio.run(codex_mod.run_codex(prompt, uid, timeout=timeout))

    def test_first_then_resume(self):
        # 首轮：生成 thread_id 并落盘
        display, artifacts = self._run("hi", "u1")
        self.assertIn("first(", display)
        sd = os.path.join(self.td, _sanitize("u1"))
        self.assertTrue(os.path.isfile(os.path.join(sd, ".codex_thread_id")))
        self.assertTrue(os.path.isfile(os.path.join(sd, ".initialized.codex")))
        tid = _read_tid(sd)
        self.assertTrue(tid)
        # 续轮：带 resume，回复以 resumed 开头
        display2, _ = self._run("again", "u1")
        self.assertIn("resumed(", display2)
        # thread_id 不变
        self.assertEqual(_read_tid(sd), tid)

    def test_resume_fail_falls_back_to_first(self):
        # 首轮成功
        self._run("hi", "u2")
        sd = os.path.join(self.td, _sanitize("u2"))
        # 续轮失败（resume not found）-> 降级首轮重试成功
        display, _ = self._run("again", "u2", mode="resume_fail")
        self.assertIn("first(", display)
        # 重试后 thread_id 被重写
        self.assertTrue(_read_tid(sd))

    def test_fail_first_returns_error(self):
        display, artifacts = self._run("hi", "u3", mode="fail_first")
        self.assertTrue(display.startswith("❌"))
        self.assertEqual(artifacts, [])

    def test_timeout(self):
        display, _ = self._run("hi", "u4", timeout=1, mode="timeout")
        self.assertIn("超时", display)

    def test_clear_wipes_thread_id(self):
        self._run("hi", "u5")
        sd = os.path.join(self.td, _sanitize("u5"))
        self.assertTrue(os.path.isfile(os.path.join(sd, ".codex_thread_id")))
        from wechatbridge.codex import handle_codex_slash_command
        asyncio.run(handle_codex_slash_command("/clear", "u5"))
        self.assertFalse(os.path.isfile(os.path.join(sd, ".codex_thread_id")))
        # clear 后再发 → 当作首轮（新 thread_id）
        display, _ = self._run("hi again", "u5")
        self.assertIn("first(", display)

    def test_resume_fail_stdout_triggers_retry(self):
        # 防御性测试（非典型真实路径）：真实 Codex 的 resume-not-found 主要在
        # JSONL 启动前写 stderr。本用例仅在续轮让 stderr 仅含无关 warning、真实
        # 文案错误出现于 stdout 的 JSONL error 事件（"no rollout found for thread
        # id"），returncode=1，验证 bridge 对未来/异常 stdout JSONL error 的防御性
        # 兼容：合并 stderr+display+stdout 后仍能识别，降级首轮重试成功。
        self._run("hi", "u-resume-stdout")
        sd = os.path.join(self.td, _sanitize("u-resume-stdout"))
        self.assertTrue(os.path.isfile(os.path.join(sd, ".codex_thread_id")))
        # 续轮：stderr 仅无关 warning，stdout JSONL error="session not found"，returncode=1
        # 合并 stderr+display+stdout 后仍能识别，降级首轮重试成功
        display, _ = self._run("again", "u-resume-stdout", mode="resume_fail_stdout")
        self.assertIn("first(", display)
        # 重试后 thread_id 被重写
        self.assertTrue(_read_tid(sd))

    def test_resume_turn_fail_no_fallback(self):
        # 首轮成功（写 rollout + thread_id），使续轮走 resume
        self._run("hi", "u-turn-fail")
        sd = os.path.join(self.td, _sanitize("u-turn-fail"))
        old_tid = _read_tid(sd)
        self.assertTrue(old_tid)
        # 续轮：thread.started 正常输出，但随后 turn.failed("rate limit exceeded")，
        # returncode=1。这是普通限流错误，不是 resume/session 错误，不得 fallback，
        # 旧 thread_id 必须保留，且只运行一次（无重试）。
        # 限流最终用户文案用 🔔（不是 ❌）。
        log = os.path.join(self.td, "inv-turn-fail.log")
        display, _ = self._run("again", "u-turn-fail", mode="resume_turn_fail", log_path=log)
        self.assertTrue(display.startswith("🔔"), msg=display[:80])
        self.assertIn("**请求较多**", display)
        # 旧 thread_id 保留
        self.assertEqual(_read_tid(sd), old_tid)
        # 只运行一次（无降级重试）
        with open(log, "r", encoding="utf-8") as f:
            self.assertEqual(len([l for l in f.read().splitlines() if l.strip()]), 1)

    def test_resume_missing_credentials_no_fallback(self):
        # 续轮普通 turn.failed（missing credentials）：不是 resume/session 错误，
        # 不得 fallback，旧 thread_id 必须保留，且只运行一次（无重试）。
        self._run("hi", "u-miss-cred")
        sd = os.path.join(self.td, _sanitize("u-miss-cred"))
        old_tid = _read_tid(sd)
        self.assertTrue(old_tid)
        log = os.path.join(self.td, "inv-miss-cred.log")
        display, _ = self._run("again", "u-miss-cred", mode="resume_missing_credentials", log_path=log)
        self.assertTrue(display.startswith("❌"))
        # 旧 thread_id 保留（不得误清，否则会丢失可续轮的会话）
        self.assertEqual(_read_tid(sd), old_tid)
        # 只运行一次（无降级重试）
        with open(log, "r", encoding="utf-8") as f:
            self.assertEqual(len([l for l in f.read().splitlines() if l.strip()]), 1)

    def test_resume_file_not_found_no_fallback(self):
        # 续轮 stderr 报 file not found：不是 session 错误，不得 fallback，
        # 旧 thread_id 保留，只运行一次。
        self._run("hi", "u-file-nf")
        sd = os.path.join(self.td, _sanitize("u-file-nf"))
        old_tid = _read_tid(sd)
        self.assertTrue(old_tid)
        log = os.path.join(self.td, "inv-file-nf.log")
        display, _ = self._run("again", "u-file-nf", mode="resume_file_not_found", log_path=log)
        self.assertTrue(display.startswith("❌"))
        self.assertEqual(_read_tid(sd), old_tid)
        with open(log, "r", encoding="utf-8") as f:
            self.assertEqual(len([l for l in f.read().splitlines() if l.strip()]), 1)

    def test_resume_permission_denied_no_fallback(self):
        # 续轮普通 turn.failed（permission denied）：不是 resume/session 错误，
        # 不得 fallback，旧 thread_id 保留，只运行一次。
        self._run("hi", "u-perm-den")
        sd = os.path.join(self.td, _sanitize("u-perm-den"))
        old_tid = _read_tid(sd)
        self.assertTrue(old_tid)
        log = os.path.join(self.td, "inv-perm-den.log")
        display, _ = self._run("again", "u-perm-den", mode="resume_permission_denied", log_path=log)
        self.assertTrue(display.startswith("❌"))
        self.assertEqual(_read_tid(sd), old_tid)
        with open(log, "r", encoding="utf-8") as f:
            self.assertEqual(len([l for l in f.read().splitlines() if l.strip()]), 1)

    def test_first_turn_failure(self):
        # 首轮普通 turn.failed（模型过载）：返回 ❌ 错误，不 fallback（本就是首轮），
        # 不写 thread_id。
        display, artifacts = self._run("hi", "u-first-turn-fail", mode="turn_fail")
        self.assertTrue(display.startswith("❌"))
        self.assertEqual(artifacts, [])
        sd = os.path.join(self.td, _sanitize("u-first-turn-fail"))
        self.assertFalse(os.path.isfile(os.path.join(sd, ".codex_thread_id")))

    def test_emoji_reply_exit0_persists_thread_id(self):
        # agent 回复文本以 ❌ 开头、退出码 0：必须是成功（不以 ❌ 判失败），
        # 且落盘 thread_id / 标记已初始化。
        display, artifacts = self._run("hi", "u-emoji-ok", mode="ok_emoji")
        # 文本确实以 ❌ 开头
        self.assertTrue(display.startswith("❌"))
        self.assertEqual(artifacts, [])
        # 但这是成功（不是 ❌ **执行出错** 气泡），且持久化了 thread_id
        self.assertNotIn("**执行出错**", display)
        sd = os.path.join(self.td, _sanitize("u-emoji-ok"))
        self.assertTrue(os.path.isfile(os.path.join(sd, ".codex_thread_id")))
        self.assertTrue(os.path.isfile(os.path.join(sd, ".initialized.codex")))
        self.assertTrue(_read_tid(sd))

    def test_structured_turn_failed_exit0_no_persist(self):
        # 零退出但结构化 turn.failed：仍判定为失败，且不落盘 thread_id。
        display, artifacts = self._run("hi", "u-tf0", mode="ok_turn_failed")
        self.assertTrue(display.startswith("❌"))
        self.assertEqual(artifacts, [])
        sd = os.path.join(self.td, _sanitize("u-tf0"))
        self.assertFalse(os.path.isfile(os.path.join(sd, ".codex_thread_id")))
        self.assertFalse(os.path.isfile(os.path.join(sd, ".initialized.codex")))

    def test_structured_error_event_exit0_no_persist(self):
        # 零退出但结构化 error 事件：仍判定为失败，且不落盘 thread_id。
        display, artifacts = self._run("hi", "u-err0", mode="ok_error")
        self.assertTrue(display.startswith("❌"))
        self.assertEqual(artifacts, [])
        sd = os.path.join(self.td, _sanitize("u-err0"))
        self.assertFalse(os.path.isfile(os.path.join(sd, ".codex_thread_id")))
        self.assertFalse(os.path.isfile(os.path.join(sd, ".initialized.codex")))


    # --- Fallback scan tests for shell-created artifacts ---

    def test_shell_file_fallback_collects_new_files(self):
        """ok_shell_files: new file created during execution is collected by fallback."""
        display, artifacts = self._run("hi", "u-shell-new", mode="ok_shell_files")
        self.assertIn("first(", display)
        names = {n for n, _ in artifacts}
        self.assertIn("result.txt", names, "fallback should collect new file")
        self.assertNotIn("out.md", names, "old file should NOT be collected")
        # All artifact paths must be absolute and under session_dir
        for _n, ap in artifacts:
            self.assertTrue(os.path.isabs(ap))
            sd = os.path.join(self.td, _sanitize("u-shell-new"))
            self.assertTrue(ap.startswith(sd))

    def test_shell_file_fallback_dedup_with_file_change(self):
        """ok_shell_and_file_change: file_change is authoritative, no duplicate."""
        display, artifacts = self._run("hi", "u-shell-dedup", mode="ok_shell_and_file_change")
        names = {n for n, _ in artifacts}
        # file_change collected result.txt; fallback should NOT add a duplicate
        self.assertEqual(names.count("result.txt") if hasattr(names, 'count')
                         else len([n for n in artifacts if n[0] == "result.txt"]),
                         len([n for n in artifacts if n[0] == "result.txt"]))
        # Exactly one result.txt in artifacts (from file_change, not fallback)
        result_entries = [a for a in artifacts if a[0] == "result.txt"]
        self.assertEqual(len(result_entries), 1, "no duplicate from fallback")

    def test_shell_pdf_merged_with_file_change(self):
        """file_change for helper.py plus a shell-written report.pdf — both return."""
        display, artifacts = self._run("hi", "u-shell-mix", mode="ok_shell_mixed_docs")
        self.assertEqual(display, "done")
        names = {n for n, _ in artifacts}
        self.assertIn("helper.py", names)
        self.assertIn("report.pdf", names)
        self.assertEqual(len([a for a in artifacts if a[0] == "helper.py"]), 1)
        self.assertEqual(len([a for a in artifacts if a[0] == "report.pdf"]), 1)

    def test_shell_overwrite_existing_pdf(self):
        """Rewriting a pre-existing pdf must still collect it."""
        from wechatbridge.runner_common import sanitize_user_id

        uid = "u-shell-ow"
        sd = os.path.join(self.td, sanitize_user_id(uid))
        os.makedirs(sd)
        pdf = os.path.join(sd, "report.pdf")
        with open(pdf, "wb") as f:
            f.write(b"%PDF-old")
        os.utime(pdf, (1_000_000, 1_000_000))
        display, artifacts = self._run("hi", uid, mode="ok_shell_mixed_docs")
        self.assertEqual(display, "done")
        names = {n for n, _ in artifacts}
        self.assertIn("report.pdf", names)
        with open(pdf, "rb") as f:
            self.assertEqual(f.read(), b"%PDF-1.4")

    def test_shell_file_fallback_old_files_not_collected(self):
        """ok_shell_old_files: files with old mtimes are NOT collected."""
        display, artifacts = self._run("hi", "u-shell-old", mode="ok_shell_old_files")
        self.assertIn("first(", display)
        names = {n for n, _ in artifacts}
        self.assertNotIn("old_result.txt", names, "old file should not be collected")
        self.assertNotIn("old_output.md", names, "old file should not be collected")

    def test_shell_file_fallback_internal_metadata_excluded(self):
        """ok_shell_internal_metadata: .codex/ and .git/ files are excluded."""
        display, artifacts = self._run("hi", "u-shell-meta", mode="ok_shell_internal_metadata")
        self.assertIn("first(", display)
        names = {n for n, _ in artifacts}
        self.assertIn("real_output.txt", names, "real file should be collected")
        self.assertNotIn("meta.json", names, ".codex/ metadata should be excluded")
        self.assertNotIn("config", names, ".git/ file should be excluded")
        # No path should contain .codex or .git as a directory component
        for _n, ap in artifacts:
            parts = ap.split(os.sep)
            self.assertNotIn(".codex", parts)
            self.assertNotIn(".git", parts)

    def test_shell_file_fallback_symlink_escape_excluded(self):
        """ok_shell_escape: symlink pointing outside allowed roots is NOT collected."""
        display, artifacts = self._run("hi", "u-shell-escape", mode="ok_shell_escape")
        self.assertIn("first(", display)
        names = {n for n, _ in artifacts}
        self.assertNotIn("escaped.txt", names, "symlink escape should be excluded")
        # Cleanup target
        try:
            os.unlink("/tmp/_codex_escape_test_target.txt")
        except OSError:
            pass

    def test_fallback_realpath_alias_dedup(self):
        """Symlink alias to a file collected via file_change: same realpath -> no duplicate."""
        import os, sys, json, uuid, datetime
        target = os.path.join(self.td, "_dedup_target.txt")
        with open(target, "w") as f:
            f.write("content")
        link = os.path.join(self.td, "_dedup_alias.txt")
        os.symlink(target, link)

        # Build a custom shim that: emits file_change for the alias path AND writes the real file.
        fake = os.path.join(self.td, "_fake_dedup.py")
        with open(fake, "w") as f:
            f.write(
                '#!/usr/bin/env python3\n'
                'import json, os, sys, uuid, datetime\n'
                'session_dir = os.getcwd()\n'
                'new_id = str(uuid.uuid4())\n'
                'now = datetime.datetime.now()\n'
                'ts = now.strftime("%Y-%m-%dT%H-%M-%S")\n'
                'rollout_dir = os.path.join(session_dir, ".codex", "sessions",\n'
                '    now.strftime("%Y"), now.strftime("%m"), now.strftime("%d"))\n'
                'os.makedirs(rollout_dir, exist_ok=True)\n'
                'with open(os.path.join(rollout_dir, f"rollout-{ts}-{new_id}.jsonl"), "w") as fo:\n'
                '    fo.write(json.dumps({"type": "session_meta", "payload": {"id": new_id}}) + "\\n")\n'
                '    fo.write(json.dumps({"type": "thread.started", "thread_id": new_id}) + "\\n")\n'
                'lines = [\n'
                '    json.dumps({"type": "thread.started", "thread_id": new_id}),\n'
                '    json.dumps({"type": "turn.started"}),\n'
                '    json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "done"}}),\n'
                '    json.dumps({"type": "item.completed", "item": {\n'
                '        "id": "i2", "type": "file_change", "status": "completed",\n'
                '        "changes": [{"kind": "add", "path": "_dedup_alias.txt"}]\n'
                '    }}),\n'
                '    json.dumps({"type": "turn.completed", "usage": {}}),\n'
                ']\n'
                'sys.stdout.write("\\n".join(lines) + "\\n")\n'
                'sys.stdout.flush()\n'
                'with open(os.path.join(session_dir, "_dedup_target.txt"), "w") as fo:\n'
                '    fo.write("content")\n'
                'sys.exit(0)\n'
            )
        os.chmod(fake, 0o755)
        shim = os.path.join(self.td, "_shim_dedup.py")
        with open(shim, "w") as f:
            f.write(
                "#!/usr/bin/env python3\n"
                "import sys, os\n"
                f"sys.argv[0] = {fake!r}\n"
                "sys.argv.insert(0, sys.executable)\n"
                "os.execv(sys.executable, sys.argv)\n"
            )
        os.chmod(shim, 0o755)
        from wechatbridge.config import config
        orig = config.codex_binary_path
        config.codex_binary_path = shim
        try:
            display, artifacts = self._run("hi", "u-dedup-realpath")
            result_entries = [a for a in artifacts if a[0] == "_dedup_target.txt"]
            self.assertEqual(len(result_entries), 1, "no duplicate from fallback for realpath alias")
        finally:
            config.codex_binary_path = orig

    def test_add_dirs_retry_argv_consistent(self):
        """Retry argv uses same validated add_dirs list as first run."""
        import os, datetime
        sd = os.path.join(self.td, _sanitize("u-retry-add"))
        os.makedirs(sd, exist_ok=True)
        from wechatbridge.codex import (
            ensure_user_codex, mark_initialized, _write_codex_thread_id,
            _has_codex_session,
        )
        mark_initialized(sd, backend="codex")
        tid = "aabbccdd-0000-0000-0000-000000000000"
        _write_codex_thread_id(sd, tid)
        now = datetime.datetime.now()
        rollout_dir = os.path.join(
            sd, ".codex", "sessions", now.strftime("%Y"),
            now.strftime("%m"), now.strftime("%d"),
        )
        os.makedirs(rollout_dir, exist_ok=True)
        with open(os.path.join(rollout_dir, f"rollout-x-{tid}.jsonl"), "w") as f:
            f.write("x")
        self.assertTrue(_has_codex_session(sd, tid))

        from wechatbridge.runner_common import save_prefs
        save_prefs("u-retry-add", {"add_dirs": [sd]})

        captured = []
        from wechatbridge import codex as codex_mod

        async def spawn(*args, **kwargs):
            captured.append(list(args))
            cmd_str = str(args)
            if "resume" in cmd_str:
                from tests.test_codex import _FakeProc
                return _FakeProc("", "error: session not found", 1)
            from tests.test_codex import _FakeProc
            return _FakeProc(
                '{"type":"thread.started","thread_id":"new-tid"}\n'
                '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n',
                "", 0,
            )

        with mock.patch("asyncio.create_subprocess_exec", side_effect=spawn):
            display, _ = asyncio.run(codex_mod.run_codex("hi", "u-retry-add"))
        self.assertEqual(len(captured), 2)
        self.assertIn("--add-dir", captured[0])
        self.assertIn(sd, captured[0])
        self.assertIn("--add-dir", captured[1])
        self.assertIn(sd, captured[1])

    def test_fallback_scan_file_limit(self):
        """When session_dir has >200 regular files, fallback scan returns empty."""
        sd = os.path.join(self.td, _sanitize("u-file-limit"))
        os.makedirs(sd, exist_ok=True)
        for i in range(205):
            with open(os.path.join(sd, f"f_{i}.txt"), "w") as f:
                f.write("x")
        from wechatbridge.codex import _snapshot_regular_files
        snap = _snapshot_regular_files([sd])
        self.assertEqual(snap, {}, ">200 files -> empty")

    def test_fallback_scan_dir_limit(self):
        """When session_dir has >50 subdirectories, fallback scan returns empty."""
        sd = os.path.join(self.td, _sanitize("u-dir-limit"))
        os.makedirs(sd, exist_ok=True)
        for i in range(55):
            d = os.path.join(sd, f"dir_{i}")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "file.txt"), "w") as f:
                f.write("x")
        from wechatbridge.codex import _snapshot_regular_files
        snap = _snapshot_regular_files([sd])
        self.assertEqual(snap, {}, ">50 dirs -> empty")

    def test_fallback_scan_timeout(self):
        """When snapshot takes >2s, fallback scan returns empty."""
        from wechatbridge import codex as codex_mod
        original = codex_mod._snapshot_regular_files

        def slow_snapshot(roots):
            import time
            time.sleep(2.5)
            return {"/fake/file.txt": 1.0}

        codex_mod._snapshot_regular_files = slow_snapshot
        try:
            display, artifacts = self._run("hi", "u-scan-timeout", mode="ok")
            self.assertEqual(artifacts, [], "slow snapshot -> empty")
        finally:
            codex_mod._snapshot_regular_files = original

    def test_fallback_scan_not_on_failure(self):
        """Fallback scan must NOT run when process fails."""
        display, artifacts = self._run("hi", "u-fail-noscan", mode="fail_first")
        self.assertTrue(display.startswith("❌"))
        self.assertEqual(artifacts, [])

    def test_fallback_scan_not_on_timeout(self):
        """Fallback scan must NOT run when process times out."""
        from wechatbridge import codex as codex_mod
        display, artifacts = self._run("hi", "u-to-noscan", timeout=1, mode="timeout")
        self.assertIn("超时", display)
        self.assertEqual(artifacts, [])

    def test_add_dirs_consistency_between_build_and_scan(self):
        """add_dirs in command builder and fallback scan use same validated paths."""
        from wechatbridge import codex as codex_mod

        # Create a real directory that validate_add_dir would accept
        add_dir = os.path.join(self.td, "validated_workdir")
        os.makedirs(add_dir, exist_ok=True)
        # Write a file in add_dir with old mtime (should NOT be collected)
        old_file = os.path.join(add_dir, "old_in_add.txt")
        with open(old_file, "w") as f:
            f.write("old")
        os.utime(old_file, (1000000, 1000000))

        # Mock validate_add_dir to return the resolved path (simulates valid dir)
        with mock.patch("wechatbridge.codex.validate_add_dir", return_value=(True, add_dir)):
            # Manually set prefs with raw add_dir to test resolution
            from wechatbridge.runner_common import save_prefs, get_session_dir
            save_prefs("u-add-consistency", {"add_dirs": [add_dir]})

            display, artifacts = self._run("hi", "u-add-consistency")
            self.assertIn("first(", display)
            # old_in_add.txt should NOT be in artifacts (old mtime)
            names = {n for n, _ in artifacts}
            self.assertNotIn("old_in_add.txt", names)

            # Verify the command used the resolved path
            sd = os.path.join(self.td, _sanitize("u-add-consistency"))
            self.assertTrue(os.path.isdir(sd))


class TestCodexEnvOverride(unittest.IsolatedAsyncioTestCase):
    """CODEX_HOME must be overridden to session_dir/.codex, beating any global
    CODEX_HOME from the service environment. The retry reuses the same env.

    Every test captures the real ``create_subprocess_exec`` calls (argv + kwargs)
    so we can assert argv tokens, cwd, env, and stdin=DEVNULL. Environment patches
    are wrapped in mock.patch.dict / mock.patch and therefore auto-restored, so
    test order is never polluted.
    """

    OK_JSONL = (
        '{"type":"thread.started","thread_id":"tid-env-1"}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"hi there"}}\n'
    )

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(lambda: _rmtree(self.td))
        from wechatbridge.config import config
        self._patchers = []
        p = mock.patch.object(config, "session_base_dir", self.td)
        p.start(); self._patchers.append(p)
        p2 = mock.patch.object(config, "agy_timeout", 60)
        p2.start(); self._patchers.append(p2)
        p3 = mock.patch.object(config, "codex_binary_path", "codex")
        p3.start(); self._patchers.append(p3)

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    async def _run_with_spawn(self, spawn, user_id="u-env"):
        from wechatbridge import codex as codex_mod
        with mock.patch.dict(
            os.environ, {"CODEX_HOME": "/global/codex-home"}, clear=False
        ), mock.patch("asyncio.create_subprocess_exec", side_effect=spawn):
            return await codex_mod.run_codex("hello", user_id)

    async def test_codex_home_overrides_global(self):
        captured = []

        async def spawn(*args, **kwargs):
            captured.append((list(args), kwargs))
            return _FakeProc(self.OK_JSONL, "", 0)

        display, _ = await self._run_with_spawn(spawn)
        self.assertEqual(display, "hi there")
        self.assertEqual(len(captured), 1)
        argv, kwargs = captured[0]
        sd = os.path.join(self.td, _sanitize("u-env"))

        # 首轮 argv 含 exec/json/skip-git/yolo/prompt
        self.assertIn("exec", argv)
        self.assertIn("--json", argv)
        self.assertIn("--skip-git-repo-check", argv)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertEqual(argv[-1], "hello")  # prompt 是末尾位置参数

        # cwd / env / stdin
        self.assertEqual(kwargs["cwd"], sd)
        env = kwargs["env"]
        self.assertEqual(env["CODEX_HOME"], os.path.join(sd, ".codex"))
        self.assertNotEqual(env["CODEX_HOME"], "/global/codex-home")
        self.assertEqual(env["HOME"], sd)
        self.assertIs(kwargs.get("stdin"), asyncio.subprocess.DEVNULL)

    async def test_retry_reuses_same_env_with_override(self):
        # 让首轮（resume）失败，触发降级首轮重试；两次 spawn 必须复用同一 env
        # 对象，且 CODEX_HOME 都指向 session_dir/.codex，第二次 argv 不含 resume，
        # 两次 stdin 都是 DEVNULL。
        captured = []

        async def spawn(*args, **kwargs):
            cmd = list(args)
            captured.append((cmd, kwargs))
            if "resume" in cmd:
                return _FakeProc("", "error: session not found", 1)
            return _FakeProc(self.OK_JSONL, "", 0)

        # 预置 initialized + thread_id + rollout，使首轮走 resume
        from wechatbridge.codex import (
            ensure_user_codex, mark_initialized, _write_codex_thread_id,
            _has_codex_session,
        )
        import datetime
        sd = ensure_user_codex("u-env-retry")
        mark_initialized(sd, backend="codex")
        tid = "feedface-0000-0000-0000-000000000000"
        _write_codex_thread_id(sd, tid)
        now = datetime.datetime.now()
        rollout_dir = os.path.join(
            sd, ".codex", "sessions", now.strftime("%Y"),
            now.strftime("%m"), now.strftime("%d"),
        )
        os.makedirs(rollout_dir, exist_ok=True)
        with open(os.path.join(rollout_dir, f"rollout-x-{tid}.jsonl"), "w") as f:
            f.write("x")
        self.assertTrue(_has_codex_session(sd, tid))

        display, _ = await self._run_with_spawn(spawn, user_id="u-env-retry")

        self.assertEqual(display, "hi there")
        self.assertEqual(len(captured), 2)  # resume 失败 + 重试首轮
        (a0, k0), (a1, k1) = captured

        # 首轮（resume）argv 含 resume / thread_id / prompt
        self.assertIn("resume", a0)
        self.assertIn(tid, a0)
        self.assertEqual(a0[-1], "hello")
        # 重试（首轮）argv 不再含 resume
        self.assertNotIn("resume", a1)
        self.assertEqual(a1[-1], "hello")

        # 两次都走 DEVNULL
        self.assertIs(k0.get("stdin"), asyncio.subprocess.DEVNULL)
        self.assertIs(k1.get("stdin"), asyncio.subprocess.DEVNULL)
        self.assertEqual(k0["cwd"], sd)
        self.assertEqual(k1["cwd"], sd)

        e0, e1 = k0["env"], k1["env"]
        # 复用同一 env 对象（retry 传 env=env）
        self.assertIs(e0, e1)
        self.assertEqual(e0["CODEX_HOME"], os.path.join(sd, ".codex"))
        self.assertEqual(e1["CODEX_HOME"], os.path.join(sd, ".codex"))
        self.assertNotEqual(e0["CODEX_HOME"], "/global/codex-home")

    async def test_codex_api_key_reinjected(self):
        # sanitize_env 会洗掉 CODEX_API_KEY；run_codex 必须显式回注到子进程 env。
        captured = []

        async def spawn(*args, **kwargs):
            captured.append((list(args), kwargs))
            return _FakeProc(self.OK_JSONL, "", 0)

        with mock.patch.dict(
            os.environ, {"CODEX_API_KEY": "sk-test-12345"}, clear=False
        ):
            display, _ = await self._run_with_spawn(spawn)
        self.assertEqual(display, "hi there")
        env = captured[0][1]["env"]
        self.assertEqual(env.get("CODEX_API_KEY"), "sk-test-12345")
        # 全局 CODEX_HOME 仍被会话私有目录覆盖
        sd = os.path.join(self.td, _sanitize("u-env"))
        self.assertEqual(env["CODEX_HOME"], os.path.join(sd, ".codex"))

    async def test_empty_prompt_rejected(self):
        # 空 / 全空白 prompt：在任何 session/env/subprocess 副作用前拒绝，
        # 不得启动进程，返回清晰错误 + 空 artifacts。
        from wechatbridge import codex as codex_mod
        launched = []

        async def spawn(*args, **kwargs):
            launched.append(1)
            return _FakeProc("", "", 1)

        with mock.patch("asyncio.create_subprocess_exec", side_effect=spawn):
            display, artifacts = await codex_mod.run_codex("   ", "u-empty")
        self.assertTrue(display.startswith("❌"))
        self.assertEqual(artifacts, [])
        self.assertEqual(launched, [])  # 进程未启动
        # 会话目录未被创建（无副作用）
        self.assertFalse(os.path.isdir(os.path.join(self.td, _sanitize("u-empty"))))


def _rmtree(path):
    import shutil
    try:
        shutil.rmtree(path)
    except OSError:
        pass


class _FakeProc:
    """Minimal stand-in for an asyncio subprocess used by env-override tests."""

    def __init__(self, stdout="", stderr="", rc=0, pid=9999):
        self._so = stdout.encode("utf-8")
        self._se = stderr.encode("utf-8")
        self.returncode = rc
        self.pid = pid

    async def communicate(self):
        return self._so, self._se


def _sanitize(user_id):
    from wechatbridge.runner_common import sanitize_user_id
    return sanitize_user_id(user_id)


def _read_tid(session_dir):
    from wechatbridge.codex import _read_codex_thread_id
    return _read_codex_thread_id(session_dir)


if __name__ == "__main__":
    unittest.main()
