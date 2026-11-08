<div align="center">

# transcript-lens · 成绩单透镜

**A local desktop tool for university counselors — grades, moral-education scores, scholarships, and student profiles.**

Import the spreadsheet your registrar exports, compute weighted averages under three
scopes, rank scholarships, build semester archives, and let a local GGUF model draft
student profiles and conversation notes. Everything runs offline on this machine.

</div>

## Why it exists

Counselors juggle three things: grade exports, a hand-maintained moral-education sheet,
and a pile of rankings nobody can reproduce. Every scholarship round means rebuilding
the same tables by hand.

transcript-lens collapses that into one desktop app: **import once, then every statistic
comes out of an encrypted local store** with fixed, reproducible definitions.

## Features

**Academic analysis**
- Reads `.xlsx` / `.xls`; the engine is picked from the file's magic bytes, so "fake xlsx"
  exports from registrars still load
- Three-step manual confirmation (category, credits, anchor check) before anything is written
- Three weighting scopes at any time: required only / required + limited-elective / all
- Per-row warnings: failed courses, absences, out-of-range scores, credit anomalies

**Student coaching**
- Roster, stated intentions (postgraduate recommendation / exam / employment / civil service / abroad),
  and a rule-based coaching outline
- **Local AI chat**: the student's grades, moral score and profile are injected as context;
  streaming answers from an on-device model

**Semester archive**
- Accumulated history per semester with visible deltas
- Delete individual records (grade / moral / profile), with a follow-up prompt when a
  semester becomes empty
- Per-semester AI profiles that can be edited and saved

**Scholarships**
- `final = weighted average (all categories) × 0.7 + moral score × 0.3`
- Per-class quotas for 1st/2nd/3rd prize; a special prize (top X and every course ≥ 90)
  that does not consume a quota and is not stacked by default
- A missing moral score counts as 0 **and is labelled as such** — never silently faked

**Model center**
- GGUF catalog pulled from Gitee, annotated **easy / tight / hard** for this machine's
  RAM and VRAM
- One-click download (resume + SHA256), install, uninstall; failed downloads fall back to
  "copy link"

**Diagnostics**
- Every critical error dialog has a **Copy error info** button that puts the full
  traceback, log path, and log tail on the clipboard

## Privacy and security

| | |
|---|---|
| Where data lives | `%LOCALAPPDATA%\SGMS\` — never in the repo or next to the exe |
| On disk | only `.enc` blobs; **plaintext grades never touch the disk** |
| Crypto | two layers: password-level (grades) and app-level (moral scores, archives, profiles, chat) |
| Identity | always keyed by student ID (`sid:<id>`) — same-name students never collide |
| Network | only four paths: password-recovery SMTP, update check, and user-confirmed downloads (exe / model) |
| LLM | on-device GGUF only; no cloud API, works fully offline |

No real grade sheets or credentials are in this repository — test data is limited to
`tests/fixtures/fake_*.xlsx`.

## Quick start

**Download a build** from [Releases](https://gitee.com/wuhaotiansystem/transcript-lens/releases).
Large builds are split into parts; concatenate them on Windows:

```bat
copy /b transcript-lens-v1.9.3.exe.part1+transcript-lens-v1.9.3.exe.part2+transcript-lens-v1.9.3.exe.part3 transcript-lens.exe
```

> The three parts concatenate to **240,408,108 bytes** (~229 MB). Just double-click the
> result: on first launch it migrates data from the previous version automatically.

**From source** (Windows 10/11, Python 3.13):

```bat
git clone https://gitee.com/wuhaotiansystem/transcript-lens.git
cd transcript-lens
pip install -r requirements.txt
python main.py
```

Optional local inference engine (everything else works without it):

```bat
pip install llama-cpp-python
```

or use **Model center → install inference backend**. Put GGUF files under
`%LOCALAPPDATA%\SGMS\models\<name>\*.gguf` and the model center picks them up.

**Build it yourself**

```bat
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_exe.ps1 -Both
```

Outputs land in `dist\`: `transcript-lens.exe` (single file) and
`transcript-lens-debug\` (onedir with a console).

A single-file build must unpack itself into a temp folder first; if security software
blocks that step you get `[PYI-xxxx:ERROR] Could not create temporary directory!` —
use the onedir build or `set TEMP=<writable dir>` before launching.

## Workflow

1. **Academic analysis** → pick semester and class, import the grade sheet
2. Confirm categories and credits → anchor check → save
3. Review weighted scores and warnings; export
4. **Scholarships** → import moral scores (bulk supported) → quotas → results
5. **Coaching / Archive** → intentions, outline, AI profile and conversation
6. **Settings** → updates, GPA scheme, delete the grade store (with inventory and double confirmation)

## Layout

```
core/        parsing, validation, weighting, encrypted storage, logical-table API, updater, inference
ui/          PySide6 interface (frameless windows, pages, dialogs, stylesheet)
ai/          local templates and a disabled cloud adapter skeleton
tests/       unit tests and fake fixtures (887+ cases)
tools/       release helpers (part splitting)
update/      version and model manifests (read from Gitee raw)
docs/        developer handbook, update guide, distribution and release SOP
main.py      entry point (GUI / --selfcheck / --bootcheck / --mailcheck)
```

## Development

```bat
python -m pytest tests -q
python main.py --selfcheck
dist\transcript-lens.exe --bootcheck
```

Read `AGENTS.md` (38 sections of hard rules) and `docs/DEV_SUMMARY.md` before changing code.
The version lives only in `core/version.py`; bumping it requires adding a migration hop in
`core/migrator.py`.

## Known limitations

- GPA implements **scheme 1 (standard 4.0 table)** only; other schemes are placeholders
- A missing moral score counts as 0 (and is labelled); empty cells are not 0
- Blank score ≠ 0: unselected courses stay out of the denominator, an absent mark `0` counts
- Scores above 100 drop out of the weighted denominator (still listed) — the UI forces confirmation
- The 1.5 s welcome screen is a documented value, not a stall

## License

[MIT](LICENSE) © 2026 wuhaotian
