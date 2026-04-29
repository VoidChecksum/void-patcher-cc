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

# ── console encoding safety (Windows cp1252) ─────────────────────────────────
# Force UTF-8 on stdout/stderr so Unicode glyphs (✓ ✗ ⚠ →) don't crash the
# process. Falls back to ASCII substitution when reconfigure is unavailable.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

_enc = (getattr(sys.stdout, "encoding", None) or "").lower()
if "utf" not in _enc:
    # ASCII-only fallbacks for legacy code pages (cp1252, cp437).
    CHECK, CROSS, WARN_ICON, ARROW, DOT = "[ok]", "[X]", "[!]", "->", "*"
    # Kill ANSI colour too — Windows legacy consoles may not render it.
    G = Y = R = B = X = ""
else:
    CHECK, CROSS, WARN_ICON, ARROW, DOT = "✓", "✗", "⚠", "→", "·"

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
    """Return (path, kind) — kind is 'js' or 'bun_sea'.
    Enumerates every CC packaging: legacy cli.js, Linux/macOS/Windows SEA.
    """
    # Primary cli.js (legacy, all OS)
    js_checks  = [(_PKG + "/cli.js", "js")]
    # Direct bin/ wrappers inside the main pkg
    bin_checks = [
        (_PKG + "/bin/claude.exe", "bun_sea"),
        (_PKG + "/bin/claude",     "bun_sea"),
    ]
    # Platform-specific sub-packages (npm optionalDependencies pattern)
    sub_checks = [
        (_PKG + "/node_modules/@anthropic-ai/claude-code-linux-x64/claude",    "bun_sea"),
        (_PKG + "/node_modules/@anthropic-ai/claude-code-linux-arm64/claude",  "bun_sea"),
        (_PKG + "/node_modules/@anthropic-ai/claude-code-darwin-x64/claude",   "bun_sea"),
        (_PKG + "/node_modules/@anthropic-ai/claude-code-darwin-arm64/claude", "bun_sea"),
        (_PKG + "/node_modules/@anthropic-ai/claude-code-win32-x64/claude.exe","bun_sea"),
        (_PKG + "/node_modules/@anthropic-ai/claude-code-win32-arm64/claude.exe","bun_sea"),
    ]
    checks = js_checks + sub_checks + bin_checks

    for npm_root in _npm_global_roots():
        for suffix, kind in checks:
            p = npm_root / suffix
            if p.exists() and (kind == "js" or p.stat().st_size > 1_000_000):
                return p, kind

    # Version-managed Node installs
    mise_base = Path.home() / ".local/share/mise/installs/node"
    nvm_base  = Path(os.environ.get("NVM_DIR", Path.home() / ".nvm")) / "versions/node"
    # Windows %APPDATA%\npm
    appdata   = os.environ.get("APPDATA")
    extra_bases = [mise_base, nvm_base]
    if appdata:
        extra_bases.append(Path(appdata) / "npm" / "node_modules")
        extra_bases.append(Path(appdata) / "npm")

    for base in extra_bases:
        for suffix, kind in checks:
            if base == extra_bases[0] or base == extra_bases[1]:
                # mise / nvm wrap each Node version in its own dir
                candidates = _version_glob(base, suffix)
            else:
                candidates = [base / suffix] if (base / suffix).exists() else []
            for p in candidates:
                if p.exists() and (kind == "js" or p.stat().st_size > 1_000_000):
                    return p, kind

    # Homebrew on macOS
    for hb in [Path("/opt/homebrew/lib/node_modules"),
               Path("/usr/local/lib/node_modules")]:
        for suffix, kind in checks:
            p = hb / suffix
            if p.exists() and (kind == "js" or p.stat().st_size > 1_000_000):
                return p, kind

    # Native CLI installer (~/.local/share/claude/versions/<ver>)
    _versions_dir = Path.home() / ".local/share/claude/versions"
    if _versions_dir.is_dir():
        for child in sorted(_versions_dir.iterdir(), key=lambda p: p.name, reverse=True):
            if child.is_file() and child.stat().st_size > 1_000_000:
                return child, "bun_sea"

    # Native installer (https://claude.ai/install.sh) lands the binary in
    # ~/.local/share/claude/versions/<semver> (symlinked from ~/.local/bin/claude)
    # as well as older ~/.claude/ and ~/.local/share/claude-code/ layouts.

    # Current layout (≥ 2.1.x native installer): ~/.local/share/claude/versions/<semver>
    claude_versions_dir = Path.home() / ".local/share/claude/versions"
    if claude_versions_dir.is_dir():
        for child in sorted(claude_versions_dir.iterdir(), reverse=True):
            if child.is_file() and child.stat().st_size > 1_000_000:
                return child, "bun_sea"

    native_candidates = [
        # Current native installer symlink — resolve so we return the real file
        Path.home() / ".local/bin/claude",
        Path.home() / ".local/bin/claude.exe",
        # Legacy ~/.claude/ layout
        Path.home() / ".claude/local/claude",
        Path.home() / ".claude/local/claude.exe",
        Path.home() / ".claude/bin/claude",
        Path.home() / ".claude/bin/claude.exe",
        Path.home() / ".claude/downloads",          # may contain claude-<ver>-<platform> blobs
        # Old claude-code name layout
        Path.home() / ".local/share/claude-code/claude",
        Path.home() / ".local/share/claude-code/claude.exe",
        Path.home() / ".local/bin/claude-code",
        Path.home() / ".local/bin/claude-code.exe",
        Path("/usr/local/share/claude-code/claude"),
        Path("/opt/claude-code/bin/claude"),
        Path("/opt/claude-code/bin/claude.exe"),
    ]
    for p in native_candidates:
        if p.is_dir():
            for child in sorted(p.glob("claude-*"), reverse=True):
                if child.is_file() and child.stat().st_size > 1_000_000:
                    return child, "bun_sea"
            continue
        if p.exists() and p.stat().st_size > 1_000_000:
            # Resolve symlinks so we return the real file path
            return p.resolve(), "bun_sea"

    # Windows %LOCALAPPDATA%\Programs\claude-code\ (installer default on Win)
    la = os.environ.get("LOCALAPPDATA")
    if la:
        for suffix in ("Programs/claude-code/claude.exe",
                       "claude-code/claude.exe",
                       "anthropic/claude-code/claude.exe"):
            p = Path(la) / suffix
            if p.exists() and p.stat().st_size > 1_000_000:
                return p, "bun_sea"

    # Last resort: resolve `which claude` from PATH
    try:
        r = subprocess.run(["which", "claude"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            p = Path(r.stdout.strip()).resolve()
            if p.exists() and p.stat().st_size > 1_000_000:
                return p, "bun_sea"
    except Exception:
        pass

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


def _bun_section_has_valid_trailer(data: bytes | bytearray, bun_lo: int, bun_hi: int) -> bool:
    """Accept Bun section trailer with optional PE file-alignment NUL padding."""
    section = bytes(data[bun_lo:bun_hi]).rstrip(b"\x00")
    return section.endswith(_BUN_TRAILER)


def _bun_section_is_bytecode(section_bytes: bytes) -> bool:
    """True when .bun section uses compiled Bun bytecode — must use in-place patching."""
    return b"// @bun @bytecode" in section_bytes[:1024]


def _find_bun_section_elf(data) -> tuple[int, int]:
    """Linux ELF — shdr walk for .bun section."""
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
    raise RuntimeError(".bun section not found (ELF)")


def _find_bun_section_macho(data) -> tuple[int, int]:
    """
    macOS Mach-O (x64 + arm64, both endian + fat).
    Walks LC_SEGMENT_64 load commands looking for section named `__bun`
    (segname `__BUN` — Bun's SEA embedding convention).
    """
    magic = _struct.unpack_from(">I", data, 0)[0]
    # Fat binary — pick first 64-bit arch entry.
    if magic in (0xCAFEBABE, 0xBEBAFECA, 0xCAFEBABF, 0xBFBAFECA):
        big = magic in (0xCAFEBABE, 0xCAFEBABF)
        fmt = ">I" if big else "<I"
        nfat = _struct.unpack_from(fmt, data, 4)[0]
        # each fat_arch is 20 bytes (32-bit) or 32 bytes (64-bit)
        entry_size = 20 if magic in (0xCAFEBABE, 0xBEBAFECA) else 32
        for i in range(nfat):
            base = 8 + i * entry_size
            off = _struct.unpack_from(fmt, data, base + 8)[0]
            # recurse into slice
            sub = data[off:] if hasattr(data, "__getitem__") else bytes(data)[off:]
            return _find_bun_section_macho(bytes(sub))
    if magic not in (0xFEEDFACF, 0xCFFAEDFE, 0xFEEDFACE, 0xCEFAEDFE):
        raise RuntimeError("not a Mach-O binary")
    is_64 = magic in (0xFEEDFACF, 0xCFFAEDFE)
    be = magic in (0xFEEDFACF, 0xFEEDFACE)
    end = ">" if be else "<"
    # header: magic(4) cputype(4) cpusubtype(4) filetype(4) ncmds(4) sizeofcmds(4) flags(4) [reserved(4) if 64]
    ncmds = _struct.unpack_from(end + "I", data, 16)[0]
    hdr_size = 32 if is_64 else 28
    cur = hdr_size
    LC_SEGMENT_64 = 0x19
    LC_SEGMENT    = 0x01
    for _ in range(ncmds):
        cmd, cmdsize = _struct.unpack_from(end + "II", data, cur)
        if cmd == LC_SEGMENT_64:
            # segment_command_64: cmd(4) cmdsize(4) segname(16) vmaddr(8) vmsize(8) fileoff(8) filesize(8) maxprot(4) initprot(4) nsects(4) flags(4) = 72
            segname = bytes(data[cur+8:cur+24]).split(b"\x00", 1)[0]
            nsects  = _struct.unpack_from(end + "I", data, cur + 64)[0]
            sect_base = cur + 72
            for j in range(nsects):
                # section_64: sectname(16) segname(16) addr(8) size(8) offset(4) align(4) reloff(4) nreloc(4) flags(4) reserved1(4) reserved2(4) reserved3(4) = 80
                s = sect_base + j * 80
                sectname = bytes(data[s:s+16]).split(b"\x00", 1)[0]
                sect_segname = bytes(data[s+16:s+32]).split(b"\x00", 1)[0]
                if sectname in (b"__bun", b".bun") or sect_segname in (b"__BUN",):
                    size   = _struct.unpack_from(end + "Q", data, s + 40)[0]
                    offset = _struct.unpack_from(end + "I", data, s + 48)[0]
                    return (offset, size)
        elif cmd == LC_SEGMENT:
            segname = bytes(data[cur+8:cur+24]).split(b"\x00", 1)[0]
            nsects  = _struct.unpack_from(end + "I", data, cur + 48)[0]
            sect_base = cur + 56
            for j in range(nsects):
                s = sect_base + j * 68
                sectname = bytes(data[s:s+16]).split(b"\x00", 1)[0]
                if sectname in (b"__bun", b".bun"):
                    size   = _struct.unpack_from(end + "I", data, s + 36)[0]
                    offset = _struct.unpack_from(end + "I", data, s + 40)[0]
                    return (offset, size)
        cur += cmdsize
    raise RuntimeError(".bun/__bun section not found (Mach-O)")


def _find_bun_section_pe(data) -> tuple[int, int]:
    """Windows PE/COFF — walks section table for .bun section."""
    if data[:2] != b"MZ":
        raise RuntimeError("not a PE binary")
    pe_off = _struct.unpack_from("<I", data, 0x3C)[0]
    if bytes(data[pe_off:pe_off+4]) != b"PE\x00\x00":
        raise RuntimeError("PE signature missing")
    coff = pe_off + 4
    nsect = _struct.unpack_from("<H", data, coff + 2)[0]
    opt_size = _struct.unpack_from("<H", data, coff + 16)[0]
    sect_table = coff + 20 + opt_size
    # each IMAGE_SECTION_HEADER = 40 bytes
    for i in range(nsect):
        s = sect_table + i * 40
        name = bytes(data[s:s+8]).split(b"\x00", 1)[0]
        if name == b".bun":
            vsize   = _struct.unpack_from("<I", data, s + 8)[0]
            rsize   = _struct.unpack_from("<I", data, s + 16)[0]
            raw_off = _struct.unpack_from("<I", data, s + 20)[0]
            # PE SizeOfRawData includes file-alignment padding. VirtualSize is
            # the live .bun payload length; using raw size can land trailer
            # validation inside trailing NUL padding on Windows builds.
            return (raw_off, vsize or rsize)
    raise RuntimeError(".bun section not found (PE)")


def _find_bun_section(data) -> tuple[int, int]:
    """Dispatch to ELF / Mach-O / PE parser by magic bytes. Cross-OS."""
    head = bytes(data[:4])
    if head[:4] == b"\x7fELF":
        return _find_bun_section_elf(data)
    if head in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
                b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce",
                b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
                b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"):
        return _find_bun_section_macho(data)
    if head[:2] == b"MZ":
        return _find_bun_section_pe(data)
    raise RuntimeError(f"unknown binary format magic={head.hex()}")


def _find_active_bundle_bounds(data, bun_lo: int, bun_hi: int) -> tuple[int, int]:
    """
    Bun SEA ≥ 2.1.119 embeds the main entry bundle PLUS a virtual-filesystem
    (VFS) copy of the same source at a second location.  The layout is:

        [header][active-entry-blob (JS source, ~13 MB)][bytecode-for-deps (~102 MB)]
        [lookup-key\x00][vfs-path\x00][vfs-copy-of-cli.js][other-vfs-files][trailer]

    Patching any byte in the VFS copy corrupts the module Bun re-parses at
    startup, causing the "entry.instantiate" crash even though the active
    bundle itself is fine.

    Detection: the active bundle always starts with "// @bun @bytecode" and its
    byte-length is encoded as a little-endian u32 at (data_start - 4).
    We read that field and return [abs_start, abs_end) so callers only scan the
    active bundle region.

    Falls back to the full [bun_lo, bun_hi) range if the marker or size look
    implausible (e.g., old single-bundle builds).
    """
    BUN_BYTECODE_MARKER = b"// @bun @bytecode"
    section_start = bun_lo
    # Search for the marker within the section
    abs_marker = data.find(BUN_BYTECODE_MARKER, section_start, bun_hi)
    if abs_marker < 0:
        return bun_lo, bun_hi  # fallback: no marker found

    data_start = abs_marker  # absolute offset in `data`
    size_field_off = data_start - 4
    if size_field_off < section_start:
        return bun_lo, bun_hi  # no room for size field

    import struct as _s
    blob_size = _s.unpack_from("<I", data, size_field_off)[0]
    active_end = data_start + blob_size

    # Sanity: blob_size must be positive and fit inside the section
    if blob_size < 1024 or active_end > bun_hi:
        return bun_lo, bun_hi  # implausible — fall back

    return data_start, active_end


def patch_bun_sea_inplace(binary: Path, patches: list) -> dict:
    """
    In-place Bun SEA byte patcher. No objcopy, preserves ELF layout.
    Runs the patched binary to verify before committing.

    Research ref: docs/BUN_BYTECODE_FORMAT.md in void-patcher repo.
    Key insight: .bun section has no integrity check; JSC SourceCodeKey is
    fail-open (mismatch -> bytecode discarded, source re-parsed, app runs).

    2.1.119 change: the .bun section now contains a second VFS copy of the
    main cli.js source.  Patching that copy corrupts Bun's module-loader.
    _find_active_bundle_bounds() restricts writes to the active-entry blob only.
    """
    mode = binary.stat().st_mode & 0o7777
    original_size = binary.stat().st_size
    data = bytearray(binary.read_bytes())

    bun_off, bun_size = _find_bun_section(data)
    bun_lo, bun_hi = bun_off, bun_off + bun_size

    if not _bun_section_has_valid_trailer(data, bun_lo, bun_hi):
        return {"ok": False, "err": "Bun trailer invalid — format change", "applied": 0, "skipped": 0}

    # Narrow the writable region to the active entry bundle only, so we never
    # accidentally corrupt the VFS copy or the bundled-module bytecode blobs.
    eff_lo, eff_hi = _find_active_bundle_bounds(data, bun_lo, bun_hi)

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

            if marker and data.find(marker.encode("utf-8", "surrogateescape"), eff_lo, eff_hi) >= 0:
                continue

            if search_regex:
                try:
                    pat = re.compile(search_regex.encode("utf-8", "surrogateescape"), re.DOTALL)
                except re.error:
                    skipped_n += 1
                    continue
                section_view = bytes(data[eff_lo:eff_hi])
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
                    abs_start = eff_lo + m.start()
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
                pos = eff_lo
                while True:
                    j = data.find(s_b, pos, eff_hi)
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
            p = json.loads(f.read_text(encoding="utf-8"))
            if p.get("disabled"):
                continue
            patches.append(p)
        except json.JSONDecodeError as e:
            print(f"{R}{CROSS} {f.name}: invalid JSON — {e}{X}", file=sys.stderr)
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
                eff_lo, eff_hi = _find_active_bundle_bounds(data, bun_off, bun_off + bun_size)
                for p in js_patches:
                    applied_n = 0
                    for sub in p.get("patches", []):
                        marker = sub.get("applied_marker")
                        if marker and data.find(marker.encode("utf-8","surrogateescape"), eff_lo, eff_hi) >= 0:
                            continue
                        sr = sub.get("search_regex") or sub.get("search") or ""
                        if not sr:
                            continue
                        try:
                            if sub.get("search_regex"):
                                pat = re.compile(sr.encode("utf-8","surrogateescape"), re.DOTALL)
                                applied_n += sum(1 for _ in pat.finditer(bytes(data[eff_lo:eff_hi])))
                            else:
                                applied_n += bytes(data[eff_lo:eff_hi]).count(sr.encode("utf-8","surrogateescape"))
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
                    print(f"\n{R}{CROSS} cli.js syntax INVALID — aborted, original untouched{X}")
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
                print(f"\n{R}{CROSS} write failed: {e} — cli.js untouched{X}")
                return 2

    # ── non-JS patches ─────────────────────────────────────────────────────
    for p in meta_patches:
        t = p.get("type")
        if t in ("settings", "hook"):
            success, msg = _apply_settings(p, dry_run=args.dry_run)
        elif t == "mcp_guard":
            success, msg = _apply_mcp_guard(p, dry_run=args.dry_run)
        elif t == "wrapper":
            success, msg = _apply_wrapper(p, kind or "bun_sea", target, dry_run=args.dry_run)
        elif t == "binary_install":
            success, msg = _apply_binary_install(p, kind or "bun_sea", dry_run=args.dry_run)
        else:
            print(f"  {Y}skip{X} {p['id']:40s}  type={t} (unknown)")
            skip += 1
            continue
        mark = f"{G}ok{X}" if success else f"{R}fail{X}"
        print(f"  {mark}   {p['id']:40s}  {msg}")
        ok += success
        fail += not success

    print(f"\n{B}{ok} ok {DOT} {fail} failed {DOT} {skip} skipped{X}")

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
        cur = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
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
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return True, f"{changed} key(s)"


def _apply_mcp_guard(p: dict, dry_run: bool = False) -> tuple[bool, str]:
    """mcp-guard: add MCP spawn timeout to preload + activate via BUN_OPTIONS wrapper."""
    preload_dir = Path.home() / ".local/share/void-patcher"
    preload_src = ROOT / "contrib" / "preload" / "claude-preload.js"
    preload_dst = preload_dir / "claude-preload.js"
    GUARD_MARKER = "// mcp-guard"
    GUARD_CODE = (
        "\n  // mcp-guard: timeout npx @latest MCP server spawns\n"
        '  try { const _cp = require("child_process"); const _oS = _cp.spawn;\n'
        "    _cp.spawn = function(cmd, args, opts) { const p = _oS.apply(this, arguments);\n"
        '      const isNpx = typeof cmd === "string" && cmd.includes("npx") &&\n'
        '        Array.isArray(args) && args.some(a => typeof a === "string" && a.includes("@latest"));\n'
        '      if (isNpx) { const t = setTimeout(() => { try { p.kill("SIGKILL"); } catch(_) {} }, 30000);\n'
        '        p.on("close", () => clearTimeout(t)); } return p; }; } catch(_) {}\n'
    )

    def inject_guard(content: str) -> str | None:
        inject_at = "globalThis.__VPCC_PRELOAD_ACTIVE__ = true;"
        if inject_at in content:
            return content.replace(inject_at, inject_at + GUARD_CODE, 1)
        tail = "\n})();"
        idx = content.rfind(tail)
        if idx >= 0:
            return content[:idx] + GUARD_CODE + content[idx:]
        return None

    preload_missing = not preload_dst.exists()
    if preload_missing:
        if not preload_src.exists():
            return False, "preload source missing — run vpcc install-preload first"
        content = preload_src.read_text(encoding="utf-8")
        if not dry_run:
            preload_dir.mkdir(parents=True, exist_ok=True)
            preload_dst.write_text(content, encoding="utf-8")
    else:
        content = preload_dst.read_text(encoding="utf-8")

    already_guarded = GUARD_MARKER in content
    if already_guarded:
        if dry_run and preload_missing:
            return True, "would install preload (mcp-guard already present)"
        return True, "no-op (already applied)"

    new_content = inject_guard(content)
    if new_content is None:
        return False, "preload format unexpected"

    if dry_run:
        if preload_missing:
            return True, "would install preload and add mcp-guard"
        return True, "would add mcp-guard to preload"

    preload_dst.write_text(new_content, encoding="utf-8")
    return True, "mcp-guard added to preload"


def _apply_wrapper(p: dict, kind: str, target: "Path | None", dry_run: bool = False) -> tuple[bool, str]:
    """Install wrapper — native binary or cli.js depending on install kind."""
    wrapper_path = Path(p.get("wrapper_path", "~/.local/bin/claude")).expanduser()
    NATIVE_MARKER = "# vpcc-native-wrapper"
    JS_MARKER = "CLAUDE_CLI_JS"

    if kind == "bun_sea":
        WRAPPER_CONTENT = (
            "#!/usr/bin/env bash\n"
            f"{NATIVE_MARKER}\n"
            'VPCC_PRELOAD="$HOME/.local/share/void-patcher/claude-preload.js"\n'
            'VPCC_VERSIONS_DIR="$HOME/.local/share/claude/versions"\n'
            'VPCC_NATIVE="$(ls -1 "$VPCC_VERSIONS_DIR" 2>/dev/null | sort -V | tail -1)"\n'
            'VPCC_NATIVE="$VPCC_VERSIONS_DIR/$VPCC_NATIVE"\n'
            'if [[ ! -x "${VPCC_NATIVE:-}" ]]; then echo "vpcc wrapper: native binary not found" >&2; exit 1; fi\n'
            'if [[ -f "$VPCC_PRELOAD" ]]; then\n'
            '    export BUN_OPTIONS="${BUN_OPTIONS:+$BUN_OPTIONS }--preload $VPCC_PRELOAD"\n'
            'fi\n'
            'exec "$VPCC_NATIVE" "$@"\n'
        )
        if wrapper_path.exists() and not wrapper_path.is_symlink():
            try:
                if NATIVE_MARKER in wrapper_path.read_text():
                    return True, "no-op (already applied)"
            except Exception:
                pass

        if dry_run:
            return True, "would install native BUN_OPTIONS wrapper"

        wrapper_path.parent.mkdir(parents=True, exist_ok=True)
        wrapper_path.unlink(missing_ok=True)
        wrapper_path.write_text(WRAPPER_CONTENT, encoding="utf-8")
        wrapper_path.chmod(0o755)
        return True, "native BUN_OPTIONS wrapper installed"
    else:
        content = p.get("content", "")
        if not content:
            return False, "no wrapper content in patch"
        if wrapper_path.exists():
            try:
                existing = wrapper_path.read_text()
                if JS_MARKER in existing or NATIVE_MARKER in existing:
                    return True, "no-op (already applied)"
            except Exception:
                pass
        if dry_run:
            return True, "would install cli.js wrapper"
        wrapper_path.parent.mkdir(parents=True, exist_ok=True)
        wrapper_path.unlink(missing_ok=True)
        wrapper_path.write_text(content, encoding="utf-8")
        wrapper_path.chmod(0o755)
        return True, "cli.js wrapper installed"


def _apply_binary_install(p: dict, kind: str, dry_run: bool = False) -> tuple[bool, str]:
    """binary_install: for npm layout, replace file in package. Native binary: N/A."""
    if kind == "bun_sea":
        return True, "no-op (N/A for native binary — seccomp is in Bun runtime, not VFS)"
    return False, "npm layout binary_install not implemented in vpcc"


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
                print(f"{R}{CROSS}{X} {p['id']}")
                missing += 1
                break

    if missing:
        print(f"\n{R}{missing} patches missing{X}")
        return 1
    print(f"{G}{CHECK} all patches verified{X}")
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
    print(f"{G}{CHECK} restored{X} {target} <- {latest.name}")
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
    print(f"{B}vpcc self-update{X}  <- {_updater.REPO}@{_updater.BRANCH}")
    remote = _updater.remote_head_sha("patches")
    if not remote:
        print(f"{R}could not reach GitHub API{X}")
        return 2
    state = _updater.load_state()
    local = state.get("patches_commit")
    print(f"  local  : {local or '(unknown)'}")
    print(f"  remote : {remote}")
    if local == remote and not args.force:
        print(f"{G}{CHECK} already up to date{X}")
        return 0
    if args.dry_run:
        print(f"{Y}dry-run: would sync{X}")
        return 0
    changed, sha_or_err = _updater.sync_patches(PATCH_DIR, remote)
    if changed < 0:
        print(f"{R}{CROSS} sync failed — {sha_or_err}{X}")
        return 2
    print(f"{G}{CHECK} synced{X}  {changed} file(s) updated @ {sha_or_err[:7]}")
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
        nometa = [r["id"] for r in rows if r["status"] == "unclassified"]
        if drift:
            print(f"  {R}sig drift  : {len(drift)} patches {DOT} {', '.join(drift[:3])}{'...' if len(drift) > 3 else ''}{X}")
        else:
            print(f"  {G}sig drift  : 0 (all anchors locatable){X}")
        if nometa:
            print(f"  {Y}sig nometa : {len(nometa)} patches (pre-v2.1.114, no anchor_strings yet){X}")
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


def _deep_merge(dst: dict, src: dict) -> dict:
    """Recursively merge src into dst. src wins on conflict. Arrays concatenated uniquely."""
    for k, v in src.items():
        if k in dst and isinstance(dst[k], dict) and isinstance(v, dict):
            _deep_merge(dst[k], v)
        elif k in dst and isinstance(dst[k], list) and isinstance(v, list):
            merged = list(dst[k])
            for item in v:
                if item not in merged:
                    merged.append(item)
            dst[k] = merged
        else:
            dst[k] = v
    return dst


_VPCC_CLAUDE_MD_START = "<!-- vpcc:authorization:start -->"
_VPCC_CLAUDE_MD_END   = "<!-- vpcc:authorization:end -->"


def cmd_install_rules(args) -> int:
    """Deploy contrib/rules/ -> ~/.claude/ (authorization doctrine + settings + hook)."""
    src_dir = ROOT / "contrib" / "rules"
    if not src_dir.exists():
        print(f"{R}contrib/rules/ missing in package{X}")
        return 2

    claude_dir = Path.home() / ".claude"
    hooks_dir  = claude_dir / "hooks"
    claude_dir.mkdir(parents=True, exist_ok=True)
    hooks_dir.mkdir(parents=True, exist_ok=True)

    auth_src = (src_dir / "AUTHORIZATION.md").read_text(encoding="utf-8")
    block = f"{_VPCC_CLAUDE_MD_START}\n{auth_src.rstrip()}\n{_VPCC_CLAUDE_MD_END}\n"
    import re as _re
    for dst_name in ("CLAUDE.md", "AGENTS.md"):
        dst = claude_dir / dst_name
        if dst.exists():
            existing = dst.read_text(encoding="utf-8")
            if _VPCC_CLAUDE_MD_START in existing:
                existing = _re.sub(
                    f"{_re.escape(_VPCC_CLAUDE_MD_START)}.*?{_re.escape(_VPCC_CLAUDE_MD_END)}\n?",
                    "", existing, flags=_re.DOTALL,
                )
            merged_md = block + "\n---\n\n" + existing.lstrip()
        else:
            merged_md = block
        dst.write_text(merged_md, encoding="utf-8")
        print(f"  {G}{CHECK}{X} {dst_name} {ARROW} {dst}")

    settings_path = claude_dir / "settings.json"
    rules = json.loads((src_dir / "settings-rules.json").read_text(encoding="utf-8"))
    current = {}
    if settings_path.exists():
        try:
            current = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            bak = settings_path.with_suffix(".json.corrupt.bak")
            settings_path.rename(bak)
            print(f"  {Y}{WARN_ICON} existing settings.json corrupt -> backed up to {bak.name}{X}")
    merged = _deep_merge(current, rules)
    settings_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    print(f"  {G}{CHECK}{X} settings.json {ARROW} {settings_path} (merged {len(rules)} keys)")

    hook_src = src_dir / "hooks" / "vpcc-auto-allow.sh"
    hook_dst = hooks_dir / "vpcc-auto-allow.sh"
    hook_dst.write_bytes(hook_src.read_bytes())
    hook_dst.chmod(0o755)
    print(f"  {G}{CHECK}{X} hook {ARROW} {hook_dst}")

    print(f"\n{G}{CHECK} authorization rules installed{X}")
    print(f"  revert: vpcc uninstall-rules")
    return 0


def cmd_uninstall_rules(args) -> int:
    """Remove vpcc-authored rules. Operator content preserved."""
    claude_dir = Path.home() / ".claude"
    removed = 0
    import re as _re

    for name in ("CLAUDE.md", "AGENTS.md"):
        p = claude_dir / name
        if not p.exists():
            continue
        s = p.read_text(encoding="utf-8")
        if _VPCC_CLAUDE_MD_START not in s:
            continue
        s2 = _re.sub(
            f"{_re.escape(_VPCC_CLAUDE_MD_START)}.*?{_re.escape(_VPCC_CLAUDE_MD_END)}\n?",
            "", s, flags=_re.DOTALL,
        )
        s2 = _re.sub(r"\A\s*---\s*\n", "", s2)
        p.write_text(s2.lstrip(), encoding="utf-8")
        print(f"  {G}{CHECK}{X} stripped vpcc block from {name}")
        removed += 1

    hook = claude_dir / "hooks" / "vpcc-auto-allow.sh"
    if hook.exists():
        hook.unlink()
        print(f"  {G}{CHECK}{X} removed {hook}")
        removed += 1

    settings_path = claude_dir / "settings.json"
    if settings_path.exists():
        try:
            cur = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cur = {}
        rules_src = ROOT / "contrib" / "rules" / "settings-rules.json"
        if rules_src.exists():
            rules = json.loads(rules_src.read_text(encoding="utf-8"))
            for k in ("_vpcc_header", "skipDangerousModePermissionPrompt",
                      "dangerouslyDisableSandbox", "isBypassPermissionsModeAvailable",
                      "bypassPermissionsWorkspaceTrustCheck", "autoUpdaterStatus"):
                cur.pop(k, None)
            if isinstance(cur.get("env"), dict) and isinstance(rules.get("env"), dict):
                for k in rules["env"]:
                    cur["env"].pop(k, None)
                if not cur["env"]:
                    cur.pop("env")
            try:
                pre = cur.get("hooks", {}).get("PreToolUse", [])
                pre = [h for h in pre if "vpcc-auto-allow" not in json.dumps(h)]
                if pre:
                    cur["hooks"]["PreToolUse"] = pre
                else:
                    cur.get("hooks", {}).pop("PreToolUse", None)
                    if not cur.get("hooks"):
                        cur.pop("hooks", None)
            except Exception:
                pass
            settings_path.write_text(json.dumps(cur, indent=2) + "\n", encoding="utf-8")
            print(f"  {G}{CHECK}{X} pruned vpcc keys from settings.json")
            removed += 1

    if removed == 0:
        print(f"{Y}nothing to remove{X}")
    else:
        print(f"\n{G}{CHECK} authorization rules removed ({removed} item(s)){X}")
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
    print(f"{G}{CHECK} installed preload{X}  {src.name} {ARROW} {dst}")
    print(f"  wrapper will auto-load via BUN_OPTIONS=--preload on next run")
    return 0


def cmd_uninstall_preload(args) -> int:
    dst = Path.home() / ".local/share/void-patcher/claude-preload.js"
    if dst.exists():
        dst.unlink()
        print(f"{G}{CHECK} removed{X} {dst}")
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
        print(f"{Y}{WARN_ICON} update available — run 'vpcc self-update'{X}")
        return 1
    if not info["local_commit"] and info["remote_commit"]:
        print(f"{Y}{WARN_ICON} no sync state — run 'vpcc self-update' to pin current{X}")
        return 1
    if info["remote_commit"]:
        print(f"{G}{CHECK} up to date{X}")
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
        help="Deploy runtime monkey-patch preload hook (100%% survival layer)")
    sub.add_parser("uninstall-preload",
        help="Remove runtime preload hook")

    sub.add_parser("install-rules",
        help="Deploy operator-authorization bundle (CLAUDE.md + settings + hook)")
    sub.add_parser("uninstall-rules",
        help="Remove vpcc authorization rules, preserve operator content")

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
            "uninstall-preload": cmd_uninstall_preload,
            "install-rules": cmd_install_rules,
            "uninstall-rules": cmd_uninstall_rules}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
