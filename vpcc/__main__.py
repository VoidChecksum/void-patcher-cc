"""
vpcc — Void Patcher for Claude Code
Supports both cli.js (≤2.1.112) and Bun SEA binary (≥2.1.114).
Regex-signature patches survive all minor/patch releases.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from . import updater as _updater
from . import scanner as _scanner

ROOT        = Path(__file__).resolve().parent.parent
PATCH_DIR   = ROOT / "patches"
BACKUP_DIR  = Path.home() / ".vpcc" / "backups"

G, Y, R, B, X = "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[0m"

_PKG = "@anthropic-ai/claude-code"
_BUN_SECTION = ".bun"


# ── target discovery ─────────────────────────────────────────────────────────

def _npm_global_roots() -> list[Path]:
    roots: list[Path] = []
    try:
        r = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            roots.insert(0, Path(r.stdout.strip()))
    except Exception:
        pass
    home = Path.home()
    roots += [
        home / ".npm-global/lib/node_modules",
        home / ".local/lib/node_modules",
        Path("/usr/local/lib/node_modules"),
        Path("/usr/lib/node_modules"),
    ]
    return roots


def _version_glob(base: Path, suffix: str) -> list[Path]:
    import glob as _glob
    return [Path(m) for m in _glob.glob(str(base / "*" / "lib" / "node_modules" / suffix))]


def find_target() -> tuple[Path | None, str]:
    """Return (path, kind) — kind is 'js' or 'bun_sea'. js preferred, binary fallback."""
    checks = [
        (_PKG + "/cli.js", "js"),
        (_PKG + "/bin/claude.exe", "bun_sea"),
        (_PKG + "/bin/claude", "bun_sea"),
    ]
    for npm_root in _npm_global_roots():
        for suffix, kind in checks:
            p = npm_root / suffix
            if p.exists() and (kind == "js" or p.stat().st_size > 1_000_000):
                return p, kind

    mise_base = Path.home() / ".local/share/mise/installs/node"
    nvm_base  = Path(os.environ.get("NVM_DIR", Path.home() / ".nvm")) / "versions/node"
    for base in [mise_base, nvm_base]:
        for suffix, kind in checks:
            for p in _version_glob(base, suffix):
                if p.exists() and (kind == "js" or p.stat().st_size > 1_000_000):
                    return p, kind

    return None, ""


def sha256_short(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


# ── Bun SEA helpers ───────────────────────────────────────────────────────────

import struct as _struct

_BUN_TRAILER = b"\n---- Bun! ----\n"


def _bun_section_is_bytecode(section_bytes: bytes) -> bool:
    """True when .bun section uses compiled Bun bytecode — must use in-place patching."""
    return b"// @bun @bytecode" in section_bytes[:1024]


def _find_bun_section(data) -> tuple[int, int]:
    """Locate .bun ELF section via direct shdr walk. Returns (file_offset, size)."""
    e_shoff     = _struct.unpack_from("<Q", data, 0x28)[0]
    e_shentsize = _struct.unpack_from("<H", data, 0x3A)[0]
    e_shnum     = _struct.unpack_from("<H", data, 0x3C)[0]
    e_shstrndx  = _struct.unpack_from("<H", data, 0x3E)[0]
    strtab_shdr = e_shoff + e_shstrndx * e_shentsize
    strtab_off  = _struct.unpack_from("<Q", data, strtab_shdr + 0x18)[0]
    strtab_size = _struct.unpack_from("<Q", data, strtab_shdr + 0x20)[0]
    strtab      = bytes(data[strtab_off:strtab_off + strtab_size])
    for i in range(e_shnum):
        sh = e_shoff + i * e_shentsize
        sh_name = _struct.unpack_from("<I", data, sh)[0]
        end = strtab.index(b"\x00", sh_name)
        if strtab[sh_name:end] == b".bun":
            return (_struct.unpack_from("<Q", data, sh + 0x18)[0],
                    _struct.unpack_from("<Q", data, sh + 0x20)[0])
    raise RuntimeError(".bun section not found")


def patch_bun_sea_inplace(binary: Path, patches: list) -> dict:
    """
    In-place Bun SEA byte patcher. No objcopy, preserves ELF layout.
    Runs the patched binary to verify before committing.

    Research ref: docs/BUN_BYTECODE_FORMAT.md in void-patcher repo.
    Key insight: .bun section has no integrity check; JSC SourceCodeKey is
    fail-open (mismatch -> bytecode discarded, source re-parsed, app runs).
    """
    mode = binary.stat().st_mode & 0o7777
    original_size = binary.stat().st_size
    data = bytearray(binary.read_bytes())

    bun_off, bun_size = _find_bun_section(data)
    bun_lo, bun_hi = bun_off, bun_off + bun_size

    if bytes(data[bun_hi - len(_BUN_TRAILER):bun_hi]) != _BUN_TRAILER:
        return {"ok": False, "err": "Bun trailer invalid — format change", "applied": 0, "skipped": 0}

    applied_total = 0
    skipped_total = 0
    per_patch = []

    for p in patches:
        applied_n = 0
        skipped_n = 0
        for sub in p.get("patches", []):
            search_regex = sub.get("search_regex")
            search = sub.get("search")
            replace = sub.get("replace", "")
            marker = sub.get("applied_marker")

            if marker and data.find(marker.encode("utf-8", "surrogateescape"), bun_lo, bun_hi) >= 0:
                continue

            if search_regex:
                try:
                    pat = re.compile(search_regex.encode("utf-8", "surrogateescape"), re.DOTALL)
                except re.error:
                    skipped_n += 1
                    continue
                section_view = bytes(data[bun_lo:bun_hi])
                for m in pat.finditer(section_view):
                    mb = m.group(0)
                    try:
                        rb = m.expand(replace.encode("utf-8", "surrogateescape"))
                    except Exception:
                        skipped_n += 1
                        continue
                    if len(rb) > len(mb):
                        skipped_n += 1
                        continue
                    if len(rb) < len(mb):
                        rb = rb + b" " * (len(mb) - len(rb))
                    abs_start = bun_lo + m.start()
                    data[abs_start:abs_start + len(mb)] = rb
                    applied_n += 1
            elif search:
                s_b = search.encode("utf-8", "surrogateescape")
                r_b = replace.encode("utf-8", "surrogateescape")
                if len(r_b) > len(s_b):
                    skipped_n += 1
                    continue
                if len(r_b) < len(s_b):
                    r_b = r_b + b" " * (len(s_b) - len(r_b))
                pos = bun_lo
                while True:
                    j = data.find(s_b, pos, bun_hi)
                    if j < 0:
                        break
                    data[j:j + len(s_b)] = r_b
                    applied_n += 1
                    pos = j + len(s_b)

        per_patch.append({"id": p["id"], "applied": applied_n, "skipped": skipped_n})
        applied_total += applied_n
        skipped_total += skipped_n

    if len(data) != original_size:
        return {"ok": False, "err": f"size drift {len(data)} vs {original_size}",
                "applied": 0, "skipped": skipped_total, "per_patch": per_patch}

    if applied_total == 0:
        return {"ok": True, "noop": True, "applied": 0, "skipped": skipped_total, "per_patch": per_patch}

    # Write to temp same-dir file, verify by running, then atomic swap.
    tmp_bin = binary.parent / f".{binary.name}.vpcctmp-{os.getpid()}"
    try:
        tmp_bin.write_bytes(bytes(data))
        tmp_bin.chmod(mode)

        r = subprocess.run([str(tmp_bin), "--version"],
                           capture_output=True, text=True, timeout=20)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0 or "Claude Code" not in out:
            tmp_bin.unlink(missing_ok=True)
            return {"ok": False, "err": f"verify failed: {out[:120]!r} rc={r.returncode}",
                    "applied": applied_total, "skipped": skipped_total, "per_patch": per_patch}

        binary.unlink()
        tmp_bin.rename(binary)
        binary.chmod(mode)
    except Exception as e:
        tmp_bin.unlink(missing_ok=True)
        return {"ok": False, "err": f"write failed: {e}", "applied": 0, "skipped": skipped_total}

    return {"ok": True, "applied": applied_total, "skipped": skipped_total, "per_patch": per_patch}


def read_bun_js(binary: Path) -> tuple[str | None, str]:
    """Extract JS text from .bun ELF section. Copies binary first (ETXTBSY guard)."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src.exe"
        shutil.copy2(binary, src)
        section = Path(tmp) / "section.bin"
        r = subprocess.run(
            ["objcopy", "--dump-section", f"{_BUN_SECTION}={section}", str(src)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return None, f"objcopy dump: {(r.stderr or r.stdout).strip()}"
        data = section.read_bytes()
        if _bun_section_is_bytecode(data):
            return None, "Bun bytecode format — text patching not supported (would corrupt binary)"
        try:
            return data.decode("utf-8", errors="surrogateescape"), ""
        except Exception as e:
            return None, str(e)


def write_bun_js(binary: Path, text: str) -> tuple[bool, str]:
    """HARD LOCK: Bun SEA bytecode cannot be safely text-patched. Refuse writes."""
    return False, "Bun SEA bytecode — binary writes disabled to prevent corruption"

def _write_bun_js_DISABLED(binary: Path, text: str) -> tuple[bool, str]:
    """Inject patched JS text back into .bun ELF section. Unlinks first (ETXTBSY guard)."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src.exe"
        shutil.copy2(binary, src)
        section = Path(tmp) / "section.bin"
        section.write_bytes(text.encode("utf-8", errors="surrogateescape"))
        out = Path(tmp) / "patched.exe"
        shutil.copy2(src, out)
        r = subprocess.run(
            ["objcopy", "--update-section", f"{_BUN_SECTION}={section}", str(out)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return False, f"objcopy inject: {(r.stderr or r.stdout).strip()}"
        mode = binary.stat().st_mode & 0o7777
        binary.unlink()        # must unlink running binary before replacing (ETXTBSY)
        shutil.copy2(out, binary)
        binary.chmod(mode)
    return True, ""


# ── patch logic ───────────────────────────────────────────────────────────────

def load_patches() -> list[dict[str, Any]]:
    patches = []
    for f in sorted(PATCH_DIR.glob("*.json")):
        try:
            patches.append(json.loads(f.read_text()))
        except json.JSONDecodeError as e:
            print(f"{R}✗ {f.name}: invalid JSON — {e}{X}", file=sys.stderr)
    return patches


def _apply_subs(text: str, patch: dict) -> tuple[str, int, str]:
    """Apply sub-patch list to text. Returns (new_text, n_applied, error)."""
    total = 0
    for sub in patch.get("patches", []):
        rep    = sub.get("replace", "")
        marker = sub.get("applied_marker")
        if marker and marker in text:
            continue  # idempotent — already applied

        pat      = sub.get("search_regex") or sub.get("search")
        is_regex = bool(sub.get("search_regex"))
        if not pat:
            continue
        try:
            if is_regex:
                new_text, n = re.subn(pat, rep, text,
                                      count=sub.get("count", 0) or 0,
                                      flags=re.DOTALL)
            else:
                cnt = sub.get("count", -1)
                new_text = text.replace(pat, rep, cnt if cnt > 0 else -1)
                n = int(new_text != text)
        except re.error as e:
            return text, total, f"regex error: {e}"

        if n == 0 and sub.get("required", False):
            return text, total, f"required pattern not found: {pat[:60]}..."
        text = new_text
        total += n if isinstance(n, int) else int(n)

    return text, total, ""


def backup(target: Path, kind: str) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ext   = "exe.bak" if kind == "bun_sea" else "js.bak"
    dst   = BACKUP_DIR / f"claude.{stamp}.{sha256_short(target)}.{ext}"
    shutil.copy2(target, dst)
    for old in sorted(BACKUP_DIR.glob("claude.*.bak"))[:-10]:
        old.unlink(missing_ok=True)
    return dst


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_patch(args) -> int:
    patches  = load_patches()
    target, kind = find_target()
    js_patches   = [p for p in patches if p.get("type") == "js_replace"]
    meta_patches = [p for p in patches if p.get("type") not in ("js_replace",)]

    print(f"{B}vpcc patch — {len(patches)} patches{X}")
    if target:
        label = "cli.js" if kind == "js" else f"Bun SEA {target.name}"
        print(f"  target : {target}")
        print(f"  format : {label}")
        print(f"  sha    : {sha256_short(target)}")
        if not args.dry_run:
            bkp = backup(target, kind)
            print(f"  backup : {bkp}")
    else:
        print(f"  {Y}target not found — settings-only patches will apply{X}")

    ok = fail = skip = 0

    # ── JS patches ─────────────────────────────────────────────────────────
    if not target:
        for p in js_patches:
            print(f"  {Y}skip{X} {p['id']:40s}  target not found")
            skip += 1
    elif kind == "bun_sea":
        # In-place ELF byte patcher — no objcopy, no corruption. Applies every
        # js_replace in a single pass, verifies by running patched binary, atomic swap.
        if args.dry_run:
            # Dry-run: apply to a temporary in-memory bytearray, count hits only.
            try:
                data = bytearray(target.read_bytes())
                bun_off, bun_size = _find_bun_section(data)
                for p in js_patches:
                    applied_n = 0
                    for sub in p.get("patches", []):
                        marker = sub.get("applied_marker")
                        if marker and data.find(marker.encode("utf-8","surrogateescape"), bun_off, bun_off+bun_size) >= 0:
                            continue
                        sr = sub.get("search_regex") or sub.get("search") or ""
                        if not sr:
                            continue
                        try:
                            if sub.get("search_regex"):
                                pat = re.compile(sr.encode("utf-8","surrogateescape"), re.DOTALL)
                                applied_n += sum(1 for _ in pat.finditer(bytes(data[bun_off:bun_off+bun_size])))
                            else:
                                applied_n += bytes(data[bun_off:bun_off+bun_size]).count(sr.encode("utf-8","surrogateescape"))
                        except re.error:
                            pass
                    msg = "no-op (already applied)" if applied_n == 0 else f"would apply {applied_n} in-place"
                    print(f"  {G}ok{X}    {p['id']:40s}  {msg}")
                    ok += 1
                print(f"  {Y}dry-run: binary not modified{X}")
            except Exception as e:
                print(f"  {R}fail{X}  [bun-inplace]  {e}")
                fail += len(js_patches)
        else:
            result = patch_bun_sea_inplace(target, js_patches)
            if not result["ok"]:
                print(f"  {R}fail{X}  [bun-inplace]  {result.get('err','unknown')}")
                fail += len(js_patches)
            else:
                for pr in result.get("per_patch", []):
                    n = pr["applied"]
                    msg = "no-op (already applied)" if n == 0 else f"{n} in-place replacement(s)"
                    print(f"  {G}ok{X}    {pr['id']:40s}  {msg}")
                    ok += 1
                if result.get("noop"):
                    pass  # all already applied
                else:
                    print(f"  {G}verified in-place{X}  (ran binary, Claude Code output confirmed)")
    else:
        # cli.js path — patch file directly
        text = target.read_text(encoding="utf-8")
        orig = text
        for p in js_patches:
            new_text, n, err = _apply_subs(text, p)
            if err:
                print(f"  {R}fail{X}  {p['id']:40s}  {err}")
                fail += 1
                continue
            msg = "no-op (already applied)" if new_text == text else f"{n} replacement(s)"
            print(f"  {G}ok{X}    {p['id']:40s}  {msg}")
            text = new_text
            ok  += 1
        if text != orig and not args.dry_run:
            # Atomic write: stage to sibling tmp, node --check it, only then
            # replace the live cli.js. On any failure the original is untouched.
            tmp = target.parent / f".{target.name}.vpcctmp-{os.getpid()}"
            try:
                tmp.write_text(text, encoding="utf-8")
                r = subprocess.run(["node", "--check", str(tmp)],
                                   capture_output=True, text=True, timeout=15)
                if r.returncode != 0:
                    tmp.unlink(missing_ok=True)
                    err = (r.stderr or r.stdout or "").strip().splitlines()[-1:]
                    print(f"\n{R}✗ cli.js syntax INVALID — aborted, original untouched{X}")
                    if err:
                        print(f"  node: {err[0][:180]}")
                    return 2
                try:
                    shutil.copystat(target, tmp)
                except Exception:
                    pass
                os.replace(tmp, target)   # atomic on same filesystem
            except Exception as e:
                tmp.unlink(missing_ok=True)
                print(f"\n{R}✗ write failed: {e} — cli.js untouched{X}")
                return 2

    # ── non-JS patches ─────────────────────────────────────────────────────
    for p in meta_patches:
        t = p.get("type")
        if t in ("settings", "hook"):
            success, msg = _apply_settings(p, dry_run=args.dry_run)
            mark = f"{G}ok{X}" if success else f"{R}fail{X}"
            print(f"  {mark}   {p['id']:40s}  {msg}")
            ok += success; fail += not success
        else:
            # mcp_guard / wrapper / binary_install — skip in standalone vpcc
            print(f"  {Y}skip{X} {p['id']:40s}  type={t} (use void-patcher for full support)")
            skip += 1

    print(f"\n{B}{ok} ok · {fail} failed · {skip} skipped{X}")

    # record state for autoheal drift detection
    if target and not args.dry_run and not fail:
        try:
            _updater.save_state(last_cc_sha=sha256_short(target), last_cc_kind=kind)
        except Exception:
            pass

    return 1 if fail else 0


def _apply_settings(patch: dict, dry_run: bool = False) -> tuple[bool, str]:
    path = Path(os.path.expanduser(patch.get("settings_path", "~/.claude/settings.json")))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        cur = json.loads(path.read_text()) if path.is_file() else {}
    except json.JSONDecodeError:
        cur = {}
    wanted = patch.get("settings", {})
    out = dict(cur)
    changed = sum(1 for k, v in wanted.items() if out.setdefault(k, None) != v and not out.__setitem__(k, v))  # type: ignore
    # simpler:
    out = dict(cur)
    changed = 0
    for k, v in wanted.items():
        if out.get(k) != v:
            out[k] = v
            changed += 1
    if not changed:
        return True, "no-op (already applied)"
    if dry_run:
        return True, f"would set {changed} key(s)"
    path.write_text(json.dumps(out, indent=2))
    return True, f"{changed} key(s)"


def cmd_verify(args) -> int:
    target, kind = find_target()
    if not target:
        print(f"{R}claude-code not found{X}")
        return 2

    if kind == "bun_sea":
        # In-place verify: read whole binary, locate .bun section, check markers inside.
        data = bytearray(target.read_bytes())
        try:
            bun_off, bun_size = _find_bun_section(data)
        except Exception as e:
            print(f"{R}ELF parse failed: {e}{X}")
            return 2
        text = bytes(data[bun_off:bun_off + bun_size]).decode("utf-8", errors="surrogateescape")
    else:
        text = target.read_text(encoding="utf-8")

    missing = 0
    for p in load_patches():
        if p.get("type") != "js_replace":
            continue
        for sub in p.get("patches", []):
            if not sub.get("required", True):
                continue
            marker = sub.get("applied_marker")
            if marker and marker not in text:
                print(f"{R}✗{X} {p['id']}")
                missing += 1
                break

    if missing:
        print(f"\n{R}{missing} patches missing{X}")
        return 1
    print(f"{G}✓ all patches verified{X}")
    return 0


def cmd_rollback(args) -> int:
    target, kind = find_target()
    if not target:
        print(f"{R}claude-code not found{X}")
        return 2
    baks = sorted(BACKUP_DIR.glob("claude.*.bak"))
    if not baks:
        print(f"{R}no backups in {BACKUP_DIR}{X}")
        return 1
    latest = baks[-1]
    mode = target.stat().st_mode & 0o7777
    target.unlink()
    shutil.copy2(latest, target)
    target.chmod(mode)
    print(f"{G}✓ restored{X} {target} ← {latest.name}")
    return 0


def cmd_status(args) -> int:
    target, kind = find_target()
    patches = load_patches()
    print(f"{B}vpcc status{X}")
    print(f"  patches : {len(patches)}")
    if target:
        label = "cli.js (JS, ≤v2.1.112)" if kind == "js" else "Bun SEA ELF (≥v2.1.114)"
        print(f"  target  : {target}")
        print(f"  format  : {label}")
        print(f"  sha256  : {sha256_short(target)}")
        print(f"  size    : {target.stat().st_size // 1024 // 1024} MB")
    else:
        print(f"  target  : {R}NOT FOUND{X}")
    baks = sorted(BACKUP_DIR.glob("claude.*.bak"))
    print(f"  backups : {len(baks)}  ({BACKUP_DIR})")
    return 0


def cmd_list(args) -> int:
    for p in load_patches():
        print(f"  {p['id']:40s}  {p.get('description','')}")
    return 0


def cmd_self_update(args) -> int:
    """Pull latest patches/*.json from GitHub."""
    print(f"{B}vpcc self-update{X}  ← {_updater.REPO}@{_updater.BRANCH}")
    remote = _updater.remote_head_sha("patches")
    if not remote:
        print(f"{R}could not reach GitHub API{X}")
        return 2
    state = _updater.load_state()
    local = state.get("patches_commit")
    print(f"  local  : {local or '(unknown)'}")
    print(f"  remote : {remote}")
    if local == remote and not args.force:
        print(f"{G}✓ already up to date{X}")
        return 0
    if args.dry_run:
        print(f"{Y}dry-run: would sync{X}")
        return 0
    changed, sha_or_err = _updater.sync_patches(PATCH_DIR, remote)
    if changed < 0:
        print(f"{R}✗ sync failed — {sha_or_err}{X}")
        return 2
    print(f"{G}✓ synced{X}  {changed} file(s) updated @ {sha_or_err[:7]}")
    if changed and not args.no_reapply:
        print(f"\n{B}re-applying patches{X}")
        class _P: dry_run = False
        return cmd_patch(_P())
    return 0


def cmd_autoheal(args) -> int:
    """Detect CC drift, re-verify, self-update + re-patch if broken."""
    return _updater.autoheal(
        find_target=find_target,
        sha256_short=sha256_short,
        load_patches=load_patches,
        cmd_verify_fn=cmd_verify,
        cmd_patch_fn=cmd_patch,
        cmd_rollback_fn=cmd_rollback,
        patch_dir=PATCH_DIR,
        force=args.force,
        quiet=args.quiet,
    )


def cmd_scan(args) -> int:
    """Sig-based offset discovery. Prints anchor offsets + regex hit status."""
    target, kind = find_target()
    if not target:
        print(f"{R}claude-code not found{X}")
        return 2

    try:
        text = _scanner.load_text_from_target(target, kind)
    except Exception as e:
        print(f"{R}extract failed: {e}{X}")
        return 2

    patches = _scanner.load_patches_from_dir(PATCH_DIR)
    sc = _scanner.SigScanner(text)
    rows = sc.scan_patches(patches)

    print(f"{B}vpcc scan — {len(rows)} js_replace patches{X}")
    print(f"  target : {target}")
    print(f"  format : {'cli.js' if kind == 'js' else '.bun section (' + str(len(text)) + ' bytes)'}")
    print(f"  sha    : {sha256_short(target)}")
    print()
    print(_scanner.format_scan_report(rows, verbose=args.verbose))

    if getattr(args, "auto_heal", False):
        res = _scanner.auto_heal_drift(text, PATCH_DIR, verbose=args.verbose)
        print(f"\n{B}auto-heal:{X}  {G}{res['healed']} healed{X}  "
              f"{res['skipped']} skipped  {R}{res['failed']} failed{X}")

    if args.export_patch:
        target_id = args.export_patch
        match = next((r for r in rows if r["id"] == target_id), None)
        if not match:
            print(f"\n{R}no patch id '{target_id}'{X}")
            return 1
        anchors = match["anchors"]
        if not anchors:
            print(f"\n{R}patch has no anchor_strings — cannot regenerate{X}")
            return 1
        regex = sc.derive_regex(anchors[0])
        print(f"\n{B}regenerated regex for {target_id}:{X}")
        print(f"  {regex}")

    return 1 if any(r["status"] == "drift" for r in rows) else 0


def cmd_doctor(args) -> int:
    """Full health report: sha, patches applied, sig drift, backup count, upstream."""
    target, kind = find_target()
    patches = load_patches()
    print(f"{B}vpcc doctor{X}")
    print(f"  vpcc ver   : {__import__('vpcc').__version__ if hasattr(__import__('vpcc'), '__version__') else '2.1.114'}")
    print(f"  patches    : {len(patches)}")
    if not target:
        print(f"  target     : {R}NOT FOUND{X}")
        return 2
    label = "cli.js" if kind == "js" else "Bun SEA ELF"
    print(f"  target     : {target}")
    print(f"  format     : {label}")
    print(f"  sha256     : {sha256_short(target)}")
    print(f"  size       : {target.stat().st_size // 1024 // 1024} MB")

    # sig drift
    try:
        text = _scanner.load_text_from_target(target, kind)
        rows = _scanner.SigScanner(text).scan_patches(_scanner.load_patches_from_dir(PATCH_DIR))
        drift = [r["id"] for r in rows if r["status"] == "drift"]
        if drift:
            print(f"  {Y}sig drift  : {len(drift)} patches — {', '.join(drift[:3])}{'...' if len(drift) > 3 else ''}{X}")
        else:
            print(f"  {G}sig drift  : 0 (all anchors locatable){X}")
    except Exception as e:
        print(f"  {R}sig scan   : failed — {e}{X}")

    # applied markers
    try:
        rc_v = cmd_verify(type("A", (), {})())
    except SystemExit:
        rc_v = 1
    print(f"  applied    : {'all' if rc_v == 0 else 'partial/none'}")

    # backups
    baks = sorted(BACKUP_DIR.glob("claude.*.bak"))
    print(f"  backups    : {len(baks)} in {BACKUP_DIR}")

    # upstream
    try:
        info = _updater.upstream_status(PATCH_DIR)
        if info["drift"]:
            print(f"  {Y}upstream   : behind — run 'vpcc self-update'{X}")
        elif info["remote_commit"]:
            print(f"  {G}upstream   : current{X}")
        else:
            print(f"  upstream   : unreachable")
    except Exception:
        print(f"  upstream   : error")

    return 0


def cmd_watch(args) -> int:
    """Daemon: poll cli.js/SEA mtime+sha; on change, backup + autoheal."""
    import time
    target, kind = find_target()
    if not target:
        print(f"{R}claude-code not found{X}")
        return 2
    print(f"{B}vpcc watch — polling every {args.interval}s{X}")
    print(f"  target: {target}")

    last_sha = sha256_short(target)
    last_mtime = target.stat().st_mtime
    print(f"  sha   : {last_sha}")

    try:
        while True:
            time.sleep(args.interval)
            try:
                target, kind = find_target()
                if not target:
                    print(f"{Y}  target vanished — waiting{X}")
                    continue
                m = target.stat().st_mtime
                if m == last_mtime:
                    continue
                cur_sha = sha256_short(target)
                if cur_sha == last_sha:
                    last_mtime = m
                    continue
                print(f"\n{Y}[{datetime.now().strftime('%H:%M:%S')}] CC changed: {last_sha} -> {cur_sha}{X}")
                backup(target, kind)
                class _A: force = False; quiet = False
                rc = cmd_autoheal(_A())
                print(f"  autoheal rc={rc}")
                last_sha = sha256_short(target)
                last_mtime = target.stat().st_mtime
            except Exception as e:
                print(f"{R}  watch loop error: {e}{X}")
    except KeyboardInterrupt:
        print(f"\n{B}watch stopped{X}")
        return 0


def cmd_install_preload(args) -> int:
    """Copy contrib/preload/claude-preload.js into wrapper's expected path."""
    src = ROOT / "contrib" / "preload" / "claude-preload.js"
    if not src.exists():
        print(f"{R}source missing: {src}{X}")
        return 2
    dst_dir = Path.home() / ".local/share/void-patcher"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "claude-preload.js"
    dst.write_bytes(src.read_bytes())
    dst.chmod(0o644)
    print(f"{G}✓ installed preload{X}  {src.name} → {dst}")
    print(f"  wrapper will auto-load via BUN_OPTIONS=--preload on next run")
    return 0


def cmd_uninstall_preload(args) -> int:
    dst = Path.home() / ".local/share/void-patcher/claude-preload.js"
    if dst.exists():
        dst.unlink()
        print(f"{G}✓ removed{X} {dst}")
    else:
        print(f"{Y}not installed{X}")
    return 0


def cmd_check_updates(args) -> int:
    """Show local vs remote patches commit, no changes."""
    info = _updater.upstream_status(PATCH_DIR)
    print(f"{B}vpcc check-updates{X}")
    print(f"  local commit  : {info['local_commit'] or '(unknown)'}")
    print(f"  remote commit : {info['remote_commit'] or '(unreachable)'}")
    print(f"  local files   : {info['local_files']}")
    if info["drift"]:
        print(f"{Y}⚠ update available — run 'vpcc self-update'{X}")
        return 1
    if not info["local_commit"] and info["remote_commit"]:
        print(f"{Y}⚠ no sync state — run 'vpcc self-update' to pin current{X}")
        return 1
    if info["remote_commit"]:
        print(f"{G}✓ up to date{X}")
    return 0


# ── entry ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="vpcc",
        description="Void Patcher for Claude Code — regex-signature patches, cli.js + Bun SEA",
    )
    sub = ap.add_subparsers(dest="cmd", metavar="command")
    p_patch = sub.add_parser("patch", help="Apply all patches")
    p_patch.add_argument("--dry-run", "-n", action="store_true")
    sub.add_parser("verify",   help="Check patches are applied")
    sub.add_parser("rollback", help="Restore from most recent backup")
    sub.add_parser("status",   help="Show install state")
    sub.add_parser("list",     help="List patches in catalog")

    p_su = sub.add_parser("self-update",
        help="Pull latest patches/*.json from GitHub and re-apply")
    p_su.add_argument("--dry-run", "-n", action="store_true")
    p_su.add_argument("--force", "-f", action="store_true",
        help="Sync even if commit hashes match")
    p_su.add_argument("--no-reapply", action="store_true",
        help="Skip re-applying after sync")

    p_ah = sub.add_parser("autoheal",
        help="Detect Claude Code drift; self-update + re-patch if broken")
    p_ah.add_argument("--force", "-f", action="store_true",
        help="Run checks even if CC sha unchanged")
    p_ah.add_argument("--quiet", "-q", action="store_true")

    sub.add_parser("check-updates",
        help="Show if remote patches differ from local")

    sub.add_parser("install-preload",
        help="Deploy runtime monkey-patch preload hook (100% survival layer)")
    sub.add_parser("uninstall-preload",
        help="Remove runtime preload hook")

    p_sc = sub.add_parser("scan",
        help="Signature-based offset discovery (survives regex drift)")
    p_sc.add_argument("--verbose", "-v", action="store_true")
    p_sc.add_argument("--export-patch", metavar="ID",
        help="Regenerate regex for patch ID from anchor strings")
    p_sc.add_argument("--auto-heal", action="store_true",
        help="Rewrite drifted regexes in patches/*.json from anchor windows")

    sub.add_parser("doctor",
        help="Full health report: target, patches, sig drift, upstream")

    p_w = sub.add_parser("watch",
        help="Daemon: poll target, autoheal on change")
    p_w.add_argument("--interval", "-i", type=int, default=10,
        help="Poll interval seconds (default 10)")

    args = ap.parse_args()
    if args.cmd is None:
        ap.print_help()
        return 0
    return {"patch": cmd_patch, "verify": cmd_verify,
            "rollback": cmd_rollback, "status": cmd_status,
            "list": cmd_list,
            "self-update": cmd_self_update,
            "autoheal": cmd_autoheal,
            "check-updates": cmd_check_updates,
            "scan": cmd_scan,
            "doctor": cmd_doctor,
            "watch": cmd_watch,
            "install-preload": cmd_install_preload,
            "uninstall-preload": cmd_uninstall_preload}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
