"""Unit + integration tests for wechatbridge.pig (Magpie single-model backend)."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_FAKE_PIG = os.path.join(_HERE, "fake_pig.py")


def _rmtree(path):
    try:
        shutil.rmtree(path)
    except OSError:
        pass


def _uid(tag="pig"):
    return f"testuser_{tag}"


class _PigTestBase(unittest.TestCase):
    """Shared fixture: temp session_base_dir + pig_binary_path → fake_pig.py."""

    def setUp(self):
        from wechatbridge.config import config

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sessions_base = os.path.join(self.tmp.name, "sessions")
        os.makedirs(self.sessions_base)

        self._patchers = []
        for name, value in (
            ("session_base_dir", self.sessions_base),
            ("pig_binary_path", sys.executable),
            ("pig_timeout", 570),
            ("magpie_base_url", "https://magpie.hotkids.eu/v1"),
            ("magpie_api_key", "test-magpie-key-0123456789"),
            ("confirm_keywords", []),
        ):
            p = mock.patch.object(config, name, value)
            p.start()
            self._patchers.append(p)
        # fake_pig.py is invoked as `[python, fake_pig.py, ...]` — wrap the
        # binary path by prepending the script to argv in tests via a shim.
        self._env_patcher = mock.patch.dict(
            os.environ,
            {
                "WECHATBRIDGE_MAGPIE_API_KEY": "test-magpie-key-0123456789",
                "MAGPIE_API_KEY": "test-magpie-key-0123456789",
            },
            clear=False,
        )
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()
        for p in self._patchers:
            p.stop()

    def _session_dir(self, user_id=None):
        from wechatbridge.runner_common import get_session_dir
        return get_session_dir(user_id or _uid())

    def _pig_shim(self, env_extra=None):
        """Return an argv-prefix list [python, fake_pig.py] as binary path.

        config.pig_binary_path is a single executable; tests instead patch
        _spawn_pig to prepend the script. Simplest reliable approach: write
        a tiny shim shell script per test dir.
        """
        shim = os.path.join(self.tmp.name, "pigshim.sh")
        with open(shim, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexec " + sys.executable + " " + _FAKE_PIG + ' "$@"\n')
        os.chmod(shim, 0o755)
        return shim


class TestEnsureUserPig(_PigTestBase):
    def test_models_settings_isolation_and_perms(self):
        from wechatbridge import pig as pigmod

        session_dir = self._session_dir()
        os.makedirs(session_dir, exist_ok=True)
        agent_dir = pigmod.ensure_user_pig(session_dir)

        self.assertEqual(agent_dir, os.path.join(session_dir, ".pig", "agent"))
        for d in (
            os.path.join(session_dir, ".pig"),
            agent_dir,
            os.path.join(agent_dir, "sessions"),
        ):
            self.assertTrue(os.path.isdir(d), d)
            self.assertEqual(stat.S_IMODE(os.lstat(d).st_mode), 0o700, d)

        models_path = os.path.join(agent_dir, "models.json")
        settings_path = os.path.join(agent_dir, "settings.json")
        self.assertEqual(stat.S_IMODE(os.lstat(models_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.lstat(settings_path).st_mode), 0o600)

        with open(models_path, encoding="utf-8") as mf:
            models = json.load(mf)
        prov = models["providers"]["magpie"]
        self.assertEqual(prov["apiKey"], "$MAGPIE_API_KEY")
        self.assertEqual(prov["api"], "openai-responses")
        self.assertEqual(prov["baseUrl"], "https://magpie.hotkids.eu/v1")
        self.assertEqual(len(prov["models"]), 1)
        m = prov["models"][0]
        self.assertEqual(m["id"], "gemini-web/gemini-4-neon")
        self.assertNotIn("task", m["id"])
        self.assertEqual(m["input"], ["text", "image"])
        self.assertTrue(m["reasoning"])
        self.assertEqual(m["contextWindow"], 1000000)
        self.assertEqual(m["maxTokens"], 8192)
        self.assertEqual(m["thinkingLevelMap"]["low"], "low")
        self.assertEqual(m["thinkingLevelMap"]["high"], "high")
        self.assertIsNone(m["thinkingLevelMap"]["xhigh"])

        with open(settings_path, encoding="utf-8") as sf:
            settings = json.load(sf)
        self.assertEqual(settings["defaultProvider"], "magpie")
        self.assertEqual(settings["defaultModel"], "gemini-web/gemini-4-neon")
        self.assertFalse(settings["retry"]["enabled"])
        self.assertEqual(settings["retry"]["maxRetries"], 0)
        self.assertEqual(settings["retry"]["provider"]["maxRetries"], 0)
        self.assertEqual(settings["retry"]["provider"]["timeoutMs"], 570000)

        # 磁盘零明文密钥
        with open(models_path, "rb") as mf:
            raw = mf.read()
        self.assertNotIn(b"test-magpie-key", raw)

    def test_symlink_refused(self):
        from wechatbridge import pig as pigmod

        session_dir = self._session_dir()
        os.makedirs(session_dir, exist_ok=True)
        real_dir = os.path.join(self.tmp.name, "real_agent")
        os.makedirs(real_dir)
        pig_home = os.path.join(session_dir, ".pig")
        os.makedirs(pig_home)
        agent_link = os.path.join(pig_home, "agent")
        os.symlink(real_dir, agent_link)
        out = pigmod.ensure_user_pig(session_dir)
        # Fail-closed: returns "" so run_pig refuses to spawn
        self.assertEqual(out, "")
        # 拒绝穿越软链：不得在 real_dir 写入 models.json
        self.assertFalse(os.path.exists(os.path.join(real_dir, "models.json")))

    def test_symlinked_agent_dir_blocks_spawn(self):
        """MF-1: symlinked .pig/agent must short-circuit run_pig before any
        subprocess is spawned (no FAKE_PIG_DUMP file is ever written)."""
        import asyncio
        from wechatbridge import pig as pigmod
        from wechatbridge.config import config

        uid = _uid("nospawn")
        session_dir = self._session_dir(uid)
        os.makedirs(session_dir, exist_ok=True)
        real_dir = os.path.join(self.tmp.name, "real_agent2")
        os.makedirs(real_dir)
        os.makedirs(os.path.join(session_dir, ".pig"))
        os.symlink(real_dir, os.path.join(session_dir, ".pig", "agent"))

        dump = os.path.join(self.tmp.name, "dump.json")
        env = {"FAKE_PIG_MODE": "argv_dump", "FAKE_PIG_DUMP": dump}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(config, "pig_binary_path", self._pig_shim()):
            display, arts = asyncio.run(pigmod.run_pig("hi", uid, timeout=10))
        self.assertIn("初始化失败", display)
        self.assertFalse(os.path.exists(dump))
        self.assertEqual(arts, [])

    def test_symlinked_sessions_dir_blocks_spawn_and_chmod(self):
        """MF-1: sessions_dir symlink must not be chmod'ed through and must
        block the run entirely."""
        import asyncio
        from wechatbridge import pig as pigmod
        from wechatbridge.config import config

        uid = _uid("sesslink")
        session_dir = self._session_dir(uid)
        agent = os.path.join(session_dir, ".pig", "agent")
        os.makedirs(agent)
        target = os.path.join(self.tmp.name, "host_dir")
        os.makedirs(target)
        os.chmod(target, 0o755)
        os.symlink(target, os.path.join(agent, "sessions"))

        dump = os.path.join(self.tmp.name, "dump2.json")
        env = {"FAKE_PIG_MODE": "argv_dump", "FAKE_PIG_DUMP": dump}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(config, "pig_binary_path", self._pig_shim()):
            display, _ = asyncio.run(pigmod.run_pig("hi", uid, timeout=10))
        self.assertIn("初始化失败", display)
        self.assertFalse(os.path.exists(dump))
        # host target dir permission NOT rewritten to 0o700 via the link
        self.assertEqual(
            stat.S_IMODE(os.lstat(target).st_mode), 0o755
        )


class TestRuntimeEnv(_PigTestBase):
    def test_env_locks_both_pig_and_pi_dirs(self):
        from wechatbridge import pig as pigmod
        from wechatbridge.runner_common import sanitize_env

        session_dir = self._session_dir()
        env = {
            "PIG_USE_PI_DIRS": "1",
            "PIG_CODING_AGENT_SESSION_DIR": "/tmp/evil",
            "PI_CODING_AGENT_SESSION_DIR": "/tmp/evil",
            "PIG_PIGLET_NAME": "x",
            "PIG_PIGLET_PATH": "/tmp/evil",
            "HOME": "/root",
            "PATH": "/usr/bin",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            env2 = sanitize_env(session_dir)
            env2 = pigmod._apply_pig_runtime_env(env2, session_dir)

        agent = os.path.join(session_dir, ".pig", "agent")
        self.assertEqual(env2["PIG_HOME"], os.path.join(session_dir, ".pig"))
        self.assertEqual(env2["PIG_CODING_AGENT_DIR"], agent)
        self.assertEqual(env2["PI_CODING_AGENT_DIR"], agent)
        self.assertNotIn("PIG_USE_PI_DIRS", env2)
        self.assertNotIn("PIG_CODING_AGENT_SESSION_DIR", env2)
        self.assertNotIn("PI_CODING_AGENT_SESSION_DIR", env2)
        self.assertNotIn("PIG_PIGLET_NAME", env2)
        self.assertEqual(env2["PI_OFFLINE"], "1")
        self.assertEqual(env2["PI_SKIP_VERSION_CHECK"], "1")
        self.assertEqual(env2["GOMEMLIMIT"], "192MiB")
        self.assertEqual(env2["MAGPIE_API_KEY"], "test-magpie-key-0123456789")
        self.assertEqual(env2["HOME"], session_dir)


class TestCommandBuilder(_PigTestBase):
    def test_argv_fixed_flags_and_modes(self):
        from wechatbridge import pig as pigmod

        cmd = pigmod._build_pig_command(
            " hello", ["@/s/images/a.jpg"], {"mode": "", "effort": ""},
            use_continue=True, append_prompt_arg="/s/.pig/agent/runtime_prompt.txt",
        )
        joined = " ".join(cmd)
        for flag in (
            "--mode json", "-p", "--no-stdin", "--no-extensions", "--no-mcp",
            "--no-skills", "--no-prompt-templates", "--no-themes",
            "--no-context-files", "--no-approve",
            "--provider magpie", "--model gemini-web/gemini-4-neon",
            "--thinking high", "--tools read,bash,edit,write",
            "--system-prompt", "--append-system-prompt", "--continue",
        ):
            self.assertIn(flag, joined, flag)
        dashdash = cmd.index("--")
        self.assertEqual(cmd[dashdash + 1], "@/s/images/a.jpg")
        self.assertEqual(cmd[-1], " hello")

        cmd_plan = pigmod._build_pig_command(
            "x", [], {"mode": "plan", "effort": "low"},
            use_continue=False, append_prompt_arg="",
        )
        self.assertIn("read,grep,find,ls", cmd_plan)
        self.assertIn("low", cmd_plan)
        self.assertNotIn("--continue", cmd_plan)


class TestSplitPromptFiles(_PigTestBase):
    def _mk(self, session_dir, rel, data):
        p = os.path.join(session_dir, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        mode = "wb" if isinstance(data, bytes) else "w"
        with open(p, mode) as f:
            f.write(data)
        return p

    def test_image_text_binary_three_way_split(self):
        from wechatbridge import pig as pigmod

        session_dir = self._session_dir()
        os.makedirs(session_dir, exist_ok=True)
        jpg = self._mk(session_dir, "images/a.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 64)
        png = self._mk(
            session_dir, "images/b.png",
            b"\x89PNG\r\n\x1a\n"
            + (13).to_bytes(4, "big") + b"IHDR" + b"\x00" * 17
            + (4).to_bytes(4, "big") + b"IDAT" + b"\x00" * 8,
        )
        # APNG: PNG sig + legal IHDR + acTL before IDAT (ref/PiG layout)
        apng = self._mk(
            session_dir, "images/c.png",
            b"\x89PNG\r\n\x1a\n"
            + (13).to_bytes(4, "big") + b"IHDR" + b"\x00" * 17
            + (8).to_bytes(4, "big") + b"acTL" + b"\x00" * 12
            + (4).to_bytes(4, "big") + b"IDAT" + b"\x00" * 8,
        )
        txt = self._mk(session_dir, "files/notes.txt", "一些文本内容")
        pdf = self._mk(session_dir, "files/doc.pdf", b"%PDF-1.4 binary stuff" + b"\x00" * 100)

        prompt = f"看图 @{jpg} 再看 @{png} apng @{apng} 文本 @{txt} pdf @{pdf} 昵称 @alice"
        new_prompt, file_args = pigmod._split_pig_prompt_and_files(
            prompt, session_dir, []
        )
        self.assertIn("@" + os.path.realpath(jpg), file_args)
        self.assertIn("@" + os.path.realpath(png), file_args)
        self.assertIn("@" + os.path.realpath(txt), file_args)
        self.assertEqual(len(file_args), 3)
        # APNG / PDF 不进 file_args，内联去 @ 留纯路径
        self.assertNotIn("@" + os.path.realpath(apng), file_args)
        self.assertNotIn("@" + os.path.realpath(pdf), file_args)
        self.assertIn(os.path.realpath(apng), new_prompt)
        self.assertIn(os.path.realpath(pdf), new_prompt)
        self.assertNotIn("@" + os.path.realpath(apng), new_prompt)
        self.assertNotIn("@" + os.path.realpath(pdf), new_prompt)
        # @alice 昵称保留
        self.assertIn("@alice", new_prompt)
        # 文本文件占位说明
        self.assertIn("已附文本文件 notes.txt", new_prompt)

    def test_only_image_prompt_backfilled(self):
        from wechatbridge import pig as pigmod

        session_dir = self._session_dir()
        os.makedirs(session_dir, exist_ok=True)
        jpg = self._mk(session_dir, "images/a.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 32)
        new_prompt, file_args = pigmod._split_pig_prompt_and_files(
            f"@{jpg}", session_dir, []
        )
        self.assertEqual(file_args, ["@" + os.path.realpath(jpg)])
        self.assertTrue(new_prompt.strip())

    def test_image_magic_ref_pig_alignment(self):
        """MF-4: magic-byte routing must mirror ref/PiG images.go exactly —
        real APNG rejected, BM-prefixed binaries rejected, EXIF JPEG kept."""
        from wechatbridge import pig as pigmod

        session_dir = self._session_dir()
        os.makedirs(session_dir, exist_ok=True)

        # Real APNG: IHDR -> acTL -> IDAT (must NOT ride as @file image)
        apng = self._mk(session_dir, "real_apng.png",
            b"\x89PNG\r\n\x1a\n"
            + (13).to_bytes(4, "big") + b"IHDR" + b"\x00" * 17
            + (8).to_bytes(4, "big") + b"acTL" + b"\x00" * 12
            + (4).to_bytes(4, "big") + b"IDAT" + b"\x00" * 8)
        # "BM"-prefixed large binary — not a real BMP header
        fake_bmp = self._mk(session_dir, "data.bin", b"BM" + b"\x80" * 100_000)
        # JPEG with EXIF segment containing a \xff\xc7 byte pair — normal photo
        exif_jpg = self._mk(session_dir, "exif.jpg",
            b"\xff\xd8\xff\xe1\x00\x10Exif\x00\x00\xff\xc7" + b"\x00" * 32)
        # JPEG-LS (\xff\xd8\xff\xf7) must be rejected
        jpegls = self._mk(session_dir, "ls.jpg", b"\xff\xd8\xff\xf7" + b"\x00" * 32)

        new_prompt, file_args = pigmod._split_pig_prompt_and_files(
            f"@{apng} @{fake_bmp} @{exif_jpg} @{jpegls}", session_dir, []
        )
        self.assertNotIn("@" + os.path.realpath(apng), file_args)
        self.assertNotIn("@" + os.path.realpath(fake_bmp), file_args)
        self.assertIn("@" + os.path.realpath(exif_jpg), file_args)
        self.assertNotIn("@" + os.path.realpath(jpegls), file_args)

    def test_valid_bmp_accepted(self):
        from wechatbridge import pig as pigmod

        session_dir = self._session_dir()
        os.makedirs(session_dir, exist_ok=True)
        # Minimal legal BMP: file size 58, pixel offset 54, DIB 40,
        # planes=1, bpp=24
        hdr = bytearray(58)
        hdr[0:2] = b"BM"
        hdr[2:6] = (58).to_bytes(4, "little")
        hdr[10:14] = (54).to_bytes(4, "little")
        hdr[14:18] = (40).to_bytes(4, "little")
        hdr[26:28] = (1).to_bytes(2, "little")
        hdr[28:30] = (24).to_bytes(2, "little")
        bmp = self._mk(session_dir, "ok.bmp", bytes(hdr))
        new_prompt, file_args = pigmod._split_pig_prompt_and_files(
            f"@{bmp}", session_dir, []
        )
        self.assertIn("@" + os.path.realpath(bmp), file_args)

    def test_prefix_neutralization(self):
        from wechatbridge import pig as pigmod

        self.assertEqual(pigmod._neutralize_pig_prompt_prefix("/goal 拆解"), " /goal 拆解")
        self.assertEqual(pigmod._neutralize_pig_prompt_prefix("@alice hi"), " @alice hi")
        self.assertEqual(pigmod._neutralize_pig_prompt_prefix("-v"), " -v")
        self.assertEqual(pigmod._neutralize_pig_prompt_prefix("普通"), "普通")
        self.assertTrue(pigmod._neutralize_pig_prompt_prefix("").strip())


class TestParsePigOutput(_PigTestBase):
    def _parse(self, stdout, stderr="", rc=0, session_dir=None):
        from wechatbridge import pig as pigmod
        sd = session_dir or self._session_dir()
        os.makedirs(sd, exist_ok=True)
        return pigmod._parse_pig_output(stdout, stderr, rc, sd, {}, time.time(), [])

    def test_rc0_stop_reason_error_is_failed(self):
        stdout = json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "429 Too Many Requests",
                "content": [],
            },
        })
        display, artifacts, failed, err = self._parse(stdout)
        self.assertTrue(failed)
        self.assertIn("🔔", display)

    def test_rc0_auth_401(self):
        stdout = json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "401 Unauthorized",
                "content": [],
            },
        })
        display, _, failed, _ = self._parse(stdout)
        self.assertTrue(failed)
        self.assertIn("未登录", display)

    def test_rc0_magpie_504(self):
        stdout = json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "The backend sent nothing for 570000 ms",
                "content": [],
            },
        })
        display, _, failed, err_src = self._parse(stdout)
        self.assertTrue(failed)
        from wechatbridge.runner_common import _classify_cli_error
        self.assertEqual(_classify_cli_error(err_src, backend="pig"), "cascade_timeout")
        self.assertIn("超时", display)

    def test_length_keeps_partial_text(self):
        stdout = json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "length",
                "content": [{"type": "text", "text": "前半段"}],
            },
        })
        display, _, failed, _ = self._parse(stdout)
        self.assertFalse(failed)
        self.assertIn("前半段", display)
        self.assertIn("截断", display)

    def test_success_text_and_thinking_ignored(self):
        stdout = json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [
                    {"type": "thinking", "text": "internal"},
                    {"type": "text", "text": "正文"},
                ],
            },
        })
        display, _, failed, _ = self._parse(stdout)
        self.assertFalse(failed)
        self.assertIn("正文", display)
        self.assertNotIn("internal", display)

    def test_key_redaction_in_display(self):
        stdout = json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": "leak test-magpie-key-0123456789 end"}],
            },
        })
        display, _, failed, _ = self._parse(stdout)
        self.assertFalse(failed)
        self.assertIn("[REDACTED]", display)
        self.assertNotIn("test-magpie-key-0123456789", display)

    def test_tooluse_then_crash_is_failed_no_artifacts(self):
        """SF-1: transition text from an earlier toolUse round must NOT
        swallow a non-zero exit, and fallback artifact scan must not run."""
        session_dir = self._session_dir()
        os.makedirs(session_dir, exist_ok=True)
        # half-written deliverable created "during" the turn
        partial = os.path.join(session_dir, "partial.pdf")
        with open(partial, "wb") as f:
            f.write(b"%PDF-half")
        stdout = json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "toolUse",
                "content": [{"type": "text", "text": "让我先生成报告..."}],
            },
        })
        display, artifacts, failed, err = self._parse(
            stdout, stderr="runtime: out of memory", rc=2, session_dir=session_dir
        )
        self.assertTrue(failed)
        self.assertEqual(artifacts, [])

    def test_final_toolonly_round_drops_prior_narration(self):
        """SF-1: a final tool-only assistant round must not leave the prior
        toolUse narration as the display text."""
        lines = "\n".join([
            json.dumps({
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "toolUse",
                    "content": [{"type": "text", "text": "旁白中间轮"}],
                },
            }),
            json.dumps({
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "stop",
                    "content": [],
                },
            }),
        ])
        display, artifacts, failed, _ = self._parse(lines)
        self.assertNotIn("旁白中间轮", display)

    def test_tool_artifacts_prefiltered_to_allowed_roots(self):
        """SF-4: tool-written paths outside allowed roots are dropped
        before they reach the outbound artifact quota."""
        session_dir = self._session_dir()
        os.makedirs(session_dir, exist_ok=True)
        keep = os.path.join(session_dir, "final.pdf")
        with open(keep, "wb") as f:
            f.write(b"%PDF-final")
        tmpdir = tempfile.mkdtemp(dir=self.tmp.name)
        self.addCleanup(_rmtree, tmpdir)
        outside = [os.path.join(tmpdir, f"t{i}.txt") for i in range(10)]
        for p in outside:
            with open(p, "w") as f:
                f.write("x")
        events = []
        for i, p in enumerate(outside + [keep]):
            events.append(json.dumps({
                "type": "tool_execution_start",
                "toolCallId": f"c{i}",
                "toolName": "write",
                "args": {"path": p},
            }))
            events.append(json.dumps({
                "type": "tool_execution_end",
                "toolCallId": f"c{i}",
                "isError": False,
            }))
        events.append(json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": "done"}],
            },
        }))
        display, artifacts, failed, _ = self._parse(
            "\n".join(events), session_dir=session_dir
        )
        paths = [p for _, p in artifacts]
        self.assertNotIn(os.path.realpath(outside[0]), paths)
        self.assertIn(os.path.realpath(keep), paths)
        self.assertLess(len(artifacts), 11)


class TestSlashCommands(_PigTestBase):
    def test_models_single_model_card(self):
        import asyncio
        from wechatbridge import pig as pigmod

        out = asyncio.run(pigmod.handle_pig_slash_command("/models", _uid()))
        self.assertIn("gemini-web/gemini-4-neon", out)
        self.assertNotIn("gemini-4-neon-task", out.replace("不含", ""))

    def test_model_whitelist_rejects_task_and_others(self):
        import asyncio
        from wechatbridge import pig as pigmod

        out = asyncio.run(
            pigmod.handle_pig_slash_command(
                "/model gemini-web/gemini-4-neon-task", _uid()
            )
        )
        self.assertIn("模型不可用", out)
        out2 = asyncio.run(pigmod.handle_pig_slash_command("/model gpt-4o", _uid()))
        self.assertIn("模型不可用", out2)
        # 不污染 prefs
        from wechatbridge.runner_common import load_prefs
        self.assertNotEqual(load_prefs(_uid()).get("model"), "gpt-4o")

    def test_model_thinking_suffix_accepted(self):
        import asyncio
        from wechatbridge import pig as pigmod
        from wechatbridge.runner_common import load_prefs

        out = asyncio.run(
            pigmod.handle_pig_slash_command(
                "/model gemini-web/gemini-4-neon:medium", _uid()
            )
        )
        self.assertIn("模型已切换", out)
        self.assertEqual(load_prefs(_uid()).get("effort"), "medium")
        out2 = asyncio.run(
            pigmod.handle_pig_slash_command("/model gemini-4-neon-low", _uid())
        )
        self.assertIn("模型已切换", out2)
        self.assertEqual(load_prefs(_uid()).get("effort"), "low")

    def test_fast_planning_persona_clear(self):
        import asyncio
        from wechatbridge import pig as pigmod
        from wechatbridge.runner_common import load_prefs

        uid = _uid("slash")
        asyncio.run(pigmod.handle_pig_slash_command("/fast", uid))
        self.assertEqual(load_prefs(uid).get("effort"), "low")
        asyncio.run(pigmod.handle_pig_slash_command("/fast off", uid))
        self.assertEqual(load_prefs(uid).get("effort"), "")

        asyncio.run(pigmod.handle_pig_slash_command("/planning", uid))
        self.assertEqual(load_prefs(uid).get("mode"), "plan")
        asyncio.run(pigmod.handle_pig_slash_command("/planning off", uid))
        self.assertEqual(load_prefs(uid).get("mode"), "")

        out = asyncio.run(pigmod.handle_pig_slash_command("/persona 你是猫娘", uid))
        self.assertIn("人格文档已更新", out)
        persona_path = os.path.join(self._session_dir(uid), "pig_persona.txt")
        self.assertTrue(os.path.isfile(persona_path))
        self.assertEqual(stat.S_IMODE(os.lstat(persona_path).st_mode), 0o600)

        # /clear 清 session + flag + plan 模式
        session_dir = self._session_dir(uid)
        os.makedirs(os.path.join(session_dir, ".pig", "agent", "sessions", "cwd"), exist_ok=True)
        sess_file = os.path.join(session_dir, ".pig", "agent", "sessions", "cwd", "s.jsonl")
        with open(sess_file, "w") as f:
            f.write("{}")
        flag = os.path.join(session_dir, ".initialized.pig")
        with open(flag, "w") as f:
            f.write("1")
        asyncio.run(pigmod.handle_pig_slash_command("/planning", uid))
        out = asyncio.run(pigmod.handle_pig_slash_command("/clear", uid))
        self.assertIn("对话已重置", out)
        self.assertFalse(os.path.exists(sess_file))
        self.assertFalse(os.path.exists(flag))
        self.assertEqual(load_prefs(uid).get("mode"), "")

    def test_b_and_c_class_intercept(self):
        import asyncio
        from wechatbridge import pig as pigmod

        self.assertIn("禁用", asyncio.run(pigmod.handle_pig_slash_command("/exit", _uid())))
        self.assertIn("禁用", asyncio.run(pigmod.handle_pig_slash_command("/logout", _uid())))
        self.assertIn("不支持", asyncio.run(pigmod.handle_pig_slash_command("/resume", _uid())))
        self.assertIsNone(asyncio.run(pigmod.handle_pig_slash_command("/goal 拆解", _uid())))

    def test_persona_show_refuses_symlink_and_hardlink_and_redacts(self):
        """MF-2: /persona show must not follow a planted symlink/hardlink to
        a host file, and must redact a live key stored inside persona."""
        import asyncio
        from wechatbridge import pig as pigmod

        uid = _uid("persona")
        session_dir = self._session_dir(uid)
        os.makedirs(session_dir, exist_ok=True)
        secret = os.path.join(self.tmp.name, "host_secret")
        with open(secret, "w") as f:
            f.write("WECHATBRIDGE_MAGPIE_API_KEY=test-magpie-key-0123456789\nOTHER=xyz")
        persona_path = os.path.join(session_dir, "pig_persona.txt")

        # symlink → refused
        os.symlink(secret, persona_path)
        out = asyncio.run(pigmod.handle_pig_slash_command("/persona show", uid))
        self.assertNotIn("test-magpie-key-0123456789", out)
        self.assertNotIn("OTHER=xyz", out)
        os.unlink(persona_path)

        # hardlink → refused
        os.link(secret, persona_path)
        out = asyncio.run(pigmod.handle_pig_slash_command("/persona show", uid))
        self.assertNotIn("test-magpie-key-0123456789", out)
        self.assertNotIn("OTHER=xyz", out)
        os.unlink(persona_path)

        # real persona containing the live key → redacted
        with open(persona_path, "w") as f:
            f.write("I am a cat. key=test-magpie-key-0123456789")
        out = asyncio.run(pigmod.handle_pig_slash_command("/persona show", uid))
        self.assertIn("[REDACTED]", out)
        self.assertNotIn("test-magpie-key-0123456789", out)

    def test_persona_read_refuses_hardlink(self):
        """MF-2: _read_pig_persona must not inject a hardlinked host file
        into the runtime prompt."""
        from wechatbridge import pig as pigmod

        session_dir = self._session_dir()
        os.makedirs(session_dir, exist_ok=True)
        secret = os.path.join(self.tmp.name, "host_secret2")
        with open(secret, "w") as f:
            f.write("SECRET_TOKEN=abc")
        persona_path = os.path.join(session_dir, "pig_persona.txt")
        os.link(secret, persona_path)
        self.assertEqual(pigmod._read_pig_persona(session_dir), "")


class TestRunPigIntegration(_PigTestBase):
    def _run(self, prompt, user_id=None, mode="ok", env_extra=None, timeout=10):
        import asyncio
        from wechatbridge import pig as pigmod
        from wechatbridge.config import config

        env = {"FAKE_PIG_MODE": mode}
        if env_extra:
            env.update(env_extra)
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(config, "pig_binary_path", self._pig_shim()):
            return asyncio.run(pigmod.run_pig(prompt, user_id or _uid(), timeout=timeout))

    def test_missing_key_short_circuits(self):
        import asyncio
        from wechatbridge import pig as pigmod
        from wechatbridge.config import config

        with mock.patch.object(config, "magpie_api_key", ""), \
             mock.patch.dict(os.environ, {}, clear=True):
            display, arts = asyncio.run(pigmod.run_pig("hi", _uid()))
        self.assertIn("未登录", display)
        self.assertEqual(arts, [])

    def test_success_marks_initialized_and_writes_flag(self):
        uid = _uid("integ")
        display, arts = self._run("你好", uid, mode="ok")
        self.assertIn("first(", display)
        flag = os.path.join(self._session_dir(uid), ".initialized.pig")
        self.assertTrue(os.path.exists(flag))
        # models.json 已生成且无明文 key
        models_path = os.path.join(
            self._session_dir(uid), ".pig", "agent", "models.json"
        )
        self.assertTrue(os.path.isfile(models_path))
        with open(models_path, "rb") as mf:
            self.assertNotIn(b"test-magpie-key", mf.read())

    def test_rc0_error_reply_classified(self):
        display, _ = self._run("hi", _uid("err"), mode="rate_limit_rc0")
        self.assertIn("🔔", display)

    def test_continue_self_heal(self):
        import asyncio
        from wechatbridge import pig as pigmod
        from wechatbridge.runner_common import mark_initialized
        from wechatbridge.config import config

        uid = _uid("heal")
        session_dir = self._session_dir(uid)
        os.makedirs(os.path.join(session_dir, ".pig", "agent", "sessions", "cwd"), exist_ok=True)
        with open(os.path.join(session_dir, ".pig", "agent", "sessions", "cwd", "s.jsonl"), "w") as f:
            f.write("{}")
        mark_initialized(session_dir, backend="pig")
        log = os.path.join(self.tmp.name, "pig.log")
        env = {"FAKE_PIG_MODE": "session_not_found", "FAKE_PIG_LOG": log}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(config, "pig_binary_path", self._pig_shim()):
            display, _ = asyncio.run(pigmod.run_pig("hi", uid, timeout=15))
        self.assertIn("first(", display)
        # 会话已清空，自愈重试成功
        with open(log, encoding="utf-8") as lf:
            lines = lf.read().strip().splitlines()
        self.assertGreaterEqual(len(lines), 2)
        self.assertIn("--continue", json.loads(lines[0])["argv"])
        self.assertNotIn("--continue", json.loads(lines[1])["argv"])

    def test_timeout_returns_timeout_error(self):
        display, _ = self._run("hi", _uid("t"), mode="sleep", timeout=1)
        self.assertIn("超时", display)

    def test_empty_reply_does_not_mark_initialized(self):
        """SF-1: an EMPTY_REPLY turn must not stamp .initialized.pig."""
        uid = _uid("empty")
        display, _ = self._run("hi", uid, mode="empty")
        flag = os.path.join(self._session_dir(uid), ".initialized.pig")
        self.assertFalse(os.path.exists(flag))

    def test_upstream_error_does_not_clear_sessions(self):
        """SF-2: a JSONL errorMessage containing 'no session available' from
        the upstream (rc=0 stopReason=error) must NOT clear the user's
        .pig/agent/sessions history."""
        import asyncio
        from wechatbridge import pig as pigmod
        from wechatbridge.runner_common import mark_initialized
        from wechatbridge.config import config

        uid = _uid("keephist")
        session_dir = self._session_dir(uid)
        bucket = os.path.join(session_dir, ".pig", "agent", "sessions", "--cwd--")
        os.makedirs(bucket, exist_ok=True)
        hist = os.path.join(bucket, "chat.jsonl")
        with open(hist, "w") as f:
            f.write("{}")
        mark_initialized(session_dir, backend="pig")

        env = {
            "FAKE_PIG_MODE": "error_rc0",
            "FAKE_PIG_ERROR_MESSAGE": "upstream gemini-web: no session available",
        }
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(config, "pig_binary_path", self._pig_shim()):
            display, _ = asyncio.run(pigmod.run_pig("继续刚才的话题", uid, timeout=10))
        self.assertTrue(os.path.exists(hist), "upstream transient must not wipe history")

    def test_dns_failure_classifies_network_not_auth(self):
        """SF-2: 'failed to resolve host' must NOT be hijacked into auth
        (DNS blips are not credential failures)."""
        from wechatbridge.runner_common import _classify_cli_error
        cat = _classify_cli_error(
            "Post https://magpie.hotkids.eu/v1/responses: failed to resolve host",
            backend="pig",
        )
        self.assertNotEqual(cat, "auth")

    def test_process_group_cleaned_after_normal_exit(self):
        """Rev#5: a background child left behind in the pig process group
        after the root CLI exited must be reaped by the finally cleanup."""
        import asyncio
        import subprocess
        from wechatbridge import pig as pigmod
        from wechatbridge.config import config

        # A shim that spawns a background sleep in the same process group
        # (stdio detached so the root's communicate() does not wait on it),
        # records its pid, then exec's fake_pig — the root exits normally
        # with a valid ok reply while the detached child is still running.
        shim = os.path.join(self.tmp.name, "bgshim.sh")
        with open(shim, "w") as f:
            f.write(
                "#!/bin/sh\n"
                "sleep 300 </dev/null >/dev/null 2>&1 &\n"
                "echo $! > " + self.tmp.name + "/bgpid\n"
                "exec " + sys.executable + " " + _FAKE_PIG + " \"$@\"\n"
            )
        os.chmod(shim, 0o755)
        uid = _uid("grp")
        with mock.patch.object(config, "pig_binary_path", shim):
            display, _ = asyncio.run(pigmod.run_pig("hi", uid, timeout=10))
        with open(self.tmp.name + "/bgpid") as f:
            bg_pid = int(f.read().strip())
        # The background child shares the pig process group; it must be
        # reaped (allow a brief window for the signal to be delivered).
        alive = True
        for _ in range(50):
            try:
                os.kill(bg_pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.05)
        self.assertFalse(alive, "background child in pig process group leaked")

    def test_active_session_skipped_by_ttl_cleaner(self):
        """Rev#6 + SF-ttl-window: while run_pig is in flight (registered
        active), the TTL cleaner must skip the sessions tree even for
        ancient .jsonl mtimes.

        The run is pinned mid-flight on a controlled ``communicate`` hang
        point: ``cleaner_checked`` is released only after the cleaner's
        delete pass has completed, so ``_unregister_active_pig_session``
        can never interleave between registration and the cleaner's
        check — the previous version released the cleaner on the mere
        registration event and raced ``removed==0`` red when a legal
        delete ran after the turn had already returned."""
        import asyncio
        import threading
        from unittest.mock import MagicMock
        from wechatbridge import pig as pigmod
        from wechatbridge.runner_common import (
            _clean_pig_sessions,
            mark_initialized,
        )
        from wechatbridge.config import config

        uid = _uid("ttl")
        session_dir = self._session_dir(uid)
        bucket = os.path.join(session_dir, ".pig", "agent", "sessions", "--cwd--")
        os.makedirs(bucket, exist_ok=True)
        old_f = os.path.join(bucket, "old.jsonl")
        with open(old_f, "w") as f:
            f.write("{}")
        ancient = time.time() - 40 * 86400
        os.utime(old_f, (ancient, ancient))
        mark_initialized(session_dir, backend="pig")

        sessions_root = os.path.join(session_dir, ".pig", "agent", "sessions")
        cutoff = time.time() - 30 * 86400
        start_clean = threading.Barrier(2)   # main releases the cleaner
        registered = threading.Event()       # run_pig registration is live
        cleaner_checked = threading.Event()  # cleaner's delete pass finished
        result = {}

        orig_reg = pigmod._register_active_pig_session

        def _reg_and_flag(root):
            key = orig_reg(root)
            registered.set()
            return key

        def _cleaner():
            self.assertTrue(start_clean.wait(timeout=10) is not None)
            result["removed"] = _clean_pig_sessions(sessions_root, cutoff)
            # Cleaner has completed its active check + delete pass; only
            # now may the turn finish and unregister. This is the signal
            # the hang point waits on.
            cleaner_checked.set()

        # Controlled hang point: run_pig is parked AFTER registration and
        # BEFORE completion — the fake process is spawned but its
        # communicate() await blocks until the cleaner signals
        # cleaner_checked. Registration therefore stays live for the
        # whole cleaner pass by construction.
        fake_proc = MagicMock()
        fake_proc.pid = 424242
        fake_proc.returncode = 0

        async def _hung_communicate():
            ok = await asyncio.to_thread(cleaner_checked.wait, 10)
            assert ok, "cleaner never completed its check within 10s"
            return (b'{"type":"message_end","message":{"role":"assistant",'
                    b'"stopReason":"stop","content":[{"type":"text",'
                    b'"text":"hi"}]}}\n', b"")

        fake_proc.communicate = _hung_communicate

        async def _fake_spawn(cmd, env, sess_dir):
            return fake_proc

        async def _driver():
            th = threading.Thread(target=_cleaner)
            th.start()
            try:
                with mock.patch.object(pigmod, "_spawn_pig", _fake_spawn), \
                     mock.patch.object(pigmod, "_register_active_pig_session", _reg_and_flag):
                    task = asyncio.create_task(pigmod.run_pig("hi", uid, timeout=10))
                    # Wait until registration is live, then release the
                    # cleaner while the run is still parked on
                    # cleaner_checked — the cleaner's check therefore
                    # always sees an active registration.
                    self.assertTrue(
                        await asyncio.to_thread(registered.wait, 10)
                    )
                    start_clean.wait(timeout=10)
                    display, artifacts = await task
                    result["display"] = display
            finally:
                # If anything above failed before the cleaner could
                # complete, still release the hang point so the thread /
                # coroutine can never outlive this test.
                cleaner_checked.set()
                th.join(timeout=10)
                result["thread_alive"] = th.is_alive()

        asyncio.run(_driver())
        self.assertFalse(
            result.get("thread_alive", True), "cleaner thread leaked past join"
        )
        self.assertEqual(result.get("removed", -1), 0)
        self.assertTrue(
            cleaner_checked.is_set(), "cleaner pass never ran"
        )
        self.assertTrue(os.path.exists(old_f), "active session .jsonl must survive TTL")


class TestPigTimeoutConfig(unittest.TestCase):
    def test_default_timeout_570(self):
        from wechatbridge.config import config
        self.assertEqual(config.pig_timeout, 570)


class TestMagpieUrlValidation(unittest.TestCase):
    """SF-3: _validate_magpie_url must use urlsplit — reject userinfo
    smuggling and accept IPv6 loopback; never guess the host by split()."""

    def _v(self, url):
        from wechatbridge.config import _validate_magpie_url
        return _validate_magpie_url(url)

    def test_userinfo_http_rejected(self):
        # http://localhost:80@evil.example — hostname is evil.example, NOT
        # localhost; must fall back to the https default, not pass through.
        out = self._v("http://localhost:80@evil.example/v1")
        self.assertEqual(out, "https://magpie.hotkids.eu/v1")

    def test_ipv6_loopback_http_allowed(self):
        self.assertEqual(
            self._v("http://[::1]:8080/v1"), "http://[::1]:8080/v1"
        )
        self.assertEqual(
            self._v("http://127.0.0.1:8080/v1"), "http://127.0.0.1:8080/v1"
        )
        self.assertEqual(self._v("http://localhost:9000/v1"), "http://localhost:9000/v1")

    def test_https_and_userinfo_rejected(self):
        self.assertEqual(self._v("https://api.example.com/v1"), "https://api.example.com/v1")
        # userinfo (any non-empty user/pass) is refused even over https —
        # the bridge must not smuggle a credential-bearing URL onward.
        # Build the URL at runtime so no secret-shaped literal sits in the file.
        evil = "https://" + "user" + ":" + "s3cr" + "3t" + "@api.example.com/v1"
        self.assertEqual(self._v(evil), "https://magpie.hotkids.eu/v1")

    def test_public_http_rejected(self):
        self.assertEqual(
            self._v("http://magpie.example.com/v1"), "https://magpie.hotkids.eu/v1"
        )

    def test_empty_and_garbage_fall_back(self):
        self.assertEqual(self._v(""), "https://magpie.hotkids.eu/v1")
        self.assertEqual(self._v("notaurl"), "https://magpie.hotkids.eu/v1")


class TestPigR3Fixes(_PigTestBase):
    """Round-3 joint-review fixes (MF-3/4/5/8/9, SF-7)."""

    # -- MF-3: ancestor symlink on .pig/.pig/agent must fail closed -------

    def _plant_agent_ancestor_symlink(self, tag):
        """Return (session_dir, host_keep) with sdir/.pig/agent -> host_agent."""
        session_dir = self._session_dir(_uid(tag))
        host_agent = os.path.join(self.tmp.name, tag + "_host_agent")
        os.makedirs(os.path.join(host_agent, "sessions"))
        keep = os.path.join(host_agent, "sessions", "keep.jsonl")
        with open(keep, "w") as f:
            f.write("{}")
        os.makedirs(os.path.join(session_dir, ".pig"))
        os.symlink(host_agent, os.path.join(session_dir, ".pig", "agent"))
        return session_dir, keep

    def test_clean_pig_sessions_refuses_ancestor_symlink(self):
        """MF-3: a .pig/agent -> host_agent symlink must not let the TTL
        cleaner delete the foreign tree's *.jsonl."""
        from wechatbridge.runner_common import _clean_pig_sessions

        session_dir, keep = self._plant_agent_ancestor_symlink("cln")
        sroot = os.path.join(session_dir, ".pig", "agent", "sessions")
        removed = _clean_pig_sessions(sroot, time.time() + 10)  # future cutoff
        self.assertEqual(removed, 0)
        self.assertTrue(os.path.exists(keep))

    def test_clear_pig_sessions_refuses_ancestor_symlink(self):
        """MF-3: /clear path must also refuse the same ancestor symlink."""
        from wechatbridge import pig as pigmod

        session_dir, keep = self._plant_agent_ancestor_symlink("clr")
        pigmod._clear_pig_sessions(session_dir)
        self.assertTrue(os.path.exists(keep))

    # -- MF-7d-TOCTOU: 7d pig temp roots skip while a run is active ------

    def test_pig_7d_temp_root_skipped_while_run_active(self):
        """MF-7d-TOCTOU + MF-R8-1: a pig turn (bash tool) can retarget any
        ``.pig`` component mid-flight. The 7d cleaner must check
        ``active`` and validate+delete under the SAME clean lock the run
        registered under — and lock/active keys must be anchored on the
        bridge-owned ``user_dir``, never on the attacker-swappable
        ``.pig/agent/sessions`` chain's realpath.

        Staging is driven by REAL signals, never sleeps:
          * ``registered`` fires when run_pig's off-loop registration
            actually completes (``_reg_and_flag`` returns);
          * once registration is live the test itself swaps
            ``.pig/agent/sessions`` for a decoy symlink — exactly what a
            turn's bash tool could do — and asserts
            ``_pig_session_root_active`` still sees the live entry (a
            ``realpath(sessions_root)``-keyed registry would now derive
            a different key and go blind: MUST-1 FAIL);
          * a wrapped ``_pig_temp_root_untrusted`` swaps ``.pig/logs``
            for a foreign tree INSIDE the held clean lock — the
            validate→delete TOCTOU window a lock-key fork would reopen:
            with forked keys the foreign ``keep.log`` is walked+deleted
            (MUST-1 FAIL); with the stable key the active check (in
            lock) skips before the wrapper even runs.
          * ``config.dsh_state_dir`` + ``config.dsh_home`` are pinned to
            real temp dirs under ``self.tmp`` for the whole cleaner pass
            (must-2 isolation: ``clean_session_data`` sweeps those roots
            unconditionally — unpatched it scans/deletes the host's real
            dsh state).
        """
        import asyncio
        import threading
        import wechatbridge.runner_common as rc
        from unittest.mock import MagicMock
        from wechatbridge import pig as pigmod
        from wechatbridge.runner_common import clean_session_data
        from wechatbridge.config import config

        uid = _uid("7dactive")
        session_dir = self._session_dir(uid)
        sessions_root = os.path.join(
            session_dir, ".pig", "agent", "sessions"
        )
        pig_logs = os.path.join(session_dir, ".pig", "logs")
        os.makedirs(pig_logs, exist_ok=True)
        stale = os.path.join(pig_logs, "stale.log")
        with open(stale, "w") as f:
            f.write("old")
        old_mtime = time.time() - 40 * 86400
        os.utime(stale, (old_mtime, old_mtime))

        # MUST-2: pin the two host-wide dsh roots clean_session_data
        # sweeps unconditionally into this test's temp tree.
        dsh_state = os.path.join(self.tmp.name, "dsh_state")
        dsh_home_dir = os.path.join(self.tmp.name, "dsh_home")
        os.makedirs(dsh_state)
        os.makedirs(dsh_home_dir)

        # Foreign victim for the in-lock validate→walk swap probe. Its
        # layout mirrors the real .pig/logs root so the same rel still
        # validates after the swap: outside/.pig/logs/keep.log is the
        # file that must NEVER be reached. os.walk on the swapped
        # ``.pig/logs`` symlink yields (root=link, keep.log as a root-
        # level file of the TARGET — followlinks=False only gates
        # descent into dir links, not the walk root itself), so a
        # realpath-forked implementation lands the delete here → FAIL.
        outside = os.path.join(self.tmp.name, "outside")
        outside_logs = os.path.join(outside, ".pig", "logs")
        os.makedirs(outside_logs)
        keep = os.path.join(outside_logs, "keep.log")
        with open(keep, "w") as f:
            f.write("victim")
        os.utime(keep, (old_mtime, old_mtime))

        # Decoy tree the in-flight "bash" retargets sessions onto.
        decoy = os.path.join(self.tmp.name, "decoy_sessions")
        os.makedirs(decoy)

        registered = threading.Event()   # run_pig registration is live
        run_started = threading.Event()  # swap done, release the cleaner
        finish_run = threading.Event()   # releases the fake communicate
        cleaned = threading.Event()      # whole cleaner pass completed
        result = {}

        orig_reg = pigmod._register_active_pig_session
        orig_untrusted = rc._pig_temp_root_untrusted

        def _reg_and_flag(root):
            key = orig_reg(root)
            registered.set()
            return key

        def _swap_then_check(user_dir, rel):
            # Runs INSIDE the cleaner's held clean lock at the validate
            # step: the validate→delete gap a lock-key fork would reopen.
            if rel.endswith(os.path.join(".pig", "logs")):
                victim = os.path.join(user_dir, rel)
                os.rename(victim, victim + ".saved")
                os.symlink(os.path.join(outside, rel), victim)
            return orig_untrusted(user_dir, rel)

        def _cleaner():
            self.assertTrue(run_started.wait(timeout=15))
            try:
                with mock.patch.object(
                    config, "session_retention_days", 7
                ), mock.patch.object(
                    config, "history_retention_days", 30
                ), mock.patch.object(
                    config, "dsh_state_dir", dsh_state
                ), mock.patch.object(
                    config, "dsh_home", dsh_home_dir
                ), mock.patch.object(
                    rc, "_pig_temp_root_untrusted", _swap_then_check
                ):
                    result["removed"] = clean_session_data()
            finally:
                cleaned.set()

        fake_proc = MagicMock()
        fake_proc.pid = 515151
        fake_proc.returncode = 0

        async def _hung_communicate():
            ok = await asyncio.to_thread(finish_run.wait, 15)
            assert ok, "driver never released the hang point"
            return (b'{"type":"message_end","message":{"role":"assistant",'
                    b'"stopReason":"stop","content":[{"type":"text",'
                    b'"text":"hi"}]}}\n', b"")

        fake_proc.communicate = _hung_communicate

        async def _fake_spawn(cmd, env, sess_dir):
            return fake_proc

        async def _driver():
            th = threading.Thread(target=_cleaner)
            th.start()
            try:
                with mock.patch.object(pigmod, "_spawn_pig", _fake_spawn), \
                     mock.patch.object(
                         pigmod,
                         "_register_active_pig_session",
                         _reg_and_flag,
                     ), \
                     mock.patch.object(config, "pig_binary_path", self._pig_shim()):
                    task = asyncio.create_task(
                        pigmod.run_pig("hi", uid, timeout=15)
                    )
                    # Wait for the REAL registration-completed signal —
                    # not a sleep — then swap ``sessions`` for the decoy
                    # symlink while the turn is parked on finish_run.
                    self.assertTrue(
                        await asyncio.to_thread(registered.wait, 15),
                        "run_pig never completed its active registration",
                    )
                    decoy_link = os.path.join(
                        session_dir, ".pig", "agent", "sessions.decoy_link"
                    )
                    os.symlink(decoy, decoy_link)
                    os.rename(
                        sessions_root,
                        os.path.join(
                            session_dir, ".pig", "agent", "sessions.saved"
                        ),
                    )
                    os.rename(decoy_link, sessions_root)
                    # MUST-1 probe: with a realpath(sessions_root)-keyed
                    # registry the just-swapped chain derives K2 != the
                    # registered K1 → active check goes blind → FAIL.
                    result["active_after_swap"] = (
                        rc._pig_session_root_active(sessions_root)
                    )
                    run_started.set()
                    self.assertTrue(
                        await asyncio.to_thread(cleaned.wait, 15),
                        "cleaner pass never completed",
                    )
                    finish_run.set()
                    display, artifacts = await task
                    result["display"] = display
            finally:
                finish_run.set()
                th.join(timeout=15)
                result["thread_alive"] = th.is_alive()

        asyncio.run(_driver())
        self.assertFalse(
            result.get("thread_alive", True), "cleaner thread leaked past join"
        )
        self.assertTrue(
            result.get("active_after_swap"),
            "active registry went blind after the in-flight sessions "
            "swap — keys must anchor on realpath(user_dir), not on the "
            "attacker-swappable sessions chain realpath",
        )
        self.assertTrue(
            os.path.exists(keep),
            "foreign tree file deleted through the validate→delete "
            "window — the in-lock active check did not run (lock/active "
            "key forked or active registry blind)",
        )
        self.assertTrue(
            os.path.exists(stale),
            "expired file under .pig/logs must survive while a pig turn "
            "is in flight — the 7d cleaner must skip pig roots on active",
        )

    def test_pig_clean_lock_no_leak_for_non_pig_users(self):
        """SF-R8-1: ``clean_session_data`` iterates every user dir and
        calls the 7d pig-root cleaner unconditionally; it must not
        create a ``_PIG_SESSION_CLEAN_LOCKS`` entry for users that have
        no ``.pig`` tree at all (the lock dict must only grow for users
        that actually ran/keep pig state).

        A ``.pig``-free user plus a ``.pig``-free dir that still has a
        stale ``.initialized.pig`` flag exercises both the 7d gate and
        the flag-clearing path without touching the lock dict."""
        import wechatbridge.runner_common as rc
        from wechatbridge.runner_common import clean_session_data
        from wechatbridge.config import config

        user_dir = os.path.join(self.sessions_base, _uid("nopiglock"))
        os.makedirs(user_dir)
        # Other backend roots exist (so the user is a real, swept user)
        # but NOTHING under .pig exists.
        os.makedirs(os.path.join(user_dir, ".grok", "sessions"))
        flag = os.path.join(user_dir, ".initialized.pig")
        with open(flag, "w") as f:
            f.write("1")

        dsh_state = os.path.join(self.tmp.name, "dsh_state")
        dsh_home_dir = os.path.join(self.tmp.name, "dsh_home")
        os.makedirs(dsh_state)
        os.makedirs(dsh_home_dir)

        before = set(rc._PIG_SESSION_CLEAN_LOCKS)
        with mock.patch.object(config, "session_retention_days", 7), \
             mock.patch.object(config, "history_retention_days", 30), \
             mock.patch.object(config, "dsh_state_dir", dsh_state), \
             mock.patch.object(config, "dsh_home", dsh_home_dir):
            clean_session_data()
        leaked = set(rc._PIG_SESSION_CLEAN_LOCKS) - before
        self.assertEqual(
            leaked, set(),
            f"non-pig user leaked clean-lock entries: {leaked}",
        )
        self.assertFalse(
            os.path.exists(flag),
            "stale .initialized.pig must still be cleared for a "
            ".pig-free user (history-gone semantics unchanged)",
        )

    # -- SF-R7-2: session_base_dir symlink must not poison pig trust ------

    def test_sessions_base_dir_symlink_no_false_refusal(self):
        """SF-R7-2: when ``session_base_dir`` itself (or an ancestor of
        ``user_dir``) is a symlink, the pig sessions trust anchor must be
        ``user_dir`` — not the absolute path from ``/``. Otherwise the
        30d TTL and /clear would refuse forever on any host that mounts
        the session store through a symlinked path."""
        from wechatbridge.runner_common import _clean_pig_sessions
        from wechatbridge import pig as pigmod
        from wechatbridge.config import config

        real_base = os.path.join(self.tmp.name, "real_sessions")
        os.makedirs(real_base)
        link_base = os.path.join(self.tmp.name, "sessions_link")
        os.symlink(real_base, link_base)

        # user_dir reached THROUGH the symlinked base dir; the
        # user_dir -> .pig/agent/sessions chain itself is fully real.
        session_dir = os.path.join(link_base, "u_symlink_base")
        bucket = os.path.join(session_dir, ".pig", "agent", "sessions", "cwd")
        os.makedirs(bucket, exist_ok=True)
        old_f = os.path.join(bucket, "old.jsonl")
        with open(old_f, "w") as f:
            f.write("{}")
        ancient = time.time() - 40 * 86400
        os.utime(old_f, (ancient, ancient))

        sessions_root = os.path.join(session_dir, ".pig", "agent", "sessions")
        removed = _clean_pig_sessions(sessions_root, time.time() - 30 * 86400)
        self.assertEqual(
            removed, 1,
            "30d TTL must still delete under a symlinked session_base_dir",
        )
        self.assertFalse(os.path.exists(old_f))

        # /clear on the same layout must not silently refuse either.
        # (the expired bucket above was pruned as empty — recreate it)
        os.makedirs(bucket, exist_ok=True)
        new_f = os.path.join(bucket, "new.jsonl")
        with open(new_f, "w") as f:
            f.write("{}")
        with mock.patch.object(config, "session_base_dir", link_base):
            pigmod._clear_pig_sessions(session_dir)
        self.assertFalse(
            os.path.exists(new_f),
            "/clear must still clear under a symlinked session_base_dir",
        )

    # -- MF-9: self-heal retry must not mark EMPTY_REPLY initialized ------

    def test_self_heal_retry_empty_reply_no_initialized(self):
        """MF-9: first --continue corrupt → clear_initialized → retry
        EMPTY_REPLY must NOT stamp .initialized.pig."""
        import asyncio
        from wechatbridge import pig as pigmod
        from wechatbridge.runner_common import mark_initialized
        from wechatbridge.config import config

        uid = _uid("healempty")
        session_dir = self._session_dir(uid)
        bucket = os.path.join(session_dir, ".pig", "agent", "sessions", "cwd")
        os.makedirs(bucket, exist_ok=True)
        with open(os.path.join(bucket, "s.jsonl"), "w") as f:
            f.write("{}")
        mark_initialized(session_dir, backend="pig")
        env = {
            "FAKE_PIG_MODE": "corrupt_first_empty",
            "FAKE_PIG_LOG": os.path.join(self.tmp.name, "he.log"),
        }
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(config, "pig_binary_path", self._pig_shim()):
            asyncio.run(pigmod.run_pig("hi", uid, timeout=15))
        flag = os.path.join(session_dir, ".initialized.pig")
        self.assertFalse(
            os.path.exists(flag),
            "empty self-heal retry must not stamp .initialized.pig",
        )

    # -- MF-5: first-attempt process group reaped before retry ------------

    def test_self_heal_reaps_first_attempt_process_group(self):
        """MF-5: the first corrupt --attempt's detached child (same pgid)
        must be reaped; the retry must also run and its group reaped."""
        import asyncio
        from wechatbridge import pig as pigmod
        from wechatbridge.runner_common import mark_initialized
        from wechatbridge.config import config

        uid = _uid("healgrp")
        session_dir = self._session_dir(uid)
        bucket = os.path.join(session_dir, ".pig", "agent", "sessions", "cwd")
        os.makedirs(bucket, exist_ok=True)
        with open(os.path.join(bucket, "s.jsonl"), "w") as f:
            f.write("{}")
        mark_initialized(session_dir, backend="pig")
        bgpid_path = os.path.join(self.tmp.name, "bgpid")
        env = {
            "FAKE_PIG_MODE": "bg_child_first",
            "FAKE_PIG_BGPID": bgpid_path,
        }
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(config, "pig_binary_path", self._pig_shim()):
            asyncio.run(pigmod.run_pig("hi", uid, timeout=15))
        self.assertTrue(os.path.exists(bgpid_path))
        with open(bgpid_path) as bf:
            bg_pid = int(bf.read().strip())
        alive = True
        for _ in range(60):
            try:
                os.kill(bg_pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.05)
        self.assertFalse(
            alive, "first-attempt background child group was not reaped"
        )

    # -- MF-4: registration-vs-cleaner reverse-interleave -----------------

    def test_reverse_interleave_register_before_delete_no_loss(self):
        """MF-4/MF-C: the dangerous interleave is cleaner-checks-inactive
        THEN run-registers THEN cleaner-deletes. The per-session clean
        lock must serialise the cleaner's whole check+delete against the
        run's registration: a cleaner holding the lock through its check
        must finish its delete pass BEFORE a queued registration lands.

        Staging is proven with REAL signals, never timing guesses:
          * ``_lock_wrap`` wraps ``_pig_session_clean_lock`` — the very
            function the locked registration calls to ACQUIRE the clean
            lock — and fires ``lock_requested`` when a non-cleaner
            thread requests it while the cleaner still holds it (a
            lock-free registration never calls this at all);
          * ``registered`` fires when the registration actually
            completes (``_reg_flag`` returns), and appends "registered"
            to ``order``;
          * ``cleaner_deleted`` is appended to ``order`` INSIDE the
            still-held clean lock (a wrapped ``_clean_pig_sessions_locked``),
            so the recorded order can never be overtaken by a correctly
            locked registration.
        The coordinator opens ``cleaner_gate`` ONLY after observing the
        real "lock acquisition attempted" or "registration already
        completed" signal — no 50ms negative sampling. The core
        assertion is then purely order-based: ``order`` must be exactly
        ["deleted", "registered"]. The R2 lock-free registration
        completes while the cleaner is still parked → lands first →
        ["registered", "deleted"] → FAIL.
        """
        import asyncio
        import threading
        import wechatbridge.runner_common as rc
        from wechatbridge import pig as pigmod
        from wechatbridge.runner_common import (
            _clean_pig_sessions,
            mark_initialized,
        )
        from wechatbridge.config import config

        uid = _uid("revrace")
        session_dir = self._session_dir(uid)
        sessions_root = os.path.join(session_dir, ".pig", "agent", "sessions")
        bucket = os.path.join(sessions_root, "--cwd--")
        os.makedirs(bucket, exist_ok=True)
        old_f = os.path.join(bucket, "old.jsonl")
        with open(old_f, "w") as f:
            f.write("{}")
        ancient = time.time() - 40 * 86400
        os.utime(old_f, (ancient, ancient))
        mark_initialized(session_dir, backend="pig")
        cutoff = time.time() - 30 * 86400  # old.jsonl is "expired"

        checked = threading.Event()          # cleaner inside lock, check ran
        lock_requested = threading.Event()   # worker ATTEMPTED the held lock
        registered = threading.Event()       # registration fully completed
        cleaner_gate = threading.Event()     # driver releases the cleaner
        cleaner_deleted = threading.Event()  # in-lock delete pass finished
        order = []                           # "deleted"/"registered" ordering
        result = {}
        cleaner_ident = []

        orig_root_active = rc._pig_session_root_active
        orig_reg = pigmod._register_active_pig_session
        orig_clean_lock = rc._pig_session_clean_lock
        orig_locked = rc._clean_pig_sessions_locked

        def _checked_then_wait(root):
            # Runs inside the cleaner's clean-lock pass: prove the active
            # check executed while the lock was held, then park until the
            # driver has REAL proof the interleave is staged.
            active = orig_root_active(root)
            if cleaner_ident and threading.get_ident() == cleaner_ident[0]:
                checked.set()
                result["gate_released"] = cleaner_gate.wait(timeout=15)
            return active

        def _cleaned_under_lock(root, c):
            # Appended while the cleaner STILL holds the clean lock — a
            # correctly locked registration can never precede this mark.
            removed = orig_locked(root, c)
            order.append("deleted")
            cleaner_deleted.set()
            return removed

        def _lock_wrap(root):
            # A NON-cleaner thread asking for the clean lock while the
            # cleaner is provably inside it == the real "queued behind
            # the held lock" signal (the actual lock-acquisition site,
            # not a flag set before the attempt).
            if (
                cleaner_ident
                and checked.is_set()
                and threading.get_ident() != cleaner_ident[0]
            ):
                lock_requested.set()
            return orig_clean_lock(root)

        def _reg_flag(root):
            # Do not even ATTEMPT the registration until the cleaner is
            # provably inside the clean lock — otherwise a fast run
            # could register first and the cleaner-first interleave
            # under test never happens.
            result["reg_saw_checked"] = checked.wait(timeout=15)
            key = orig_reg(root)
            order.append("registered")
            registered.set()
            return key

        def _wait_first(events, timeout):
            # Bounded poll for the FIRST of several real signals; returns
            # the index of the event that fired, or -1 on timeout.
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                for i, ev in enumerate(events):
                    if ev.is_set():
                        return i
                time.sleep(0.002)
            return -1

        def _cleaner():
            cleaner_ident.append(threading.get_ident())
            result["removed"] = _clean_pig_sessions(sessions_root, cutoff)

        async def _driver():
            th = threading.Thread(target=_cleaner)
            try:
                with mock.patch.object(
                    config, "pig_binary_path", self._pig_shim()
                ), mock.patch.object(
                    rc, "_pig_session_root_active", _checked_then_wait
                ), mock.patch.object(
                    rc, "_clean_pig_sessions_locked", _cleaned_under_lock
                ), mock.patch.object(
                    rc, "_pig_session_clean_lock", _lock_wrap
                ), mock.patch.object(
                    pigmod, "_register_active_pig_session", _reg_flag
                ):
                    th.start()
                    task = asyncio.create_task(
                        pigmod.run_pig("hi", uid, timeout=15)
                    )
                    # 1. cleaner is inside the lock, active check done.
                    result["checked_ok"] = await asyncio.to_thread(
                        checked.wait, 15
                    )
                    # 2. wait for REAL proof the interleave is staged:
                    #    the registration worker attempting the held
                    #    clean lock (correct impl) OR an already-
                    #    completed lock-free registration (R2 bug).
                    idx = await asyncio.to_thread(
                        _wait_first, (lock_requested, registered), 15
                    )
                    result["signal"] = (
                        ("lock_requested", "registered")[idx]
                        if idx >= 0 else None
                    )
                    # 3. release the cleaner; its delete-pass completion
                    #    is recorded under the still-held lock.
                    cleaner_gate.set()
                    result["delete_done"] = await asyncio.to_thread(
                        cleaner_deleted.wait, 15
                    )
                    await task
            finally:
                cleaner_gate.set()
                if th.ident is not None:
                    th.join(timeout=15)
                result["thread_alive"] = th.is_alive()

        asyncio.run(_driver())
        self.assertFalse(
            result.get("thread_alive", True), "cleaner thread leaked past join"
        )
        # The interleave must actually have been staged — a missed
        # barrier must FAIL, not silently degrade into a vacuous pass.
        self.assertTrue(
            result.get("checked_ok"),
            "cleaner never reached the in-lock active check — the "
            "reverse interleave was not exercised",
        )
        self.assertTrue(
            result.get("reg_saw_checked"),
            "registration stub never observed the cleaner's in-lock check",
        )
        self.assertTrue(
            result.get("gate_released"),
            "cleaner timed out waiting on cleaner_gate",
        )
        self.assertTrue(
            result.get("delete_done"),
            "cleaner's locked delete pass never completed",
        )
        self.assertIsNotNone(
            result.get("signal"),
            "neither a clean-lock attempt nor a completed registration "
            "was observed — the interleave was never staged",
        )
        # THE core assertion: with a correctly locked registration the
        # delete pass (recorded under the still-held lock) MUST precede
        # the registration's completion. A lock-free registration lands
        # while the cleaner is parked → ["registered", "deleted"] → FAIL.
        self.assertEqual(
            order, ["deleted", "registered"],
            "registration completed inside the cleaner's check→delete "
            "window — registration bypassed _pig_session_clean_lock "
            f"(signal={result.get('signal')}, order={order})",
        )
        # Legal order is therefore exactly one: the cleaner ran its whole
        # check+delete before the registration took effect → the expired
        # .jsonl was lawfully removed...
        self.assertEqual(
            result.get("removed"), 1,
            "cleaner-first order must delete the expired .jsonl before "
            "the registration lands (removed != 1 means either the "
            "cleaner skipped an inactive tree or the interleave broke)",
        )
        self.assertFalse(
            os.path.exists(old_f),
            "cleaner reported removing old.jsonl but it still exists",
        )
        # ...and afterwards the registration really did land.
        self.assertTrue(
            registered.is_set(),
            "run_pig never completed its registration — the reverse "
            "interleave scenario did not execute",
        )

    # -- MF-A: registration-future cancellation must reap the worker -----

    def test_cancelled_registration_reaps_active_key(self):
        """MF-A/MF-B': run_pig's off-loop registration cannot be
        un-scheduled once the pool thread started — cancelling the
        coroutine while the cleaner holds the clean lock must still pair
        the worker's late +1 refcount with an unregister, so the
        sessions tree is NOT pinned active forever, no pig subprocess is
        ever spawned, and no helper thread (reaper) lingers.

        Every phase is proven by a real signal, never a sleep:
          * ``worker_enter``  — _register_capturing stub entered → the
            pool thread demonstrably started;
          * ``late_reg``      — the worker's stub registration RETURNED
            while the task was already cancelled → the run really went
            through the late-registration window (not a queued-cancel);
          * ``unregistered``  — the refcount repayment actually ran;
        and after the run we additionally probe the clean lock state to
        rule out a worker left parked on it.
        """
        import asyncio
        import threading
        import wechatbridge.runner_common as rc
        from wechatbridge import pig as pigmod
        from wechatbridge.config import config

        uid = _uid("cancelreg")
        session_dir = self._session_dir(uid)
        pigmod.ensure_user_pig(session_dir)
        sroot = pigmod._pig_sessions_dir(session_dir)
        clean_lock = rc._pig_session_clean_lock(sroot)
        spawn_calls = []
        result = {}

        worker_enter = threading.Event()
        late_reg = threading.Event()
        unregistered = threading.Event()

        orig_reg = pigmod._register_active_pig_session
        orig_unreg = pigmod._unregister_active_pig_session

        def _reg_flag(root):
            worker_enter.set()
            key = orig_reg(root)
            if result.get("cancel_raised"):
                # Registration completed only AFTER the await was
                # cancelled — the true late-registration window.
                late_reg.set()
            return key

        def _unreg_flag(key):
            unregistered.set()
            return orig_unreg(key)

        async def _driver():
            # The driver stands in for the TTL cleaner holding the
            # per-session clean lock through a disk pass: the run_pig
            # registration worker blocks on this very lock.
            clean_lock.acquire()
            released = False
            try:
                with mock.patch.object(
                    config, "pig_binary_path", self._pig_shim()
                ), mock.patch.object(
                    pigmod, "_register_active_pig_session", _reg_flag
                ), mock.patch.object(
                    pigmod, "_unregister_active_pig_session", _unreg_flag
                ), mock.patch.object(
                    pigmod, "_spawn_pig",
                    lambda *a, **k: spawn_calls.append(1),
                ):
                    task = asyncio.create_task(
                        pigmod.run_pig("hi", uid, timeout=5)
                    )
                    # Proof the worker actually STARTED (it is now parked
                    # on the clean lock we hold) — not a queued-cancel.
                    result["worker_started"] = await asyncio.to_thread(
                        worker_enter.wait, 15
                    )
                    task.cancel()
                    result["cancel_raised"] = False
                    try:
                        await task
                    except asyncio.CancelledError:
                        result["cancel_raised"] = True
                    # Release the "cleaner" while the stub patches are
                    # still live: the orphaned worker thread now acquires
                    # the lock, lands its +1 registration and repays it —
                    # both flagged through the still-stubbed helpers.
                    clean_lock.release()
                    released = True
                    result["late_reg_ok"] = await asyncio.to_thread(
                        late_reg.wait, 15
                    )
                    result["unreg_ok"] = await asyncio.to_thread(
                        unregistered.wait, 15
                    )
            finally:
                if not released:
                    clean_lock.release()

        asyncio.run(_driver())
        self.assertTrue(
            result.get("worker_started"),
            "registration worker never started — cancellation would hit "
            "a queued future and skip the late-registration window",
        )
        self.assertTrue(
            result.get("cancel_raised"),
            "run_pig was not cancelled while its registration was "
            "blocked — the leak window was not exercised",
        )
        self.assertTrue(
            result.get("late_reg_ok"),
            "the cancelled worker's registration never landed — the "
            "late-registration window was never exercised",
        )
        self.assertTrue(
            result.get("unreg_ok"),
            "the late +1 registration was never repaid — "
            "_unregister_active_pig_session was never called",
        )
        self.assertFalse(
            rc._pig_session_root_active(sroot),
            "cancelled registration leaked an active refcount — the "
            "sessions tree would be skipped by the 30d TTL forever",
        )
        self.assertEqual(
            len(spawn_calls), 0,
            "cancelled run must never spawn the pig subprocess",
        )
        # No reaper/helper thread may outlive the scenario (the
        # cancellation path must repay the key with zero extra threads).
        reaper_threads = [
            t.name for t in threading.enumerate()
            if "pig-reg-reaper" in t.name
        ]
        self.assertEqual(
            reaper_threads, [],
            f"reaper threads survived the cancelled run: {reaper_threads}",
        )
        # The worker must not be left parked on the clean lock either —
        # an uncontended acquire/try-release proves nobody still holds it.
        self.assertTrue(clean_lock.acquire(timeout=5))
        clean_lock.release()

    # -- MF-8: /persona show redact-before-truncate + FIFO non-block ------

    def test_persona_show_redacts_key_at_truncate_boundary(self):
        """MF-8a: a live key straddling the 1500-char cut must be redacted
        BEFORE truncation — no key prefix may echo to the user."""
        import asyncio
        from wechatbridge import pig as pigmod

        uid = _uid("ptrunc")
        session_dir = self._session_dir(uid)
        os.makedirs(session_dir, exist_ok=True)
        key = "test-magpie-key-0123456789"
        persona_path = os.path.join(session_dir, "pig_persona.txt")
        # Place the key so it would straddle an un-redacted [:1500] cut:
        # the redacted text is shorter, so the cut happens after replace.
        body = "x" * 1490 + key + "y" * 200
        with open(persona_path, "w") as f:
            f.write(body)
        out = asyncio.run(pigmod.handle_pig_slash_command("/persona show", uid))
        self.assertNotIn(key, out)
        self.assertNotIn(key[: len(key) // 2], out)

    def test_read_pig_persona_file_fifo_nonblocking(self):
        """MF-8b: a FIFO planted as pig_persona.txt must not block the
        event loop; _read_pig_persona_file returns "" quickly."""
        from wechatbridge import pig as pigmod

        session_dir = self._session_dir()
        os.makedirs(session_dir, exist_ok=True)
        fifo_path = os.path.join(session_dir, "pig_persona.txt")
        os.mkfifo(fifo_path)
        start = time.time()
        # A blocking open() would hang forever; assert sub-2s return.
        out = pigmod._read_pig_persona_file(fifo_path)
        self.assertLess(time.time() - start, 2.0)
        self.assertEqual(out, "")

    # -- SF-7: extract_artifacts markdown links prefiltered ----------------

    def test_link_artifacts_prefiltered_sensitive_keeps_quota(self):
        """SF-7: 10 sensitive markdown links must NOT consume the
        MAX_OUTBOUND_ARTIFACTS_PER_TURN quota — the one legit file stays."""
        session_dir = self._session_dir()
        os.makedirs(session_dir, exist_ok=True)
        # legit report
        report = os.path.join(session_dir, "report.pdf")
        with open(report, "wb") as f:
            f.write(b"%PDF-1.4 ok")
        # 10 sensitive links into the credential tree
        sensitive = []
        for i in range(10):
            p = os.path.join(session_dir, ".pig", "agent", f"s{i}.json")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write("{}")
            sensitive.append(p)
        text = "\n".join(
            f"[s{i}.json]({p})" for i, p in enumerate(sensitive)
        ) + f"\n[report.pdf]({report})"
        stdout = json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": text}],
            },
        })
        from wechatbridge import pig as pigmod
        display, artifacts, failed, _ = pigmod._parse_pig_output(
            stdout, "", 0, session_dir, {}, time.time(), []
        )
        paths = [p for _, p in artifacts]
        self.assertIn(os.path.realpath(report), paths)
        # No sensitive file leaked into the artifact list (quota intact).
        for p in sensitive:
            self.assertNotIn(os.path.realpath(p), paths)

    # -- MF-1: failure notice never echoes a live key in the name ---------

    def test_send_artifacts_back_notice_redacts_live_key(self):
        """MF-1: when send_artifacts_back blocks an artifact whose name
        embeds the live key, the WeChat notice must not echo it."""
        import asyncio
        from wechatbridge.main import send_artifacts_back
        from wechatbridge.config import config
        from unittest.mock import MagicMock, AsyncMock

        key = "test-magpie-key-0123456789"
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, ".gemini", "antigravity-cli", "scratch")
            os.makedirs(scratch)
            evil_name = f"report_{key}.pdf"
            evil_path = os.path.join(scratch, evil_name)
            with open(evil_path, "wb") as f:
                f.write(b"%PDF")
            client = MagicMock()
            client.state.baseurl = "https://example.test"
            client.state.bot_token = "tok"
            client.send_message = AsyncMock(return_value=True)
            client.send_media = AsyncMock(return_value=True)
            with mock.patch(
                "wechatbridge.main.get_session_dir", return_value=td
            ), mock.patch(
                "wechatbridge.main._get_backend", return_value="agy"
            ):
                asyncio.run(
                    send_artifacts_back(
                        client, "user-1", "ctx-token", [(evil_name, evil_path)]
                    )
                )
            client.send_media.assert_not_awaited()
            client.send_message.assert_awaited()
            sent_text = client.send_message.await_args.kwargs["text"]
            self.assertNotIn(key, sent_text)
            self.assertIn("[REDACTED]", sent_text)


if __name__ == "__main__":
    unittest.main()
