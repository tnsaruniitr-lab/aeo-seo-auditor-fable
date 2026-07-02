#!/usr/bin/env python3
"""
_timeout.py — minimal portable timeout wrapper.

GNU `timeout` and `gtimeout` are not installed by default on macOS, which
made the unified orchestrator mark every child as `unparseable_output` with
exit 127 ("timeout: command not found"). This wrapper mimics the subset of
GNU timeout's interface the orchestrator relies on:

    python3 _timeout.py SECONDS CMD [ARG ...]

Semantics matched against GNU coreutils `timeout`:
  * Runs CMD with the given args.
  * Sends SIGTERM if CMD doesn't exit within SECONDS, then SIGKILL after a
    short grace period.
  * Exits 124 on timeout (same as GNU timeout).
  * Exits 127 if CMD cannot be found, 126 on other exec errors.
  * Otherwise propagates the child's exit code.

Dependencies: python3 stdlib only.
"""

import os
import signal
import subprocess
import sys


def main() -> int:
    if len(sys.argv) < 3:
        sys.stderr.write('usage: _timeout.py SECONDS CMD [ARG ...]\n')
        return 2
    try:
        duration = float(sys.argv[1])
    except ValueError:
        sys.stderr.write(f'_timeout.py: invalid duration: {sys.argv[1]}\n')
        return 2
    cmd = sys.argv[2:]

    # Run in its own process group so we can signal the whole subtree
    # (e.g. python3 → curl child) rather than just the direct child.
    try:
        proc = subprocess.Popen(cmd, start_new_session=True)
    except FileNotFoundError as e:
        sys.stderr.write(f'_timeout.py: {e}\n')
        return 127
    except OSError as e:
        sys.stderr.write(f'_timeout.py: {e}\n')
        return 126

    try:
        return proc.wait(timeout=duration)
    except subprocess.TimeoutExpired:
        pass

    # Soft kill the group, then escalate.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            break
        try:
            proc.wait(timeout=2)
            break
        except subprocess.TimeoutExpired:
            continue
    return 124


if __name__ == '__main__':
    sys.exit(main())
