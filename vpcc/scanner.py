"""
vpcc.scanner — signature-based offset discovery.

When CC updates mangle a single regex patch, the *anchor strings* (stable
human-readable tokens like "tengu_refusal_api_response", "function s5K")
usually survive. SigScanner locates those anchors in the cli.js text or the
Bun SEA .bun section, returns byte offsets, and can regenerate a probable
search_regex from the surrounding window.

Pure stdlib. Zero deps.
"""
from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any


class SigScanner:
    """Signature-driven anchor locator + regex derivation."""

    def __init__(self, text: str | bytes):
        if isinstance(text, bytes):
            try:
                text = text.decode("utf-8", errors="surrogateescape")
            except Exception:
                text = text.decode("latin1")
        self.text = text

    # anchor location ----------------------------------------------------

    def find_anchor(self, anchors: list[str], max_dist: int = 400) -> int | None:
        """First offset where ALL anchors appear within max_dist bytes of the 1st."""
        if not anchors:
            return None
        first = self.text.find(anchors[0])
        while first >= 0:
            window = self.text[first: first + len(anchors[0]) + max_dist + 200]
            if all(a in window for a in anchors[1:]):
                return first
            first = self.text.find(anchors[0], first + 1)
        return None

    def all_occurrences(self, anchor: str) -> list[int]:
        offs: list[int] = []
        i = self.text.find(anchor)
        while i >= 0:
            offs.append(i)
            i = self.text.find(anchor, i + 1)
        return offs

    # regex derivation ---------------------------------------------------

    @staticmethod
    def _minify_names(s: str) -> str:
        return re.sub(r"\b[A-Za-z_\$][\w\$]{0,2}\b",
                      lambda m: r"[A-Za-z_$][\w$]*" if len(m.group(0)) <= 3 else m.group(0),
                      s)

    def derive_regex(self, anchor: str, before: int = 60, after: int = 60,
                     softmin: bool = True) -> str | None:
        """Escaped, minifier-tolerant regex around `anchor`."""
        i = self.text.find(anchor)
        if i < 0:
            return None
        ctx = self.text[max(0, i - before): i + len(anchor) + after]
        esc = re.escape(ctx)
        if softmin:
            esc = self._minify_names(esc)
        return esc

    # patch-file driver --------------------------------------------------

    def scan_patches(self, patches: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for p in patches:
            pid = p.get("id", "?")
            anchors = p.get("anchor_strings") or []
            sig_regex = None
            for sub in p.get("patches", []):
                sig_regex = sub.get("search_regex") or sub.get("search")
                if sig_regex:
                    break

            anchor_off = self.find_anchor(anchors) if anchors else None
            regex_hit = False
            if sig_regex:
                try:
                    regex_hit = re.search(sig_regex, self.text, re.DOTALL) is not None
                except re.error:
                    regex_hit = False

            status = (
                "ok" if (regex_hit or (anchors and anchor_off is not None))
                else "drift"
            )
            out.append({
                "id": pid,
                "anchors": anchors,
                "anchor_offset": anchor_off,
                "regex_hit": regex_hit,
                "status": status,
            })
        return out


# helpers --------------------------------------------------------------------

def load_text_from_target(target: Path, kind: str) -> str:
    """Extract patchable text from cli.js or Bun SEA .bun section."""
    if kind == "js":
        return target.read_text(encoding="utf-8", errors="surrogateescape")

    import struct as _struct
    data = bytearray(target.read_bytes())
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
            off  = _struct.unpack_from("<Q", data, sh + 0x18)[0]
            size = _struct.unpack_from("<Q", data, sh + 0x20)[0]
            return bytes(data[off:off + size]).decode("utf-8", errors="surrogateescape")
    raise RuntimeError(".bun section not found")


def format_scan_report(rows: list[dict[str, Any]], verbose: bool = False) -> str:
    G, Y, R, X = "\033[32m", "\033[33m", "\033[31m", "\033[0m"
    lines = []
    ok = drift = 0
    for r in rows:
        mark = f"{G}ok{X}" if r["status"] == "ok" else f"{R}drift{X}"
        ok += r["status"] == "ok"
        drift += r["status"] == "drift"
        off = r["anchor_offset"]
        off_s = f"@0x{off:08x}" if off is not None else "--"
        line = f"  {mark:20s}  {r['id']:42s}  {off_s:>14s}  regex={'Y' if r['regex_hit'] else 'N'}"
        lines.append(line)
        if verbose and r["anchors"]:
            lines.append(f"    anchors: {', '.join(r['anchors'])}")
    lines.append(f"\n  {ok} ok · {Y}{drift} drift{X}")
    return "\n".join(lines)


def load_patches_from_dir(patch_dir: Path) -> list[dict[str, Any]]:
    out = []
    for f in sorted(patch_dir.glob("*.json")):
        try:
            obj = json.loads(f.read_text())
            if obj.get("type") == "js_replace":
                out.append(obj)
        except json.JSONDecodeError:
            continue
    return out
