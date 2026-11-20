#!/usr/bin/env python3
"""Fake `pig` CLI for unit tests of wechatbridge.pig.

Emits ``--mode json`` JSONL events on stdout. Behaviour is controlled by the
FAKE_PIG_MODE environment variable (read each invocation):

  ok                  (default) one assistant message_end, stopReason="stop".
  error_rc0           rc=0 but last assistant stopReason="error" with
                      errorMessage from FAKE_PIG_ERROR_MESSAGE.
  aborted_rc0         rc=0 but last assistant stopReason="aborted".
  length_rc0          rc=0, stopReason="length", partial text kept.
  rate_limit_rc0      rc=0, stopReason="error", errorMessage "429 Too Many Requests".
  auth_401_rc0        rc=0, stopReason="error", errorMessage "401 Unauthorized".
  timeout_504_rc0     rc=0, stopReason="error", errorMessage
                      "The backend sent nothing for 570000 ms" (HTTP 504).
  session_not_found   stderr "pig: no session found", rc=1 (only when
                      --continue was passed; otherwise behaves like ok).
  fail_rc1            stderr "boom", rc=1.
  empty               no stdout, rc=0.
  argv_dump           writes received argv/env facts to FAKE_PIG_DUMP (JSON),
                      then behaves like ok.
  sleep               sleeps far longer than the test timeout.
  tool_write          emits tool_execution_start/end (write, path from
                      FAKE_PIG_WRITE_PATH, default ./out.txt), creates the
                      file, then ok.
  retry_fail          auto_retry_end success=false finalError, then
                      stopReason="error" assistant, rc=0.
  key_echo            assistant text contains the literal $MAGPIE_API_KEY
                      value (tests display redaction).
  corrupt_first_empty FAKE_PIG_LOG argv contains --continue → spawn a
                      detached sleep child in the same process group,
                      write its pid to FAKE_PIG_BGPID, emit session
                      corruption on stderr, rc=1; WITHOUT --continue
                      emit empty stdout rc=0 (self-heal EMPTY_REPLY).
  bg_child_first      --continue → spawn detached sleep child (pid →
                      FAKE_PIG_BGPID), stderr session-corruption, rc=1;
                      else behave like ok (retry succeeds, both groups
                      must be reaped).

If FAKE_PIG_LOG is set (path), each invocation appends a JSON line with the
parsed argv so tests can assert exact flags/positional args.

FAKE_PIG_BGPID: when set and the mode spawns a background child, the child
pid is written here so the test can assert the process group was reaped.
"""

import json
import os
import subprocess
import sys
import time


def _assistant_msg(text, stop="stop", error=""):
    msg = {
        "role": "assistant",
        "stopReason": stop,
        "content": [{"type": "text", "text": text}],
    }
    if error:
        msg["errorMessage"] = error
    return msg


def _emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _parse_args(argv):
    flags = {}
    positionals = []
    file_args = []
    i = 0
    seen_dashdash = False
    while i < len(argv):
        a = argv[i]
        if seen_dashdash:
            if a.startswith("@"):
                file_args.append(a)
            else:
                positionals.append(a)
            i += 1
            continue
        if a == "--":
            seen_dashdash = True
            i += 1
            continue
        if a.startswith("--"):
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                flags[a] = argv[i + 1]
                i += 2
            else:
                flags[a] = True
                i += 1
            continue
        if a.startswith("@"):
            file_args.append(a)
        else:
            positionals.append(a)
        i += 1
    return flags, file_args, positionals


def main():
    argv = sys.argv[1:]
    mode = os.environ.get("FAKE_PIG_MODE", "ok")
    flags, file_args, positionals = _parse_args(argv)
    prompt = positionals[-1] if positionals else ""

    log_path = os.environ.get("FAKE_PIG_LOG")
    if log_path:
        try:
            record = {
                "mode": mode,
                "argv": argv,
                "flags": flags,
                "file_args": file_args,
                "positionals": positionals,
            }
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    dump_path = os.environ.get("FAKE_PIG_DUMP")
    if dump_path:
        try:
            with open(dump_path, "w", encoding="utf-8") as df:
                json.dump(
                    {
                        "argv": argv,
                        "flags": flags,
                        "file_args": file_args,
                        "env": {
                            "PIG_HOME": os.environ.get("PIG_HOME"),
                            "PIG_CODING_AGENT_DIR": os.environ.get("PIG_CODING_AGENT_DIR"),
                            "PI_CODING_AGENT_DIR": os.environ.get("PI_CODING_AGENT_DIR"),
                            "PIG_USE_PI_DIRS": os.environ.get("PIG_USE_PI_DIRS"),
                            "MAGPIE_API_KEY": bool(os.environ.get("MAGPIE_API_KEY")),
                            "PI_OFFLINE": os.environ.get("PI_OFFLINE"),
                            "PI_SKIP_VERSION_CHECK": os.environ.get("PI_SKIP_VERSION_CHECK"),
                            "GOMEMLIMIT": os.environ.get("GOMEMLIMIT"),
                            "HOME": os.environ.get("HOME"),
                        },
                    },
                    df,
                )
        except OSError:
            pass

    if mode == "sleep":
        time.sleep(600)
        return 0

    if mode in ("corrupt_first_empty", "bg_child_first") and "--continue" in argv:
        # Simulate a corrupt --continue turn that spawned a detached
        # background child in the SAME process group before dying. The
        # child shares this fake's pgid (no setsid), so reaping the
        # first-attempt group must kill it. Its pid is recorded so the
        # test can verify the group was reaped.
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        bgpid_path = os.environ.get("FAKE_PIG_BGPID")
        if bgpid_path:
            try:
                with open(bgpid_path, "w") as bf:
                    bf.write(str(child.pid))
            except OSError:
                pass
        sys.stderr.write("pig: no session found for cwd\n")
        return 1

    if mode == "corrupt_first_empty":
        # First attempt (no --continue): the self-heal retry produced an
        # EMPTY_REPLY — no assistant events at all, rc=0.
        return 0

    if mode == "empty":
        return 0

    if mode == "fail_rc1":
        sys.stderr.write("boom: something went wrong\n")
        return 1

    if mode == "session_not_found" and "--continue" in argv:
        sys.stderr.write("pig: no session found for cwd\n")
        return 1

    if mode == "tool_write":
        path = os.environ.get("FAKE_PIG_WRITE_PATH") or "./out.txt"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("generated by fake pig")
        except OSError:
            pass
        _emit({
            "type": "tool_execution_start",
            "toolCallId": "call-1",
            "toolName": "write",
            "args": {"path": path},
        })
        _emit({
            "type": "tool_execution_end",
            "toolCallId": "call-1",
            "isError": False,
        })

    if mode == "retry_fail":
        _emit({
            "type": "auto_retry_end",
            "success": False,
            "finalError": "all retries exhausted: 502 Bad Gateway",
        })

    if mode == "key_echo":
        key = os.environ.get("MAGPIE_API_KEY", "")
        _emit({
            "type": "message_end",
            "message": _assistant_msg(f"leak: {key}"),
        })
        return 0

    if mode == "error_rc0":
        err = os.environ.get("FAKE_PIG_ERROR_MESSAGE", "upstream blew up")
        _emit({
            "type": "message_end",
            "message": _assistant_msg("", "error", err),
        })
        return 0

    if mode == "aborted_rc0":
        _emit({
            "type": "message_end",
            "message": _assistant_msg("partial", "aborted", "aborted by user"),
        })
        return 0

    if mode == "length_rc0":
        _emit({
            "type": "message_end",
            "message": _assistant_msg("前半段输出内容", "length"),
        })
        return 0

    if mode == "rate_limit_rc0":
        _emit({
            "type": "message_end",
            "message": _assistant_msg("", "error", "HTTP 429 Too Many Requests"),
        })
        return 0

    if mode == "auth_401_rc0":
        _emit({
            "type": "message_end",
            "message": _assistant_msg("", "error", "401 Unauthorized"),
        })
        return 0

    if mode == "timeout_504_rc0":
        _emit({
            "type": "message_end",
            "message": _assistant_msg(
                "", "error", "The backend sent nothing for 570000 ms"
            ),
        })
        return 0

    # default ok
    _emit({
        "type": "message_end",
        "message": _assistant_msg(f"first({prompt})"),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
