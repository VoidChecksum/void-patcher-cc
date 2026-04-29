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


def load_patch_types(patch_dir: Path = Path("patches")) -> dict[str, str]:
    """Map patch id to patch type so meta-patch failures are not called regex drift."""
    out: dict[str, str] = {}
    for f in patch_dir.glob("*.json"):
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        pid = obj.get("id")
        if isinstance(pid, str):
            out[pid] = str(obj.get("type") or "")
    return out


def classify_rows(rows: list[dict], patch_types: dict[str, str] | None = None) -> dict[str, list | int]:
    """Classify dry-run failures into JS signature breakage vs meta-patch failures."""
    patch_types = patch_types or {}
    broken_signatures: list[dict] = []
    meta_failures: list[dict] = []
    for r in rows:
        ptype = patch_types.get(r["id"], "")
        is_js_patch = ptype in ("", "js_replace")
        if r["status"] == "fail":
            (broken_signatures if is_js_patch else meta_failures).append(r)
        elif r["status"] == "ok" and is_js_patch and "would apply 0" in r["msg"]:
            broken_signatures.append(r)
    return {
        "broken_signatures": broken_signatures,
        "meta_failures": meta_failures,
        "needs_issue": len(broken_signatures) + len(meta_failures),
    }


def main() -> int:
    rc_status, out_status = sh(["vpcc", "status"])
    rc_dry, out_dry       = sh(["vpcc", "patch", "--dry-run"])
    rows                  = parse_dry_run(out_dry)

    ok     = [r for r in rows if r["status"] == "ok"]
    patch_types = load_patch_types()
    classified = classify_rows(rows, patch_types)
    broken = classified["broken_signatures"]
    meta_failed = classified["meta_failures"]
    clean_ok = [r for r in ok if r not in broken]

    rc_real, out_real = sh(["vpcc", "patch"])
    rc_verify, out_verify = sh(["vpcc", "verify"])

    lines = [
        f"# upstream-watch report",
        f"",
        f"- **Claude Code version**: `{CC_VERSION}`",
        f"- **vpcc patches total**: {len(rows)}",
        f"- **clean applies**: {len(clean_ok)}",
        f"- **broken signatures**: {len(broken)}",
    ]
    if meta_failed:
        lines.append(f"- **meta patch failures**: {len(meta_failed)}")
    lines.append("")
    if broken:
        lines += ["## Broken signature patches", ""]
        for b in broken:
            lines.append(f"- `{b['id']}` — {b['msg']}")
        lines += ["", "### Signature action", "",
                  "Update the regex/offsets in `patches/<id>.json` for the "
                  "affected patches and push a new release.", ""]
    if meta_failed:
        lines += ["## Meta patch failures", ""]
        for b in meta_failed:
            lines.append(f"- `{b['id']}` — {b['msg']}")
        lines += ["", "### Meta action", "",
                  "Investigate the patch implementation or install/runtime "
                  "environment; this is not regex signature drift.", ""]
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

    needs_issue = classified["needs_issue"]
    if GHOUT:
        with open(GHOUT, "a") as f:
            f.write(f"broken={needs_issue}\n")
            f.write(f"broken_signatures={len(broken)}\n")
            f.write(f"meta_failed={len(meta_failed)}\n")
            f.write(f"total={len(rows)}\n")
    print(f"broken={needs_issue} signatures={len(broken)} meta={len(meta_failed)} total={len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
