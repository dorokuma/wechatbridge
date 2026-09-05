"""Hardening / cleanup / message-safety probes (stdlib unittest only).

Exercises real production helpers and async methods with mocks — not
string-copied stand-ins of the user-facing copy.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock
from unittest.mock import AsyncMock, MagicMock


class TestSplitMessageChunks(unittest.TestCase):
    def test_join_equals_original_fixed(self):
        from wechatbridge.runner_common import split_message_chunks

        samples = [
            "",
            "short",
            "a" * 50,
            "line1\nline2\nline3",
            "word " * 400,
            "x" * 10 + "\n" + "y" * 10,
            "  keep  spaces  at  edges  ",
            "\n\n\n",
            "中文" * 300,
        ]
        for text in samples:
            for limit in (5, 20, 80, 2000):
                chunks = split_message_chunks(text, limit)
                self.assertEqual("".join(chunks), text, msg=repr(text[:40]))
                for c in chunks:
                    self.assertLessEqual(len(c), limit if limit > 0 else len(c))

    def test_random_join_equals_original(self):
        import random
        from wechatbridge.runner_common import split_message_chunks

        rng = random.Random(42)
        alphabet = "abc \n中文，。"
        for _ in range(30):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 500)))
            limit = rng.randint(1, 120)
            chunks = split_message_chunks(text, limit)
            self.assertEqual("".join(chunks), text)


class TestPathIsUnder(unittest.TestCase):
    def test_child_and_escape(self):
        from wechatbridge.runner_common import path_is_under

        with tempfile.TemporaryDirectory() as td:
            child = os.path.join(td, "a", "b")
            os.makedirs(child)
            self.assertTrue(path_is_under(child, td))
            self.assertTrue(path_is_under(td, td))
            outside = os.path.join(
                tempfile.gettempdir(), "not-under-" + os.path.basename(td)
            )
            self.assertFalse(path_is_under(outside, td))

    def test_symlink_escape_blocked(self):
        from wechatbridge.runner_common import path_is_under

        with tempfile.TemporaryDirectory() as td:
            allowed = os.path.join(td, "allowed")
            secret = os.path.join(td, "secret")
            os.makedirs(allowed)
            os.makedirs(secret)
            leak = os.path.join(allowed, "leak")
            os.symlink(secret, leak)
            self.assertFalse(path_is_under(leak, allowed))


class TestRemoveOldFilesDangling(unittest.TestCase):
    def test_dangling_file_and_dir_links_removed_immediately(self):
        from wechatbridge.runner_common import _remove_old_files_under

        with tempfile.TemporaryDirectory() as td:
            bad_file = os.path.join(td, "python")
            os.symlink("/no/such/target/python-xyz", bad_file)
            bad_dir = os.path.join(td, "lib64")
            os.symlink("/no/such/lib", bad_dir)
            keep = os.path.join(td, "keep.txt")
            with open(keep, "w", encoding="utf-8") as f:
                f.write("ok")
            old = os.path.join(td, "old.txt")
            with open(old, "w", encoding="utf-8") as f:
                f.write("bye")
            old_mtime = time.time() - 10 * 86400
            os.utime(old, (old_mtime, old_mtime))

            cutoff = time.time() - 7 * 86400
            removed = _remove_old_files_under(td, cutoff)

            self.assertGreaterEqual(removed, 3)
            self.assertFalse(os.path.lexists(bad_file))
            self.assertFalse(os.path.lexists(bad_dir))
            self.assertFalse(os.path.exists(old))
            self.assertTrue(os.path.exists(keep))

    def test_intact_young_symlink_kept(self):
        from wechatbridge.runner_common import _remove_old_files_under

        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, "real.txt")
            with open(target, "w", encoding="utf-8") as f:
                f.write("data")
            link = os.path.join(td, "alias")
            os.symlink(target, link)
            cutoff = time.time() - 7 * 86400
            _remove_old_files_under(td, cutoff)
            self.assertTrue(os.path.lexists(link))
            self.assertTrue(os.path.exists(target))

    def test_does_not_follow_symlink_into_outside_tree(self):
        """followlinks=False: must not age-delete files outside root via symlink."""
        from wechatbridge.runner_common import _remove_old_files_under

        with tempfile.TemporaryDirectory() as outer:
            with tempfile.TemporaryDirectory() as root:
                victim = os.path.join(outer, "outside-old.txt")
                with open(victim, "w", encoding="utf-8") as f:
                    f.write("keep-me")
                old_mtime = time.time() - 10 * 86400
                os.utime(victim, (old_mtime, old_mtime))
                os.symlink(outer, os.path.join(root, "escape"))
                cutoff = time.time() - 7 * 86400
                _remove_old_files_under(root, cutoff)
                self.assertTrue(os.path.exists(victim), "must not delete outside tree")


class TestOversizedArtifactNotice(unittest.TestCase):
    def test_helper_never_embeds_absolute_path(self):
        from wechatbridge.runner_common import format_oversized_artifact_notice

        art_path = "/root/.local/share/wechatbridge/default/sessions/u1/scratch/report.pdf"
        text = format_oversized_artifact_notice("report.pdf", 120.5)
        self.assertNotIn(art_path, text)
        self.assertNotIn("/root/", text)
        self.assertNotIn("sessions/", text)
        self.assertIn("report.pdf", text)
        self.assertIn("无法发到微信", text)

    def test_send_artifacts_back_uses_safe_notice(self):
        """Call the real async helper; assert user text has no server path."""
        from wechatbridge.main import send_artifacts_back

        async def _run():
            with tempfile.TemporaryDirectory() as td:
                scratch = os.path.join(td, ".gemini", "antigravity-cli", "scratch")
                os.makedirs(scratch)
                big_path = os.path.join(scratch, "huge.bin")
                with open(big_path, "wb") as f:
                    f.write(b"x" * 64)

                client = MagicMock()
                client.state.baseurl = "https://example.test"
                client.state.bot_token = "tok"
                client.send_message = AsyncMock(return_value=True)
                client.send_media = AsyncMock(return_value=True)

                with mock.patch(
                    "wechatbridge.main.get_session_dir", return_value=td
                ), mock.patch(
                    "wechatbridge.main._get_backend", return_value="agy"
                ), mock.patch(
                    "wechatbridge.main.config"
                ) as cfg:
                    cfg.max_outbound_file_bytes = 8  # force oversized
                    await send_artifacts_back(
                        client,
                        "user-1",
                        "ctx-token",
                        [("huge.bin", big_path)],
                    )

                client.send_media.assert_not_awaited()
                client.send_message.assert_awaited()
                kwargs = client.send_message.await_args.kwargs
                text = kwargs["text"]
                self.assertNotIn(big_path, text)
                self.assertNotIn(td, text)
                self.assertIn("huge.bin", text)
                self.assertIn("无法发到微信", text)

        asyncio.run(_run())


class TestFormatCliErrorCodex(unittest.TestCase):
    """format_cli_error must recognise codex-specific login/未登录 signals
    without misclassifying ordinary API errors, rate limits or model errors.
    agy/grok results must stay unchanged."""

    def _fmt(self, raw, backend):
        from wechatbridge.runner_common import format_cli_error
        return format_cli_error(raw, backend=backend)

    # --- codex: positive (should be 未登录) ---
    def test_codex_login_phrase(self):
        out = self._fmt("Error: you must run `codex login` to authenticate", "codex")
        self.assertIn("**未登录**", out)

    def test_codex_api_key_env(self):
        out = self._fmt("CODEX_API_KEY is not set or is invalid", "codex")
        self.assertIn("**未登录**", out)

    def test_codex_api_key_spaced(self):
        out = self._fmt("Please set the codex api key before use", "codex")
        self.assertIn("**未登录**", out)

    def test_codex_api_key_hyphen(self):
        out = self._fmt("Set a codex api-key to use the CLI", "codex")
        self.assertIn("**未登录**", out)

    def test_codex_authentication_required(self):
        out = self._fmt("Authentication required: sign in first", "codex")
        self.assertIn("**未登录**", out)

    def test_codex_not_authenticated(self):
        out = self._fmt("Request failed: you are not authenticated", "codex")
        self.assertIn("**未登录**", out)

    def test_codex_not_logged_in(self):
        out = self._fmt("You are not logged in to codex", "codex")
        self.assertIn("**未登录**", out)

    def test_codex_please_login(self):
        out = self._fmt("Please log in to continue", "codex")
        self.assertIn("**未登录**", out)

    def test_codex_unauthorized(self):
        out = self._fmt("401 Unauthorized: invalid auth token", "codex")
        self.assertIn("**未登录**", out)

    def test_codex_no_valid_credentials(self):
        out = self._fmt("No valid credentials found for this request", "codex")
        self.assertIn("**未登录**", out)

    # --- codex: negative (must NOT be 未登录) ---
    def test_codex_rate_limit_not_login(self):
        out = self._fmt("Rate limit reached, please slow down", "codex")
        self.assertNotIn("**未登录**", out)
        self.assertIn("**请求较多**", out)
        self.assertIn("🔔", out)
        self.assertNotIn("❌", out)

    def test_codex_model_not_found_not_login(self):
        out = self._fmt("error: model not found: gpt-9", "codex")
        self.assertNotIn("**未登录**", out)
        self.assertIn("**模型无效**", out)

    def test_codex_api_error_not_login(self):
        out = self._fmt("API error: 500 Internal Server Error", "codex")
        self.assertNotIn("**未登录**", out)
        self.assertIn("**执行失败**", out)

    def test_codex_bad_request_not_login(self):
        out = self._fmt("Bad request: invalid parameters", "codex")
        self.assertNotIn("**未登录**", out)
        self.assertIn("**执行失败**", out)

    def test_codex_file_not_found_not_login(self):
        out = self._fmt("file not found: ./main.py", "codex")
        self.assertNotIn("**未登录**", out)
        self.assertIn("**未找到**", out)

    # --- regression: agy/grok results unchanged ---
    def test_agy_not_signed_in_still_login(self):
        out = self._fmt("Error: not signed in", "agy")
        self.assertIn("**未登录**", out)

    def test_grok_login_still_login(self):
        out = self._fmt("Run `grok login` first", "grok")
        self.assertIn("**未登录**", out)

    def test_agy_does_not_recognise_codex_login(self):
        """Backend scoping: a codex-only hint ('codex login') under agy must
        NOT become 未登录 (it is not matched by the generic auth block)."""
        out = self._fmt("Run `codex login --with-api-key` to begin", "agy")
        self.assertNotIn("**未登录**", out)
        self.assertIn("**执行失败**", out)


class TestSendArtifactsBackCodexAddDirs(unittest.IsolatedAsyncioTestCase):
    """Second-factor verification of codex --add-dir roots at send time.

    Uses a mock client (no network). Verifies the real send function never
    calls the upload/send API for artifacts reachable only via an invalid
    add-dir root: deleted dir, plain file, out-of-bounds path, symlink escape.
    Legitimate directories are still sent back.
    """

    async def test_add_dir_roots_reverified(self):
        from wechatbridge.main import send_artifacts_back
        from wechatbridge.config import config

        with tempfile.TemporaryDirectory() as base:
            session_dir = os.path.join(base, "session")
            os.makedirs(session_dir)
            allowed_extra = os.path.join(base, "allowed_extra")
            os.makedirs(allowed_extra)

            # legitimate add_dir under an allowed root
            good_dir = os.path.join(allowed_extra, "proj")
            os.makedirs(good_dir)
            good_art = os.path.join(good_dir, "out.txt")
            with open(good_art, "w", encoding="utf-8") as f:
                f.write("ok")

            # deleted dir (created then removed)
            gone_dir = os.path.join(allowed_extra, "gone")
            os.makedirs(gone_dir)
            gone_art = os.path.join(gone_dir, "x.txt")
            with open(gone_art, "w", encoding="utf-8") as f:
                f.write("x")
            shutil.rmtree(gone_dir)

            # plain file (not a directory)
            file_dir = os.path.join(allowed_extra, "notdir")
            with open(file_dir, "w", encoding="utf-8") as f:
                f.write("i am a file")
            file_art = file_dir  # artifact is the file itself

            # out-of-bounds dir (outside configured allowed roots)
            oob = os.path.join(base, "oob")
            os.makedirs(oob)
            oob_art = os.path.join(oob, "secret.txt")
            with open(oob_art, "w", encoding="utf-8") as f:
                f.write("secret")

            # symlink escape: allowed_extra/escape -> oob (outside allowed roots)
            escape_link = os.path.join(allowed_extra, "escape")
            os.symlink(oob, escape_link)
            escape_art = os.path.join(oob, "leak.txt")
            with open(escape_art, "w", encoding="utf-8") as f:
                f.write("leak")

            # control: artifact directly under session_dir (always allowed)
            ctrl_art = os.path.join(session_dir, "ctrl.txt")
            with open(ctrl_art, "w", encoding="utf-8") as f:
                f.write("ctrl")

            prefs = {
                "backend": "codex",
                "add_dirs": [good_dir, gone_dir, file_dir, oob, escape_link],
            }
            prefs_path = os.path.join(session_dir, "prefs.json")
            with open(prefs_path, "w", encoding="utf-8") as f:
                json.dump(prefs, f)

            client = MagicMock()
            client.state.baseurl = "https://example.test"
            client.state.bot_token = "tok"
            client.send_message = AsyncMock(return_value=True)
            client.send_media = AsyncMock(return_value=True)

            artifacts = [
                ("out.txt", good_art),
                ("x.txt", gone_art),
                ("notdir", file_art),
                ("secret.txt", oob_art),
                ("leak.txt", escape_art),
                ("ctrl.txt", ctrl_art),
            ]

            with mock.patch(
                "wechatbridge.main.get_session_dir", return_value=session_dir
            ), mock.patch(
                "wechatbridge.runner_common.get_session_dir", return_value=session_dir
            ), mock.patch(
                "wechatbridge.main._get_backend", return_value="codex"
            ), mock.patch.object(
                config, "add_dir_roots", [allowed_extra]
            ):
                await send_artifacts_back(
                    client, "user-1", "ctx-token", artifacts
                )

            sent = {c.kwargs["path"] for c in client.send_media.await_args_list}

            # Legitimate add-dir artifact + session control are sent.
            self.assertIn(good_art, sent)
            self.assertIn(ctrl_art, sent)

            # Invalid add-dir roots never become allow roots -> no upload.
            self.assertNotIn(gone_art, sent)
            self.assertNotIn(file_art, sent)
            self.assertNotIn(oob_art, sent)
            self.assertNotIn(escape_art, sent)

            # Exactly the two legitimate artifacts are uploaded; the send API is
            # never called for any deleted/file/oob/symlink-escape target.
            self.assertEqual(len(client.send_media.await_args_list), 2)

            # Skipped (whitelist) artifacts get a short Chinese notice — no abs path.
            self.assertEqual(client.send_message.await_count, 4)
            for call in client.send_message.await_args_list:
                text = call.kwargs["text"]
                self.assertIn("未能发送", text)
                self.assertNotIn(base, text)
                self.assertNotIn(session_dir, text)
                self.assertNotIn(oob, text)


class TestAgyExtractArtifactsUnquote(unittest.TestCase):
    """agy extract_artifacts must URL-decode percent-encoded paths/names."""

    def test_space_and_cjk_percent_encoded(self):
        from wechatbridge.agy import extract_artifacts

        text = (
            "here is [my report.pdf](file:///tmp/scratch/my%20report.pdf) "
            "and [报告.pdf](file:///tmp/scratch/%E6%8A%A5%E5%91%8A.pdf)"
        )
        arts = extract_artifacts(text)
        paths = {p for _, p in arts}
        names = {n for n, _ in arts}
        self.assertIn("/tmp/scratch/my report.pdf", paths)
        self.assertIn("/tmp/scratch/报告.pdf", paths)
        self.assertIn("my report.pdf", names)
        self.assertIn("报告.pdf", names)
        # Encoded forms must not leak into resolved paths
        for p in paths:
            self.assertNotIn("%20", p)
            self.assertNotIn("%E6", p)


class TestSendArtifactsBackAgyAddDirs(unittest.IsolatedAsyncioTestCase):
    """agy must honour validated --add-dir roots at send time (like codex)."""

    async def test_agy_add_dir_artifact_sent(self):
        from wechatbridge.main import send_artifacts_back
        from wechatbridge.config import config

        with tempfile.TemporaryDirectory() as base:
            session_dir = os.path.join(base, "session")
            os.makedirs(session_dir)
            scratch = os.path.join(
                session_dir, ".gemini", "antigravity-cli", "scratch"
            )
            os.makedirs(scratch)

            allowed_extra = os.path.join(base, "allowed_extra")
            os.makedirs(allowed_extra)
            good_dir = os.path.join(allowed_extra, "proj")
            os.makedirs(good_dir)
            good_art = os.path.join(good_dir, "doc.pdf")
            with open(good_art, "w", encoding="utf-8") as f:
                f.write("%PDF-1.4 ok")

            scratch_art = os.path.join(scratch, "local.txt")
            with open(scratch_art, "w", encoding="utf-8") as f:
                f.write("scratch")

            # out-of-bounds must still be blocked
            oob = os.path.join(base, "oob")
            os.makedirs(oob)
            oob_art = os.path.join(oob, "secret.txt")
            with open(oob_art, "w", encoding="utf-8") as f:
                f.write("secret")

            prefs = {"backend": "agy", "add_dirs": [good_dir]}
            with open(
                os.path.join(session_dir, "prefs.json"), "w", encoding="utf-8"
            ) as f:
                json.dump(prefs, f)

            client = MagicMock()
            client.state.baseurl = "https://example.test"
            client.state.bot_token = "tok"
            client.send_message = AsyncMock(return_value=True)
            client.send_media = AsyncMock(return_value=True)

            artifacts = [
                ("doc.pdf", good_art),
                ("local.txt", scratch_art),
                ("secret.txt", oob_art),
            ]

            with mock.patch(
                "wechatbridge.main.get_session_dir", return_value=session_dir
            ), mock.patch(
                "wechatbridge.runner_common.get_session_dir", return_value=session_dir
            ), mock.patch(
                "wechatbridge.main._get_backend", return_value="agy"
            ), mock.patch.object(
                config, "add_dir_roots", [allowed_extra]
            ):
                await send_artifacts_back(
                    client, "user-1", "ctx-token", artifacts
                )

            sent = {c.kwargs["path"] for c in client.send_media.await_args_list}
            self.assertIn(good_art, sent)
            self.assertIn(scratch_art, sent)
            self.assertNotIn(oob_art, sent)
            self.assertEqual(len(client.send_media.await_args_list), 2)


class TestMediaTypeForPath(unittest.TestCase):
    def test_pdf_png_svg(self):
        from wechatbridge.ilink import media_type_for_path, MEDIA_IMAGE, MEDIA_FILE

        self.assertEqual(media_type_for_path("/tmp/a.pdf"), MEDIA_FILE)
        self.assertEqual(media_type_for_path("/tmp/a.docx"), MEDIA_FILE)
        self.assertEqual(media_type_for_path("/tmp/a.xlsx"), MEDIA_FILE)
        self.assertEqual(media_type_for_path("/tmp/a.txt"), MEDIA_FILE)
        self.assertEqual(media_type_for_path("/tmp/a.zip"), MEDIA_FILE)
        self.assertEqual(media_type_for_path("/tmp/a.png"), MEDIA_IMAGE)
        self.assertEqual(media_type_for_path("/tmp/a.jpg"), MEDIA_IMAGE)
        self.assertEqual(media_type_for_path("/tmp/a.jpeg"), MEDIA_IMAGE)
        self.assertEqual(media_type_for_path("/tmp/a.gif"), MEDIA_IMAGE)
        self.assertEqual(media_type_for_path("/tmp/a.webp"), MEDIA_IMAGE)
        self.assertEqual(media_type_for_path("/tmp/a.svg"), MEDIA_FILE)
        self.assertEqual(media_type_for_path("/tmp/a.heic"), MEDIA_FILE)
        self.assertEqual(media_type_for_path("/tmp/a.bin"), MEDIA_FILE)


class TestArtifactSendFailureNotice(unittest.TestCase):
    def test_helper_no_absolute_path(self):
        from wechatbridge.runner_common import format_artifact_send_failure_notice

        art_path = "/root/.local/share/wechatbridge/default/sessions/u1/x.pdf"
        for reason in ("skipped", "not_found", "send_failed", "error"):
            text = format_artifact_send_failure_notice("x.pdf", reason)
            self.assertNotIn(art_path, text)
            self.assertNotIn("/root/", text)
            self.assertIn("x.pdf", text)
            self.assertIn("未能发送", text)

    def test_send_failed_notifies_user(self):
        from wechatbridge.main import send_artifacts_back

        async def _run():
            with tempfile.TemporaryDirectory() as td:
                scratch = os.path.join(td, ".gemini", "antigravity-cli", "scratch")
                os.makedirs(scratch)
                art = os.path.join(scratch, "report.pdf")
                with open(art, "w", encoding="utf-8") as f:
                    f.write("pdf")

                client = MagicMock()
                client.state.baseurl = "https://example.test"
                client.state.bot_token = "tok"
                client.send_message = AsyncMock(return_value=True)
                client.send_media = AsyncMock(return_value=False)  # send fails

                with mock.patch(
                    "wechatbridge.main.get_session_dir", return_value=td
                ), mock.patch(
                    "wechatbridge.main._get_backend", return_value="agy"
                ):
                    await send_artifacts_back(
                        client, "user-1", "ctx-token", [("report.pdf", art)]
                    )

                client.send_media.assert_awaited()
                client.send_message.assert_awaited()
                text = client.send_message.await_args.kwargs["text"]
                self.assertIn("report.pdf", text)
                self.assertIn("未能发送", text)
                self.assertNotIn(art, text)
                self.assertNotIn(td, text)

        asyncio.run(_run())

    def test_not_found_notifies_user(self):
        from wechatbridge.main import send_artifacts_back

        async def _run():
            with tempfile.TemporaryDirectory() as td:
                scratch = os.path.join(td, ".gemini", "antigravity-cli", "scratch")
                os.makedirs(scratch)
                missing = os.path.join(scratch, "gone.pdf")

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
                    await send_artifacts_back(
                        client, "user-1", "ctx-token", [("gone.pdf", missing)]
                    )

                client.send_media.assert_not_awaited()
                client.send_message.assert_awaited()
                text = client.send_message.await_args.kwargs["text"]
                self.assertIn("gone.pdf", text)
                self.assertIn("未能发送", text)
                self.assertNotIn(missing, text)
                self.assertNotIn(td, text)

        asyncio.run(_run())


class TestGrokRelativePathArtifacts(unittest.TestCase):
    """grok relative file_path must join session_dir then abspath (like codex)."""

    def test_relative_path_resolved(self):
        from wechatbridge.grok import _extract_grok_artifacts
        import urllib.parse

        with tempfile.TemporaryDirectory() as session_dir:
            # Create the relative file under session_dir
            rel_name = "notes/out.txt"
            abs_file = os.path.join(session_dir, "notes", "out.txt")
            os.makedirs(os.path.dirname(abs_file))
            with open(abs_file, "w", encoding="utf-8") as f:
                f.write("hello")

            # Fake grok chat_history.jsonl layout
            cwd_encoded = urllib.parse.quote(session_dir, safe="")
            session_id = "sess-rel-1"
            hist_dir = os.path.join(
                session_dir, ".grok", "sessions", cwd_encoded, session_id
            )
            os.makedirs(hist_dir)
            hist = os.path.join(hist_dir, "chat_history.jsonl")
            line = {
                "type": "assistant",
                "tool_calls": [
                    {
                        "name": "write",
                        "arguments": json.dumps({"file_path": rel_name}),
                    }
                ],
            }
            with open(hist, "w", encoding="utf-8") as f:
                f.write(json.dumps(line) + "\n")

            arts = _extract_grok_artifacts(session_dir, session_id, since=0.0)
            self.assertEqual(len(arts), 1)
            name, path = arts[0]
            self.assertEqual(name, "out.txt")
            self.assertEqual(path, os.path.abspath(abs_file))
            self.assertTrue(os.path.isabs(path))

    def test_search_replace_path_key(self):
        from wechatbridge.grok import _extract_grok_artifacts
        import urllib.parse

        with tempfile.TemporaryDirectory() as session_dir:
            abs_file = os.path.join(session_dir, "report.docx")
            with open(abs_file, "w", encoding="utf-8") as f:
                f.write("doc")
            session_id = "sess-sr-1"
            hist_dir = os.path.join(
                session_dir,
                ".grok",
                "sessions",
                urllib.parse.quote(session_dir, safe=""),
                session_id,
            )
            os.makedirs(hist_dir)
            line = {
                "type": "assistant",
                "tool_calls": [
                    {
                        "name": "search_replace",
                        "arguments": {"path": "report.docx"},
                    }
                ],
            }
            with open(os.path.join(hist_dir, "chat_history.jsonl"), "w", encoding="utf-8") as f:
                f.write(json.dumps(line) + "\n")
            arts = _extract_grok_artifacts(session_dir, session_id, since=0.0)
            self.assertEqual(arts, [("report.docx", os.path.abspath(abs_file))])


class TestGrokSessionScanArtifacts(unittest.TestCase):
    def test_scan_picks_new_pdf_skips_bundled(self):
        from wechatbridge.grok import _scan_grok_session_artifacts, _merge_grok_artifacts

        with tempfile.TemporaryDirectory() as session_dir:
            pdf = os.path.join(session_dir, "out.pdf")
            with open(pdf, "wb") as f:
                f.write(b"%PDF-1.4")
            bundled_dir = os.path.join(session_dir, ".grok", "bundled", "skills", "pdf")
            os.makedirs(bundled_dir)
            bundled = os.path.join(bundled_dir, "form.pdf")
            with open(bundled, "wb") as f:
                f.write(b"%PDF-1.4")
            old = os.path.join(session_dir, "old.docx")
            with open(old, "wb") as f:
                f.write(b"PK")
            os.utime(old, (1_000_000, 1_000_000))
            arts = _scan_grok_session_artifacts(session_dir, since=time.time())
            paths = {p for _, p in arts}
            self.assertIn(os.path.realpath(pdf), paths)
            self.assertNotIn(os.path.realpath(bundled), paths)
            self.assertNotIn(os.path.realpath(old), paths)

    def test_merge_dedupes_by_realpath(self):
        from wechatbridge.grok import _merge_grok_artifacts

        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "a.pdf")
            with open(p, "wb") as f:
                f.write(b"x")
            merged = _merge_grok_artifacts(
                [("a.pdf", p)],
                [("a.pdf", os.path.realpath(p))],
            )
            self.assertEqual(merged, [("a.pdf", p)])


class TestGrokAuthPassthrough(unittest.TestCase):
    def test_apply_reinjects_xai_key(self):
        from wechatbridge.grok import _apply_grok_runtime_env, _grok_has_credentials
        from wechatbridge.runner_common import sanitize_env

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"XAI_API_KEY": "secret-xai"}, clear=False):
                env = sanitize_env(td)
                self.assertNotIn("XAI_API_KEY", env)
                env = _apply_grok_runtime_env(env)
                self.assertEqual(env["XAI_API_KEY"], "secret-xai")
                self.assertTrue(_grok_has_credentials())

    def test_has_credentials_false_without_auth_or_key(self):
        from wechatbridge.grok import _grok_has_credentials

        with mock.patch("wechatbridge.grok._host_grok_dir", return_value="/no/such/grok-home"):
            with mock.patch.dict(os.environ, {"XAI_API_KEY": ""}, clear=False):
                os.environ.pop("XAI_API_KEY", None)
                self.assertFalse(_grok_has_credentials())


class TestILinkDeliveryAccepted(unittest.TestCase):
    def test_predicate(self):
        from wechatbridge.ilink import ilink_delivery_accepted

        self.assertTrue(ilink_delivery_accepted(0, ""))
        self.assertTrue(ilink_delivery_accepted(0, None))
        self.assertTrue(ilink_delivery_accepted(-1, "7487118974343175304"))
        self.assertTrue(ilink_delivery_accepted(1, "abc"))
        self.assertTrue(ilink_delivery_accepted(-1, 42))  # non-empty non-str id
        self.assertFalse(ilink_delivery_accepted(-1, ""))
        self.assertFalse(ilink_delivery_accepted(-1, "   "))
        self.assertFalse(ilink_delivery_accepted(1, None))
        self.assertFalse(ilink_delivery_accepted(1, 0))


class TestILinkPostSendmessageRetry(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from wechatbridge.ilink import ILinkClient

        self.client = ILinkClient()

    async def asyncTearDown(self):
        await self.client.http_client.aclose()

    async def test_ret_minus_one_with_message_id_is_success(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"ret": -1, "message_id": "mid-ok-1"}
        self.client.http_client.post = AsyncMock(return_value=mock_resp)

        ok = await self.client._post_sendmessage_with_retry(
            url="https://example.test/send",
            headers={},
            body={},
            to_user_id="u1",
            max_attempts=1,
        )
        self.assertTrue(ok)
        self.client.http_client.post.assert_awaited_once()

    async def test_ret_nonzero_without_message_id_fails_fast(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"ret": -1, "message_id": ""}
        self.client.http_client.post = AsyncMock(return_value=mock_resp)

        with mock.patch("wechatbridge.ilink.asyncio.sleep", new_callable=AsyncMock):
            ok = await self.client._post_sendmessage_with_retry(
                url="https://example.test/send",
                headers={},
                body={},
                to_user_id="u1",
                max_attempts=2,
            )
        self.assertFalse(ok)
        self.assertEqual(self.client.http_client.post.await_count, 2)

    async def test_ret_zero_succeeds(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"ret": 0, "message_id": "m0"}
        self.client.http_client.post = AsyncMock(return_value=mock_resp)
        ok = await self.client._post_sendmessage_with_retry(
            url="https://example.test/send",
            headers={},
            body={},
            to_user_id="u1",
            max_attempts=1,
        )
        self.assertTrue(ok)


class TestInboundStreamCapLogic(unittest.TestCase):
    def test_content_length_and_stream_abort_rules(self):
        max_in = 100

        def reject_cl(declared: int | None) -> bool:
            return declared is not None and declared > max_in

        def reject_buf(buf_len: int, piece_len: int) -> bool:
            return buf_len + piece_len > max_in

        self.assertTrue(reject_cl(101))
        self.assertFalse(reject_cl(100))
        self.assertFalse(reject_cl(None))
        self.assertTrue(reject_buf(90, 20))
        self.assertFalse(reject_buf(90, 10))


class TestSensitiveEnv(unittest.TestCase):
    def test_strips_api_keys_keeps_harmless(self):
        from wechatbridge.runner_common import _is_sensitive_env_name, sanitize_env

        self.assertTrue(_is_sensitive_env_name("XAI_API_KEY"))
        self.assertTrue(_is_sensitive_env_name("OPENAI_API_KEY"))
        self.assertTrue(_is_sensitive_env_name("BOT_TOKEN"))
        self.assertFalse(_is_sensitive_env_name("HOME"))
        self.assertFalse(_is_sensitive_env_name("PATH"))
        self.assertFalse(_is_sensitive_env_name("LANG"))

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(
                os.environ,
                {
                    "PATH": "/usr/bin",
                    "XAI_API_KEY": "secret",
                    "LANG": "C",
                    "HOME": "/tmp/other",
                },
                clear=False,
            ):
                env = sanitize_env(td)
            self.assertEqual(env["HOME"], td)
            self.assertNotIn("XAI_API_KEY", env)
            self.assertIn("LANG", env)
            self.assertEqual(env["LANG"], "C")


class TestClearInitializedIfNoHistory(unittest.TestCase):
    def test_codex_empty_history_clears_flag_and_thread_id_without_grok(self):
        """Regression: codex branch must only clear codex artifacts.

        When codex history (``.codex/sessions``) is empty, the codex branch
        once wrongly also deleted the grok flag, set ``cleared['grok']``, and
        logged a misleading message. The fix keeps the codex branch scoped
        to ``.initialized.codex`` and ``.codex_thread_id`` only.
        """
        from wechatbridge.runner_common import _clear_initialized_if_no_history

        with tempfile.TemporaryDirectory() as user_dir:
            # `.codex/sessions` 存在但不含任何文件（空历史）
            sessions = os.path.join(user_dir, ".codex", "sessions")
            os.makedirs(sessions)
            # 待清理的两个 codex 文件
            codex_flag = os.path.join(user_dir, ".initialized.codex")
            codex_tid = os.path.join(user_dir, ".codex_thread_id")
            with open(codex_flag, "w", encoding="utf-8") as f:
                f.write("")
            with open(codex_tid, "w", encoding="utf-8") as f:
                f.write("stale-tid")

            cleared = _clear_initialized_if_no_history(user_dir)

            # 两个文件都被删除
            self.assertFalse(os.path.exists(codex_flag), ".initialized.codex must be removed")
            self.assertFalse(os.path.exists(codex_tid), ".codex_thread_id must be removed")
            # 返回值标记 codex 被清理
            self.assertTrue(cleared.get("codex"), "cleared should contain codex=True")
            # 不得误标记 grok（回归点）
            self.assertNotIn("grok", cleared, "codex branch must not flag grok")

    def test_grok_branch_independent_of_codex(self):
        """grok branch should clear only its own flag when grok history is empty.

        Sanity check that the two branches are independent: a cleared codex
        flag must not bleed into the grok result and vice versa.
        """
        from wechatbridge.runner_common import _clear_initialized_if_no_history

        with tempfile.TemporaryDirectory() as user_dir:
            grok_flag = os.path.join(user_dir, ".initialized.grok")
            with open(grok_flag, "w", encoding="utf-8") as f:
                f.write("")

            cleared = _clear_initialized_if_no_history(user_dir)

            self.assertFalse(os.path.exists(grok_flag))
            self.assertTrue(cleared.get("grok"))
            self.assertNotIn("codex", cleared)

    def test_dsh_branch_clears_flag_when_no_history(self):
        from wechatbridge.runner_common import _clear_initialized_if_no_history

        with tempfile.TemporaryDirectory() as user_dir:
            dsh_flag = os.path.join(user_dir, ".initialized.dsh")
            with open(dsh_flag, "w", encoding="utf-8") as f:
                f.write("")

            cleared = _clear_initialized_if_no_history(user_dir)

            self.assertFalse(os.path.exists(dsh_flag))
            self.assertTrue(cleared.get("dsh"))
            self.assertNotIn("grok", cleared)
            self.assertNotIn("codex", cleared)
            self.assertNotIn("agy", cleared)


class TestHostDshSessionCleanup(unittest.TestCase):
    """Machine-wide $DSH_HOME/sessions/ cleanup during session data pruning."""

    def test_expired_dsh_sessions_removed_and_fresh_kept(self):
        from wechatbridge.runner_common import clean_session_data
        from wechatbridge.config import config

        with tempfile.TemporaryDirectory() as td:
            host_dsh = os.path.join(td, "dsh-home")
            sessions_root = os.path.join(host_dsh, "sessions")

            # 1. Expired session under cwd1 (mtime = 40 days ago)
            old_session = os.path.join(sessions_root, "cwd1", "session-old")
            os.makedirs(old_session, exist_ok=True)
            old_file = os.path.join(old_session, "chat.jsonl")
            with open(old_file, "w", encoding="utf-8") as f:
                f.write('{"role": "user"}\n')
            old_mtime = time.time() - 40 * 86400
            os.utime(old_file, (old_mtime, old_mtime))
            os.utime(old_session, (old_mtime, old_mtime))

            # 2. Fresh session under cwd1 (mtime = now)
            fresh_session = os.path.join(sessions_root, "cwd1", "session-fresh")
            os.makedirs(fresh_session, exist_ok=True)
            fresh_file = os.path.join(fresh_session, "chat.jsonl")
            with open(fresh_file, "w", encoding="utf-8") as f:
                f.write('{"role": "user"}\n')

            # 3. Expired session under cwd2 (will become empty and cwd2 rmdir'd)
            old_session2 = os.path.join(sessions_root, "cwd2", "session-old2")
            os.makedirs(old_session2, exist_ok=True)
            old_file2 = os.path.join(old_session2, "chat.jsonl")
            with open(old_file2, "w", encoding="utf-8") as f:
                f.write('{"role": "user"}\n')
            os.utime(old_file2, (old_mtime, old_mtime))
            os.utime(old_session2, (old_mtime, old_mtime))

            session_base = os.path.join(td, "user_sessions")
            os.makedirs(session_base, exist_ok=True)

            with mock.patch.object(config, "dsh_home", host_dsh), \
                 mock.patch.object(config, "session_base_dir", session_base), \
                 mock.patch.object(config, "history_retention_days", 30):
                removed = clean_session_data()

            self.assertGreaterEqual(removed, 2)
            # Expired sessions removed
            self.assertFalse(os.path.exists(old_session), "old session-old should be removed")
            self.assertFalse(os.path.exists(old_session2), "old session-old2 should be removed")
            # Fresh session preserved
            self.assertTrue(os.path.exists(fresh_session), "fresh session should be preserved")
            self.assertTrue(os.path.exists(fresh_file), "fresh file should be preserved")
            # Empty bucket cwd2 should be pruned
            self.assertFalse(os.path.exists(os.path.join(sessions_root, "cwd2")), "empty cwd2 bucket should be pruned")
            # cwd1 still exists because it has fresh_session
            self.assertTrue(os.path.exists(os.path.join(sessions_root, "cwd1")), "cwd1 bucket should still exist")

    def test_implicit_dsh_home_skips_cleanup(self):
        from wechatbridge.runner_common import clean_session_data
        from wechatbridge.config import config

        with tempfile.TemporaryDirectory() as td:
            host_dsh = os.path.join(td, ".dsh")
            sessions_root = os.path.join(host_dsh, "sessions")

            # Expired session under host ~/.dsh/sessions
            old_session = os.path.join(sessions_root, "cwd1", "session-old")
            os.makedirs(old_session, exist_ok=True)
            old_file = os.path.join(old_session, "chat.jsonl")
            with open(old_file, "w", encoding="utf-8") as f:
                f.write('{"role": "user"}\n')
            old_mtime = time.time() - 40 * 86400
            os.utime(old_file, (old_mtime, old_mtime))
            os.utime(old_session, (old_mtime, old_mtime))

            session_base = os.path.join(td, "user_sessions")
            os.makedirs(session_base, exist_ok=True)

            with mock.patch.object(config, "dsh_home", ""), \
                 mock.patch.dict(os.environ, {"WECHATBRIDGE_HOST_HOME": td}, clear=False), \
                 mock.patch.object(config, "session_base_dir", session_base), \
                 mock.patch.object(config, "history_retention_days", 30):
                removed = clean_session_data()

            self.assertEqual(removed, 0)
            self.assertTrue(
                os.path.exists(old_session),
                "implicit host ~/.dsh session must NOT be removed",
            )

    def test_whitespace_dsh_home_not_explicit_and_skips_cleanup(self):
        from wechatbridge.runner_common import clean_session_data
        from wechatbridge.config import config, host_dsh_home, is_dsh_home_explicit

        with tempfile.TemporaryDirectory() as td:
            host_dsh = os.path.join(td, ".dsh")
            sessions_root = os.path.join(host_dsh, "sessions")

            old_session = os.path.join(sessions_root, "cwd1", "session-old")
            os.makedirs(old_session, exist_ok=True)
            old_file = os.path.join(old_session, "chat.jsonl")
            with open(old_file, "w", encoding="utf-8") as f:
                f.write('{"role": "user"}\n')
            old_mtime = time.time() - 40 * 86400
            os.utime(old_file, (old_mtime, old_mtime))
            os.utime(old_session, (old_mtime, old_mtime))

            session_base = os.path.join(td, "user_sessions")
            os.makedirs(session_base, exist_ok=True)

            with mock.patch.object(config, "dsh_home", "   "), \
                 mock.patch.dict(os.environ, {"WECHATBRIDGE_HOST_HOME": td}, clear=False), \
                 mock.patch.object(config, "session_base_dir", session_base), \
                 mock.patch.object(config, "history_retention_days", 30):
                self.assertFalse(is_dsh_home_explicit())
                self.assertEqual(host_dsh_home(), host_dsh)
                removed = clean_session_data()

            self.assertEqual(removed, 0)
            self.assertTrue(os.path.exists(old_session), "whitespace dsh_home must NOT remove host sessions")

    def test_relative_dsh_home_normalized_to_abspath_and_cleaned(self):
        from wechatbridge.runner_common import clean_session_data
        from wechatbridge.config import config, host_dsh_home, is_dsh_home_explicit

        with tempfile.TemporaryDirectory() as td:
            rel_dir = os.path.join(".", "relative_dsh_home_test")
            abs_dir = os.path.abspath(rel_dir)
            sessions_root = os.path.join(abs_dir, "sessions")
            self.addCleanup(lambda: shutil.rmtree(abs_dir, ignore_errors=True))

            old_session = os.path.join(sessions_root, "cwd1", "session-old")
            os.makedirs(old_session, exist_ok=True)
            old_file = os.path.join(old_session, "chat.jsonl")
            with open(old_file, "w", encoding="utf-8") as f:
                f.write('{"role": "user"}\n')
            old_mtime = time.time() - 40 * 86400
            os.utime(old_file, (old_mtime, old_mtime))
            os.utime(old_session, (old_mtime, old_mtime))

            session_base = os.path.join(td, "user_sessions")
            os.makedirs(session_base, exist_ok=True)

            with mock.patch.object(config, "dsh_home", rel_dir), \
                 mock.patch.object(config, "session_base_dir", session_base), \
                 mock.patch.object(config, "history_retention_days", 30):
                self.assertTrue(is_dsh_home_explicit())
                self.assertEqual(host_dsh_home(), abs_dir)
                removed = clean_session_data()

            self.assertGreaterEqual(removed, 1)
            self.assertFalse(os.path.exists(old_session), "relative dsh_home session must be cleaned via abspath")

    def test_absolute_dsh_home_behavior_unchanged(self):
        from wechatbridge.config import config, host_dsh_home, is_dsh_home_explicit
        with mock.patch.object(config, "dsh_home", "/abs/path/to/dsh"):
            self.assertTrue(is_dsh_home_explicit())
            self.assertEqual(host_dsh_home(), "/abs/path/to/dsh")



class TestCleanCodexSessionsOSError(unittest.TestCase):
    """Regression: a single unreadable directory must not abort the whole codex
    session cleanup. year/month/day os.listdir OSErrors are caught and the loop
    continues to the next bucket."""

    def test_unreadable_month_dir_does_not_abort(self):
        from wechatbridge.runner_common import _clean_codex_sessions

        with tempfile.TemporaryDirectory() as root:
            sessions = os.path.join(root, "sessions")
            # 2025/01/05 有一个过期 rollout（应被删除）
            day_ok = os.path.join(sessions, "2025", "01", "05")
            os.makedirs(day_ok)
            old = os.path.join(day_ok, "rollout-2025-01-05T00-00-00-old.jsonl")
            with open(old, "w", encoding="utf-8") as f:
                f.write("x")
            old_mtime = time.time() - 100 * 86400
            os.utime(old, (old_mtime, old_mtime))
            # 2025/02 是一个不可读（listdir 抛 OSError）的月目录
            bad_month = os.path.join(sessions, "2025", "02")
            os.makedirs(bad_month)

            cutoff = time.time() - 30 * 86400
            real_listdir = os.listdir

            def fake_listdir(path, *a, **k):
                p = str(path).rstrip(os.sep)
                if p == bad_month:
                    raise OSError("permission denied")
                return real_listdir(path, *a, **k)

            with mock.patch(
                "wechatbridge.runner_common.os.listdir", side_effect=fake_listdir
            ):
                removed = _clean_codex_sessions(sessions, cutoff)

            # 过期 rollout 被删除，且不可读月目录没有令整个清理中断/抛异常
            self.assertGreaterEqual(removed, 1)
            self.assertFalse(os.path.exists(old), "old rollout should be removed")
            # 不可读目录本身仍在（我们没有权限删除它，只是跳过）
            self.assertTrue(os.path.isdir(bad_month))

    def test_unreadable_day_dir_does_not_abort(self):
        from wechatbridge.runner_common import _clean_codex_sessions

        with tempfile.TemporaryDirectory() as root:
            sessions = os.path.join(root, "sessions")
            # 2025/01/05 有一个过期 rollout（应被删除）
            day_ok = os.path.join(sessions, "2025", "01", "05")
            os.makedirs(day_ok)
            old_ok = os.path.join(day_ok, "rollout-2025-01-05T00-00-00-old.jsonl")
            with open(old_ok, "w", encoding="utf-8") as f:
                f.write("x")
            old_mtime = time.time() - 100 * 86400
            os.utime(old_ok, (old_mtime, old_mtime))
            # 2025/01/06 是一个不可读（day 层 listdir 抛 OSError）的日目录
            bad_day = os.path.join(sessions, "2025", "01", "06")
            os.makedirs(bad_day)
            # 2025/01/07 是另一个 day，过期 rollout 仍应被清理（证明单个
            # 不可读 day 不会中断整轮清理）
            day_other = os.path.join(sessions, "2025", "01", "07")
            os.makedirs(day_other)
            old_other = os.path.join(day_other, "rollout-2025-01-07T00-00-00-old.jsonl")
            with open(old_other, "w", encoding="utf-8") as f:
                f.write("x")
            os.utime(old_other, (old_mtime, old_mtime))

            cutoff = time.time() - 30 * 86400
            real_listdir = os.listdir

            def fake_listdir(path, *a, **k):
                p = str(path).rstrip(os.sep)
                if p == bad_day:
                    raise OSError("permission denied")
                return real_listdir(path, *a, **k)

            with mock.patch(
                "wechatbridge.runner_common.os.listdir", side_effect=fake_listdir
            ):
                removed = _clean_codex_sessions(sessions, cutoff)

            # 两个可读 day 的过期 rollout 均被删除；不可读 day 没有令整轮
            # 清理中断或抛异常
            self.assertGreaterEqual(removed, 2)
            self.assertFalse(os.path.exists(old_ok), "old rollout (05) should be removed")
            self.assertFalse(os.path.exists(old_other), "old rollout (07) should be removed")
            # 不可读 day 目录本身仍在（只是跳过，没权限删除）
            self.assertTrue(os.path.isdir(bad_day))


class TestPerUserLockContract(unittest.IsolatedAsyncioTestCase):
    """生产链路契约：main._safe_process_message 的 per-user 锁 + 全局并发门。

    不触网、不依赖真实 Codex。process_message 被 mock 以控制阻塞；
    全局 main.user_locks 与 _global_task_sem 在 setUp 保存、tearDown 还原，
    避免污染顺序。
    """

    def setUp(self):
        from wechatbridge import main as main_mod
        self.main = main_mod
        # 保存并隔离全局状态，避免测试间污染顺序
        self._orig_locks = main_mod.user_locks
        self._orig_sem = main_mod._global_task_sem
        main_mod.user_locks = {}
        main_mod._global_task_sem = None
        # 全局并发槽放大，避免 fail-fast 干扰序列化/并行断言
        self._sem_patch = mock.patch.object(
            main_mod.config, "max_concurrent_tasks", 16
        )
        self._sem_patch.start()
        self.addCleanup(self._sem_patch.stop)

    def tearDown(self):
        self.main.user_locks = self._orig_locks
        self.main._global_task_sem = self._orig_sem

    def _client(self):
        client = MagicMock()
        client.state = MagicMock()
        client.state.baseurl = "https://example.test"
        client.state.bot_token = "tok"
        return client

    def _msg(self, uid, kind=None):
        m = {"from_user_id": uid, "context_token": "ctx-" + uid}
        if kind:
            m["_kind"] = kind
        return m

    async def test_same_user_serializes(self):
        """同 user 两条消息：第一条阻塞期间，第二条必须排队等待。"""
        released = asyncio.Event()
        started, ended = [], []

        async def _pm(client, msg):
            started.append(msg.get("from_user_id"))
            await released.wait()
            ended.append(msg.get("from_user_id"))

        client = self._client()
        msg1 = self._msg("alice")
        msg2 = self._msg("alice")
        with mock.patch.object(self.main, "process_message", new=_pm):
            t1 = asyncio.create_task(self.main._safe_process_message(client, msg1))
            t2 = asyncio.create_task(self.main._safe_process_message(client, msg2))
            await asyncio.sleep(0)
            # 仅第一条进入 process_message；第二条被同 user 锁串行化阻塞
            self.assertEqual(started, ["alice"])
            self.assertEqual(ended, [])
            released.set()
            await asyncio.gather(t1, t2, return_exceptions=True)
        # 第二条在第一个释放后才开始并结束
        self.assertEqual(started, ["alice", "alice"])
        self.assertEqual(ended, ["alice", "alice"])

    async def test_same_user_clear_queues_behind_run(self):
        """同 user：run 进行中发 /clear，clear 必须排队，不能在 run 完成前
        删除/改写 codex thread 状态；最终 clear 生效。"""
        from wechatbridge.codex import (
            handle_codex_slash_command, _write_codex_thread_id,
        )
        from wechatbridge.runner_common import (
            ensure_session_dir, mark_initialized,
        )

        uid = "u-lock-clear"
        base = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(base, ignore_errors=True))
        with mock.patch.object(self.main.config, "session_base_dir", base):
            sd = ensure_session_dir(uid)
            _write_codex_thread_id(sd, "tid-lock-clear")
            mark_initialized(sd, backend="codex")
            tid_path = os.path.join(sd, ".codex_thread_id")

            run_release = asyncio.Event()
            order = []

            async def _pm(client, msg):
                if msg.get("_kind") == "clear":
                    order.append("clear")
                    # 真正的 /clear 会删除 codex thread 状态
                    await handle_codex_slash_command("/clear", uid)
                else:
                    order.append("run_start")
                    await run_release.wait()
                    order.append("run_end")

            client = self._client()
            msg_run = self._msg(uid, kind="run")
            msg_clear = self._msg(uid, kind="clear")
            with mock.patch.object(self.main, "process_message", new=_pm):
                t_run = asyncio.create_task(
                    self.main._safe_process_message(client, msg_run)
                )
                t_clear = asyncio.create_task(
                    self.main._safe_process_message(client, msg_clear)
                )
                await asyncio.sleep(0)
                # run 仍阻塞时，clear 被同 user 锁串行化阻塞，尚未删除 thread_id
                self.assertIn("run_start", order)
                self.assertNotIn("clear", order)
                self.assertTrue(
                    os.path.isfile(tid_path),
                    "run 完成前 clear 不应删除 codex thread 状态",
                )
                run_release.set()
                await asyncio.gather(t_run, t_clear, return_exceptions=True)
            # run 完成后，clear 执行并生效（thread_id 被删除）
            self.assertFalse(
                os.path.isfile(tid_path),
                "clear 必须最终生效",
            )
            self.assertEqual(order, ["run_start", "run_end", "clear"])

    async def test_different_users_run_in_parallel(self):
        """不同 user 两条消息允许并行，互不阻塞（event/barrier，不靠 sleep）。"""
        started = []
        both_started = asyncio.Event()
        rel_a, rel_b = asyncio.Event(), asyncio.Event()

        async def _pm(client, msg):
            uid = msg.get("from_user_id")
            started.append(uid)
            if len(started) >= 2:
                both_started.set()
            ev = rel_a if uid == "alice" else rel_b
            await ev.wait()

        client = self._client()
        msg_a = self._msg("alice")
        msg_b = self._msg("bob")
        with mock.patch.object(self.main, "process_message", new=_pm):
            t_a = asyncio.create_task(
                self.main._safe_process_message(client, msg_a)
            )
            t_b = asyncio.create_task(
                self.main._safe_process_message(client, msg_b)
            )
            # 两个不同 user 同时进入 process_message（不互相阻塞）
            await asyncio.wait_for(both_started.wait(), timeout=5)
            self.assertEqual(sorted(started), ["alice", "bob"])
            rel_a.set()
            rel_b.set()
            await asyncio.gather(t_a, t_b, return_exceptions=True)


class TestFormatCliErrorRateQuota(unittest.TestCase):
    """Split eligibility/429/quota so users are not told they spam when
    Google control-plane eligibility is temporarily RESOURCE_EXHAUSTED.

    Throttle / quota final user copy uses 🔔 (not ❌).
    """

    def _fmt(self, raw: str, backend: str = "agy") -> str:
        from wechatbridge.runner_common import format_cli_error

        return format_cli_error(raw, backend=backend)

    def _assert_notice(self, out: str, title: str) -> None:
        self.assertIn(f"**{title}**", out)
        self.assertIn("🔔", out)
        self.assertNotIn("❌", out)
        self.assertNotIn("请求过于频繁", out)

    def test_eligibility_resource_exhausted_not_too_frequent(self):
        raw = (
            "Eligibility check failed: RESOURCE_EXHAUSTED (code 429): "
            "Resource has been exhausted (e.g. check quota)."
        )
        out = self._fmt(raw)
        self._assert_notice(out, "助手通道繁忙")
        self.assertIn("暂时限流或繁忙", out)
        self.assertNotIn("用得有点多", out)

    def test_resource_exhausted_without_eligibility(self):
        out = self._fmt("gRPC error: RESOURCE_EXHAUSTED: Resource has been exhausted")
        self._assert_notice(out, "助手通道繁忙")

    def test_explicit_rate_limit(self):
        out = self._fmt("Error: rate limit exceeded, please slow down")
        self._assert_notice(out, "请求较多")
        self.assertNotIn("额度相关", out)

    def test_too_many_requests(self):
        out = self._fmt("HTTP 429 Too Many Requests")
        # "too many requests" is more specific than bare 429
        self._assert_notice(out, "请求较多")

    def test_quota_exceeded(self):
        out = self._fmt("You exceeded your current quota, please check billing")
        self._assert_notice(out, "额度相关")
        self.assertNotIn("助手通道繁忙", out)

    def test_bare_429_not_too_frequent(self):
        out = self._fmt("upstream returned status 429")
        self._assert_notice(out, "助手通道繁忙")

    def test_auth_branch_still_login(self):
        out = self._fmt("Not signed in. Please run login --device")
        self.assertIn("未登录", out)
        self.assertIn("❌", out)
        self.assertNotIn("🔔", out)

    def test_timeout_branch_unchanged(self):
        out = self._fmt("Timeout waiting for cascade response")
        self.assertIn("模型响应超时", out)
        self.assertIn("❌", out)


class TestFormatNoticeAndThrottleDetect(unittest.TestCase):
    def test_format_notice_bell(self):
        from wechatbridge.runner_common import format_notice

        out = format_notice("上游繁忙，正在重试", "第 1/2 次重试，请稍候…")
        self.assertTrue(out.startswith("🔔"))
        self.assertIn("**上游繁忙，正在重试**", out)
        self.assertIn("第 1/2 次", out)
        self.assertNotIn("❌", out)

    def test_format_notice_title_only(self):
        from wechatbridge.runner_common import format_notice

        out = format_notice("上游冷却中")
        self.assertEqual(out, "🔔 **上游冷却中** 🔔")

    def test_is_upstream_throttle_reply_positive(self):
        from wechatbridge.runner_common import (
            format_cli_error,
            format_notice,
            is_upstream_throttle_reply,
        )

        for raw in (
            "RESOURCE_EXHAUSTED (code 429)",
            "rate limit exceeded",
            "quota exceeded",
            "status 429",
        ):
            out = format_cli_error(raw, backend="agy")
            self.assertTrue(is_upstream_throttle_reply(out), msg=out)

        self.assertTrue(
            is_upstream_throttle_reply(format_notice("请求较多", "稍后再试"))
        )

    def test_is_upstream_throttle_reply_negative(self):
        from wechatbridge.runner_common import (
            format_cli_error,
            format_error,
            is_upstream_throttle_reply,
        )

        self.assertFalse(is_upstream_throttle_reply(""))
        self.assertFalse(is_upstream_throttle_reply(None))  # type: ignore[arg-type]
        self.assertFalse(is_upstream_throttle_reply("普通回复文本"))
        self.assertFalse(
            is_upstream_throttle_reply(format_error("未登录", "请联系管理员"))
        )
        self.assertFalse(
            is_upstream_throttle_reply(
                format_cli_error("Timeout waiting for cascade response")
            )
        )
        # Free-text title without bold markers must not match
        self.assertFalse(is_upstream_throttle_reply("助手通道繁忙 请稍后再试"))
        # Normal model reply that happens to mention a throttle title in bold
        # must NOT trigger retry (requires real 🔔 bubble header)
        self.assertFalse(
            is_upstream_throttle_reply(
                "说明一下：上游出现 **请求较多** 时会限流，这是正常设计。"
            )
        )
        self.assertFalse(
            is_upstream_throttle_reply(
                "关于 **助手通道繁忙** 的排查步骤如下……"
            )
        )

    def test_is_bridge_formatted_reply(self):
        from wechatbridge.runner_common import (
            format_error,
            format_notice,
            is_bridge_formatted_reply,
        )

        self.assertTrue(is_bridge_formatted_reply(format_error("未登录", "x")))
        self.assertTrue(is_bridge_formatted_reply(format_notice("请求较多", "y")))
        self.assertTrue(is_bridge_formatted_reply("🔔 **额度相关** 🔔"))
        self.assertFalse(is_bridge_formatted_reply(""))
        self.assertFalse(is_bridge_formatted_reply("普通回复"))
        self.assertFalse(is_bridge_formatted_reply("**请求较多** 没有 emoji"))
        self.assertFalse(is_bridge_formatted_reply("❌ bare cross without bold title ❌"))

    def test_classify_upstream_failure(self):
        from wechatbridge.runner_common import (
            classify_upstream_failure,
            format_notice,
            is_upstream_quota_reply,
        )

        thr = format_notice("助手通道繁忙", "稍等")
        quo = format_notice("额度相关", "配额受限")
        self.assertEqual(classify_upstream_failure(thr), "throttle")
        self.assertEqual(classify_upstream_failure(quo), "quota")
        self.assertTrue(is_upstream_quota_reply(quo))
        self.assertFalse(is_upstream_quota_reply(thr))
        self.assertIsNone(classify_upstream_failure("hello"))


class TestFormatCliErrorTightening(unittest.TestCase):
    """#6: bare 429 / wide quota must not over-match."""

    def _fmt(self, raw: str) -> str:
        from wechatbridge.runner_common import format_cli_error

        return format_cli_error(raw, backend="agy")

    def test_1429_not_bare_429(self):
        out = self._fmt("internal error code 1429 in pipeline stage")
        self.assertNotIn("助手通道繁忙", out)
        self.assertNotIn("请求较多", out)
        # Falls through to generic 执行失败
        self.assertIn("执行失败", out)

    def test_quota_usage_report_not_quota(self):
        out = self._fmt("quota usage report ready for download")
        self.assertNotIn("额度相关", out)
        self.assertIn("执行失败", out)

    def test_eligibility_still_busy(self):
        out = self._fmt(
            "Eligibility check failed: RESOURCE_EXHAUSTED (code 429): "
            "Resource has been exhausted (e.g. check quota)."
        )
        self.assertIn("助手通道繁忙", out)
        self.assertIn("🔔", out)

    def test_status_429_still_busy(self):
        out = self._fmt("upstream returned status 429")
        self.assertIn("助手通道繁忙", out)


class TestUpstreamGuard(unittest.TestCase):
    def test_mark_and_remaining(self):
        from wechatbridge.runner_common import UpstreamGuard
        from wechatbridge import config as cfg_mod

        g = UpstreamGuard()
        with mock.patch.object(cfg_mod.config, "upstream_cooldown", 20), mock.patch.object(
            cfg_mod.config, "upstream_user_gap", 10
        ):
            g.mark_throttle("u1")
            self.assertGreater(g.global_remaining(), 15)
            self.assertLessEqual(g.global_remaining(), 20)
            self.assertGreater(g.user_gap_remaining("u1"), 5)
            self.assertLessEqual(g.user_gap_remaining("u1"), 10)
            self.assertEqual(g.user_gap_remaining("other"), 0.0)

            g.clear_user_gap("u1")
            self.assertEqual(g.user_gap_remaining("u1"), 0.0)
            # global cooldown kept after clear_user_gap
            self.assertGreater(g.global_remaining(), 0)

    def test_mark_extends_not_shortens(self):
        from wechatbridge.runner_common import UpstreamGuard
        from wechatbridge import config as cfg_mod

        g = UpstreamGuard()
        with mock.patch.object(cfg_mod.config, "upstream_cooldown", 30), mock.patch.object(
            cfg_mod.config, "upstream_user_gap", 5
        ):
            g.mark_throttle("u1")
            first_global = g.global_cooldown_until
            with mock.patch.object(cfg_mod.config, "upstream_cooldown", 5):
                g.mark_throttle("u1")
            # shorter second mark must not pull global_cooldown backward
            self.assertEqual(g.global_cooldown_until, first_global)


class TestRunLlmWithGuard(unittest.IsolatedAsyncioTestCase):
    """Behaviour of main._run_llm_with_guard (A/B/C) with mocked _run_llm."""

    async def asyncSetUp(self):
        from wechatbridge.runner_common import UpstreamGuard
        from wechatbridge import main as main_mod

        self.main = main_mod
        # Fresh guard so tests don't leak state
        self.guard = UpstreamGuard()
        self._guard_patch = mock.patch.object(main_mod, "upstream_guard", self.guard)
        self._guard_patch.start()
        self.client = MagicMock()
        self.client.state = MagicMock()
        self.client.state.baseurl = "https://example.test"
        self.client.state.bot_token = "tok"
        self.client.send_message = AsyncMock(return_value=True)
        self.sent_texts: list[str] = []

        async def _capture_send(**kwargs):
            self.sent_texts.append(kwargs.get("text") or "")
            return True

        self.client.send_message.side_effect = _capture_send

    async def asyncTearDown(self):
        self._guard_patch.stop()

    def _throttle(self, title: str = "助手通道繁忙") -> tuple[str, list]:
        from wechatbridge.runner_common import format_notice

        return format_notice(title, "上游助手通道暂时限流或繁忙，请稍等片刻再试。"), []

    def _ok(self, text: str = "hello") -> tuple[str, list]:
        return text, []

    async def test_success_no_retry(self):
        with mock.patch.object(
            self.main, "_run_llm", new=AsyncMock(return_value=self._ok("ok"))
        ) as run, mock.patch.object(
            self.main.asyncio, "sleep", new=AsyncMock()
        ) as sleep:
            reply, arts = await self.main._run_llm_with_guard(
                self.client, "user-a", "ctx", "hi"
            )
        self.assertEqual(reply, "ok")
        self.assertEqual(arts, [])
        self.assertEqual(run.await_count, 1)
        self.assertEqual(self.sent_texts, [])
        sleep.assert_not_awaited()

    async def test_A_retry_then_success(self):
        from wechatbridge import config as cfg_mod

        throttle = self._throttle()
        ok = self._ok("recovered")
        run = AsyncMock(side_effect=[throttle, ok])
        with mock.patch.object(self.main, "_run_llm", new=run), mock.patch.object(
            self.main.asyncio, "sleep", new=AsyncMock()
        ) as sleep, mock.patch.object(
            cfg_mod.config, "upstream_retry_max", 2
        ), mock.patch.object(
            cfg_mod.config, "upstream_backoff", [2, 5, 12]
        ), mock.patch.object(
            self.main, "_get_backend", return_value="agy"
        ):
            reply, _ = await self.main._run_llm_with_guard(
                self.client, "user-a", "ctx", "hi"
            )
        self.assertEqual(reply, "recovered")
        self.assertEqual(run.await_count, 2)
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("上游繁忙，正在重试", self.sent_texts[0])
        self.assertIn("🔔", self.sent_texts[0])
        self.assertIn("第 1/2 次", self.sent_texts[0])
        sleep.assert_awaited()
        # user gap cleared on success
        self.assertEqual(self.guard.user_gap_remaining("user-a"), 0.0)

    async def test_A_exhaust_retries_returns_throttle(self):
        from wechatbridge import config as cfg_mod

        throttle = self._throttle("请求较多")
        run = AsyncMock(return_value=throttle)
        with mock.patch.object(self.main, "_run_llm", new=run), mock.patch.object(
            self.main.asyncio, "sleep", new=AsyncMock()
        ), mock.patch.object(
            cfg_mod.config, "upstream_retry_max", 2
        ), mock.patch.object(
            cfg_mod.config, "upstream_backoff", [0, 0, 0]
        ), mock.patch.object(
            cfg_mod.config, "upstream_cooldown", 20
        ), mock.patch.object(
            cfg_mod.config, "upstream_user_gap", 10
        ), mock.patch.object(
            self.main, "_get_backend", return_value="codex"
        ):
            reply, _ = await self.main._run_llm_with_guard(
                self.client, "user-b", "ctx", "hi"
            )
        self.assertIn("请求较多", reply)
        self.assertIn("🔔", reply)
        self.assertEqual(run.await_count, 3)  # 1 + 2 retries
        # A notices for each retry (not for the final failure)
        self.assertEqual(len(self.sent_texts), 2)
        for t in self.sent_texts:
            self.assertIn("上游繁忙，正在重试", t)
            self.assertIn("🔔", t)
        self.assertGreater(self.guard.global_remaining(), 0)
        self.assertGreater(self.guard.user_gap_remaining("user-b"), 0)

    async def test_B_global_cooldown_notifies(self):
        from wechatbridge import config as cfg_mod

        self.guard.global_cooldown_until = time.time() + 7.0
        with mock.patch.object(
            self.main, "_run_llm", new=AsyncMock(return_value=self._ok("after-cool"))
        ), mock.patch.object(
            self.main.asyncio, "sleep", new=AsyncMock()
        ) as sleep, mock.patch.object(
            cfg_mod.config, "upstream_retry_max", 0
        ):
            reply, _ = await self.main._run_llm_with_guard(
                self.client, "user-c", "ctx", "hi"
            )
        self.assertEqual(reply, "after-cool")
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("上游冷却中", self.sent_texts[0])
        self.assertIn("🔔", self.sent_texts[0])
        self.assertIn("秒后自动继续", self.sent_texts[0])
        # slept for the remaining cooldown
        self.assertTrue(sleep.await_count >= 1)
        cool_arg = sleep.await_args_list[0].args[0]
        self.assertGreater(cool_arg, 5)

    async def test_C_user_gap_silent(self):
        from wechatbridge import config as cfg_mod

        self.guard.user_gap_until["user-d"] = time.time() + 4.0
        with mock.patch.object(
            self.main, "_run_llm", new=AsyncMock(return_value=self._ok("after-gap"))
        ), mock.patch.object(
            self.main.asyncio, "sleep", new=AsyncMock()
        ) as sleep, mock.patch.object(
            cfg_mod.config, "upstream_retry_max", 0
        ):
            reply, _ = await self.main._run_llm_with_guard(
                self.client, "user-d", "ctx", "hi"
            )
        self.assertEqual(reply, "after-gap")
        # C: no WeChat notice while waiting the gap
        self.assertEqual(self.sent_texts, [])
        self.assertTrue(sleep.await_count >= 1)
        gap_arg = sleep.await_args_list[0].args[0]
        self.assertGreater(gap_arg, 2)

    async def test_non_throttle_error_no_retry(self):
        from wechatbridge.runner_common import format_error

        err = format_error("未登录", "请联系管理员"), []
        with mock.patch.object(
            self.main, "_run_llm", new=AsyncMock(return_value=err)
        ) as run, mock.patch.object(
            self.main.asyncio, "sleep", new=AsyncMock()
        ):
            reply, _ = await self.main._run_llm_with_guard(
                self.client, "user-e", "ctx", "hi"
            )
        self.assertIn("未登录", reply)
        self.assertEqual(run.await_count, 1)
        self.assertEqual(self.sent_texts, [])

    async def test_quota_default_no_retry(self):
        """#7: 额度相关 must not burn short-window retry budget (default 0 extra)."""
        from wechatbridge import config as cfg_mod

        quota = self._throttle("额度相关")
        run = AsyncMock(return_value=quota)
        with mock.patch.object(self.main, "_run_llm", new=run), mock.patch.object(
            self.main, "_guard_sleep", new=AsyncMock()
        ), mock.patch.object(
            cfg_mod.config, "upstream_retry_max", 2
        ), mock.patch.object(
            cfg_mod.config, "upstream_quota_retry_max", 0
        ), mock.patch.object(
            cfg_mod.config, "upstream_cooldown", 20
        ), mock.patch.object(
            cfg_mod.config, "upstream_user_gap", 10
        ), mock.patch.object(
            self.main, "_get_backend", return_value="agy"
        ):
            reply, _ = await self.main._run_llm_with_guard(
                self.client, "user-quota", "ctx", "hi"
            )
        self.assertIn("额度相关", reply)
        self.assertIn("🔔", reply)
        # Only the first attempt — no extra retries
        self.assertEqual(run.await_count, 1)
        self.assertEqual(self.sent_texts, [])  # no A retry notices
        # Still marks cooldown / gap so we do not hammer immediately
        self.assertGreater(self.guard.global_remaining(), 0)
        self.assertGreater(self.guard.user_gap_remaining("user-quota"), 0)

    async def test_guard_sleep_releases_global_slot(self):
        """#4: A/C/B sleeps must not hold the global concurrency semaphore."""
        sem = asyncio.Semaphore(1)
        await sem.acquire()  # we own the only slot, matching _safe_process_message
        slot = self.main._GlobalSlot(sem)
        token = self.main._global_slot_ctx.set(slot)
        try:
            self.assertTrue(slot.held)
            # While sleeping with slot released, another waiter can acquire
            acquired = asyncio.Event()

            async def other():
                await sem.acquire()
                acquired.set()
                sem.release()

            t = asyncio.create_task(other())
            # Give other a chance — should NOT acquire while we still hold
            await asyncio.sleep(0)
            self.assertFalse(acquired.is_set())

            sleep_task = asyncio.create_task(self.main._guard_sleep(0.05))
            # other should get the slot during our released sleep
            await asyncio.wait_for(acquired.wait(), timeout=1.0)
            await sleep_task
            await t
            self.assertTrue(slot.held)
        finally:
            self.main._global_slot_ctx.reset(token)
            if slot.held:
                sem.release()
                slot.held = False

    async def test_A_send_message_failure_still_retries(self):
        """Optional polish: A retry notice send_message raise must not abort retries."""
        from wechatbridge import config as cfg_mod

        throttle = self._throttle()
        ok = self._ok("recovered-after-send-fail")
        run = AsyncMock(side_effect=[throttle, ok])
        self.client.send_message = AsyncMock(side_effect=RuntimeError("net down"))
        with mock.patch.object(self.main, "_run_llm", new=run), mock.patch.object(
            self.main, "_guard_sleep", new=AsyncMock()
        ), mock.patch.object(
            cfg_mod.config, "upstream_retry_max", 2
        ), mock.patch.object(
            cfg_mod.config, "upstream_backoff", [0, 0, 0]
        ), mock.patch.object(
            self.main, "_get_backend", return_value="agy"
        ):
            reply, _ = await self.main._run_llm_with_guard(
                self.client, "user-send-fail", "ctx", "hi"
            )
        self.assertEqual(reply, "recovered-after-send-fail")
        self.assertEqual(run.await_count, 2)
        self.client.send_message.assert_awaited()

    async def test_B_send_message_failure_still_sleeps_and_runs(self):
        """Optional polish: B cooldown notice send_message raise must not abort."""
        from wechatbridge import config as cfg_mod

        self.guard.global_cooldown_until = time.time() + 7.0
        self.client.send_message = AsyncMock(side_effect=RuntimeError("net down"))
        with mock.patch.object(
            self.main, "_run_llm", new=AsyncMock(return_value=self._ok("after-cool"))
        ) as run, mock.patch.object(
            self.main, "_guard_sleep", new=AsyncMock()
        ) as sleep, mock.patch.object(
            cfg_mod.config, "upstream_retry_max", 0
        ):
            reply, _ = await self.main._run_llm_with_guard(
                self.client, "user-b-send-fail", "ctx", "hi"
            )
        self.assertEqual(reply, "after-cool")
        self.assertEqual(run.await_count, 1)
        sleep.assert_awaited()
        self.client.send_message.assert_awaited()

    async def test_slot_reacquire_timeout_returns_busy(self):
        """Optional polish: after A backoff, re-acquire timeout → 现在有点忙, no leak."""
        from wechatbridge import config as cfg_mod

        # Simulate _safe_process_message: this task holds the only global slot.
        # During A backoff sleep_released releases it; a peer steals it and never
        # gives it back → re-acquire times out → busy reply, held stays False.
        sem = asyncio.Semaphore(1)
        await sem.acquire()
        our_slot = self.main._GlobalSlot(sem)
        token = self.main._global_slot_ctx.set(our_slot)
        throttle = self._throttle()
        run = AsyncMock(return_value=throttle)
        peer_held = {"ok": False}

        async def steal_during_sleep(_seconds):
            if not peer_held["ok"] and not our_slot.held:
                await asyncio.wait_for(sem.acquire(), timeout=0.2)
                peer_held["ok"] = True  # keep holding — force re-acquire timeout
            return None

        try:
            with mock.patch.object(self.main, "_run_llm", new=run), mock.patch.object(
                self.main.asyncio, "sleep", new=AsyncMock(side_effect=steal_during_sleep)
            ), mock.patch.object(
                cfg_mod.config, "upstream_retry_max", 2
            ), mock.patch.object(
                cfg_mod.config, "upstream_backoff", [0.01]
            ), mock.patch.object(
                cfg_mod.config, "slot_reacquire_timeout", 0.05
            ), mock.patch.object(
                cfg_mod.config, "slot_reacquire_attempts", 2
            ), mock.patch.object(
                self.main, "_get_backend", return_value="agy"
            ):
                reply, arts = await self.main._run_llm_with_guard(
                    self.client, "user-reaq", "ctx", "hi"
                )
            self.assertIn("现在有点忙", reply)
            self.assertEqual(arts, [])
            # Slot must not claim held if re-acquire failed (no double-release later)
            self.assertFalse(our_slot.held)
            self.assertTrue(peer_held["ok"])
            # peer still owns the only permit
            self.assertEqual(sem._value, 0)
        finally:
            self.main._global_slot_ctx.reset(token)
            if our_slot.held:
                sem.release()
                our_slot.held = False
            if peer_held.get("ok") and sem._value == 0:
                try:
                    sem.release()
                except ValueError:
                    pass


class _FakeGrokProc:
    """Minimal asyncio subprocess stand-in for run_grok path tests."""

    def __init__(self, stdout="", stderr="", rc=0, pid=4242):
        self._so = stdout.encode("utf-8")
        self._se = stderr.encode("utf-8")
        self.returncode = rc
        self.pid = pid

    async def communicate(self):
        return self._so, self._se


class TestGrokThrottlePreserve(unittest.IsolatedAsyncioTestCase):
    """#1/#2: drive real run_grok with mocked subprocess (no real grok binary)."""

    async def asyncSetUp(self):
        from wechatbridge.config import config

        self.td = tempfile.mkdtemp(prefix="wb-grok-throttle-")
        self._patchers = [
            mock.patch.object(config, "session_base_dir", self.td),
            mock.patch.object(config, "agy_timeout", 30),
            mock.patch.object(config, "grok_binary_path", "grok"),
            # Avoid depending on host ~/.grok/auth.json
            mock.patch(
                "wechatbridge.grok._sync_grok_auth", return_value=True
            ),
        ]
        for p in self._patchers:
            p.start()

    async def asyncTearDown(self):
        for p in self._patchers:
            p.stop()
        shutil.rmtree(self.td, ignore_errors=True)

    def _session_dir(self, user_id: str) -> str:
        from wechatbridge.runner_common import sanitize_user_id

        return os.path.join(self.td, sanitize_user_id(user_id))

    def _init_flag(self, user_id: str) -> str:
        return os.path.join(self._session_dir(user_id), ".initialized.grok")

    async def test_structured_rate_limit_nonzero_keeps_bell(self):
        """Non-zero exit + structured rate-limit JSON → 🔔 throttle, not ❌ 执行失败."""
        from wechatbridge import grok as grok_mod
        from wechatbridge.runner_common import (
            is_bridge_formatted_reply,
            is_upstream_throttle_reply,
        )

        err_json = json.dumps(
            {
                "type": "error",
                "message": "rate limit exceeded, please slow down",
            }
        )

        async def spawn(*_a, **_k):
            return _FakeGrokProc(err_json, "rate limit exceeded", rc=1)

        with mock.patch("asyncio.create_subprocess_exec", side_effect=spawn):
            display, arts = await grok_mod.run_grok("hi", "u-rl-nz")

        self.assertEqual(arts, [])
        self.assertTrue(display.startswith("🔔"), msg=display[:120])
        self.assertIn("请求较多", display)
        self.assertTrue(is_bridge_formatted_reply(display))
        self.assertTrue(is_upstream_throttle_reply(display))
        # Must not be washed into generic execution failure
        self.assertNotIn("执行失败", display)
        self.assertNotIn("执行出错", display)
        self.assertFalse(os.path.isfile(self._init_flag("u-rl-nz")))

    async def test_zero_exit_structured_rate_limit_no_mark(self):
        """Zero exit + structured rate-limit bubble → failed, no mark_initialized."""
        from wechatbridge import grok as grok_mod
        from wechatbridge.runner_common import (
            is_bridge_formatted_reply,
            is_upstream_throttle_reply,
        )

        err_json = json.dumps(
            {
                "type": "error",
                "message": "rate limit exceeded, please slow down",
            }
        )

        async def spawn(*_a, **_k):
            return _FakeGrokProc(err_json, "", rc=0)

        with mock.patch("asyncio.create_subprocess_exec", side_effect=spawn):
            display, arts = await grok_mod.run_grok("hi", "u-rl-z")

        self.assertEqual(arts, [])
        self.assertTrue(display.startswith("🔔"), msg=display[:120])
        self.assertIn("请求较多", display)
        self.assertTrue(is_bridge_formatted_reply(display))
        self.assertTrue(is_upstream_throttle_reply(display))
        # Session flag must NOT be written on first-message throttle
        self.assertFalse(os.path.isfile(self._init_flag("u-rl-z")))

    async def test_guard_retries_bell_throttle_from_grok_shape(self):
        """Guard recognises grok-shaped 🔔 rate-limit and retries."""
        from wechatbridge import main as main_mod
        from wechatbridge import config as cfg_mod
        from wechatbridge.runner_common import UpstreamGuard, format_cli_error

        guard = UpstreamGuard()
        client = MagicMock()
        client.state = MagicMock()
        client.state.baseurl = "https://example.test"
        client.state.bot_token = "tok"
        sent: list[str] = []

        async def _cap(**kwargs):
            sent.append(kwargs.get("text") or "")
            return True

        client.send_message = AsyncMock(side_effect=_cap)
        throttle = format_cli_error("rate limit exceeded", backend="grok"), []
        ok = ("recovered", [])
        run = AsyncMock(side_effect=[throttle, ok])

        with mock.patch.object(main_mod, "upstream_guard", guard), mock.patch.object(
            main_mod, "_run_llm", new=run
        ), mock.patch.object(
            main_mod, "_guard_sleep", new=AsyncMock()
        ), mock.patch.object(
            cfg_mod.config, "upstream_retry_max", 2
        ), mock.patch.object(
            cfg_mod.config, "upstream_backoff", [0, 0, 0]
        ), mock.patch.object(
            main_mod, "_get_backend", return_value="grok"
        ):
            reply, _ = await main_mod._run_llm_with_guard(
                client, "u-grok", "ctx", "hi"
            )
        self.assertEqual(reply, "recovered")
        self.assertEqual(run.await_count, 2)
        self.assertTrue(any("上游繁忙，正在重试" in t for t in sent))


class TestModelCmdRejectsBellThrottle(unittest.IsolatedAsyncioTestCase):
    """#3: /model must not parse 🔔 throttle text as a model list."""

    async def test_agy_model_bell_throttle(self):
        from wechatbridge.runner_common import format_notice
        from wechatbridge import agy as agy_mod

        thr = format_notice("助手通道繁忙", "上游限流")
        with mock.patch.object(
            agy_mod, "_run_agy_subcommand", new=AsyncMock(return_value=thr)
        ):
            out = await agy_mod._cmd_model("gemini-2.5", "u-model-agy")
        self.assertIn("无法获取模型列表", out)
        self.assertNotIn("模型已切换", out)

    async def test_grok_model_bell_throttle(self):
        from wechatbridge.runner_common import format_notice
        from wechatbridge import grok as grok_mod

        thr = format_notice("请求较多", "稍后再试")
        with mock.patch.object(
            grok_mod, "_run_grok_subcommand", new=AsyncMock(return_value=thr)
        ):
            out = await grok_mod._cmd_model("grok-4.5", "u-model-grok")
        self.assertIn("无法获取模型列表", out)
        self.assertNotIn("模型已切换", out)

    async def test_codex_model_bell_throttle(self):
        from wechatbridge.runner_common import format_notice
        from wechatbridge import codex as codex_mod

        thr = format_notice("助手通道繁忙", "上游限流")
        with mock.patch.object(
            codex_mod, "_run_codex_subcommand", new=AsyncMock(return_value=thr)
        ):
            out = await codex_mod._cmd_model("gpt-5.1-codex", "u-model-codex")
        self.assertIn("无法获取模型列表", out)
        self.assertNotIn("模型已切换", out)


class TestEnvIntList(unittest.TestCase):
    """#8 optional: _env_int_list boundary cases."""

    def test_default_and_valid(self):
        from wechatbridge.config import _env_int_list

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WB_TEST_BACKOFF", None)
            self.assertEqual(_env_int_list("WB_TEST_BACKOFF", "2,5,12"), [2, 5, 12])
        with mock.patch.dict(os.environ, {"WB_TEST_BACKOFF": "1, 3, 7"}):
            self.assertEqual(_env_int_list("WB_TEST_BACKOFF", "2,5,12"), [1, 3, 7])

    def test_skips_invalid_and_negative(self):
        from wechatbridge.config import _env_int_list

        with mock.patch.dict(os.environ, {"WB_TEST_BACKOFF": "2,x,-1,4"}):
            self.assertEqual(_env_int_list("WB_TEST_BACKOFF", "2,5,12"), [2, 4])

    def test_all_invalid_falls_back(self):
        from wechatbridge.config import _env_int_list

        with mock.patch.dict(os.environ, {"WB_TEST_BACKOFF": "x,y,-3"}):
            self.assertEqual(_env_int_list("WB_TEST_BACKOFF", "2,5,12"), [2, 5, 12])

    def test_zero_allowed(self):
        from wechatbridge.config import _env_int_list

        with mock.patch.dict(os.environ, {"WB_TEST_BACKOFF": "0,2"}):
            self.assertEqual(_env_int_list("WB_TEST_BACKOFF", "2,5,12"), [0, 2])


class TestPreflightBeforeSlot(unittest.IsolatedAsyncioTestCase):
    """#4: preflight C/B runs without holding global slot."""

    async def test_preflight_gap_and_cooldown(self):
        from wechatbridge import main as main_mod
        from wechatbridge.runner_common import UpstreamGuard

        guard = UpstreamGuard()
        guard.user_gap_until["u-pre"] = time.time() + 3.0
        guard.global_cooldown_until = time.time() + 5.0
        client = MagicMock()
        client.state = MagicMock()
        client.state.baseurl = "https://example.test"
        client.state.bot_token = "tok"
        sent: list[str] = []

        async def _cap(**kwargs):
            sent.append(kwargs.get("text") or "")
            return True

        client.send_message = AsyncMock(side_effect=_cap)

        with mock.patch.object(main_mod, "upstream_guard", guard), mock.patch.object(
            main_mod.asyncio, "sleep", new=AsyncMock()
        ) as sleep:
            await main_mod._await_upstream_preflight(client, "u-pre", "ctx")
        # gap + cool sleeps
        self.assertGreaterEqual(sleep.await_count, 2)
        self.assertTrue(any("上游冷却中" in t for t in sent))
        self.assertTrue(any("🔔" in t for t in sent))


if __name__ == "__main__":
    unittest.main()



class TestClassifyCliError(unittest.TestCase):
    """_classify_cli_error returns correct category for each error pattern."""

    def _cat(self, raw: str, backend: str = "agy") -> str:
        from wechatbridge.runner_common import _classify_cli_error
        return _classify_cli_error(raw, backend=backend)

    def _fmt(self, raw: str, backend: str = "agy") -> str:
        from wechatbridge.runner_common import format_cli_error
        return format_cli_error(raw, backend=backend)

    # --- payload_too_large --------------------------------------------------------

    def test_request_payload_size_exceeds(self):
        self.assertEqual(self._cat("request payload size exceeds the limit"), "payload_too_large")

    def test_payload_too_large(self):
        self.assertEqual(self._cat("payload too large"), "payload_too_large")

    def test_request_entity_too_large(self):
        self.assertEqual(self._cat("Request Entity Too Large"), "payload_too_large")

    def test_content_length_exceeds(self):
        self.assertEqual(self._cat("Content-Length exceeds the limit of 8MB"), "payload_too_large")

    def test_content_length_is_too_large(self):
        self.assertEqual(self._cat("content length is too large"), "payload_too_large")

    def test_http_status_413_explicit(self):
        self.assertEqual(self._cat("HTTP status 413"), "payload_too_large")
        self.assertEqual(self._cat("status code 413"), "payload_too_large")
        self.assertEqual(self._cat("http code 413"), "payload_too_large")
        self.assertEqual(self._cat("code=413"), "payload_too_large")

    def test_json_code_413(self):
        self.assertEqual(self._cat('{"code": 413, "message": "too big"}'), "payload_too_large")

    def test_413_in_date_not_matched(self):
        """A bare 413 in a date string must NOT be classified as payload_too_large."""
        self.assertNotEqual(self._cat("Session expired on 2024-04-13 at 10:00"), "payload_too_large")

    def test_413_in_path_not_matched(self):
        """A bare 413 in a file path or directory name must NOT match."""
        self.assertNotEqual(self._cat("file not found: /tmp/record-413.txt"), "payload_too_large")

    def test_413_in_version_not_matched(self):
        """Version numbers containing 413 must NOT match."""
        self.assertNotEqual(self._cat("library version 1.413.0"), "payload_too_large")

    # --- context_too_large --------------------------------------------------------

    def test_input_token_count_exceeds(self):
        self.assertEqual(self._cat("input token count exceeds the maximum"), "context_too_large")

    def test_context_length_exceeded(self):
        self.assertEqual(self._cat("context length exceeded"), "context_too_large")

    def test_maximum_context_length(self):
        self.assertEqual(self._cat("maximum context length is 128k tokens"), "context_too_large")

    def test_context_window_exceeded(self):
        self.assertEqual(self._cat("context window exceeded"), "context_too_large")

    def test_too_many_tokens(self):
        self.assertEqual(self._cat("too many tokens in this conversation"), "context_too_large")

    def test_your_input_context_is_too_long(self):
        self.assertEqual(self._cat("your input context is too long for this model"), "context_too_large")

    def test_invalid_argument_with_context(self):
        self.assertEqual(self._cat("INVALID_ARGUMENT: context is too long"), "context_too_large")

    def test_resource_exhausted_with_token(self):
        self.assertEqual(self._cat("RESOURCE_EXHAUSTED: token limit exceeded"), "context_too_large")

    def test_resource_exhausted_with_maximum(self):
        self.assertEqual(self._cat("resource_exhausted: maximum context size reached"), "context_too_large")

    # --- invalid_argument (generic) -----------------------------------------------

    def test_generic_invalid_argument(self):
        self.assertEqual(self._cat("INVALID_ARGUMENT: unknown field xyz"), "invalid_argument")

    def test_invalid_argument_no_context_token(self):
        self.assertEqual(self._cat("invalid_argument: bad request format"), "invalid_argument")

    # --- auth ---------------------------------------------------------------------

    def test_auth_generic(self):
        self.assertEqual(self._cat("Not signed in. Please run login --device"), "auth")

    def test_auth_unauthorized(self):
        self.assertEqual(self._cat("unauthorized access"), "auth")

    def test_auth_401_with_token(self):
        self.assertEqual(self._cat("401 error: token expired"), "auth")

    def test_auth_codex_specific(self):
        self.assertEqual(self._cat("codex login required", backend="codex"), "auth")

    def test_auth_codex_api_key(self):
        self.assertEqual(self._cat("codex_api_key missing", backend="codex"), "auth")

    # --- rate_limit ---------------------------------------------------------------

    def test_eligibility_resource_exhausted(self):
        self.assertEqual(self._cat("Eligibility check failed: RESOURCE_EXHAUSTED"), "resource_exhausted")

    def test_resource_exhausted_generic(self):
        self.assertEqual(self._cat("RESOURCE_EXHAUSTED: resource has been exhausted"), "resource_exhausted")

    def test_rate_limit_exceeded(self):
        self.assertEqual(self._cat("rate limit exceeded, please slow down"), "rate_limit")

    def test_too_many_requests(self):
        self.assertEqual(self._cat("HTTP 429 Too Many Requests"), "rate_limit")

    def test_bare_429(self):
        self.assertEqual(self._cat("upstream returned status 429"), "bare_429")

    # --- quota --------------------------------------------------------------------

    def test_quota_exceeded(self):
        self.assertEqual(self._cat("You exceeded your current quota"), "quota")

    def test_daily_quota(self):
        self.assertEqual(self._cat("daily quota exceeded"), "quota")

    def test_usage_limit(self):
        self.assertEqual(self._cat("usage limit reached"), "quota")

    # --- network ------------------------------------------------------------------

    def test_connection_refused(self):
        self.assertEqual(self._cat("connection refused"), "network")

    def test_connection_reset(self):
        self.assertEqual(self._cat("connection reset by peer"), "network")

    def test_network_unreachable(self):
        self.assertEqual(self._cat("network is unreachable"), "network")

    # --- agent_stream_interrupted -------------------------------------------------

    def test_agent_stream_interrupted_real_error(self):
        err = "Error: the connection to the agent was interrupted before the response finished: subscriber fell behind updates, stalled for 8s"
        self.assertEqual(self._cat(err), "agent_stream_interrupted")

    def test_agent_stream_interrupted_stalled_variants(self):
        self.assertEqual(self._cat("subscriber fell behind updates, stalled for 5s"), "agent_stream_interrupted")
        self.assertEqual(self._cat("subscriber fell behind updates, stalled for 13s"), "agent_stream_interrupted")
        self.assertEqual(self._cat("subscriber fell behind updates, stalled for 13 s"), "agent_stream_interrupted")
        self.assertEqual(self._cat("stalled for 8s"), "agent_stream_interrupted")

    def test_agent_stream_interrupted_conversation_stream_failed(self):
        self.assertEqual(self._cat("conversation update stream failed"), "agent_stream_interrupted")

    def test_agent_stream_interrupted_connection_interrupted(self):
        self.assertEqual(self._cat("connection to the agent was interrupted"), "agent_stream_interrupted")

    def test_agent_stream_interrupted_negative_installed(self):
        err = "package wechatbridge-cli 1.6.0, installed using Python 3.13.5"
        self.assertNotEqual(self._cat(err), "agent_stream_interrupted")

    # --- cascade_timeout ----------------------------------------------------------

    def test_cascade_timeout(self):
        self.assertEqual(self._cat("timeout waiting for cascade"), "cascade_timeout")

    def test_cascade_response_timeout(self):
        self.assertEqual(self._cat("timeout waiting for response"), "cascade_timeout")

    # --- timeout ------------------------------------------------------------------

    def test_generic_timeout(self):
        self.assertEqual(self._cat("timeout occurred"), "timeout")

    def test_timed_out(self):
        self.assertEqual(self._cat("operation timed out"), "timeout")

    def test_deadline_exceeded(self):
        self.assertEqual(self._cat("deadline exceeded"), "timeout")

    # --- permission ---------------------------------------------------------------

    def test_permission_denied(self):
        self.assertEqual(self._cat("permission denied"), "permission")

    # --- session_not_found --------------------------------------------------------

    def test_no_session_found(self):
        self.assertEqual(self._cat("no session found"), "session_not_found")

    # --- model_invalid ------------------------------------------------------------

    def test_model_not_found(self):
        self.assertEqual(self._cat("model not found"), "model_invalid")

    def test_model_unknown(self):
        self.assertEqual(self._cat("unknown model xyz"), "model_invalid")

    # --- command_not_found --------------------------------------------------------

    def test_command_not_found(self):
        self.assertEqual(self._cat("command not found: xyz"), "command_not_found")

    # --- not_found ----------------------------------------------------------------

    def test_not_found_generic(self):
        self.assertEqual(self._cat("not found"), "not_found")

    def test_enoent(self):
        self.assertEqual(self._cat("enoent: no such file"), "not_found")

    # --- unknown ------------------------------------------------------------------

    def test_empty_is_unknown(self):
        self.assertEqual(self._cat(""), "unknown")

    def test_garbage_is_unknown(self):
        self.assertEqual(self._cat("abc123 random noise"), "unknown")


class TestFormatCliErrorCategories(unittest.TestCase):
    """format_cli_error produces correct user-facing copy for each category."""

    def _fmt(self, raw: str, backend: str = "agy") -> str:
        from wechatbridge.runner_common import format_cli_error
        return format_cli_error(raw, backend=backend)

    def test_payload_too_large_title(self):
        out = self._fmt("request payload size exceeds the limit")
        self.assertIn("请求内容过大", out)
        self.assertIn("❌", out)
        self.assertIn("/new", out)

    def test_context_too_large_title(self):
        out = self._fmt("context length exceeded")
        self.assertIn("会话内容过长", out)
        self.assertIn("❌", out)
        self.assertIn("/new", out)

    def test_invalid_argument_title(self):
        out = self._fmt("INVALID_ARGUMENT: bad field")
        self.assertIn("请求参数无效", out)
        self.assertIn("❌", out)

    def test_rate_limit_still_notice(self):
        out = self._fmt("rate limit exceeded")
        self.assertIn("🔔", out)
        self.assertIn("请求较多", out)

    def test_quota_still_notice(self):
        out = self._fmt("quota exceeded")
        self.assertIn("🔔", out)
        self.assertIn("额度相关", out)

    def test_auth_still_error(self):
        out = self._fmt("not signed in")
        self.assertIn("❌", out)
        self.assertIn("未登录", out)

    def test_network_still_error(self):
        out = self._fmt("connection refused")
        self.assertIn("网络错误", out)

    def test_agent_stream_interrupted_copy(self):
        out = self._fmt("Error: the connection to the agent was interrupted before the response finished: subscriber fell behind updates, stalled for 8s")
        self.assertIn("助手连接中断", out)
        self.assertIn("❌", out)
        self.assertIn("/new", out)

    def test_timeout_still_error(self):
        out = self._fmt("timeout")
        self.assertIn("超时", out)

    def test_unknown_still_generic(self):
        out = self._fmt("something weird happened")
        self.assertIn("执行失败", out)


class TestFormatCliErrorNoRawInReply(unittest.TestCase):
    """format_cli_error must never echo raw English text to WeChat users."""

    def _fmt(self, raw: str) -> str:
        from wechatbridge.runner_common import format_cli_error
        return format_cli_error(raw, backend="agy")

    def test_no_raw_payload_error(self):
        out = self._fmt("request payload size exceeds the limit")
        self.assertNotIn("payload", out.lower())
        self.assertNotIn("request", out.lower())

    def test_no_raw_context_error(self):
        out = self._fmt("context length exceeded")
        self.assertNotIn("context", out.lower())
        self.assertNotIn("exceeded", out.lower())

    def test_no_raw_invalid_argument(self):
        out = self._fmt("INVALID_ARGUMENT: bad field")
        self.assertNotIn("INVALID_ARGUMENT", out)

    def test_no_raw_rate_limit(self):
        out = self._fmt("rate limit exceeded")
        self.assertNotIn("rate", out.lower())
        self.assertNotIn("limit", out.lower())

    def test_no_raw_agent_stream_interrupted(self):
        out = self._fmt("Error: the connection to the agent was interrupted before the response finished: subscriber fell behind updates, stalled for 8s")
        self.assertNotIn("subscriber", out.lower())
        self.assertNotIn("interrupted", out.lower())
        self.assertNotIn("stalled", out.lower())


class TestFormatCliErrorLogging(unittest.TestCase):
    """format_cli_error logs category, not raw text."""

    def test_log_contains_category_not_raw(self):
        from wechatbridge.runner_common import format_cli_error, _classify_cli_error
        import logging
        import io

        # First, verify the category is correct
        category = _classify_cli_error("request payload size exceeds the limit")
        self.assertEqual(category, "payload_too_large")

        # Capture log output — set logger level to INFO so our info() call propagates
        log_capture = io.StringIO()
        handler = logging.StreamHandler(log_capture)
        handler.setLevel(logging.DEBUG)
        logger = logging.getLogger("wechatbridge.runner")
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        try:
            format_cli_error("request payload size exceeds the limit", backend="agy")
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

        log_output = log_capture.getvalue()
        self.assertIn("category=payload_too_large", log_output)
        # Verify raw text is NOT in the log
        self.assertNotIn("request payload", log_output)

    def test_category_independent_of_raw(self):
        """_classify_cli_error is a pure function; verify it directly."""
        from wechatbridge.runner_common import _classify_cli_error

        # payload_too_large
        self.assertEqual(_classify_cli_error("request payload size exceeds the limit"), "payload_too_large")
        self.assertEqual(_classify_cli_error("payload too large"), "payload_too_large")
        # context_too_large
        self.assertEqual(_classify_cli_error("context length exceeded"), "context_too_large")
        self.assertEqual(_classify_cli_error("too many tokens"), "context_too_large")
        # invalid_argument
        self.assertEqual(_classify_cli_error("INVALID_ARGUMENT: bad field"), "invalid_argument")
        # rate_limit (resource_exhausted without context/token keyword)
        self.assertEqual(_classify_cli_error("RESOURCE_EXHAUSTED: quota"), "resource_exhausted")
        # rate_limit (explicit)
        self.assertEqual(_classify_cli_error("rate limit exceeded"), "rate_limit")
        # bare 429
        self.assertEqual(_classify_cli_error("status 429"), "bare_429")
        # quota
        self.assertEqual(_classify_cli_error("quota exceeded"), "quota")


class TestFormatCliError413Boundary(unittest.TestCase):
    """413 must not be misclassified when appearing in dates, paths, or versions."""

    def _cat(self, raw: str) -> str:
        from wechatbridge.runner_common import _classify_cli_error
        return _classify_cli_error(raw)

    def test_date_with_413(self):
        self.assertNotEqual(self._cat("2024-04-13T10:00:00"), "payload_too_large")

    def test_path_with_413(self):
        self.assertNotEqual(self._cat("/tmp/file-413.txt"), "payload_too_large")

    def test_version_with_413(self):
        self.assertNotEqual(self._cat("version 1.413.2"), "payload_too_large")

    def test_error_code_413_without_prefix(self):
        """A bare '413' without status/code/http prefix should NOT match."""
        self.assertNotEqual(self._cat("error 413 occurred"), "payload_too_large")

    def test_explicit_status_413(self):
        self.assertEqual(self._cat("status 413"), "payload_too_large")

    def test_explicit_code_413(self):
        self.assertEqual(self._cat("code: 413"), "payload_too_large")


class TestFormatCliErrorContextResourceExhausted(unittest.TestCase):
    """RESOURCE_EXHAUSTED with context/token keywords must classify as context_too_large."""

    def _cat(self, raw: str) -> str:
        from wechatbridge.runner_common import _classify_cli_error
        return _classify_cli_error(raw)

    def test_resource_exhausted_with_context(self):
        self.assertEqual(self._cat("RESOURCE_EXHAUSTED: context length exceeded"), "context_too_large")

    def test_resource_exhausted_with_token(self):
        self.assertEqual(self._cat("RESOURCE_EXHAUSTED: token limit reached"), "context_too_large")

    def test_resource_exhausted_with_maximum(self):
        self.assertEqual(self._cat("RESOURCE_EXHAUSTED: maximum context size"), "context_too_large")

    def test_invalid_argument_with_context(self):
        self.assertEqual(self._cat("INVALID_ARGUMENT: context too long"), "context_too_large")


class TestFormatCliErrorPreservesExistingThrottle(unittest.TestCase):
    """Existing throttle/quota behavior must not be regressed."""

    def _fmt(self, raw: str) -> str:
        from wechatbridge.runner_common import format_cli_error
        return format_cli_error(raw, backend="agy")

    def test_eligibility_still_busy(self):
        out = self._fmt("Eligibility check failed: RESOURCE_EXHAUSTED (code 429)")
        self.assertIn("助手通道繁忙", out)
        self.assertIn("🔔", out)

    def test_quota_exceeded_still_quota(self):
        out = self._fmt("You exceeded your current quota")
        self.assertIn("额度相关", out)
        self.assertIn("🔔", out)

    def test_rate_limit_still_busy(self):
        out = self._fmt("rate limit exceeded")
        self.assertIn("请求较多", out)
        self.assertIn("🔔", out)

    def test_bare_429_still_busy(self):
        out = self._fmt("upstream returned status 429")
        self.assertIn("助手通道繁忙", out)
        self.assertIn("🔔", out)


class TestClearInitializedPreservesHistory(unittest.TestCase):
    """clear_initialized must only remove .initialized flags, not conversation history."""

    def test_clear_initialized_only_removes_flags(self):
        from wechatbridge.runner_common import clear_initialized, ensure_session_dir

        with tempfile.TemporaryDirectory() as td:
            sd = os.path.join(td, "user_test")
            os.makedirs(sd, exist_ok=True)
            # Create .initialized flag
            flag = os.path.join(sd, ".initialized.agy")
            with open(flag, "w") as f:
                f.write("1")
            # Create some history files
            history_dir = os.path.join(sd, ".gemini", "antigravity-cli", "conversations")
            os.makedirs(history_dir, exist_ok=True)
            history_file = os.path.join(history_dir, "conv.db")
            with open(history_file, "w") as f:
                f.write("history data")
            # Create prefs
            prefs_file = os.path.join(sd, "prefs.json")
            with open(prefs_file, "w") as f:
                f.write("{}")

            clear_initialized(sd, backend="agy")

            # Flag is removed
            self.assertFalse(os.path.exists(flag))
            # History is preserved
            self.assertTrue(os.path.exists(history_file))
            # Prefs are preserved
            self.assertTrue(os.path.exists(prefs_file))


_UPDATE_SH = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "deploy", "update.sh"))


class TestUpdateScriptPackageMatch(unittest.TestCase):
    """Test update.sh logic for exact package name matching directly from deploy/update.sh."""

    @classmethod
    def setUpClass(cls):
        cls.script_path = _UPDATE_SH
        with open(cls.script_path, "r", encoding="utf-8") as f:
            cls.script_content = f.read()

    def _extract_match_pipelines(self) -> tuple[str, str]:
        """Extract the exact short-list and full-list matching pipelines from deploy/update.sh."""
        import re
        m = re.search(
            r"if\s+run_pipx\s+list\s+--short\s+2>/dev/null\s*\|\s*(.+?)\s*\|\|\s*run_pipx\s+list\s+2>/dev/null\s*\|\s*(.+?);\s*then",
            self.script_content,
        )
        self.assertIsNotNone(m, f"Could not find run_pipx list condition in {self.script_path}")
        short_pipe = m.group(1).strip()
        full_pipe = m.group(2).strip()
        return short_pipe, full_pipe

    def _test_short_match(self, input_text: str) -> bool:
        import subprocess
        short_pipe, _ = self._extract_match_pipelines()
        p = subprocess.run(["bash", "-c", short_pipe], input=input_text.encode("utf-8"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return p.returncode == 0

    def _test_full_match(self, input_text: str) -> bool:
        import subprocess
        _, full_pipe = self._extract_match_pipelines()
        p = subprocess.run(["bash", "-c", full_pipe], input=input_text.encode("utf-8"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return p.returncode == 0

    def test_script_syntax_valid(self):
        """deploy/update.sh itself must pass bash syntax check."""
        import subprocess
        p = subprocess.run(["bash", "-n", self.script_path], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, f"bash -n failed on update.sh: {p.stderr}")

    def test_short_exact_match(self):
        self.assertTrue(self._test_short_match("wechatbridge-cli 1.6.0\nother-pkg 0.1\n"))

    def test_short_prefix_no_match(self):
        self.assertFalse(self._test_short_match("wechatbridge-foo 1.0.0\nwechatbridge 1.0.0\n"))

    def test_full_exact_match(self):
        self.assertTrue(self._test_full_match("   package wechatbridge-cli 1.6.0, installed using Python 3.13.5\n"))

    def test_full_prefix_no_match(self):
        self.assertFalse(self._test_full_match("   package wechatbridge-foo 1.6.0, installed using Python 3.13.5\n"))
        self.assertFalse(self._test_full_match("   package wechatbridge 1.6.0, installed using Python 3.13.5\n"))

    def test_update_script_execution_branches(self):
        """Execute deploy/update.sh in isolated mock environment to verify upgrade vs install decision."""
        import subprocess, tempfile, shutil
        td = tempfile.mkdtemp()
        try:
            log_file = os.path.join(td, "pipx.log")
            mock_pipx = os.path.join(td, "pipx")
            with open(mock_pipx, "w", encoding="utf-8") as f:
                f.write(
                    f"#!/usr/bin/env bash\n"
                    f"if [ \"$1\" = \"list\" ] && [ \"${{2:-}}\" = \"--short\" ]; then\n"
                    f"  echo \"${{MOCK_PIPX_SHORT:-}}\"\n"
                    f"elif [ \"$1\" = \"list\" ]; then\n"
                    f"  echo \"${{MOCK_PIPX_FULL:-}}\"\n"
                    f"elif [ \"$1\" = \"upgrade\" ] || [ \"$1\" = \"install\" ]; then\n"
                    f"  echo \"$*\" >> {log_file!r}\n"
                    f"fi\n"
                    f"exit 0\n"
                )
            os.chmod(mock_pipx, 0o755)

            mock_wechatbridge = os.path.join(td, "wechatbridge")
            with open(mock_wechatbridge, "w", encoding="utf-8") as f:
                f.write("#!/usr/bin/env bash\necho 'wechatbridge 1.6.0'\nexit 0\n")
            os.chmod(mock_wechatbridge, 0o755)

            systemctl_log = os.path.join(td, "systemctl.log")
            mock_systemctl = os.path.join(td, "systemctl")
            with open(mock_systemctl, "w", encoding="utf-8") as f:
                f.write(
                    f"#!/usr/bin/env bash\n"
                    f"echo \"$*\" >> {systemctl_log!r}\n"
                    f"if [ \"$1\" = \"list-units\" ]; then\n"
                    f"  echo \"wechatbridge.service loaded active running\"\n"
                    f"fi\n"
                    f"exit 0\n"
                )
            os.chmod(mock_systemctl, 0o755)

            mock_sudo = os.path.join(td, "sudo")
            with open(mock_sudo, "w", encoding="utf-8") as f:
                f.write(
                    "#!/usr/bin/env bash\n"
                    "while [ $# -gt 0 ]; do\n"
                    "  case \"$1\" in\n"
                    "    -u)\n"
                    "      shift 2\n"
                    "      ;;\n"
                    "    -H)\n"
                    "      shift 1\n"
                    "      ;;\n"
                    "    --)\n"
                    "      shift\n"
                    "      break\n"
                    "      ;;\n"
                    "    *)\n"
                    "      break\n"
                    "      ;;\n"
                    "  esac\n"
                    "done\n"
                    "exec \"$@\"\n"
                )
            os.chmod(mock_sudo, 0o755)

            env = os.environ.copy()
            env["PATH"] = f"{td}:{env.get('PATH', '')}"
            # Test with WECHATBRIDGE_USER differing from whoami to verify sudo option consumption
            env["WECHATBRIDGE_USER"] = "different_user_for_test"

            # 1. Exact match in short output -> triggers upgrade
            if os.path.exists(log_file):
                os.remove(log_file)
            if os.path.exists(systemctl_log):
                os.remove(systemctl_log)
            env["MOCK_PIPX_SHORT"] = "wechatbridge-cli 1.6.0\nfoo 1.0"
            env["MOCK_PIPX_FULL"] = ""
            p = subprocess.run(["bash", self.script_path], env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            with open(log_file, "r", encoding="utf-8") as f:
                self.assertIn("upgrade wechatbridge-cli", f.read())
            self.assertTrue(os.path.exists(systemctl_log))
            with open(systemctl_log, "r", encoding="utf-8") as f:
                slog = f.read()
                self.assertIn("list-units", slog)
                self.assertIn("restart", slog)

            # 2. Prefix mismatch in short output -> triggers install
            if os.path.exists(log_file):
                os.remove(log_file)
            if os.path.exists(systemctl_log):
                os.remove(systemctl_log)
            env["MOCK_PIPX_SHORT"] = "wechatbridge-foo 1.6.0\nwechatbridge 1.6.0"
            env["MOCK_PIPX_FULL"] = ""
            p = subprocess.run(["bash", self.script_path], env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            with open(log_file, "r", encoding="utf-8") as f:
                self.assertIn("install wechatbridge-cli", f.read())
            self.assertTrue(os.path.exists(systemctl_log))
            with open(systemctl_log, "r", encoding="utf-8") as f:
                slog = f.read()
                self.assertIn("list-units", slog)
                self.assertIn("restart", slog)

            # 3. Exact match in full output -> triggers upgrade
            if os.path.exists(log_file):
                os.remove(log_file)
            if os.path.exists(systemctl_log):
                os.remove(systemctl_log)
            env["MOCK_PIPX_SHORT"] = ""
            env["MOCK_PIPX_FULL"] = "  package wechatbridge-cli 1.6.0, installed using Python 3.13.5\n"
            p = subprocess.run(["bash", self.script_path], env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            with open(log_file, "r", encoding="utf-8") as f:
                self.assertIn("upgrade wechatbridge-cli", f.read())
            self.assertTrue(os.path.exists(systemctl_log))
            with open(systemctl_log, "r", encoding="utf-8") as f:
                slog = f.read()
                self.assertIn("list-units", slog)
                self.assertIn("restart", slog)

            # 4. Prefix mismatch in full output -> triggers install
            if os.path.exists(log_file):
                os.remove(log_file)
            if os.path.exists(systemctl_log):
                os.remove(systemctl_log)
            env["MOCK_PIPX_SHORT"] = ""
            env["MOCK_PIPX_FULL"] = "  package wechatbridge-foo 1.6.0, installed using Python 3.13.5\n"
            p = subprocess.run(["bash", self.script_path], env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            with open(log_file, "r", encoding="utf-8") as f:
                self.assertIn("install wechatbridge-cli", f.read())
            self.assertTrue(os.path.exists(systemctl_log))
            with open(systemctl_log, "r", encoding="utf-8") as f:
                slog = f.read()
                self.assertIn("list-units", slog)
                self.assertIn("restart", slog)
        finally:
            shutil.rmtree(td, ignore_errors=True)


class _FakeAgyProc:
    def __init__(self, stdout: str, stderr: str, rc: int = 0, pid: int = 4242):
        self._stdout = stdout.encode("utf-8")
        self._stderr = stderr.encode("utf-8")
        self.returncode = rc
        self.pid = pid

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):
        pass


class TestAgyStreamRetry(unittest.IsolatedAsyncioTestCase):
    """Test agy runner retry behavior for stream interruption and cascade timeouts."""

    def setUp(self):
        from wechatbridge.config import config

        self.td = tempfile.mkdtemp(prefix="wb-agy-retry-")
        self._patchers = [
            mock.patch.object(config, "session_base_dir", self.td),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        shutil.rmtree(self.td, ignore_errors=True)

    def test_is_transient_stream_error_helper(self):
        from wechatbridge.agy import _is_transient_stream_error

        self.assertIsNone(_is_transient_stream_error(""))
        self.assertEqual(_is_transient_stream_error("timeout waiting for cascade"), "cascade")
        self.assertEqual(_is_transient_stream_error("timeout waiting for response"), "cascade")
        self.assertEqual(
            _is_transient_stream_error("subscriber fell behind updates, stalled for 8s"),
            "stream",
        )
        self.assertEqual(_is_transient_stream_error("conversation update stream failed"), "stream")
        self.assertEqual(_is_transient_stream_error("connection to the agent was interrupted"), "stream")
        self.assertIsNone(_is_transient_stream_error("package wechatbridge-cli 1.6.0, installed using Python 3.13.5"))
        self.assertIsNone(_is_transient_stream_error("some random error"))

    async def test_stream_interrupted_retries_and_recovers(self):
        from wechatbridge import agy as agy_mod

        calls = 0
        stream_err = (
            "Error: the connection to the agent was interrupted before the response finished: "
            "subscriber fell behind updates, stalled for 8s"
        )

        async def spawn(*_a, **_k):
            nonlocal calls
            calls += 1
            if calls == 1:
                return _FakeAgyProc("", stream_err, rc=1)
            return _FakeAgyProc("hello from retry", "", rc=0)

        with mock.patch("asyncio.create_subprocess_exec", side_effect=spawn), \
             mock.patch.object(agy_mod, "terminate_process", new=AsyncMock()), \
             mock.patch.object(agy_mod.asyncio, "sleep", new=AsyncMock()) as mock_sleep:
            display, arts = await agy_mod.run_agy("hi", "u-stream-ok")

        self.assertEqual(calls, 2)
        self.assertEqual(display, "hello from retry")
        self.assertEqual(arts, [])
        mock_sleep.assert_awaited_once_with(3)

    async def test_stream_interrupted_retry_fails_returns_stream_interrupted_copy(self):
        from wechatbridge import agy as agy_mod

        calls = 0
        stream_err = (
            "Error: the connection to the agent was interrupted before the response finished: "
            "subscriber fell behind updates, stalled for 8s"
        )

        async def spawn(*_a, **_k):
            nonlocal calls
            calls += 1
            return _FakeAgyProc("", stream_err, rc=1)

        with mock.patch("asyncio.create_subprocess_exec", side_effect=spawn), \
             mock.patch.object(agy_mod, "terminate_process", new=AsyncMock()), \
             mock.patch.object(agy_mod.asyncio, "sleep", new=AsyncMock()) as mock_sleep:
            display, arts = await agy_mod.run_agy("hi", "u-stream-fail")

        self.assertEqual(calls, 2)
        self.assertEqual(arts, [])
        self.assertIn("助手连接中断", display)
        self.assertIn("❌", display)
        self.assertIn("/new", display)
        mock_sleep.assert_awaited_once_with(3)

    async def test_stream_interrupted_retry_timeout_returns_stream_interrupted_copy(self):
        from wechatbridge import agy as agy_mod

        calls = 0
        stream_err = (
            "Error: the connection to the agent was interrupted before the response finished: "
            "subscriber fell behind updates, stalled for 8s"
        )

        class _TimeoutProc(_FakeAgyProc):
            async def communicate(self):
                raise asyncio.TimeoutError()

        async def spawn(*_a, **_k):
            nonlocal calls
            calls += 1
            if calls == 1:
                return _FakeAgyProc("", stream_err, rc=1)
            return _TimeoutProc("", "", rc=0)

        mock_term = AsyncMock()
        with mock.patch("asyncio.create_subprocess_exec", side_effect=spawn), \
             mock.patch.object(agy_mod, "terminate_process", new=mock_term), \
             mock.patch.object(agy_mod.asyncio, "sleep", new=AsyncMock()):
            display, arts = await agy_mod.run_agy("hi", "u-stream-timeout")

        self.assertEqual(calls, 2)
        self.assertEqual(arts, [])
        self.assertIn("助手连接中断", display)
        mock_term.assert_awaited_once()

    async def test_cascade_timeout_retries_and_fails_with_cascade_copy(self):
        from wechatbridge import agy as agy_mod

        calls = 0
        cascade_err = "Timeout waiting for cascade response"

        async def spawn(*_a, **_k):
            nonlocal calls
            calls += 1
            return _FakeAgyProc("", cascade_err, rc=1)

        with mock.patch("asyncio.create_subprocess_exec", side_effect=spawn), \
             mock.patch.object(agy_mod, "terminate_process", new=AsyncMock()), \
             mock.patch.object(agy_mod.asyncio, "sleep", new=AsyncMock()) as mock_sleep:
            display, arts = await agy_mod.run_agy("hi", "u-cascade-fail")

        self.assertEqual(calls, 2)
        self.assertEqual(arts, [])
        self.assertIn("模型响应超时", display)
        mock_sleep.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
