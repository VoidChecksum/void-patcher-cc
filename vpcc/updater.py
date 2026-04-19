"""
vpcc.updater — self-update + autoheal logic.

Two flows:
  1. self_update()  — pull latest patches/ from GitHub, overwrite local PATCH_DIR.
  2. autoheal()     — detect Claude Code sha drift, re-verify, self-update + re-patch if broken.

Stdlib only. No git/pip required at runtime.
"""
from __future__ import annotations
import io
import json
import os
import shutil
import ssl
import tarfile
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO       = "VoidChecksum/void-patcher-cc"
BRANCH     = "main"
API_BASE   = f"https://api.github.com/repos/{REPO}"
UA         = "vpcc-updater/1.0"
STATE_DIR  = Path.home() / ".vpcc"
STATE_FILE = STATE_DIR / "state.json"


# ── HTTP ─────────────────────────────────────────────────────────────────────

def _req(url: str, accept: str = "application/json", timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


def remote_head_sha(path: str = "patches") -> str | None:
    """Latest commit sha touching `path` on BRANCH."""
    try:
        data = json.loads(_req(f"{API_BASE}/commits?sha={BRANCH}&path={path}&per_page=1"))
        return data[0]["sha"] if data else None
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, IndexError):
        return None


def download_tarball(sha: str = BRANCH) -> bytes:
    return _req(f"{API_BASE}/tarball/{sha}", accept="application/octet-stream", timeout=60)


# ── state ────────────────────────────────────────────────────────────────────

def load_state() -> dict[str, Any]:
    if not STATE_FILE.is_file():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        return {}


def save_state(**kwargs) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    s = load_state()
    s.update(kwargs)
    s["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(s, indent=2))


# ── patch dir sync ───────────────────────────────────────────────────────────

def sync_patches(patch_dir: Path, sha: str | None = None) -> tuple[int, str]:
    """
    Download tarball, extract patches/*.json into patch_dir.
    Returns (changed_file_count, commit_sha).
    """
    target_sha = sha or remote_head_sha() or BRANCH
    try:
        tar_bytes = download_tarball(target_sha)
    except urllib.error.URLError as e:
        return -1, f"download failed: {e}"

    try:
        tf = tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz")
    except tarfile.TarError as e:
        return -1, f"tar open failed: {e}"

    staged: dict[str, bytes] = {}
    with tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            parts = Path(m.name).parts
            if len(parts) < 3 or parts[1] != "patches" or not parts[2].endswith(".json"):
                continue
            f = tf.extractfile(m)
            if f is None:
                continue
            staged[parts[2]] = f.read()

    if not staged:
        return -1, "tarball contained no patches/"

    # Validate every staged file is parseable JSON with the expected shape
    # BEFORE touching patch_dir. Broken tarball must never overwrite live patches.
    for name, content in staged.items():
        try:
            obj = json.loads(content)
        except json.JSONDecodeError as e:
            return -1, f"remote {name}: invalid JSON — {e}"
        if not isinstance(obj, dict) or "id" not in obj:
            return -1, f"remote {name}: missing 'id' field"

    patch_dir.mkdir(parents=True, exist_ok=True)
    existing = {p.name for p in patch_dir.glob("*.json")}

    # Stage every change into sibling .vpcc-new files, then atomic-rename as a
    # group. Partial failure leaves old patch_dir fully intact.
    staged_paths: list[tuple[Path, Path]] = []  # (tmp, dst)
    try:
        for name, content in staged.items():
            dst = patch_dir / name
            if dst.is_file() and dst.read_bytes() == content:
                continue
            tmp = patch_dir / f".{name}.vpcc-new"
            tmp.write_bytes(content)
            staged_paths.append((tmp, dst))
    except Exception as e:
        for tmp, _ in staged_paths:
            tmp.unlink(missing_ok=True)
        return -1, f"stage failed: {e}"

    changed = 0
    for tmp, dst in staged_paths:
        os.replace(tmp, dst)
        changed += 1

    for stale in existing - set(staged):
        (patch_dir / stale).unlink(missing_ok=True)
        changed += 1

    save_state(patches_commit=target_sha, patches_count=len(staged))
    return changed, target_sha


# ── autoheal ─────────────────────────────────────────────────────────────────

def autoheal(
    find_target,
    sha256_short,
    load_patches,
    cmd_verify_fn,
    cmd_patch_fn,
    patch_dir: Path,
    force: bool = False,
    quiet: bool = False,
    cmd_rollback_fn=None,
) -> int:
    """
    Detect Claude Code drift → self-update + re-patch if needed.

    Returns: 0 ok, 1 patched, 2 drift-but-verified, 3 failure.
    """
    def log(msg: str) -> None:
        if not quiet:
            print(msg)

    target, kind = find_target()
    if not target:
        log("vpcc autoheal: Claude Code not installed — nothing to do")
        return 0

    cur_sha = sha256_short(target)
    state = load_state()
    last_sha = state.get("last_cc_sha")
    drifted = cur_sha != last_sha

    if not drifted and not force:
        log(f"vpcc autoheal: CC unchanged ({cur_sha}) — skip")
        return 0

    log(f"vpcc autoheal: CC drift detected ({last_sha} → {cur_sha})")

    # Step 1 — verify current patches against new binary
    class _A: pass
    rc = cmd_verify_fn(_A())
    if rc == 0:
        log("vpcc autoheal: patches still valid, updating state")
        save_state(last_cc_sha=cur_sha, last_cc_kind=kind)
        return 2

    # Step 2 — patches broken, pull latest from GitHub
    log("vpcc autoheal: patches broken, syncing latest from GitHub")
    changed, sha_or_err = sync_patches(patch_dir)
    if changed < 0:
        log(f"vpcc autoheal: sync failed — {sha_or_err}")
        return 3
    log(f"vpcc autoheal: synced {changed} file(s) @ {sha_or_err[:7]}")

    # Step 3 — re-apply. cmd_patch_fn makes a backup first, so any bad write
    # can be undone via cmd_rollback_fn.
    class _P: dry_run = False
    rc = cmd_patch_fn(_P())
    if rc != 0:
        log("vpcc autoheal: re-patch failed — rolling back to pre-patch backup")
        if cmd_rollback_fn is not None:
            try:
                cmd_rollback_fn(type("A", (), {})())
            except Exception as e:
                log(f"vpcc autoheal: rollback failed — {e}")
        return 3

    # Step 4 — confirm verify passes against the fresh patches. If not, the
    # new signatures applied cleanly but didn't land correct markers — treat
    # as suspect and rollback to the pre-heal binary.
    rc_v = cmd_verify_fn(type("A", (), {})())
    if rc_v != 0:
        log("vpcc autoheal: post-patch verify failed — rolling back")
        if cmd_rollback_fn is not None:
            try:
                cmd_rollback_fn(type("A", (), {})())
            except Exception as e:
                log(f"vpcc autoheal: rollback failed — {e}")
        return 3

    target2, _ = find_target()
    save_state(last_cc_sha=sha256_short(target2) if target2 else cur_sha, last_cc_kind=kind)
    log("vpcc autoheal: healed")
    return 1


# ── upstream-patches check (maintainer-side used by CI too) ──────────────────

def upstream_status(patch_dir: Path) -> dict[str, Any]:
    """Compare local patches commit vs remote HEAD. Returns dict w/ {local, remote, drift}."""
    state = load_state()
    local = state.get("patches_commit")
    remote = remote_head_sha("patches")
    return {
        "local_commit":  local,
        "remote_commit": remote,
        "drift":         bool(remote and local and local != remote),
        "local_files":   len(list(patch_dir.glob("*.json"))),
    }
