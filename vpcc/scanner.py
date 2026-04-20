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
            markers: list[str] = []
            for sub in p.get("patches", []):
                if not sig_regex:
                    sig_regex = sub.get("search_regex") or sub.get("search")
                m = sub.get("applied_marker")
                if m:
                    markers.append(m)

            anchor_off = self.find_anchor(anchors) if anchors else None
            regex_hit = False
            if sig_regex:
                try:
                    regex_hit = re.search(sig_regex, self.text, re.DOTALL) is not None
                except re.error:
                    regex_hit = False
            # Post-patch the original anchor/regex often no longer matches — the
            # bytes were replaced. Treat presence of any applied_marker as proof
            # the patch already landed, so scan/doctor don't cry drift.
            marker_hit = any(m in self.text for m in markers)

            if regex_hit or (anchors and anchor_off is not None) or marker_hit:
                status = "ok"
            elif not anchors and not regex_hit:
                # Pre-metadata patch (no anchor_strings yet) — cannot be
                # classified as drift without a locator. Distinguish it.
                status = "unclassified"
            else:
                status = "drift"
            out.append({
                "id": pid,
                "anchors": anchors,
                "anchor_offset": anchor_off,
                "regex_hit": regex_hit,
                "marker_hit": marker_hit,
                "status": status,
            })
        return out


# helpers --------------------------------------------------------------------

def load_text_from_target(target: Path, kind: str) -> str:
    """Extract patchable text from cli.js or Bun SEA .bun section.
    Cross-platform: dispatches ELF / Mach-O / PE by magic bytes.
    Delegates to vpcc.__main__._find_bun_section to avoid duplication.
    """
    if kind == "js":
        return target.read_text(encoding="utf-8", errors="surrogateescape")
    from . import __main__ as _m
    data = bytearray(target.read_bytes())
    off, size = _m._find_bun_section(data)
    return bytes(data[off:off + size]).decode("utf-8", errors="surrogateescape")


def format_scan_report(rows: list[dict[str, Any]], verbose: bool = False) -> str:
    G, Y, R, X = "\033[32m", "\033[33m", "\033[31m", "\033[0m"
    lines = []
    ok = drift = unclassified = 0
    for r in rows:
        status = r["status"]
        if status == "ok":
            mark = f"{G}ok{X}"; ok += 1
        elif status == "drift":
            mark = f"{R}drift{X}"; drift += 1
        else:
            mark = f"{Y}nometa{X}"; unclassified += 1
        off = r["anchor_offset"]
        off_s = f"@0x{off:08x}" if off is not None else "--"
        line = f"  {mark:22s}  {r['id']:42s}  {off_s:>14s}  regex={'Y' if r['regex_hit'] else 'N'}"
        lines.append(line)
        if verbose and r["anchors"]:
            lines.append(f"    anchors: {', '.join(r['anchors'])}")
    tail = f"\n  {G}{ok} ok{X}"
    if drift:        tail += f"  {R}{drift} drift{X}"
    if unclassified: tail += f"  {Y}{unclassified} nometa{X} (pre-v2.1.114 patches — anchor_strings not yet backfilled)"
    lines.append(tail)
    return "\n".join(lines)


def load_patches_from_dir(patch_dir: Path, respect_scan_flag: bool = True) -> list[dict[str, Any]]:
    """Load js_replace patches. When respect_scan_flag=True (default), patches
    that explicitly set `scan_signatures: false` are excluded — they are not
    text-scannable in the target (bytecode-only, superseded, etc.) and including
    them would pollute scan/doctor output with false drift/nometa noise."""
    out = []
    for f in sorted(patch_dir.glob("*.json")):
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "js_replace":
            continue
        if respect_scan_flag and obj.get("scan_signatures", True) is False:
            continue
        obj["__file"] = str(f)
        out.append(obj)
    return out


def auto_heal_drift(text: str, patch_dir: Path, verbose: bool = False) -> dict[str, int]:
    """
    For every patch whose anchors resolve but regex doesn't, regenerate a
    probable search_regex from the anchor context window and rewrite the
    patch JSON file. Pure additive — only touches drift, leaves working
    patches alone.
    """
    sc = SigScanner(text)
    healed = 0
    skipped = 0
    failed = 0
    for p in load_patches_from_dir(patch_dir):
        anchors = p.get("anchor_strings") or []
        if not anchors:
            skipped += 1
            continue
        first_anchor = anchors[0]
        sub = (p.get("patches") or [{}])[0]
        sig_regex = sub.get("search_regex")
        if not sig_regex:
            skipped += 1
            continue
        try:
            regex_hit = re.search(sig_regex, text, re.DOTALL) is not None
        except re.error:
            regex_hit = False
        if regex_hit:
            skipped += 1
            continue
        anchor_off = sc.find_anchor(anchors)
        if anchor_off is None:
            failed += 1
            continue
        new_regex = sc.derive_regex(first_anchor)
        if not new_regex:
            failed += 1
            continue
        sub["search_regex"] = new_regex
        sub.setdefault("replace", new_regex)  # identity replace is safer than nothing
        file_path = Path(p.pop("__file"))
        file_path.write_text(json.dumps(p, indent=2) + "\n", encoding="utf-8")
        if verbose:
            print(f"  healed {p['id']} @ 0x{anchor_off:x}")
        healed += 1
    return {"healed": healed, "skipped": skipped, "failed": failed}
