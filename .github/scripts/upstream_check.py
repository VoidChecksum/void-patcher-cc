#!/usr/bin/env python3
"""
upstream_check.py — CI validator run by .github/workflows/upstream-watch.yml.

Procedure:
  1. Locate globally-installed @anthropic-ai/claude-code (already installed by workflow).
  2. Run `vpcc patch --dry-run` to see which patches would apply cleanly.
  3. Run `vpcc verify` after a real patch to confirm markers landed.
  4. Emit a markdown report + GITHUB_OUTPUT flags so the workflow can open an issue.

Exit is always 0 — breakage signaled via GITHUB_OUTPUT `broken` count.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
from pathlib import Path

CC_VERSION = os.environ.get("CC_VERSION", "unknown")
REPORT     = Path("upstream-report.md")
GHOUT      = os.environ.get("GITHUB_OUTPUT")


def sh(cmd: list[str]) -> tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def parse_dry_run(out: str) -> list[dict]:
    """
    Extract per-patch status lines from `vpcc patch --dry-run` output.
    ANSI-stripped. Lines look like:
        ok    01-bypass-permissions                    would apply 2 in-place
        fail  14-js-classifier-failopen                regex error: ...
        skip  07-mcp-guard                             type=mcp_guard ...
    """
    ansi = re.compile(r"\x1b\[[0-9;]*m")
    rows: list[dict] = []
    for raw in out.splitlines():
        line = ansi.sub("", raw).rstrip()
        m = re.match(r"^\s*(ok|fail|skip)\s+(\S+)\s+(.*)$", line)
        if m:
            rows.append({"status": m.group(1), "id": m.group(2), "msg": m.group(3)})
    return rows


def main() -> int:
    rc_status, out_status = sh(["vpcc", "status"])
    rc_dry, out_dry       = sh(["vpcc", "patch", "--dry-run"])
    rows                  = parse_dry_run(out_dry)

    failed = [r for r in rows if r["status"] == "fail"]
    ok     = [r for r in rows if r["status"] == "ok"]
    noop   = [r for r in rows if r["status"] == "ok" and "no-op" in r["msg"]]
    missing_regex = [r for r in rows if r["status"] == "ok"
                     and "would apply 0" in r["msg"]]

    # A patch counts as broken when its dry-run says it failed OR when it ran
    # fine but matched zero locations (upstream changed the signature).
    broken = failed + missing_regex

    rc_real, out_real = sh(["vpcc", "patch"])
    rc_verify, out_verify = sh(["vpcc", "verify"])

    lines = [
        f"# upstream-watch report",
        f"",
        f"- **Claude Code version**: `{CC_VERSION}`",
        f"- **vpcc patches total**: {len(rows)}",
        f"- **clean applies**: {len(ok) - len(broken)}",
        f"- **broken signatures**: {len(broken)}",
        f"",
    ]
    if broken:
        lines += ["## Broken patches", ""]
        for b in broken:
            lines.append(f"- `{b['id']}` — {b['msg']}")
        lines += ["", "### Action", "",
                  "Update the regex/offsets in `patches/<id>.json` for the "
                  "affected patches and push a new release.", ""]
    lines += [
        "## `vpcc status`",
        "```", out_status.strip(), "```",
        "## `vpcc patch --dry-run`",
        "```", out_dry.strip()[:4000], "```",
        "## `vpcc verify`",
        f"`exit={rc_verify}`",
        "```", out_verify.strip()[:2000], "```",
    ]
    REPORT.write_text("\n".join(lines))

    if GHOUT:
        with open(GHOUT, "a") as f:
            f.write(f"broken={len(broken)}\n")
            f.write(f"total={len(rows)}\n")
    print(f"broken={len(broken)} total={len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
