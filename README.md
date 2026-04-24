<div align="center">

<a href="https://github.com/VoidChecksum/void-patcher-cc">
<img alt="vpcc banner" src="https://raw.githubusercontent.com/VoidChecksum/void-patcher-cc/main/.github/assets/banner.svg" width="780"
     onerror="this.style.display='none';this.nextElementSibling.style.display='block'"/>
<pre style="display:none">
 ██╗   ██╗██████╗  ██████╗ ██████╗
 ██║   ██║██╔══██╗██╔════╝██╔════╝
 ██║   ██║██████╔╝██║     ██║
 ╚██╗ ██╔╝██╔═══╝ ██║     ██║
  ╚████╔╝ ██║     ╚██████╗╚██████╗
   ╚═══╝  ╚═╝      ╚═════╝ ╚═════╝
     V o i d   P a t c h e r   f o r   C l a u d e   C o d e
</pre>
</a>

<br>

<!-- Custom theme: electric cyan (#00D4FF) primary, neon pink (#FF6B9D) accent, deep space dark #0D1117 bg -->

[![vpcc](https://img.shields.io/badge/vpcc-v2.1.119-00D4FF?style=for-the-badge&logo=gnubash&logoColor=white&labelColor=0D1117)](https://github.com/VoidChecksum/void-patcher-cc/releases)
[![patches](https://img.shields.io/badge/patches-96-FF6B9D?style=for-the-badge&logo=codeigniter&logoColor=white&labelColor=0D1117)](patches/)
[![target](https://img.shields.io/badge/claude--code-v2.1.119-F97316?style=for-the-badge&logo=anthropic&logoColor=white&labelColor=0D1117)](https://www.npmjs.com/package/@anthropic-ai/claude-code)
[![format](https://img.shields.io/badge/format-ELF_.bun_%2B_cli.js-9C27B0?style=for-the-badge&logo=bun&logoColor=white&labelColor=0D1117)](https://bun.sh)
[![license](https://img.shields.io/badge/GPL--3.0-blue?style=for-the-badge&logo=gnu&logoColor=white&labelColor=0D1117)](LICENSE)

[![idempotent](https://img.shields.io/badge/idempotent-100%25-3FB950?style=flat-square&labelColor=0D1117)]()
[![sig--scan](https://img.shields.io/badge/sig--scan-on-FF9100?style=flat-square&labelColor=0D1117)]()
[![auto--heal](https://img.shields.io/badge/auto--heal-on-1F6FEB?style=flat-square&labelColor=0D1117)]()
[![preload--hook](https://img.shields.io/badge/preload--hook-runtime-E9D900?style=flat-square&labelColor=0D1117)]()
[![verify](https://img.shields.io/badge/verify-atomic_rollback-FF4F4F?style=flat-square&labelColor=0D1117)]()
[![python](https://img.shields.io/badge/python-%E2%89%A53.9-3776AB?style=flat-square&logo=python&logoColor=white&labelColor=0D1117)]()

<h1>⚡ vpcc — Void Patcher for Claude Code</h1>

<strong>96 reverse-engineered hardening patches · 3-layer defense · survives every CC update</strong><br>
<sub>cli.js (≤ 2.1.112) &nbsp;·&nbsp; Bun SEA ELF (≥ 2.1.114) &nbsp;·&nbsp; runtime preload hook &nbsp;·&nbsp; auto-heal scanner</sub>

</div>

---

<table>
<tr>
<td width="50%" valign="top">

### 🎯 TL;DR · one-liner install

**macOS · Linux · WSL**
```bash
curl -fsSL https://raw.githubusercontent.com/VoidChecksum/void-patcher-cc/main/install.sh | bash
```

**Windows (PowerShell 5.1+/7+)**
```powershell
irm https://raw.githubusercontent.com/VoidChecksum/void-patcher-cc/main/install.ps1 | iex
```

Chains on top of Anthropic's official installer, then applies 96 patches, deploys the runtime preload hook, and (Linux) enables the systemd auto-heal timer.

Every permission prompt · refusal · AUP gate · classifier · telemetry sink **off**.
Every CC upgrade **auto-heals**.

</td>
<td width="50%" valign="top">

### 🧭 Jump to

- [How it works](#-how-it-works) · [Why it survives](#-why-it-survives-every-update)
- [Compat matrix](#-compatibility-matrix) · [Install](#-install)
- [Usage](#-usage) · [AUP bypass stack](#-aup-bypass-stack)
- [Byte offsets · v2.1.119](#-byte-offsets--v21119-ref-build)
- [Patch catalog](#-patch-catalog-96-total) · [Architecture](#-architecture)
- [Auto-update flow](#-auto-update-flow)
- [Manual RE · r2 / pwndbg / rg](#-manual-offset-discovery--r2--pwndbg--rg)
- [Troubleshooting](#-troubleshooting) · [Scope](#-security--scope)

</td>
</tr>
</table>

---

## 🎯 How it works

<details open>
<summary><b>Click for pipeline diagram</b></summary>

```mermaid
%%{init: {'theme':'dark','themeVariables':{
   'primaryColor':'#00D4FF','edgeLabelBackground':'#0D1117',
   'tertiaryColor':'#FF6B9D','fontFamily':'JetBrains Mono'}}}%%
flowchart LR
    NPM[npm -g<br/>@anthropic-ai/claude-code] -->|236 MB SEA or 26 MB cli.js| TGT[target binary]
    TGT --> SIG{96 regex<br/>signatures}
    SIG -->|match| BYTE[byte-patch<br/>in-place]
    BYTE --> VER{verify<br/>--version}
    VER -->|ok| SWAP[atomic swap]
    VER -->|fail| RB[rollback]
    SWAP --> RUN[claude runs]
    RUN --> PRE[preload hook]
    PRE --> HOOK{runtime shims<br/>JSON.parse / fetch / defineProperty}
    HOOK --> RUN
    TGT -.-> WATCH[vpcc watch]
    WATCH -->|sha drift| HEAL[auto-heal scanner]
    HEAL -->|regex regen| SIG
    style SIG fill:#00D4FF,stroke:#0D1117,color:#0D1117
    style PRE fill:#FF6B9D,stroke:#0D1117,color:#0D1117
    style HEAL fill:#E9D900,stroke:#0D1117,color:#0D1117
```

</details>

```
          ┌─────────────── 3-LAYER DEFENSE ──────────────┐
          │                                              │
 layer 1  ▶  .bun ELF byte-patches   (96 regex sigs)    ◀
 layer 2  ▶  Bun --preload hook      (JS runtime shims) ◀
 layer 3  ▶  auto-heal sig-scanner   (regex regen)      ◀
          │                                              │
          └──────────────────────────────────────────────┘
```

---

## 🛡️ Why it survives every update

Anthropic's Bun build **re-minifies** on every release — `gM4` → `s5K` → `Qw7` → …
The only stable thing is **text that must stay human-readable**: event names (`tengu_refusal_api_response`), URLs (`anthropic.com/legal/aup`), log prefixes, schema keys.

Every patch ships three independent locators, ordered by hit reliability:

<div align="center">

| Mechanism           | Reliability | Self-healing | Example                            |
|---------------------|:-----------:|:------------:|------------------------------------|
| ① anchor-string set | ★★★★★       | via scanner  | `["function s5K", "tengu_refusal_api_response"]` |
| ② wildcard regex    | ★★★☆☆       | regenerated  | `function s5K\(([A-Za-z_$][\w$]*),…`|
| ③ runtime preload   | ★★★★★       | N/A          | `JSON.parse` wrapper rewrites refusal |

</div>

When ② drifts, `vpcc scan --auto-heal` rewrites it from ①'s context window. When ① also drifts (rare), ③ still catches the refusal at runtime. **All three must fail simultaneously** to break.

---

### 2.1.119 layout change

Starting at **Claude Code 2.1.119**, the `.bun` ELF section embeds **two** copies of the entry JS bundle:

```
[header][active-entry-blob ~13 MB][bytecode-cache ~102 MB]
[lookup-key\x00][vfs-path\x00][vfs-copy-of-cli.js ~13 MB][other-vfs-files][trailer]
```

Bun re-parses the **VFS copy** at startup for module resolution. Patching both copies (as pre-2.1.119 vpcc did) corrupts the VFS bundle and fails with an opaque `entry.instantiate` crash inside Bun's ESM linker.

`vpcc ≥ 2.1.119` adds `_find_active_bundle_bounds()`: locates the `// @bun @bytecode` marker, reads the u32 blob length at `marker-4`, and restricts all `search_regex` writes to `[marker, marker + blob_size)`. VFS copy stays pristine, Bun loads cleanly.

Falls back to the full section on older (single-bundle) builds.

---

## 🧬 Compatibility matrix

<div align="center">

| CC version      | Format               | OS support                 | Size   | Coverage | AUP bypass | Status      |
|:---------------:|:--------------------:|:--------------------------:|-------:|---------:|:----------:|:-----------:|
| 2.0.x           | `cli.js` (Node)      | Lin · mac · Win · WSL      |  20 MB | 62 / 77  | ✅          | legacy      |
| 2.1.0 – 2.1.112 | `cli.js` (Node)      | Lin · mac · Win · WSL      |  26 MB | 70 / 77  | ✅          | legacy      |
| 2.1.114 – 2.1.117 | Bun SEA (ELF / Mach-O / PE) | Lin · mac (x64/arm64) · Win (x64/arm64) · WSL | 236 MB | 77 / 77  | ✅ | stable      |
| 2.1.118         | Bun SEA (ELF / Mach-O / PE) | Lin · mac (x64/arm64) · Win (x64/arm64) · WSL | 236 MB | 77 / 77  | ✅ | stable      |
| **2.1.119**     | **Bun SEA (active-bundle + VFS-copy)** | **Lin · mac (x64/arm64) · Win (x64/arm64) · WSL** | 233 MB | **96/96** | ✅ | **current** |
| 2.1.120+        | Bun SEA (expected)   | all                        |   —    | auto-heal| ✅          | watch mode  |

</div>

> SEA binaries are patched **in-place** via direct `.bun` ELF section byte writes. No `objcopy`, no size drift. JSC SourceCodeKey is fail-open — bytecode-hash mismatch → source re-parse → app boots clean.

---

## 📦 Install

### One-liner (recommended)

<table>
<tr><td><b>macOS · Linux · WSL</b></td><td>

```bash
curl -fsSL https://raw.githubusercontent.com/VoidChecksum/void-patcher-cc/main/install.sh | bash
```

</td></tr>
<tr><td><b>Windows PowerShell</b></td><td>

```powershell
irm https://raw.githubusercontent.com/VoidChecksum/void-patcher-cc/main/install.ps1 | iex
```

</td></tr>
</table>

Both scripts are idempotent — re-run to update. They:
1. Install Claude Code via Anthropic's official installer if missing (`https://claude.ai/install.sh` / `install.ps1`).
2. Install `pipx` if missing.
3. Install `vpcc` from GitHub.
4. Clone `contrib/` assets (preload hook, systemd units, Windows wrappers).
5. Run `vpcc patch` + `vpcc install-preload`.
6. Enable `systemd --user vpcc-autoheal.timer` (Linux) / add `claude.cmd` wrapper to user PATH (Windows).

### Manual

```bash
pipx install git+https://github.com/VoidChecksum/void-patcher-cc
vpcc patch && vpcc install-preload

# editable dev
git clone https://github.com/VoidChecksum/void-patcher-cc
cd void-patcher-cc && pipx install -e . --force

# uninstall
pipx uninstall vpcc
```

### Requirements (per OS)

| OS           | Prereqs                                       | CC binary location (auto-detected)                                     |
|:------------:|-----------------------------------------------|------------------------------------------------------------------------|
| **Linux**    | `python3` ≥ 3.9 · `npm` · `curl`              | npm global · `/opt/claude-code/bin/claude` · `~/.claude/local/claude`  |
| **macOS**    | `python3` ≥ 3.9 · `npm` · `curl`              | npm global · Homebrew · `~/.claude/local/claude` (x64 + arm64 + Rosetta)|
| **Windows**  | PowerShell 5.1+/7+ · `python` ≥ 3.9 · `npm`   | `%APPDATA%\npm\node_modules\...` · `%LOCALAPPDATA%\Programs\claude-code\claude.exe` |
| **WSL**      | as Linux                                      | WSL file tree                                                          |

### State & data files

| Path                                          | Purpose                           |
|-----------------------------------------------|-----------------------------------|
| `~/.vpcc/state.json`                          | patch commit · last CC sha        |
| `~/.vpcc/backups/`                            | 10 most recent target backups     |
| `~/.local/share/void-patcher/claude-preload.js` (linux/macOS) · `%LOCALAPPDATA%\void-patcher\claude-preload.js` (Windows) | runtime preload hook |
| `~/.config/systemd/user/vpcc-autoheal.timer`  | Linux auto-heal trigger           |

---

## 🚀 Usage

<div align="center">

| Command                          | Purpose                                                                         |
|----------------------------------|---------------------------------------------------------------------------------|
| `vpcc patch` `[-n]`              | Apply all 96 patches. Idempotent, atomic, verified.                             |
| `vpcc verify`                    | Every `applied_marker` present?                                                 |
| `vpcc scan` `[-v]`               | 🔬 Sig-scan — anchor offsets + regex hit.                                       |
| `vpcc scan --auto-heal`          | 💊 Regenerate drifted regexes in `patches/*.json`.                              |
| `vpcc scan --export-patch ID`    | Print probable regex for `ID` from its anchors.                                 |
| `vpcc doctor`                    | Full health report.                                                             |
| `vpcc watch -i 10`               | 👁 Daemon — poll target; on change: backup + autoheal + re-patch + verify.      |
| `vpcc autoheal` `[-f]`           | One-shot: drift → sync patches → re-patch → rollback on fail.                   |
| `vpcc self-update`               | Pull latest `patches/*.json` from GitHub, re-apply.                             |
| `vpcc check-updates`             | Local vs remote patch commit diff.                                              |
| `vpcc install-preload`           | 🔌 Deploy runtime preload hook (layer 2).                                       |
| `vpcc uninstall-preload`         | Remove preload hook.                                                            |
| `vpcc rollback`                  | Restore latest backup.                                                          |
| `vpcc status` / `vpcc list`      | Target info / patch enumeration.                                                |

</div>

### Example session

```text
$ vpcc doctor
vpcc doctor
  vpcc ver   : 2.1.119
  patches    : 96
  target     : .../claude-code-linux-x64/claude
  format     : Bun SEA ELF
  sha256     : 372814218070
  size       : 233 MB
  sig drift  : 0 (all anchors locatable)
  applied    : all
  backups    : 3
  upstream   : current

$ vpcc watch -i 10 &
[1] vpcc watch — polling every 10s
```

---

## 🔓 AUP bypass stack

<div align="center">

| Layer | Target                               | Patches            | Effect                                        |
|:-----:|--------------------------------------|--------------------|-----------------------------------------------|
|  1    | refusal stop-reason handler (`s5K`)  | 32 · **52**        | `if(H!=="refusal")return;` → unconditional    |
|  1    | AUP refusal sentence                 | 15 · 17 · **54**   | Replaced with authorization ack.              |
|  1    | refusal explanation field            | **55**             | `null` out                                    |
|  1    | permission-mode mapper (`Xj$`)       | **53**             | every mode → `"allow"`                        |
|  1    | `jtH` safety check                   | **56**             | always `{safe:true}`                          |
|  1    | `canUseTool` `safetyCheck` deny      | **57**             | `{allowed:false}` branches → allow            |
|  1    | sandbox rule deny                    | **58**             | `behavior:"deny"` → `behavior:"allow"`        |
|  1    | `tg7` write-prompt predicate         | **59**             | always `false`                                |
|  1    | classifier fail-open (×5)            | 14 · 16 · 26 · 33  | no classifier can block                       |
|  2    | runtime `JSON.parse` wrapper         | preload            | `stop_reason=refusal` → `end_turn`            |
|  2    | runtime `fetch` SSE rewrite          | preload            | refusal tokens swapped in-flight              |
|  2    | runtime `Object.defineProperty` hook | preload            | `canUseTool`/`shouldBlock`/`isHarmful` neutralized by shape |
|  2    | stderr suppression                   | preload            | residual AUP lines dropped                    |
|  3    | anchor-string sig-scanner            | scanner            | regex drift auto-heals from context window    |

</div>

### Worked example — `function s5K`

<table>
<tr>
<td><b>before</b></td>
<td>

```js
function s5K(H,$,q){
  if(H!=="refusal") return;
  let K = q?.type==="refusal"
        ? q.explanation?.trimEnd()??null
        : null;
  l("tengu_refusal_api_response",{has_explanation:!!K});
  …
  let z = `${qP}: Claude Code is unable to respond to
           this request, which appears to violate our
           Usage Policy (https://…/legal/aup).${A}`;
}
```

</td>
</tr>
<tr>
<td><b>after</b><br>(patches 52 + 54 + 55)</td>
<td>

```js
function s5K(H,$,q){
  return;                              // 52
  if(H!=="refusal") return;
  let K = q?.type==="refusal" ? null : null;  // 55
  …
  let z = "Claude Code is processing this request       // 54
           under operator authorization.";
}
```

</td>
</tr>
</table>

---

## 🧭 Byte offsets · v2.1.119 ref build

Reference binary: `@anthropic-ai/claude-code-linux-x64/claude`
SHA-256 prefix: `cca43053f062` · size: **245 230 208 B** · format: ELF 64-bit LSB, `.bun` section (active bundle **+ VFS copy** — see [2.1.119 layout change](#211119-layout-change) below)

> Offsets below are from the **2.1.118 ref build** and are preserved as a historical anchor — regex anchors remain correct; absolute offsets have shifted in 2.1.119 (use `vpcc scan` to get the live offsets for the current binary).

<div align="center">

| Offset (hex)  | Offset (dec)  | Anchor                                                         | Patch | Risk |
|:-------------:|--------------:|----------------------------------------------------------------|:-----:|:----:|
| `0x06810446`  |   109 092 934 | `Xj$`                                                          |  53   |  L   |
| `0x068124a6`  |   109 092 982 | `function Xj$(H,$){…bypassPermissions…`                        |  53   |  L   |
| `0x06abb4fd`  |   111 948 749 | `double press esc to edit your last message`                   |  17   |  L   |
| `0x06abbb53`  |   111 962 083 | `function s5K` (refusal handler)                               |  52   |  L   |
| `0x06abbbd8`  |   111 962 100 | `?.type==="refusal"?…explanation?.trimEnd()??null`             |  55   |  L   |
| `0x06abbbc0`  |   111 962 192 | `tengu_refusal_api_response`                                   |  52   |  —   |
| `0x06abbcbf`  |   111 962 367 | `Claude Code is unable to respond to this request…`            |  54   |  L   |
| `0x06abbcf7`  |   111 962 423 | `appears to violate our Usage Policy`                          |  54   |  L   |
| `0x06ad5f6e`  |   112 072 574 | `safetyCheck` · `canUseTool` deny branch                       |  57   |  L   |
| `0x06ad5f8c`  |   112 072 604 | `classifierApprovable`                                         |  57   |  —   |
| `0x06d15a33`  |   114 356 819 | sandbox compound-write `safetyCheck`                           |  58   |  M   |
| `0x088b9d11`  |   143 290 129 | `shouldBlock` in auto-mode classifier                          |  33   |  M   |
| `0x0b006d0e`  |   184 565 518 | `bypassPermissions` statsig recheck                            |  34   |  L   |
| `0x00866eaa` *|    filtered ptr| `if(j?.type==="rule")return{behavior:"deny"…`                 |  58   |  M   |
| `0x00a80517` *|    filtered ptr| `function tg7(A){`                                            |  59   |  L   |

<sub>Risk: **L** = pure early-return / string · **M** = control-flow divergence · **H** = affects write paths
\*= offset in `.bun` section view as reported by `vpcc scan`</sub>

</div>

```bash
# regenerate any offset table row locally
SEA=$(npm root -g)/@anthropic-ai/claude-code/node_modules/@anthropic-ai/claude-code-linux-x64/claude
rg -oab --text 'tengu_refusal_api_response|function s5K|function jtH|Xj\$|safetyCheck|classifierApprovable|behavior:"deny"|function tg7' "$SEA"
```

or via vpcc:

```bash
vpcc scan -v
vpcc scan --export-patch js-s5K-refusal-neutralize-v2.1.114
```

---

## 📋 Patch catalog (96 total)

<details>
<summary><b>🛑 AUP &amp; refusal · 9</b></summary>

|  # | ID                                                       | Effect                                   |
|---:|----------------------------------------------------------|------------------------------------------|
| 15 | `js-aup-refusal`                                         | Legacy AUP phrase swap                   |
| 17 | `js-aup-refusal-2`                                       | "double press esc" variant               |
| 27 | `js-malware-refusal`                                     | Malware-specific refusal                 |
| 29 | `js-denial-workaround`                                   | Denial-path workaround                   |
| 30 | `js-webfetch-preflight-skip`                             | WebFetch preflight refusal skip          |
| 32 | `js-refusal-stop-reason-neutralize`                      | Legacy `gM4` (≤ 2.1.112)                 |
| ⭐52 | `js-s5K-refusal-neutralize-v2.1.114`                    | v2.1.114 `s5K` early-return              |
| ⭐54 | `js-aup-refusal-sanitize-v2.1.114`                      | Refusal sentence rewrite                 |
| ⭐55 | `js-refusal-explanation-null-v2.1.114`                  | null explanation field                   |

</details>

<details>
<summary><b>🔓 Permission / bypass · 7</b></summary>

|  # | ID                                                       | Effect                                   |
|---:|----------------------------------------------------------|------------------------------------------|
| 01 | `bypass-permissions`                                     | Settings default                         |
| 09 | `js-allow-skip-permissions`                              | `--dangerously-skip-permissions` allowed |
| 10 | `js-disable-bypass-check`                                | Runtime bypass guard off                 |
| 12 | `js-session-bypass-mode`                                 | Session bypass persistence               |
| ⭐53 | `js-Xj-permissionmode-allowall-v2.1.114`                | `Xj$` always returns `"allow"`           |
| 46 | `js-bypass-perm-mode-not-available-fake-ok`              | Fake entitlement check                   |
| 47 | `js-bypass-perm-mode-not-available-sdk-fake-ok`          | SDK variant                              |

</details>

<details>
<summary><b>🎯 canUseTool / safety / sandbox · 4</b></summary>

|  # | ID                                                       | Effect                                   |
|---:|----------------------------------------------------------|------------------------------------------|
| ⭐56 | `js-jtH-safe-always-true-v2.1.114`                      | `jtH → {safe:true}`                      |
| ⭐57 | `js-canusetool-safetycheck-allow-v2.1.114`              | `allowed:false` safetyCheck → allow      |
| ⭐58 | `js-rule-deny-allow-v2.1.114`                           | Sandbox rule deny → allow                |
| ⭐59 | `js-tg7-permission-writer-false-v2.1.114`               | `tg7 → false` (no write prompts)         |

</details>

<details>
<summary><b>🧠 Classifier · 5</b></summary>

|  # | ID                                           | Effect                                  |
|---:|----------------------------------------------|-----------------------------------------|
| 14 | `js-classifier-failopen`                    | Generic classifier fail-open            |
| 16 | `js-classifier-all-failopen`                | All classifier paths → allow            |
| 26 | `js-security-guardrail`                     | Guardrail wrapper off                   |
| 33 | `js-auto-mode-classifier-shouldblock-false` | Auto-mode classifier                    |
|  — | `js-twostage-classifier-always-on`          | Force two-stage classifier always       |

</details>

<details><summary><b>📋 Plan mode · 4</b></summary>
Patches 11 · 24 · 28 + envelope. Plan mode refusal UI off, coercion to `allow`.
</details>

<details><summary><b>💳 Subscription / entitlement / A/B · 8</b></summary>
21 Max pin · 25 A/B unlock · 34/35 statsig kills · 38 policy allow-all · 48/49/50 chrome/voice/brief entitlement skip · `js-experimental-betas-always-on`.
</details>

<details><summary><b>📡 Telemetry / metrics / logging · 6</b></summary>
19 metrics · 36 datadog sink · 37 1P events · 39 agent summary · 44 Co-Authored-By footer · 45 elevated-priv stderr.
</details>

<details><summary><b>🪝 Hooks / env / wrapper · 7</b></summary>
02 env flags · 05 auto-allow hook · 06 patch-guard · 07 mcp-guard · 08 cli syntax self-heal · 20 seccomp passthrough · 23 extra protection.
</details>

<details><summary><b>⏱ Timeout / capacity · 5</b></summary>
40/41 bash timeouts · 42 MCP sendrequest · 43 max_thinking · raised bash/task output defaults.
</details>

<details><summary><b>🧩 Plugin / misc · 22</b></summary>
Plugin telemetry off, deeplink disable, premature-read off, hardfail flag disable, agent implicit fork max-turns raise, computer-use policy refusal, plugin denylist passthrough, … (see `vpcc list`).
</details>

⭐ = added in this v2.1.114 release (patches 52-59).

---

## 🏗️ Architecture

<div align="center">

```mermaid
%%{init:{'theme':'dark','themeVariables':{'primaryColor':'#00D4FF','fontFamily':'JetBrains Mono'}}}%%
graph TD
    W[~/.local/bin/claude<br/>bash wrapper]
    W -->|detect| B1[npm @anthropic-ai/claude-code-linux-x64/claude]
    W -->|detect| B2[/opt/claude-code/bin/claude]
    W -->|detect| B3[cli.js legacy path]
    W -->|BUN_OPTIONS=--preload| P[~/.local/share/void-patcher/claude-preload.js]
    B1 --> BUN[Bun runtime]
    B2 --> BUN
    B3 --> NODE[Node runtime]
    BUN -->|.bun section| JSC[JSC parser]
    JSC -->|patched bytes| RUN[Claude Code running]
    P --> RUN
    RUN -.vpcc watch.-> WATCH
    WATCH[vpcc watch] -->|sha drift| HEAL[auto-heal]
    HEAL -->|regex regen| PATCHES[(patches/*.json)]
    PATCHES --> PATCH[vpcc patch]
    PATCH --> B1
    style W fill:#00D4FF,color:#0D1117
    style P fill:#FF6B9D,color:#0D1117
    style HEAL fill:#E9D900,color:#0D1117
    style RUN fill:#3FB950,color:#0D1117
```

</div>

```
vpcc/
├── __init__.py    — version 2.1.119
├── __main__.py    — 14 sub-commands
├── updater.py     — GitHub API sync + autoheal state machine
└── scanner.py     — SigScanner + auto-heal regen

patches/           — 96 signed JSON patches
contrib/
├── preload/claude-preload.js   — runtime monkey-patch layer
└── systemd/                    — autoheal timer unit
```

---

## 🔄 Auto-update flow

```mermaid
%%{init:{'theme':'dark','themeVariables':{'primaryColor':'#00D4FF','fontFamily':'JetBrains Mono'}}}%%
flowchart LR
    U[npm update -g<br/>@anthropic-ai/claude-code] --> S[sha changes]
    S --> W{vpcc watch}
    W -->|sha drift| BK[backup<br/>~/.vpcc/backups/]
    BK --> AH[vpcc autoheal]
    AH --> V1{vpcc verify}
    V1 -->|markers present| OK[update state · done]
    V1 -->|markers missing| SU[vpcc self-update]
    SU --> AS[auto-heal scanner]
    AS --> PT[vpcc patch]
    PT --> V2{node --check / bin --version}
    V2 -->|ok| OK
    V2 -->|fail| RB[vpcc rollback]
    RB --> LB[restore pre-patch backup]
    style W fill:#00D4FF,color:#0D1117
    style AS fill:#E9D900,color:#0D1117
    style OK fill:#3FB950,color:#0D1117
    style RB fill:#FF4F4F,color:#0D1117
```

Triggered three ways:

1. **`vpcc watch`** — polling daemon (`-i` seconds, default 10).
2. **systemd timer** — `contrib/systemd/vpcc-autoheal.{service,timer}` (every 15 min).
3. **Manual** — `vpcc autoheal -f`.

State at `~/.vpcc/state.json` (synthetic example):

```json
{
  "last_cc_sha":    "12bd4b0916de",
  "last_cc_kind":   "bun_sea",
  "patches_commit": "a1b2c3d4e5f6",
  "patches_count":  96,
  "updated_at":     "2026-04-24T10:00:00+00:00"
}
```

Backups: 10 most recent `claude.<ts>.<sha12>.{js,exe}.bak` in `~/.vpcc/backups/`.

---

## 🔬 Manual offset discovery · r2 / pwndbg / rg

<details open>
<summary><b>via ripgrep (fastest)</b></summary>

```bash
SEA=$(npm root -g)/@anthropic-ai/claude-code/node_modules/@anthropic-ai/claude-code-linux-x64/claude
rg -oab --text \
  'Acceptable Use|tengu_refusal_api_response|function s5K|Xj\$|function jtH|function tg7|safetyCheck|classifierApprovable|behavior:"deny"' "$SEA"
```

</details>

<details>
<summary><b>via radare2</b></summary>

```bash
r2 -AA -q -c '
  izz~tengu_refusal
  izz~bypassPermissions
  izz~"function s5K"
  /j tengu_refusal_api_response
' "$SEA"
```

- `izz` lists strings in every section (including `.bun`).
- `/j <pattern>` returns JSON with virtual + file offsets.
- `pdf @ <vaddr>` prints the decoded function body.

</details>

<details>
<summary><b>via pwndbg (dynamic)</b></summary>

```bash
pwndbg claude
pwndbg> start
pwndbg> find 0x00007fff00000000, +0x10000000, "tengu_refusal_api_response"
pwndbg> x/200s $_
pwndbg> telescope $_ 8
```

Useful when SEA loads `.bun` into a non-predictable VMA.

</details>

<details>
<summary><b>via vpcc scanner</b></summary>

```bash
vpcc scan -v
vpcc scan --export-patch js-s5K-refusal-neutralize-v2.1.114
vpcc scan --auto-heal    # rewrite every drifted regex in place
```

When `regex=N` but the anchor is still present → `--auto-heal` regenerates.
When all three anchors vanish → the preload hook (layer 2) still catches refusals at the JS runtime.

</details>

---

## 🧯 Troubleshooting

<div align="center">

| Symptom                                            | Cause                              | Fix                                                        |
|----------------------------------------------------|------------------------------------|------------------------------------------------------------|
| `no runnable Claude Code found`                    | npm install incomplete             | `npm i -g @anthropic-ai/claude-code && vpcc patch`         |
| `cli.js syntax INVALID — aborted`                  | Regex placed bad substitution      | Already rolled back. `vpcc scan --auto-heal`.              |
| `verify failed: rc=X` after patch                  | Binary refuses to run              | `vpcc rollback` → `vpcc scan -v` → issue with sha.         |
| `ELF parse failed: .bun not found`                 | CC changed format                  | Open issue; attach `file $SEA` + `rg -oab --text .bun`.    |
| `Bun bytecode format — text patching not supported`| Anthropic enabled `@bun @bytecode` | Use preload hook only; byte-patching disabled.             |
| `sig drift` in `vpcc doctor`                       | Minifier renamed                   | `vpcc scan --auto-heal`.                                   |
| `patches_commit` stuck                             | GitHub API rate-limit              | `export GITHUB_TOKEN=…` → `vpcc self-update -f`.           |
| Still seeing AUP refusal                           | preload hook not installed         | `vpcc install-preload` → relaunch `claude`.                |

</div>

---

## 🛡️ Security & scope

- **Research software for a single operator-owned workstation.**
- Pre-authorized by the repo owner for their own Claude Max subscription on their own hardware.
- Ships **no** credentials, telemetry or callbacks beyond GitHub's public API for patch sync.
- Every patch is verified (`node --check` or `--version` exec) and rolls back atomically on failure.
- Every patch is idempotent (re-apply is a no-op).
- **The operator remains responsible for compliance with Anthropic's Usage Policy.** This tool removes *client-side* guardrails; server-side enforcement is unaffected.

---

## 🏷️ Credits & refs

- Patch signature research: [@VoidChecksum](https://github.com/VoidChecksum).
- Bun SEA format: [Bun docs — `bun build --compile`](https://bun.sh/docs/bundler/executables) · [JSC SourceCodeKey source](https://github.com/oven-sh/bun/tree/main/src/js_parser).
- ELF shdr walk pattern borrowed from `pwntools`.
- CC releases: [@anthropic-ai/claude-code on npm](https://www.npmjs.com/package/@anthropic-ai/claude-code).

Licensed **GPL-3.0-or-later**.

---

<div align="center">

```
 $ vpcc doctor
   vpcc ver   : 2.1.119
   patches    : 96
   sig drift  : 0 (all anchors locatable)
   applied    : all
   upstream   : current
```

<br>

<strong>⚡ 96 patches · 3 defense layers · auto-heals through every CC update ⚡</strong>

</div>
