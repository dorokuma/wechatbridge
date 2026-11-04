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

    async def _pump_until(self, predicate, tries=100):
        """Yield to the loop until predicate() holds (bounded).

        A single sleep(0) is not enough on Python <= 3.11: the pre-3.12
        asyncio.wait_for in _safe_process_message costs extra loop passes
        before process_message is entered, so first-entry assertions must
        pump instead of assuming one yield suffices.
        """
        for _ in range(tries):
            if predicate():
                return
            await asyncio.sleep(0)

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
            await self._pump_until(lambda: len(started) >= 1)
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
                await self._pump_until(lambda: "run_start" in order)
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

    async def test_prune_keeps_lock_for_queued_user(self):
        """同 user：一条处理中、一条等锁时，prune 不得删除该用户的锁。

        回归：锁刚释放、等待者还没被唤醒时 lock.locked() 为 False，此时删锁
        会让后来者拿到新锁、打破串行。用 Lock 子类在 release() 里同步调
        prune——release() 在事件循环内同步执行，正好落在「已释放、等待者
        尚未被唤醒」的空隙，无需手动 release。
        """
        release = asyncio.Event()
        started, observed, entered = [], [], []
        main = self.main

        class _ProbingLock(asyncio.Lock):
            async def acquire(self):
                entered.append(1)  # t2 已抵达锁等待点（t1 仍持有锁）
                return await super().acquire()

            def release(self):
                super().release()
                # 同步调用：此刻等待者必然还没被唤醒
                main._prune_user_locks()
                observed.append("alice" in main.user_locks)

        async def _pm(client, msg):
            started.append(msg.get("from_user_id"))
            await release.wait()

        main.user_locks["alice"] = _ProbingLock()
        client = self._client()
        with mock.patch.object(main, "process_message", new=_pm):
            t1 = asyncio.create_task(main._safe_process_message(client, self._msg("alice")))
            t2 = asyncio.create_task(main._safe_process_message(client, self._msg("alice")))
            try:
                await self._pump_until(lambda: len(started) >= 1)
                # 确定性等待：t1、t2 都已进入 acquire——t1 持锁，t2 在等锁
                await self._pump_until(lambda: len(entered) >= 2)
            finally:
                release.set()
                await asyncio.gather(t1, t2)
        # 每次 release 后的空隙里，alice 的锁都必须存活
        self.assertTrue(observed)
        self.assertTrue(all(observed), "等待者未唤醒时 prune 不得删除该锁")
        # 全部完成后 prune 可以清掉空闲锁
        main._prune_user_locks()
        self.assertNotIn("alice", main.user_locks)


class TestQueueAndProgressNotice(unittest.IsolatedAsyncioTestCase):
    """UX notices: same-user queue notice + long-task progress notice.

    Both are fire-and-forget — failures must never affect processing, locks,
    or leak background tasks. State isolation mirrors TestPerUserLockContract.
    """

    def setUp(self):
        from wechatbridge import main as main_mod
        self.main = main_mod
        self._orig_locks = main_mod.user_locks
        self._orig_refs = dict(main_mod._user_lock_refs)
        self._orig_sem = main_mod._global_task_sem
        main_mod.user_locks = {}
        main_mod._user_lock_refs.clear()
        main_mod._global_task_sem = None
        self._sem_patch = mock.patch.object(
            main_mod.config, "max_concurrent_tasks", 16
        )
        self._sem_patch.start()
        self.addCleanup(self._sem_patch.stop)

    def tearDown(self):
        self.main.user_locks = self._orig_locks
        self.main._user_lock_refs.clear()
        self.main._user_lock_refs.update(self._orig_refs)
        self.main._global_task_sem = self._orig_sem

    def _client(self):
        client = MagicMock()
        client.state = MagicMock()
        client.state.baseurl = "https://example.test"
        client.state.bot_token = "tok"
        client.send_message = AsyncMock(return_value=True)
        return client

    def _msg(self, uid):
        return {"from_user_id": uid, "context_token": "ctx-" + uid}

    async def _pump_until(self, predicate, tries=200):
        for _ in range(tries):
            if predicate():
                return
            await asyncio.sleep(0)
        self.fail("pump_until predicate never satisfied")

    async def test_queue_notice_sent_when_user_lock_held(self):
        """同 user 第二条消息在锁被占用时收到一次性排队提示。"""
        released = asyncio.Event()
        started = []

        async def _pm(client, msg):
            started.append(msg.get("from_user_id"))
            await released.wait()

        client = self._client()
        with mock.patch.object(self.main.config, "queue_notice", True), \
             mock.patch.object(self.main, "process_message", new=_pm):
            t1 = asyncio.create_task(
                self.main._safe_process_message(client, self._msg("alice"))
            )
            t2 = asyncio.create_task(
                self.main._safe_process_message(client, self._msg("alice"))
            )
            # t1 holds the user lock; wait until t2 sent the queue notice
            await self._pump_until(
                lambda: client.send_message.await_count >= 1
            )
            texts = [
                c.kwargs.get("text", "")
                for c in client.send_message.await_args_list
            ]
            self.assertIn("排队", "".join(texts))
            # Only one notice while still queued
            self.assertEqual(client.send_message.await_count, 1)
            released.set()
            await asyncio.gather(t1, t2, return_exceptions=True)
        self.assertEqual(started, ["alice", "alice"])
        # Notice is one-shot per message: nothing more after entering the lock
        self.assertEqual(client.send_message.await_count, 1)

    async def test_queue_notice_disabled_by_env(self):
        """WECHATBRIDGE_QUEUE_NOTICE 关闭时不发排队提示。"""
        released = asyncio.Event()

        async def _pm(client, msg):
            await released.wait()

        client = self._client()
        with mock.patch.object(self.main.config, "queue_notice", False), \
             mock.patch.object(self.main, "process_message", new=_pm):
            t1 = asyncio.create_task(
                self.main._safe_process_message(client, self._msg("alice"))
            )
            t2 = asyncio.create_task(
                self.main._safe_process_message(client, self._msg("alice"))
            )
            # t2 must be queued behind the lock — pump enough loop passes
            await self._pump_until(
                lambda: self.main._user_lock_refs.get("alice", 0) >= 2
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(client.send_message.await_count, 0)
            released.set()
            await asyncio.gather(t1, t2, return_exceptions=True)
        self.assertEqual(client.send_message.await_count, 0)

    async def test_queue_notice_failure_does_not_break_queue(self):
        """排队提示 send_message 抛错：排队与锁流程不受影响。"""
        released = asyncio.Event()
        ended = []

        async def _pm(client, msg):
            await released.wait()
            ended.append(msg.get("from_user_id"))

        client = self._client()
        client.send_message = AsyncMock(side_effect=RuntimeError("ilink down"))
        with mock.patch.object(self.main.config, "queue_notice", True), \
             mock.patch.object(self.main, "process_message", new=_pm):
            t1 = asyncio.create_task(
                self.main._safe_process_message(client, self._msg("alice"))
            )
            t2 = asyncio.create_task(
                self.main._safe_process_message(client, self._msg("alice"))
            )
            await self._pump_until(
                lambda: client.send_message.await_count >= 1
            )
            released.set()
            results = await asyncio.gather(t1, t2, return_exceptions=True)
        # Neither task raised; both ran process_message to completion
        self.assertEqual(results, [None, None])
        self.assertEqual(ended, ["alice", "alice"])
        # Lock released cleanly — no residual refs
        self.assertNotIn("alice", self.main._user_lock_refs)

    async def test_progress_notice_fires_once_after_delay(self):
        """任务超过延迟阈值 → 发一次进度提示；任务结束不重复发。

        watcher 在 process_message 包装层里，故 mock 更深的 _process_message_impl
        保留真实 watcher 生命周期。
        """
        async def _impl(client, msg):
            await asyncio.sleep(0.35)

        client = self._client()
        with mock.patch.object(self.main.config, "progress_notice", True), \
             mock.patch.object(self.main.config, "progress_notice_delay", 0.05), \
             mock.patch.object(self.main, "_process_message_impl", new=_impl):
            await self.main._safe_process_message(client, self._msg("bob"))
        texts = [
            c.kwargs.get("text", "")
            for c in client.send_message.await_args_list
        ]
        self.assertEqual(
            1, sum("仍在处理中" in t for t in texts),
            "进度提示应恰好发一次",
        )

    async def test_progress_notice_disabled_by_env(self):
        """WECHATBRIDGE_PROGRESS_NOTICE 关闭时不发进度提示。"""
        async def _impl(client, msg):
            await asyncio.sleep(0.15)

        client = self._client()
        with mock.patch.object(self.main.config, "progress_notice", False), \
             mock.patch.object(self.main.config, "progress_notice_delay", 0.05), \
             mock.patch.object(self.main, "_process_message_impl", new=_impl):
            await self.main._safe_process_message(client, self._msg("bob"))
        self.assertEqual(client.send_message.await_count, 0)

    async def test_progress_notice_not_sent_for_fast_task(self):
        """任务在阈值内完成 → 不发提示、watcher 已取消无残留。"""
        async def _impl(client, msg):
            return

        client = self._client()
        with mock.patch.object(self.main.config, "progress_notice", True), \
             mock.patch.object(self.main.config, "progress_notice_delay", 0.05), \
             mock.patch.object(self.main, "_process_message_impl", new=_impl):
            await self.main._safe_process_message(client, self._msg("bob"))
        await asyncio.sleep(0.1)  # watcher window passes with nothing to send
        self.assertEqual(client.send_message.await_count, 0)

    async def test_progress_notice_send_failure_swallowed(self):
        """进度提示发送抛错：吞掉只记 debug，主流程正常结束。"""
        done = []

        async def _impl(client, msg):
            await asyncio.sleep(0.15)
            done.append(1)

        client = self._client()
        client.send_message = AsyncMock(side_effect=RuntimeError("ilink down"))
        with mock.patch.object(self.main.config, "progress_notice", True), \
             mock.patch.object(self.main.config, "progress_notice_delay", 0.05), \
             mock.patch.object(self.main, "_process_message_impl", new=_impl):
            await self.main._safe_process_message(client, self._msg("bob"))
        self.assertEqual(done, [1])
        self.assertGreaterEqual(client.send_message.await_count, 1)


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
            # run_grok gates on _grok_has_credentials(), which reads the real
            # host auth.json / XAI_API_KEY; without this the subprocess tests
            # only pass on machines where grok is already signed in.
            mock.patch(
                "wechatbridge.grok._grok_has_credentials", return_value=True
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

    @staticmethod
    def _read_log(p):
        if not os.path.exists(p):
            return ""
        with open(p, encoding="utf-8") as f:
            return f.read()

    def _update_sh_mocks(self, td):
        """Isolated mocks in td; returns (pipx_log, git_log, systemctl_log)."""
        log_file = os.path.join(td, "pipx.log")
        git_log = os.path.join(td, "git.log")
        systemctl_log = os.path.join(td, "systemctl.log")

        def _write(name, body):
            with open(os.path.join(td, name), "w", encoding="utf-8") as f:
                f.write(body)
            os.chmod(os.path.join(td, name), 0o755)

        _write("pipx", (
            "#!/usr/bin/env bash\n"
            'echo "$*" >> "$PIPX_LOG"\n'
            'if [ "$1" = "list" ]; then\n'
            '  [ "${2:-}" = "--short" ] && echo "$MOCK_PIPX_SHORT"\n'
            '  if [ "${2:-}" = "--json" ]; then echo "$MOCK_PIPX_JSON"; [ "$MOCK_PIPX_JSON_OK" = "1" ] && exit 0; exit 1; fi\n'
            '  echo "$MOCK_PIPX_FULL"; exit 0\n'
            "fi\n"
            "exit 0\n"
        ))
        _write("git", (
            "#!/usr/bin/env bash\n"
            'if [ "$1" = "-C" ] && [ "$3" = "pull" ]; then echo "$*" >> "$GIT_LOG"; [ "$MOCK_GIT_FAIL" = "1" ] && exit 1; exit 0; fi\n'
            'exec /usr/bin/git "$@"\n'
        ))
        _write("wechatbridge", "#!/usr/bin/env bash\necho 'wechatbridge 1.6.0'\n")
        _write("systemctl", (
            "#!/usr/bin/env bash\n"
            'echo "$*" >> "$SYSTEMCTL_LOG"\n'
            '[ "$1" = "list-units" ] && echo "wechatbridge.service loaded active running"\n'
            "exit 0\n"
        ))
        _write("sudo", (
            "#!/usr/bin/env bash\n"
            'while [ $# -gt 0 ]; do case "$1" in -u) shift 2 ;; -H) shift ;; *) break ;; esac; done\n'
            'exec "$@"\n'
        ))
        return log_file, git_log, systemctl_log

    def test_update_script_execution_branches(self):
        """Execute deploy/update.sh in isolated mock environment to verify upgrade vs install decision."""
        import subprocess, tempfile, shutil
        td = tempfile.mkdtemp()
        try:
            log_file, git_log, systemctl_log = self._update_sh_mocks(td)
            env = os.environ.copy()
            env["PATH"] = f"{td}:{env.get('PATH', '')}"
            # WECHATBRIDGE_USER differs from whoami to verify sudo option consumption
            env.update({"WECHATBRIDGE_USER": "different_user_for_test", "PIPX_LOG": log_file,
                        "GIT_LOG": git_log, "SYSTEMCTL_LOG": systemctl_log,
                        "MOCK_PIPX_JSON_OK": "0", "MOCK_GIT_FAIL": "0"})

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
            # 5. Install source: local path -> git pull --ff-only + pipx upgrade (never reinstall); PyPI name -> plain upgrade; broken source -> non-zero exit.
            repo = os.path.join(td, "repo")
            subprocess.run(["/usr/bin/git", "init", "-q", repo], check=True)
            src = '{"venvs": {"wechatbridge-cli": {"metadata": {"main_package": {"package_or_url": "%s"}}}}}'
            env.update({"MOCK_PIPX_SHORT": "wechatbridge-cli", "MOCK_PIPX_FULL": "", "MOCK_PIPX_JSON_OK": "1"})
            for s in (repo, f"file://{repo}"):
                [os.remove(f) for f in (log_file, git_log, systemctl_log) if os.path.exists(f)]
                env["MOCK_PIPX_JSON"] = src % s
                p = subprocess.run(["bash", self.script_path], env=env, capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn("pull --ff-only", self._read_log(git_log))
                self.assertIn("upgrade wechatbridge-cli", self._read_log(log_file))
                self.assertNotIn("reinstall", self._read_log(log_file))
                self.assertIn("restart", self._read_log(systemctl_log))
            [os.remove(f) for f in (log_file, git_log, systemctl_log) if os.path.exists(f)]
            env["MOCK_PIPX_JSON"] = src % "wechatbridge-cli"
            p = subprocess.run(["bash", self.script_path], env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertFalse(os.path.exists(git_log))
            self.assertIn("upgrade wechatbridge-cli", self._read_log(log_file))
            for s, pulled in ((os.path.join(td, "nope"), False), (td, False), (repo, True)):
                [os.remove(f) for f in (log_file, git_log, systemctl_log) if os.path.exists(f)]
                env["MOCK_PIPX_JSON"] = src % s
                env["MOCK_GIT_FAIL"] = "1" if pulled else "0"
                p = subprocess.run(["bash", self.script_path], env=env, capture_output=True, text=True)
                self.assertNotEqual(p.returncode, 0)
                self.assertIn("ERROR", p.stderr)
                self.assertEqual("pull --ff-only" in self._read_log(git_log), pulled)
                self.assertNotIn("upgrade", self._read_log(log_file))
                self.assertFalse(os.path.exists(systemctl_log))
            # `pipx list --json` broken (empty / exit 1) -> pipx_metadata.json
            venv_dir = os.path.join(td, "pipx home", "venvs", "wechatbridge-cli")
            os.makedirs(venv_dir)
            with open(os.path.join(venv_dir, "pipx_metadata.json"), "w", encoding="utf-8") as f:
                f.write('{"main_package": {"package_or_url": "%s"}}' % repo)
            env.update({"PIPX_HOME": os.path.join(td, "pipx home"), "MOCK_GIT_FAIL": "0"})
            for js in ("", src % repo):
                [os.remove(f) for f in (log_file, git_log, systemctl_log) if os.path.exists(f)]
                if js: os.remove(os.path.join(venv_dir, "pipx_metadata.json"))
                env.update({"MOCK_PIPX_JSON": js, "MOCK_PIPX_JSON_OK": "0"})
                p = subprocess.run(["bash", self.script_path], env=env, capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn("pull --ff-only", self._read_log(git_log))
                self.assertIn("upgrade wechatbridge-cli", self._read_log(log_file))
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_access_control_env_warning(self):
        """Extract check_access_control_env from update.sh and run it against
        temp env files: warn when no active whitelist/admin line, stay quiet
        when ADMINS is configured."""
        import re
        import subprocess
        m = re.search(
            r"(?ms)^(check_access_control_env\(\)\s*\{.*?^\})",
            self.script_content,
        )
        self.assertIsNotNone(
            m, "Could not extract check_access_control_env from update.sh"
        )
        func_src = m.group(1)
        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            cfg = os.path.join(home, ".config", "wechatbridge")
            os.makedirs(cfg)
            env_file = os.path.join(cfg, ".env")
            # Stub `eval` so `eval echo "~user"` returns our temp home
            # (the real expansion would point at the caller's home).
            wrapper = (
                func_src
                + '\nTARGET_USER="t_wb_user"'
                + f"\neval() {{ echo {home!r}; }}"
                + "\ncheck_access_control_env"
            )

            def _run():
                return subprocess.run(
                    ["bash", "-c", wrapper],
                    capture_output=True, text=True,
                ).stdout

            # 两项都未有效配置 → 打印警告
            with open(env_file, "w", encoding="utf-8") as f:
                f.write(
                    "# WECHATBRIDGE_ALLOWED_SENDERS=wxid_a\n"
                    "WECHATBRIDGE_ALLOWED_SENDERS=\n"
                    "WECHATBRIDGE_ADMINS=\n"
                )
            self.assertIn("WARNING", _run())

            # 已配 ADMINS → 不警告
            with open(env_file, "w", encoding="utf-8") as f:
                f.write("WECHATBRIDGE_ADMINS=wxid_admin\n")
            self.assertNotIn("WARNING", _run())


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


class TestAuditHardeningFixes(unittest.IsolatedAsyncioTestCase):
    """Regression tests for deep-audit security and correctness fixes."""

    def setUp(self):
        from wechatbridge.config import config as cfg
        self._cfg = cfg
        self._tmp = tempfile.TemporaryDirectory()
        self.session_base = self._tmp.name
        self._orig_base = cfg.session_base_dir
        self._orig_backend = cfg.backend
        cfg.session_base_dir = self.session_base

    def tearDown(self):
        self._cfg.session_base_dir = self._orig_base
        self._cfg.backend = self._orig_backend
        self._tmp.cleanup()

    async def test_agy_send_artifacts_allows_session_dir_and_blocks_sensitive_or_hardlink(self):
        from wechatbridge import main as main_mod
        from wechatbridge.runner_common import ensure_session_dir

        self._cfg.backend = "agy"
        uid = "u-agy-sec"
        sdir = ensure_session_dir(uid)

        # Normal artifact directly in session_dir (should be allowed)
        good_file = os.path.join(sdir, "report.pdf")
        with open(good_file, "wb") as f:
            f.write(b"%PDF-1.4 ok")

        # Normal artifact in .gemini/antigravity-cli/scratch (should be allowed)
        scratch_dir = os.path.join(sdir, ".gemini", "antigravity-cli", "scratch")
        os.makedirs(scratch_dir, exist_ok=True)
        scratch_file = os.path.join(scratch_dir, "chart.png")
        with open(scratch_file, "wb") as f:
            f.write(b"PNGDATA")

        # Sensitive files inside session_dir (must be blocked)
        prefs_file = os.path.join(sdir, "prefs.json")
        with open(prefs_file, "w") as f:
            f.write('{"model": "gemini-4-argon"}')
        gemini_dir = os.path.join(sdir, ".gemini")
        os.makedirs(gemini_dir, exist_ok=True)
        token_file = os.path.join(gemini_dir, "antigravity-oauth-token")
        with open(token_file, "w") as f:
            f.write("secret-oauth-token")

        # Hardlink inside session_dir pointing to external file (must be blocked)
        ext_file = os.path.join(self.session_base, "external_secret.txt")
        with open(ext_file, "w") as f:
            f.write("top-secret")
        hardlink_file = os.path.join(sdir, "linked.txt")
        os.link(ext_file, hardlink_file)

        client = mock.MagicMock()
        client.state.baseurl = "https://ilink"
        client.state.bot_token = "tok"
        client.send_media = AsyncMock(return_value=True)
        client.send_message = AsyncMock(return_value=True)

        await main_mod.send_artifacts_back(
            artifacts=[
                ("report.pdf", good_file),
                ("chart.png", scratch_file),
                ("prefs.json", prefs_file),
                ("antigravity-oauth-token", token_file),
                ("linked.txt", hardlink_file),
            ],
            client=client,
            from_user=uid,
            context_token="ctx",
        )

        sent_paths = [c.kwargs["path"] for c in client.send_media.await_args_list]
        self.assertEqual(
            sent_paths,
            [os.path.realpath(good_file), os.path.realpath(scratch_file)],
        )

    def test_agy_extract_artifacts_balanced_parens_and_model_tsv_parsing(self):
        from wechatbridge import agy as agy_mod

        text = (
            "已生成 [report (1).pdf](file:///tmp/sess/report%20(1).pdf) "
            "和 [v2 [final].docx](file:///tmp/sess/v2%20[final].docx)。"
        )
        arts = agy_mod.extract_artifacts(text)
        self.assertEqual(
            arts,
            [
                ("report (1).pdf", "/tmp/sess/report (1).pdf"),
                ("v2 [final].docx", "/tmp/sess/v2 [final].docx"),
            ],
        )

        tsv_models = (
            "gemini-4-argon\tGemini 3.1 Pro (High)\n"
            "claude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)\n"
        )
        self.assertEqual(
            agy_mod._parse_agy_model_ids(tsv_models),
            ["gemini-4-argon", "claude-opus-4-6-thinking"],
        )

    async def test_planning_and_fast_toggle_and_clear_resets_mode(self):
        from wechatbridge import agy as agy_mod
        from wechatbridge.runner_common import load_prefs

        uid = "u-toggle"
        r1 = await agy_mod.handle_slash_command("/planning", uid)
        self.assertIn("已开启规划模式", r1)
        self.assertEqual(load_prefs(uid).get("mode"), "plan")

        # Calling /planning again toggles it off
        r2 = await agy_mod.handle_slash_command("/planning", uid)
        self.assertIn("已关闭规划模式", r2)
        self.assertEqual(load_prefs(uid).get("mode"), "")

        # Enable again and verify /clear resets sticky mode
        await agy_mod.handle_slash_command("/planning", uid)
        self.assertEqual(load_prefs(uid).get("mode"), "plan")
        await agy_mod.handle_slash_command("/clear", uid)
        self.assertEqual(load_prefs(uid).get("mode"), "")

        # /fast off
        await agy_mod.handle_slash_command("/fast", uid)
        self.assertEqual(load_prefs(uid).get("effort"), "low")
        r_fast_off = await agy_mod.handle_slash_command("/fast off", uid)
        self.assertIn("已关闭快速模式", r_fast_off)
        self.assertEqual(load_prefs(uid).get("effort"), "")

    def test_sanitize_prompt_at_paths_allows_session_and_add_dirs_blocks_external(self):
        from wechatbridge.runner_common import _sanitize_prompt_at_paths, ensure_session_dir

        sdir = ensure_session_dir("u-at")
        extra_dir = os.path.join(self.session_base, "allowed_extra")
        os.makedirs(extra_dir, exist_ok=True)
        prompt = (
            f"请看 @{sdir}/img.png 和 @{extra_dir}/doc.md "
            "不要读 @/etc/passwd 或 @../../etc/shadow"
        )
        sanitized = _sanitize_prompt_at_paths(prompt, sdir, extra_roots=[extra_dir])
        self.assertIn(f"@{sdir}/img.png", sanitized)
        self.assertIn(f"@{extra_dir}/doc.md", sanitized)
        self.assertNotIn("/etc/passwd", sanitized)
        self.assertNotIn("/etc/shadow", sanitized)
        self.assertEqual(sanitized.count("[blocked-path]"), 2)

    async def test_ilink_get_updates_raises_on_5xx_and_encrypt_roundtrip(self):
        import httpx
        from wechatbridge.ilink import ILinkClient, _encrypt_aes_ecb, _decrypt_aes_ecb

        key = os.urandom(16)
        for length in (0, 1, 15, 16, 17, 32, 100):
            raw = os.urandom(length)
            enc = _encrypt_aes_ecb(raw, key)
            self.assertEqual(_decrypt_aes_ecb(enc, key), raw)

        client = ILinkClient()
        try:
            req = httpx.Request("POST", "https://ilink/ilink/bot/getupdates")
            resp_502 = httpx.Response(502, request=req)
            client.http_client.post = AsyncMock(
                side_effect=httpx.HTTPStatusError("502 Bad Gateway", request=req, response=resp_502)
            )
            client.state.clear = mock.MagicMock()
            with self.assertRaises(httpx.HTTPStatusError):
                await client.get_updates("buf1", "https://ilink", "tok")
            client.state.clear.assert_not_called()
        finally:
            await client.close()

    async def test_price_http_error_does_not_leak_url_or_appkey(self):
        import httpx
        from wechatbridge import price as price_mod

        req = httpx.Request(
            "GET",
            "https://openapi.dataoke.com/api/goods/get-goods-details?appKey=SECRET_DT_KEY_123&id=1",
        )
        err = httpx.ConnectError(
            "Connection failed for https://openapi.dataoke.com/api?appKey=SECRET_DT_KEY_123",
            request=req,
        )
        with mock.patch.object(price_mod, "dt_configured", return_value=True), \
             mock.patch.object(price_mod, "query_tb_item", side_effect=err):
            reply = await price_mod.handle_price_query({"kind": "tb_item", "item_id": "1"})
        self.assertNotIn("SECRET_DT_KEY_123", reply)
        self.assertIn("比价接口请求失败", reply)

        with self.assertRaises(ValueError):
            price_mod._resolve_short_link("http://127.0.0.1:8080/admin")
        self.assertFalse(price_mod._is_allowed_short_url("https://evil.u.jd.com/abc"))
        self.assertFalse(price_mod._is_allowed_short_url("https://a.3.cn/xyz"))
        self.assertTrue(price_mod._is_allowed_short_url("https://u.jd.com/abc"))

    def test_ensure_user_gemini_refreshes_stale_oauth_token_and_blocks_copied_secrets(self):
        from wechatbridge import agy as agy_mod
        from wechatbridge.runner_common import is_sensitive_artifact_path, _sanitize_prompt_at_paths

        fake_host_home = os.path.join(self.session_base, "host_home")
        host_agy = os.path.join(fake_host_home, ".gemini", "antigravity-cli")
        os.makedirs(host_agy, exist_ok=True)
        src_token = os.path.join(host_agy, "antigravity-oauth-token")
        with open(src_token, "w") as f:
            f.write("old-token-value-111111")

        def _fake_expanduser(p: str) -> str:
            if p.startswith("~/"):
                return os.path.join(fake_host_home, p[2:])
            return p

        with mock.patch("os.path.expanduser", side_effect=_fake_expanduser):
            sdir = agy_mod.ensure_user_gemini("u-tok-refresh")
            dst_token = os.path.join(sdir, ".gemini", "antigravity-cli", "antigravity-oauth-token")
            with open(dst_token) as f:
                self.assertEqual(f.read(), "old-token-value-111111")

            # Make dst older and update src token
            os.utime(dst_token, (1000.0, 1000.0))
            with open(src_token, "w") as f:
                f.write("refreshed-token-value-222222")
            os.utime(src_token, (2000.0, 2000.0))

            agy_mod.ensure_user_gemini("u-tok-refresh")
            with open(dst_token) as f:
                self.assertEqual(f.read(), "refreshed-token-value-222222")

        # Renamed / copied credentials must still be blocked by is_sensitive_artifact_path
        copied_secret = os.path.join(sdir, "token.copy")
        with open(copied_secret, "w") as f:
            f.write("refreshed-token-value-222222")
        self.assertTrue(is_sensitive_artifact_path(copied_secret, sdir))

        auth_bak = os.path.join(sdir, "auth.json.bak")
        with open(auth_bak, "w") as f:
            f.write("{}")
        self.assertTrue(is_sensitive_artifact_path(auth_bak, sdir))

        scratch_settings = os.path.join(sdir, ".gemini", "antigravity-cli", "scratch", "settings.json")
        os.makedirs(os.path.dirname(scratch_settings), exist_ok=True)
        with open(scratch_settings, "w") as f:
            f.write("{}")
        self.assertTrue(is_sensitive_artifact_path(scratch_settings, sdir))

        # @<path> sanitization for bare sensitive names, URL-encoded traversal, and URLs
        p_out = _sanitize_prompt_at_paths(
            "read @prefs.json @auth.json.bak @..%2f..%2fetc%2fpasswd @https://evil.com/a",
            sdir,
        )
        self.assertEqual(
            p_out,
            "read [blocked-path] [blocked-path] [blocked-path] [blocked-path]",
        )


class TestAccessControlFailClosed(unittest.IsolatedAsyncioTestCase):
    """Fail-closed whitelist regression (v1.8.0 breaking change).

    Empty WECHATBRIDGE_ALLOWED_SENDERS no longer means allow-all:
    only WECHATBRIDGE_ADMINS members pass; both empty = deny everyone.
    "*" remains the explicit opt-in for open access.
    """

    def setUp(self):
        from wechatbridge import main as main_mod
        self.main = main_mod
        self._orig_allowed = main_mod.config.allowed_senders
        self._orig_admins = main_mod.config.admin_users

    def tearDown(self):
        self.main.config.allowed_senders = self._orig_allowed
        self.main.config.admin_users = self._orig_admins

    def _client(self):
        client = MagicMock()
        client.state = MagicMock()
        client.state.baseurl = "https://example.test"
        client.state.bot_token = "tok"
        client.send_message = AsyncMock(return_value=True)
        return client

    def _msg(self, uid):
        return {
            "from_user_id": uid,
            "context_token": "ctx-" + uid,
            "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
        }

    async def test_empty_whitelist_fail_closed(self):
        """空白名单 + 空管理员 → 非管理员被拒；顺带断言管理员放行与 '*' 放行。"""
        from wechatbridge import main as main_mod

        sent_texts = []

        async def _cap(**kwargs):
            sent_texts.append(kwargs.get("text") or "")
            return True

        client = self._client()
        client.send_message = AsyncMock(side_effect=_cap)

        # 空白名单 + 空管理员：拒绝所有人
        self.main.config.allowed_senders = []
        self.main.config.admin_users = []
        await main_mod.process_message(client, self._msg("random-user"))
        self.assertTrue(any("未授权用户" in t for t in sent_texts))

        # 空白名单 + 有管理员：管理员放行、非管理员被拒
        sent_texts.clear()
        self.main.config.admin_users = ["admin-1"]
        with mock.patch.object(
            main_mod, "gate_and_run", new=AsyncMock(return_value=("ok", []))
        ) as gr:
            await main_mod.process_message(client, self._msg("admin-1"))
            gr.assert_awaited()
        await main_mod.process_message(client, self._msg("random-user"))
        self.assertTrue(any("未授权用户" in t for t in sent_texts))

        # "*" 显式放开：任何人放行
        sent_texts.clear()
        self.main.config.allowed_senders = ["*"]
        self.main.config.admin_users = []
        with mock.patch.object(
            main_mod, "gate_and_run", new=AsyncMock(return_value=("ok", []))
        ) as gr:
            await main_mod.process_message(client, self._msg("random-user"))
            gr.assert_awaited()
        self.assertFalse(any("未授权用户" in t for t in sent_texts))


class TestDshHostCredentialContentMatch(unittest.TestCase):
    """Regression: host credential content blocks renamed artifacts.

    The dsh child process reads the machine-wide DSH_HOME, and grok/codex
    children read the host ~/.grok / ~/.codex — all under the same UID as
    the bridge. A file copied out of any of them under an innocent name
    must still be rejected, even when the session dir contributes no
    secret files.
    """

    def test_host_credentials_content_blocks_renamed_artifact(self):
        from wechatbridge import runner_common as rc

        with tempfile.TemporaryDirectory() as td:
            host_home = os.path.join(td, "dsh_home")
            sess_dir = os.path.join(td, "session")
            os.makedirs(host_home)
            os.makedirs(sess_dir)
            fake_dsh_cred = b"\n".join([
                b"provider: deepseek",
                b"model: fake-dsh-model",
                b"base_url: https://api.example.invalid/v1",
                b"token: t-" + b"x" * 20,
            ]) + b"\n"
            with open(os.path.join(host_home, ".credentials.yaml"), "wb") as f:
                f.write(fake_dsh_cred)
            # dsh 子进程复制/改名后的文件，落在会话目录内
            out_txt = os.path.join(sess_dir, "out.txt")
            with open(out_txt, "wb") as f:
                f.write(b"prefix\n" + fake_dsh_cred + b"suffix\n")

            with mock.patch.object(rc, "host_dsh_home", return_value=host_home):
                self.assertTrue(rc.is_sensitive_artifact_path(out_txt, sess_dir))
                # 会话目录为空串时宿主凭据内容匹配仍生效
                self.assertTrue(rc.is_sensitive_artifact_path(out_txt, ""))

    def test_host_grok_codex_credentials_block_renamed_artifact(self):
        """Host ~/.grok/auth.json and ~/.codex/auth.json content must also
        block renamed artifacts (children share the bridge UID and can copy
        host files into the session dir)."""
        from wechatbridge import runner_common as rc

        with tempfile.TemporaryDirectory() as td:
            host_home = os.path.join(td, "host")
            sess_dir = os.path.join(td, "session")
            os.makedirs(os.path.join(host_home, ".grok"))
            os.makedirs(os.path.join(host_home, ".codex"))
            os.makedirs(sess_dir)
            grok_secret = b"{" + b"grok" * 40 + b"}"
            codex_secret = b"{" + b"codex" * 40 + b"}"
            with open(os.path.join(host_home, ".grok", "auth.json"), "wb") as f:
                f.write(grok_secret)
            with open(os.path.join(host_home, ".codex", "auth.json"), "wb") as f:
                f.write(codex_secret)
            # grok/codex 子进程改名复制后的文件
            g_out = os.path.join(sess_dir, "notes.txt")
            c_out = os.path.join(sess_dir, "result.md")
            with open(g_out, "wb") as f:
                f.write(b"x\n" + grok_secret + b"\n")
            with open(c_out, "wb") as f:
                f.write(codex_secret + b"\nend")

            with mock.patch.dict(
                os.environ, {"WECHATBRIDGE_HOST_HOME": host_home}, clear=False
            ):
                self.assertTrue(rc.is_sensitive_artifact_path(g_out, sess_dir))
                self.assertTrue(rc.is_sensitive_artifact_path(c_out, sess_dir))
                # 会话目录为空串时宿主凭据内容匹配仍生效
                self.assertTrue(rc.is_sensitive_artifact_path(g_out, ""))
                self.assertTrue(rc.is_sensitive_artifact_path(c_out, ""))


class TestUxErrorAndHelp(unittest.IsolatedAsyncioTestCase):
    """v1.9.0 UX batch: FileNotFoundError 归类、超时/unknown 文案、
    /model /add-dir 无参行为、二维码清理、poll 恢复日志、price 防御解析。"""

    def setUp(self):
        from wechatbridge.config import config
        self._cfg = config
        self._tmp = tempfile.TemporaryDirectory()
        self.session_base = self._tmp.name
        self._orig_base = config.session_base_dir
        config.session_base_dir = self.session_base

    def tearDown(self):
        self._cfg.session_base_dir = self._orig_base
        self._tmp.cleanup()

    # ---- FileNotFoundError → command not found 归类 ---------------------------

    async def _assert_fnf_maps_to_command_not_found(self, run_coro, missing_name):
        """ENOENT must name the spawned binary (e.filename == cmd[0]) to map
        to 「助手不可用」— mocking the real asyncio failure semantics."""
        async def spawn(*_a, **_k):
            raise FileNotFoundError(2, "No such file or directory", missing_name)

        with mock.patch("asyncio.create_subprocess_exec", side_effect=spawn):
            display, artifacts = await run_coro()
        self.assertEqual(artifacts, [])
        self.assertTrue(display.startswith("❌"), msg=display[:80])
        self.assertIn("助手不可用", display)
        self.assertIn("/backend", display)

    async def test_run_agy_binary_missing(self):
        from wechatbridge import agy as agy_mod
        from wechatbridge.config import config

        with mock.patch.object(config, "agy_binary_path", "/nonexistent/agy"):
            await self._assert_fnf_maps_to_command_not_found(
                lambda: agy_mod.run_agy("hi", "u-fnf-agy"), "/nonexistent/agy"
            )

    async def test_run_grok_binary_missing(self):
        from wechatbridge import grok as grok_mod
        from wechatbridge.config import config

        with mock.patch.object(config, "grok_binary_path", "/nonexistent/grok"), \
             mock.patch("wechatbridge.grok._sync_grok_auth", return_value=True), \
             mock.patch("wechatbridge.grok._grok_has_credentials", return_value=True):
            await self._assert_fnf_maps_to_command_not_found(
                lambda: grok_mod.run_grok("hi", "u-fnf-grok"), "/nonexistent/grok"
            )

    async def test_run_codex_binary_missing(self):
        from wechatbridge import codex as codex_mod
        from wechatbridge.config import config

        with mock.patch.object(config, "codex_binary_path", "/nonexistent/codex"):
            await self._assert_fnf_maps_to_command_not_found(
                lambda: codex_mod.run_codex("hi", "u-fnf-codex"), "/nonexistent/codex"
            )

    async def test_run_dsh_binary_missing(self):
        from wechatbridge import dsh as dsh_mod
        from wechatbridge.config import config

        host_home = os.path.join(self.session_base, "dsh-home")
        os.makedirs(host_home, exist_ok=True)
        with open(os.path.join(host_home, ".credentials.yaml"), "w") as f:
            f.write("provider: deepseek\n")

        async def spawn(*_a, **_k):
            raise FileNotFoundError(2, "No such file or directory", "/nonexistent/dsh")

        with mock.patch.object(config, "dsh_binary_path", "/nonexistent/dsh"), \
             mock.patch.object(config, "dsh_home", host_home), \
             mock.patch("asyncio.create_subprocess_exec", side_effect=spawn):
            display, artifacts = await dsh_mod.run_dsh("hi", "u-fnf-dsh")
        self.assertEqual(artifacts, [])
        self.assertIn("助手不可用", display)
        self.assertIn("/backend", display)

    async def test_agy_subcommand_binary_missing(self):
        from wechatbridge import agy as agy_mod
        from wechatbridge.config import config

        async def spawn(*_a, **_k):
            raise FileNotFoundError(2, "No such file or directory", "/nonexistent/agy")

        with mock.patch.object(config, "agy_binary_path", "/nonexistent/agy"), \
             mock.patch("asyncio.create_subprocess_exec", side_effect=spawn):
            out = await agy_mod._run_agy_subcommand(["models"], "u-fnf-sub")
        self.assertIn("助手不可用", out)

    # ---- FileNotFoundError 指向 session_dir/其它路径 → 不得报「未安装」 -------

    async def test_run_agy_fnf_other_path_not_binary_missing(self):
        """session_dir / 其它 ENOENT 走通用错误，不误报「助手不可用」。"""
        from wechatbridge import agy as agy_mod
        from wechatbridge.config import config

        async def spawn(*_a, **_k):
            raise FileNotFoundError(2, "No such file or directory", self.session_base)

        with mock.patch.object(config, "agy_binary_path", "/nonexistent/agy"), \
             mock.patch("asyncio.create_subprocess_exec", side_effect=spawn):
            display, artifacts = await agy_mod.run_agy("hi", "u-fnf-cwd")
        self.assertEqual(artifacts, [])
        self.assertIn("执行出错", display)
        self.assertNotIn("助手不可用", display)
        self.assertNotIn(self.session_base, display)

    async def test_run_grok_fnf_other_path_not_binary_missing(self):
        from wechatbridge import grok as grok_mod
        from wechatbridge.config import config

        async def spawn(*_a, **_k):
            raise FileNotFoundError(2, "No such file or directory", self.session_base)

        with mock.patch.object(config, "grok_binary_path", "/nonexistent/grok"), \
             mock.patch("wechatbridge.grok._sync_grok_auth", return_value=True), \
             mock.patch("wechatbridge.grok._grok_has_credentials", return_value=True), \
             mock.patch("asyncio.create_subprocess_exec", side_effect=spawn):
            display, artifacts = await grok_mod.run_grok("hi", "u-fnf-cwd-g")
        self.assertEqual(artifacts, [])
        self.assertIn("执行出错", display)
        self.assertNotIn("助手不可用", display)

    # ---- format_timeout_error / format_cli_error unknown 日志 ------------------

    def test_format_timeout_error_backends(self):
        from wechatbridge.runner_common import format_timeout_error

        for be in ("agy", "grok", "codex"):
            out = format_timeout_error(90, backend=be)
            self.assertTrue(out.startswith("❌"), msg=be)
            self.assertIn("处理超时", out)
            self.assertIn("超过 90 秒", out)
            self.assertIn("/fast", out)
            self.assertIn("/new", out)
        dsh_out = format_timeout_error(90, backend="dsh")
        self.assertIn("处理超时", dsh_out)
        self.assertIn("/new", dsh_out)
        self.assertNotIn("/fast", dsh_out)

    def test_format_cli_error_unknown_logs_detail(self):
        from wechatbridge.runner_common import format_cli_error

        with self.assertLogs("wechatbridge.runner", level="WARNING") as cm:
            out = format_cli_error("totally weird backend gibberish", backend="agy")
        self.assertTrue(out.startswith("❌"))
        self.assertIn("执行失败", out)
        self.assertIn("/new", out)
        self.assertIn("/backend", out)
        self.assertTrue(
            any("unknown detail" in m and "weird backend gibberish" in m for m in cm.output),
            msg=str(cm.output),
        )

    def test_format_cli_error_command_not_found_hint(self):
        from wechatbridge.runner_common import format_cli_error

        out = format_cli_error("command not found", backend="grok")
        self.assertIn("助手不可用", out)
        self.assertIn("/backend", out)

    # ---- /model /add-dir 无参与 clear -----------------------------------------

    async def test_agy_model_no_arg_shows_current(self):
        from wechatbridge import agy as agy_mod
        from wechatbridge.runner_common import save_prefs

        save_prefs("u-m-noparam", {"model": "gemini-x"})
        out = await agy_mod._cmd_model("", "u-m-noparam")
        self.assertIn("当前模型", out)
        self.assertIn("gemini-x", out)
        self.assertIn("/model <", out)

    async def test_grok_model_no_arg_shows_current(self):
        from wechatbridge import grok as grok_mod
        from wechatbridge.runner_common import save_prefs

        save_prefs("u-m-noparam-g", {"model": "grok-9"})
        out = await grok_mod._cmd_model("", "u-m-noparam-g")
        self.assertIn("当前模型", out)
        self.assertIn("grok-9", out)
        self.assertIn("/model <", out)

    def test_agy_add_dir_list_and_clear(self):
        from wechatbridge import agy as agy_mod
        from wechatbridge.runner_common import save_prefs, load_prefs

        out_empty = agy_mod._cmd_add_dir("", "u-add-1")
        self.assertIn("/add-dir <", out_empty)
        save_prefs("u-add-1", {"add_dirs": ["/tmp/a", "/tmp/b"]})
        out_list = agy_mod._cmd_add_dir("", "u-add-1")
        self.assertIn("/tmp/a", out_list)
        self.assertIn("/tmp/b", out_list)
        out_clear = agy_mod._cmd_add_dir("clear", "u-add-1")
        self.assertIn("已清空", out_clear)
        self.assertEqual(load_prefs("u-add-1").get("add_dirs"), [])

    # ---- codex rc=0 结构化错误日志 --------------------------------------------

    async def test_codex_rc0_structured_error_logs_warning(self):
        from wechatbridge import codex as codex_mod
        from wechatbridge.config import config

        err_jsonl = json.dumps(
            {"type": "turn.failed", "error": {"message": "rate limit exceeded"}}
        )

        async def spawn(*_a, **_k):
            return _FakeGrokProc(err_jsonl, "", rc=0)

        with mock.patch.object(config, "codex_binary_path", "codex"), \
             mock.patch("asyncio.create_subprocess_exec", side_effect=spawn), \
             self.assertLogs("codex_runner", level="WARNING") as cm:
            display, _ = await codex_mod.run_codex("hi", "u-rc0-err")
        self.assertTrue(display.startswith("🔔"), msg=display[:80])
        self.assertTrue(
            any("structured error (rc=0)" in m for m in cm.output),
            msg=str(cm.output),
        )

    # ---- 登录二维码清理 --------------------------------------------------------

    async def test_login_flow_removes_qr_files(self):
        from wechatbridge import main as main_mod
        from wechatbridge.config import config

        png = os.path.join(self.session_base, "qrcode.png")
        urlf = os.path.join(self.session_base, ".current_qrcode_url.txt")
        os.makedirs(self.session_base, exist_ok=True)
        for p in (png, urlf):
            with open(p, "w") as f:
                f.write("x")

        client = MagicMock()
        client.get_qrcode = AsyncMock(return_value=("qrstr", ""))
        client.poll_qrcode_status = AsyncMock(return_value=("tok", "https://b"))
        client.state = MagicMock()

        with mock.patch.object(config, "qrcode_png_path", png), \
             mock.patch.object(config, "qrcode_url_path", urlf):
            ok = await main_mod.login_flow(client)
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(png))
        self.assertFalse(os.path.exists(urlf))

    # ---- price 防御解析 --------------------------------------------------------

    def test_price_safe_env_int(self):
        from wechatbridge import price as price_mod

        with mock.patch.dict(os.environ, {"PRICE_HTTP_TIMEOUT": "abc"}, clear=False):
            self.assertEqual(price_mod._safe_env_int("PRICE_HTTP_TIMEOUT", 15, min_val=1), 15)
        with mock.patch.dict(os.environ, {"PRICE_HTTP_TIMEOUT": "0"}, clear=False):
            self.assertEqual(price_mod._safe_env_int("PRICE_HTTP_TIMEOUT", 15, min_val=1), 15)
        with mock.patch.dict(os.environ, {"PRICE_HTTP_TIMEOUT": "8"}, clear=False):
            self.assertEqual(price_mod._safe_env_int("PRICE_HTTP_TIMEOUT", 15, min_val=1), 8)

    def test_price_module_import_with_bad_env(self):
        """非法 PRICE_HTTP_TIMEOUT 不应在 import 时抛 ValueError。"""
        import importlib
        import sys

        saved = sys.modules.pop("wechatbridge.price", None)
        try:
            with mock.patch.dict(os.environ, {"PRICE_HTTP_TIMEOUT": "notanint"}, clear=False):
                mod = importlib.import_module("wechatbridge.price")
            self.assertEqual(mod._DT_TIMEOUT, 15)
        finally:
            sys.modules.pop("wechatbridge.price", None)
            if saved is not None:
                sys.modules["wechatbridge.price"] = saved

    # ---- 长轮询恢复日志 --------------------------------------------------------

    async def test_poll_loop_logs_recovery_after_errors(self):
        from wechatbridge import main as main_mod
        from wechatbridge.config import config

        calls = 0

        class _Client:
            def __init__(self):
                self.state = MagicMock()
                self.state.bot_token = "tok"
                self.state.baseurl = "https://b"
                self.state.load = MagicMock(return_value=True)

            async def get_updates(self, buf, baseurl, bot_token):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("boom")
                if calls == 2:
                    return [], "buf2"
                # 恢复确认后退出主循环（CancelledError 不被内层 except Exception 吞掉）
                raise asyncio.CancelledError()

            async def close(self):
                return None

        client = _Client()

        def _swallow_bg(coro):
            coro.close()  # _spawn_bg 被 mock 掉，直接关闭避免 unawaited 告警

        with mock.patch.object(main_mod, "ILinkClient", return_value=client), \
             mock.patch.object(main_mod, "ensure_runtime_dirs"), \
             mock.patch.object(main_mod, "clean_scratch"), \
             mock.patch.object(main_mod, "_prune_user_locks"), \
             mock.patch.object(main_mod, "_log_access_control_state"), \
             mock.patch.object(main_mod, "_spawn_bg", side_effect=_swallow_bg), \
             mock.patch.object(config, "update_check_enabled", False), \
             mock.patch.object(main_mod.asyncio, "sleep", new=AsyncMock()), \
             self.assertLogs("wechatbridge", level="INFO") as cm:
            with self.assertRaises(asyncio.CancelledError):
                await main_mod.main_loop()

        self.assertTrue(
            any("Poll loop recovered after 1 consecutive error(s)" in m for m in cm.output),
            msg=str(cm.output),
        )

class TestAgyExtractArtifactsFourModes(unittest.TestCase):
    """P0-3a: agy extract_artifacts supports 4 link modes like dsh,
    gated on allowed roots + file existence."""

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="wb-agy-ext-")
        self.session = os.path.join(self.td, "session")
        self.scratch = os.path.join(self.td, "scratch")
        os.makedirs(self.session)
        os.makedirs(self.scratch)
        # Real files under allowed roots
        for rel in (
            os.path.join(self.session, "a.pdf"),
            os.path.join(self.session, "b.docx"),
            os.path.join(self.session, "sub", "c.csv"),
            os.path.join(self.scratch, "d.png"),
        ):
            os.makedirs(os.path.dirname(rel), exist_ok=True)
            with open(rel, "w") as f:
                f.write("x")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def _extract(self, text):
        from wechatbridge.agy import extract_artifacts
        return extract_artifacts(
            text, session_dir=self.session,
            allowed_roots=[self.scratch],
        )

    def test_all_four_modes(self):
        text = (
            f"[report](file:///{self.session}/a.pdf) "
            f"[doc]({self.session}/b.docx) "
            f"[csv](./sub/c.csv) "
            f"and bare file:///{self.scratch}/d.png done"
        )
        arts = self._extract(text)
        paths = {p for _, p in arts}
        self.assertIn(os.path.join(self.session, "a.pdf"), paths)
        self.assertIn(os.path.join(self.session, "b.docx"), paths)
        self.assertIn(os.path.join(self.session, "sub", "c.csv"), paths)
        self.assertIn(os.path.join(self.scratch, "d.png"), paths)

    def test_outside_allowed_root_skipped(self):
        outside = os.path.join(self.td, "outside")
        os.makedirs(outside)
        ext = os.path.join(outside, "e.pdf")
        with open(ext, "w") as f:
            f.write("x")
        arts = self._extract(f"[e](file:///{ext})")
        self.assertEqual(arts, [])

    def test_nonexistent_file_skipped(self):
        arts = self._extract(f"[gone](file:///{self.session}/gone.pdf)")
        self.assertEqual(arts, [])


class TestSessionFallbackArtifacts(unittest.TestCase):
    """P0-3b: session_dir snapshot fallback for agy/dsh/codex."""

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="wb-fb-")
        self.session = os.path.join(self.td, "session")
        self.scratch = os.path.join(self.td, "scratch")
        os.makedirs(self.session)
        os.makedirs(self.scratch)

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def _snapshot(self):
        from wechatbridge.runner_common import _snapshot_regular_files
        return _snapshot_regular_files([self.session])

    def _collect(self, before, t0):
        from wechatbridge.runner_common import (
            ARTIFACT_FALLBACK_EXTENSIONS,
            collect_session_fallback_artifacts,
        )
        return collect_session_fallback_artifacts(
            self.session, before, t0,
            ext_whitelist=ARTIFACT_FALLBACK_EXTENSIONS,
        )

    def test_new_pdf_collected(self):
        t0 = time.time()
        before = self._snapshot()
        pdf = os.path.join(self.session, "out.pdf")
        with open(pdf, "wb") as f:
            f.write(b"%PDF")
        arts = self._collect(before, t0)
        paths = {p for _, p in arts}
        self.assertIn(os.path.realpath(pdf), paths)

    def test_mtime_bump_collected(self):
        old = os.path.join(self.session, "old.docx")
        with open(old, "wb") as f:
            f.write(b"x")
        os.utime(old, (1000.0, 1000.0))
        t0 = time.time()
        before = self._snapshot()
        os.utime(old, None)
        arts = self._collect(before, t0)
        self.assertIn(os.path.realpath(old), {p for _, p in arts})

    def test_sensitive_excluded(self):
        t0 = time.time()
        before = self._snapshot()
        for name in ("prefs.json", ".initialized.agy", ".codex_thread_id"):
            with open(os.path.join(self.session, name), "w") as f:
                f.write("x")
        arts = self._collect(before, t0)
        self.assertEqual(arts, [])

    def test_whitelist_extension_only(self):
        t0 = time.time()
        before = self._snapshot()
        with open(os.path.join(self.session, "script.py"), "w") as f:
            f.write("x")
        arts = self._collect(before, t0)
        self.assertEqual(arts, [])

    def test_hardlink_rejected(self):
        ext = os.path.join(self.td, "external.txt")
        with open(ext, "w") as f:
            f.write("secret")
        t0 = time.time()
        before = self._snapshot()
        hl = os.path.join(self.session, "linked.txt")
        os.link(ext, hl)
        arts = self._collect(before, t0)
        self.assertNotIn(os.path.realpath(hl), {p for _, p in arts})

    def test_symlink_escape_rejected(self):
        ext = os.path.join(self.td, "secret.pdf")
        with open(ext, "w") as f:
            f.write("x")
        t0 = time.time()
        before = self._snapshot()
        link = os.path.join(self.session, "escape.pdf")
        os.symlink(ext, link)
        arts = self._collect(before, t0)
        self.assertEqual(arts, [])

    def test_shared_scratch_never_scanned(self):
        """红线: snapshot roots only ever contain session_dir, never the shared
        config.agy_scratch_dir — files created there must NOT appear."""
        t0 = time.time()
        before = self._snapshot()
        with open(os.path.join(self.scratch, "shared.pdf"), "w") as f:
            f.write("x")
        arts = self._collect(before, t0)
        self.assertEqual(arts, [])


class TestSendArtifactsBackZeroAndCap(unittest.IsolatedAsyncioTestCase):
    """P0-3c 0-byte skip + P1-3b outbound cap."""

    async def test_zero_byte_skipped_no_notice(self):
        from wechatbridge.main import send_artifacts_back
        with tempfile.TemporaryDirectory() as td:
            empty = os.path.join(td, "empty.pdf")
            with open(empty, "wb") as f:
                pass
            ok_file = os.path.join(td, "ok.pdf")
            with open(ok_file, "wb") as f:
                f.write(b"x")
            client = MagicMock()
            client.state.baseurl = "https://x"
            client.state.bot_token = "t"
            client.send_message = AsyncMock(return_value=True)
            client.send_media = AsyncMock(return_value=True)
            with mock.patch(
                "wechatbridge.main.get_session_dir", return_value=td
            ), mock.patch(
                "wechatbridge.main._get_backend", return_value="agy"
            ):
                await send_artifacts_back(
                    client, "u1", "ctx", [("empty.pdf", empty), ("ok.pdf", ok_file)]
                )
            sent = {c.kwargs["path"] for c in client.send_media.await_args_list}
            self.assertIn(os.path.realpath(ok_file), sent)
            self.assertNotIn(empty, sent)

    async def test_cap_ten_and_notice(self):
        from wechatbridge.main import send_artifacts_back, MAX_OUTBOUND_ARTIFACTS_PER_TURN
        with tempfile.TemporaryDirectory() as td:
            arts = []
            for i in range(15):
                p = os.path.join(td, f"f{i}.pdf")
                with open(p, "wb") as f:
                    f.write(b"x")
                arts.append((f"f{i}.pdf", p))
            client = MagicMock()
            client.state.baseurl = "https://x"
            client.state.bot_token = "t"
            client.send_message = AsyncMock(return_value=True)
            client.send_media = AsyncMock(return_value=True)
            with mock.patch(
                "wechatbridge.main.get_session_dir", return_value=td
            ), mock.patch(
                "wechatbridge.main._get_backend", return_value="agy"
            ):
                await send_artifacts_back(client, "u1", "ctx", arts)
            self.assertEqual(
                len(client.send_media.await_args_list),
                MAX_OUTBOUND_ARTIFACTS_PER_TURN,
            )
            texts = [c.kwargs["text"] for c in client.send_message.await_args_list]
            self.assertTrue(any("共生成 15 个文件" in t for t in texts))


class TestDisplayFilenameAndNotices(unittest.TestCase):
    def test_sanitize_display_filename(self):
        from wechatbridge.main import _sanitize_display_filename
        self.assertEqual(_sanitize_display_filename("a@b\nc.pdf"), "abc.pdf")
        self.assertEqual(len(_sanitize_display_filename("x" * 200 + ".pdf")), 80)
        self.assertEqual(_sanitize_display_filename("normal.pdf"), "normal.pdf")


class TestAgyDisplayStripsFilePaths(unittest.IsolatedAsyncioTestCase):
    """S1: agy 显示文本不得泄露服务器路径——裸 file:///、[x](file:///abs)、
    [x](/abs)、[x](./rel) 与 dsh 同规则剥除，仅保留链接文字。"""

    def setUp(self):
        from wechatbridge.config import config
        from wechatbridge.runner_common import get_session_dir
        self._cfg = config
        self.td = tempfile.mkdtemp(prefix="wb-agy-disp-")
        self.scratch = os.path.join(self.td, "scratch")
        os.makedirs(self.scratch)
        self._patchers = [
            mock.patch.object(config, "session_base_dir", self.td),
            mock.patch.object(config, "agy_scratch_dir", self.scratch),
        ]
        for p in self._patchers:
            p.start()
        # 真实 per-user session_dir（ensure_user_gemini 会按需创建）
        self.session = get_session_dir("u-agydisp")
        os.makedirs(self.session)

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        shutil.rmtree(self.td, ignore_errors=True)

    def _mkfile(self, root, name):
        path = os.path.join(root, name)
        with open(path, "w") as f:
            f.write("x")
        return path

    async def _run_with_stdout(self, stdout_text):
        from wechatbridge import agy as agy_mod
        from wechatbridge.config import config

        async def spawn(*_a, **_k):
            return _FakeAgyProc(stdout_text, "", rc=0)

        with mock.patch.object(config, "agy_binary_path", "agy"), \
             mock.patch("asyncio.create_subprocess_exec", side_effect=spawn), \
             mock.patch.object(agy_mod, "terminate_process", new=AsyncMock()):
            return await agy_mod.run_agy("hi", "u-agydisp")

    async def test_bare_file_uri_stripped_from_display(self):
        f = self._mkfile(self.session, "x.pdf")
        display, arts = await self._run_with_stdout(
            f"已生成 file://{f} 请查收"
        )
        self.assertNotIn(self.td, display)
        self.assertNotIn("file://", display)
        self.assertNotIn(f, display)
        # 产物仍正常提取
        self.assertEqual([n for n, _ in arts], ["x.pdf"])

    async def test_markdown_file_uri_link_keeps_name_only(self):
        f = self._mkfile(self.session, "report.pdf")
        display, _ = await self._run_with_stdout(
            f"see [report.pdf](file://{f}) done"
        )
        self.assertEqual(display, "see [report.pdf] done")
        self.assertNotIn(self.td, display)

    async def test_markdown_abs_link_keeps_name_only(self):
        f = self._mkfile(self.session, "b.docx")
        display, _ = await self._run_with_stdout(f"see [doc]({f}) here")
        self.assertEqual(display, "see [doc] here")
        self.assertNotIn(self.td, display)

    async def test_markdown_rel_link_keeps_name_only(self):
        os.makedirs(os.path.join(self.session, "sub"), exist_ok=True)
        f = self._mkfile(os.path.join(self.session, "sub"), "c.csv")
        display, _ = await self._run_with_stdout("see [csv](./sub/c.csv) here")
        self.assertEqual(display, "see [csv] here")
        self.assertNotIn(self.td, display)

    async def test_fullwidth_paren_wrapped_bare_uri(self):
        """回归：全角括号（file:///abs）中的裸 URI——）必须被当作尾随
        标点剥离 URI 本体（产物可提取），且展示文本不得吞掉 ）。"""
        f = self._mkfile(self.session, "report.pdf")
        display, arts = await self._run_with_stdout(
            f"请查收（file://{f}）"
        )
        self.assertNotIn("file://", display)
        self.assertNotIn(self.td, display)
        # ） 保留：URI 剥除后留下空的全角括号
        self.assertEqual(display, "请查收（）")
        # 产物正常提取（）不再粘连在 URI 尾部）
        self.assertEqual([n for n, _ in arts], ["report.pdf"])


class TestAgyImportWithoutYaml(unittest.TestCase):
    """S2: 默认 agy 后端不得因 import dsh 而依赖顶层 yaml。"""

    def test_agy_imports_without_yaml(self):
        import importlib
        import sys

        orig_yaml = sys.modules.get("yaml")
        orig_dsh = sys.modules.get("wechatbridge.dsh")
        orig_agy = sys.modules.get("wechatbridge.agy")
        sys.modules["yaml"] = None
        sys.modules.pop("wechatbridge.dsh", None)
        sys.modules.pop("wechatbridge.agy", None)
        try:
            mod = importlib.import_module("wechatbridge.agy")
            self.assertTrue(hasattr(mod, "run_agy"))
            self.assertNotIn("wechatbridge.dsh", sys.modules)
        finally:
            if orig_yaml is not None:
                sys.modules["yaml"] = orig_yaml
            else:
                sys.modules.pop("yaml", None)
            if orig_dsh is not None:
                sys.modules["wechatbridge.dsh"] = orig_dsh
            else:
                sys.modules.pop("wechatbridge.dsh", None)
            sys.modules["wechatbridge.agy"] = mod
            if orig_agy is not None:
                sys.modules["wechatbridge.agy"] = orig_agy


class TestSnapshotRealpathKeys(unittest.TestCase):
    """S3: session_dir 经软链接父目录访问时，快照前后键一致（realpath）。"""

    def test_symlinked_session_dir_keys_match(self):
        from wechatbridge.runner_common import (
            ARTIFACT_FALLBACK_EXTENSIONS,
            _snapshot_regular_files,
            collect_session_fallback_artifacts,
        )

        td = tempfile.mkdtemp(prefix="wb-snap-")
        self.addCleanup(lambda: shutil.rmtree(td, ignore_errors=True))
        real_dir = os.path.join(td, "real_session")
        os.makedirs(real_dir)
        link_path = os.path.join(td, "link_session")
        os.symlink(real_dir, link_path)

        # 预先存在、mtime 在 t0-2s 内的文件：不得当作新产物
        old_file = os.path.join(real_dir, "old.pdf")
        with open(old_file, "wb") as f:
            f.write(b"x")
        os.utime(old_file, (1000000.0, 1000000.0))

        t0 = time.time()
        before = _snapshot_regular_files([link_path])
        # 快照键必须是 realpath 形式
        self.assertEqual(set(before), {os.path.realpath(old_file)})

        # 本轮新建文件仍被回收
        new_file = os.path.join(real_dir, "new.pdf")
        with open(new_file, "wb") as f:
            f.write(b"x")
        arts = collect_session_fallback_artifacts(
            link_path, before, t0, ext_whitelist=ARTIFACT_FALLBACK_EXTENSIONS
        )
        paths = {p for _, p in arts}
        self.assertIn(os.path.realpath(new_file), paths)
        self.assertNotIn(os.path.realpath(old_file), paths)



class TestProcessMessageMediaNotices(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from wechatbridge import main as main_mod
        from wechatbridge.config import config
        self.main = main_mod
        self.td = tempfile.mkdtemp(prefix="wb-pm-")
        self._patchers = [
            mock.patch.object(config, "session_base_dir", self.td),
            mock.patch.object(config, "allowed_senders", ["*"]),
            mock.patch.object(config, "admin_users", []),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        shutil.rmtree(self.td, ignore_errors=True)

    def _client(self):
        client = MagicMock()
        client.state = MagicMock()
        client.state.baseurl = "https://x"
        client.state.bot_token = "t"
        client.send_message = AsyncMock(return_value=True)
        return client

    async def test_unsupported_type_notice(self):
        client = self._client()
        msg = {
            "from_user_id": "u1",
            "context_token": "ctx",
            "item_list": [{"type": 5, "video_item": {}}],
        }
        await self.main.process_message(client, msg)
        texts = [c.kwargs["text"] for c in client.send_message.await_args_list]
        self.assertTrue(any("暂不支持该消息类型" in t for t in texts))

    async def test_empty_item_list_silent(self):
        client = self._client()
        msg = {"from_user_id": "u1", "context_token": "ctx", "item_list": []}
        await self.main.process_message(client, msg)
        texts = [c.kwargs["text"] for c in client.send_message.await_args_list]
        self.assertFalse(any("暂不支持该消息类型" in t for t in texts))

    async def test_multi_media_notice(self):
        client = self._client()
        media = {"encrypt_query_param": "q", "aes_key": "AAAA"}
        msg = {
            "from_user_id": "u1",
            "context_token": "ctx",
            "item_list": [
                {"type": 2, "image_item": {"media": media}},
                {"type": 4, "file_item": {"media": media, "file_name": "f.pdf"}},
            ],
        }
        with mock.patch.object(
            self.main, "gate_and_run", new=AsyncMock(return_value=("ok", []))
        ), mock.patch.object(
            client, "download_and_decrypt_media", new=AsyncMock(return_value=b"\x89PNG\x00")
        ):
            await self.main.process_message(client, msg)
        texts = [c.kwargs["text"] for c in client.send_message.await_args_list]
        hit = [t for t in texts if "仅处理首个附件" in t]
        self.assertTrue(hit)
        # 提示与正文之间须有空行（与 cancelled_notice 一致）
        self.assertIn("请分条发送。\n\nok", hit[0])

    async def test_file_message_carries_orig_filename(self):
        client = self._client()
        media = {"encrypt_query_param": "q", "aes_key": "AAAA"}
        msg = {
            "from_user_id": "u1",
            "context_token": "ctx",
            "item_list": [
                {"type": 4, "file_item": {"media": media, "file_name": "报告 (终版).pdf"}},
            ],
        }
        captured = []
        async def _gate(client, from_user, ctx, prompt, **kw):
            captured.append(prompt)
            return "done", []
        with mock.patch.object(
            self.main, "gate_and_run", new=AsyncMock(side_effect=_gate)
        ), mock.patch.object(
            client, "download_and_decrypt_media", new=AsyncMock(return_value=b"%PDF")
        ):
            await self.main.process_message(client, msg)
        self.assertEqual(len(captured), 1)
        self.assertIn("请分析这个文件 @", captured[0])
        self.assertIn("原文件名：报告 (终版).pdf", captured[0])

    async def test_file_message_with_caption_uses_bracket(self):
        client = self._client()
        media = {"encrypt_query_param": "q", "aes_key": "AAAA"}
        msg = {
            "from_user_id": "u1",
            "context_token": "ctx",
            "item_list": [
                {"type": 1, "text_item": {"text": "帮我看看"}},
                {"type": 4, "file_item": {"media": media, "file_name": "data.xlsx"}},
            ],
        }
        captured = []
        async def _gate(client, from_user, ctx, prompt, **kw):
            captured.append(prompt)
            return "done", []
        with mock.patch.object(
            self.main, "gate_and_run", new=AsyncMock(side_effect=_gate)
        ), mock.patch.object(
            client, "download_and_decrypt_media", new=AsyncMock(return_value=b"PK")
        ):
            await self.main.process_message(client, msg)
        self.assertEqual(len(captured), 1)
        self.assertIn("[附件（原文件名：data.xlsx）: @", captured[0])


if __name__ == "__main__":
    unittest.main()
