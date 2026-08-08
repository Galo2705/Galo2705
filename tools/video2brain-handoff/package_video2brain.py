#!/usr/bin/env python3
"""package_video2brain.py — build a clean, shareable ZIP of a video2brain install.

    - strips secrets, caches, virtualenvs, model weights, media and generated state
    - refuses to package if an API key or private key is still in the tree
    - derives .env.example from the variables the code actually reads
    - generates setup / operating / cloud docs and a CLAUDE.md from what it finds

Self-contained: one file, stdlib only. No rsync, no zip binary. Python 3.8+.
Runs on macOS and on a bare Ubuntu VPS.

    python3 package_video2brain.py                      # auto-detect the project
    python3 package_video2brain.py -s ~/video2brain     # explicit source
    python3 package_video2brain.py -s ~/video2brain -n  # dry run, write nothing
    python3 package_video2brain.py --help
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import date
from fnmatch import fnmatch
from pathlib import Path

# ============================================================ exclude rules ==

ALWAYS_EXCLUDE = [
    # version control internals
    ".svn/", ".hg/",
    # python
    "__pycache__/", "*.pyc", "*.pyo", "*.egg-info/", ".eggs/",
    ".venv/", "venv/", "env/", "ENV/", ".tox/", ".pytest_cache/",
    ".mypy_cache/", ".ruff_cache/", ".ipynb_checkpoints/", "htmlcov/", ".coverage",
    # node
    "node_modules/", ".next/", ".nuxt/", ".parcel-cache/", ".turbo/",
    # os / editor noise
    ".DS_Store", "Thumbs.db", "*.swp", "*~", ".idea/", ".vscode/",
    # secrets — never ship
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", ".netrc", ".npmrc", ".pypirc",
    "credentials.json", "client_secret*.json", "token.json", "service-account*.json",
    ".aws/", ".ssh/", ".gnupg/", "secrets/",
    ".claude/settings.local.json", ".claude/.credentials.json",
]

MODEL_EXCLUDE = [
    "*.bin", "*.gguf", "*.ggml", "*.pt", "*.pth", "*.onnx", "*.safetensors",
    "*.mlmodel", "*.mlmodelc/", "models/", "whisper.cpp/",
]

MEDIA_EXCLUDE = [
    "*.mp4", "*.mkv", "*.mov", "*.webm", "*.avi", "*.flv",
    "*.mp3", "*.wav", "*.m4a", "*.flac", "*.ogg", "*.opus", "*.aac",
]

STATE_EXCLUDE = [
    "*.db", "*.db-journal", "*.db-wal", "*.db-shm", "*.sqlite", "*.sqlite3",
    "*.log", "logs/", "log/", "cache/", ".cache/", "tmp/", "temp/",
    "out/", "output/", "outputs/", "downloads/", "dist/", "build/",
    "vault/", "vaults/", "transcripts/", "data/raw/", "data/processed/",
]

# Rescued even though a broader rule would drop them.
FORCE_INCLUDE = [".env.example", ".env.sample", ".env.template"]

# ========================================================= secret detection ==

HIGH_PATTERNS = [
    ("Anthropic API key",       r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    ("OpenAI API key",          r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}"),
    ("GitHub token",            r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    ("GitHub fine-grained PAT", r"\bgithub_pat_[A-Za-z0-9_]{40,}"),
    ("AWS access key id",       r"\bAKIA[0-9A-Z]{16}\b"),
    ("Slack token",             r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),
    ("Google API key",          r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ("Private key block",       r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    ("Telegram bot token",      r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b"),
    ("JWT",                     r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{8,}"),
    ("Stripe key",              r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{20,}"),
    ("HuggingFace token",       r"\bhf_[A-Za-z0-9]{30,}"),
]

MEDIUM_PATTERNS = [
    ("Hardcoded credential",
     r"(?i)\b(api[_-]?key|apikey|secret|password|passwd|token|access[_-]?key)\b"
     r"\s*[:=]\s*[\"'][^\"'\s]{12,}[\"']"),
    ("Bearer token", r"(?i)\bbearer\s+[A-Za-z0-9_\-\.]{24,}"),
    ("Credentials in URL",
     r"(?i)\b(?:postgres|postgresql|mysql|mongodb|redis|amqp|ftp)(?:\+\w+)?://[^:/\s]+:[^@\s]{4,}@"),
]

PLACEHOLDER_HINTS = (
    "your", "xxx", "changeme", "change_me", "placeholder", "example", "sample",
    "replace", "todo", "fixme", "dummy", "<", ">", "...", "none", "null",
    "abc123", "test", "fake", "redacted", "insert", "put_", "my_",
)

# Lines that read a secret from the environment are the correct pattern, not a leak.
ENV_READ_RX = re.compile(r"os\.getenv|os\.environ|process\.env|Deno\.env|\$\{|getenv\(|config\.get")

# ====================================================== environment scraping ==

ENV_REF_PATTERNS = [
    r"os\.environ\.get\(\s*[\"']([A-Z][A-Z0-9_]{2,})[\"']",
    r"os\.getenv\(\s*[\"']([A-Z][A-Z0-9_]{2,})[\"']",
    r"os\.environ\[\s*[\"']([A-Z][A-Z0-9_]{2,})[\"']\s*\]",
    r"process\.env\.([A-Z][A-Z0-9_]{2,})",
    r"process\.env\[\s*[\"']([A-Z][A-Z0-9_]{2,})[\"']\s*\]",
    r"Deno\.env\.get\(\s*[\"']([A-Z][A-Z0-9_]{2,})[\"']",
    r"\$\{([A-Z][A-Z0-9_]{2,})(?::[-=][^}]*)?\}",
]

SHELL_NOISE = {
    "PATH", "HOME", "USER", "PWD", "SHELL", "LANG", "LC_ALL", "TERM", "TMPDIR",
    "EDITOR", "PS1", "OLDPWD", "HOSTNAME", "LOGNAME", "SHLVL", "DISPLAY", "TZ",
    "PYTHONPATH", "VIRTUAL_ENV", "NODE_ENV", "IFS", "RANDOM", "UID", "PYTHONUNBUFFERED",
}

EXTERNAL_TOOLS = {
    "ffmpeg":         r"\bffmpeg\b",
    "ffprobe":        r"\bffprobe\b",
    "whisper.cpp":    r"whisper\.cpp|whisper-cli|whisper_cpp|ggml-(?:large|medium|small|base|tiny)",
    "faster-whisper": r"faster[_-]whisper",
    "openai-whisper": r"^\s*import whisper\b|openai-whisper",
    "whisperx":       r"whisperx",
    "pyannote":       r"pyannote",
    "yt-dlp":         r"yt[-_]dlp|youtube[-_]dl",
    "ollama":         r"\bollama\b",
    "sqlite":         r"\bsqlite3?\b",
    "redis":          r"\bredis\b",
    "obsidian":       r"\bobsidian\b",
}

TEXT_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".sh", ".bash", ".zsh",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".md", ".txt",
    ".html", ".css", ".sql", ".rb", ".go", ".rs", ".java", ".plist", ".service",
    ".xml", ".csv", ".tsv", ".example", ".sample", ".template", ".env", "",
}
MAX_SCAN_BYTES = 2 * 1024 * 1024

# ===================================================================== ui ====

_TTY = sys.stdout.isatty()
BOLD = "\033[1m" if _TTY else ""
DIM = "\033[2m" if _TTY else ""
RED = "\033[31m" if _TTY else ""
GREEN = "\033[32m" if _TTY else ""
YELLOW = "\033[33m" if _TTY else ""
OFF = "\033[0m" if _TTY else ""


def step(msg): print("\n%s==>%s %s%s%s" % (BOLD, OFF, BOLD, msg, OFF))
def ok(msg): print("  %s+%s %s" % (GREEN, OFF, msg))
def warn(msg): print("  %s!%s %s" % (YELLOW, OFF, msg))
def note(msg): print("  %s%s%s" % (DIM, msg, OFF))


def die(msg):
    print("\n%serror:%s %s" % (RED, OFF, msg), file=sys.stderr)
    sys.exit(1)


def human(size):
    size = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%dB" % size if unit == "B" else "%.1f%s" % (size, unit)
        size /= 1024
    return "%.1fGB" % size


# ============================================================== path filter ==

def rule_matches(pattern, parts, is_dir):
    """gitignore-ish matching against a path given as its relative components."""
    dir_only = pattern.endswith("/")
    pat = pattern.rstrip("/")
    if dir_only and not is_dir:
        # a directory rule still kills everything beneath it, which the walk
        # handles by pruning; a plain file can only match via its parents
        parts = parts[:-1]
        if not parts:
            return False
    if "/" in pat:                       # anchored to the project root
        rel = "/".join(parts)
        return fnmatch(rel, pat) or rel.startswith(pat + "/")
    return any(fnmatch(part, pat) for part in parts)


class Filter:
    def __init__(self, excludes, extra_includes):
        self.excludes = list(excludes)
        self.user_includes = list(extra_includes)
        self.includes = FORCE_INCLUDE + self.user_includes

    def verdict(self, parts, is_dir):
        """-> None to keep, or the pattern that dropped it."""
        for pat in self.includes:
            if rule_matches(pat, parts, is_dir):
                return None
        for pat in self.excludes:
            if rule_matches(pat, parts, is_dir):
                return pat
        return None

    def should_descend(self, parts):
        """This directory is excluded — could a --include still rescue something
        underneath it? If so the walk must not prune it; the per-file verdict
        still drops everything that no include matches.

        Only user includes count here. The built-in rescues are bare filenames
        that never justify walking into a .venv or a node_modules.
        """
        prefix = "/".join(parts) + "/"
        for pat in self.user_includes:
            if "/" not in pat.rstrip("/"):
                return True                       # basename rule, any depth
            if pat.startswith(prefix):
                return True
        return False


def build_excludes(opts):
    rules = list(ALWAYS_EXCLUDE)
    if not opts.keep_git:
        rules.append(".git/")
    if not opts.include_models:
        rules += MODEL_EXCLUDE
    if not opts.include_media:
        rules += MEDIA_EXCLUDE
    if not opts.include_data:
        rules += STATE_EXCLUDE
    return rules


# ================================================================= staging ===

def stage_project(src, stage, filt):
    """Copy src -> stage through the filter. Returns (kept, dropped, symlinks)."""
    kept = []
    dropped = {}          # pattern -> [count, bytes]
    symlinks = []

    def drop(pattern, size):
        row = dropped.setdefault(pattern, [0, 0])
        row[0] += 1
        row[1] += size

    def dir_size(path):
        total = count = 0
        for dp, _, fns in os.walk(path):
            for fn in fns:
                count += 1
                try:
                    total += os.lstat(os.path.join(dp, fn)).st_size
                except OSError:
                    pass
        return count, total

    for dirpath, dirnames, filenames in os.walk(src, topdown=True):
        rel_dir = os.path.relpath(dirpath, src)
        base_parts = [] if rel_dir == "." else rel_dir.split(os.sep)

        surviving = []
        for d in sorted(dirnames):
            full = os.path.join(dirpath, d)
            parts = base_parts + [d]
            if os.path.islink(full):
                symlinks.append("/".join(parts) + "/")
                continue
            pattern = filt.verdict(parts, True)
            if pattern and not filt.should_descend(parts):
                count, size = dir_size(full)
                row = dropped.setdefault(pattern, [0, 0])
                row[0] += count
                row[1] += size
                continue
            surviving.append(d)
        dirnames[:] = surviving

        for f in sorted(filenames):
            full = os.path.join(dirpath, f)
            parts = base_parts + [f]
            try:
                size = os.lstat(full).st_size
            except OSError:
                continue
            if os.path.islink(full):
                symlinks.append("/".join(parts))
                continue
            pattern = filt.verdict(parts, False)
            if pattern:
                drop(pattern, size)
                continue
            dest = os.path.join(stage, *parts)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            try:
                shutil.copy2(full, dest)
            except OSError as exc:
                warn("could not copy %s (%s)" % ("/".join(parts), exc))
                continue
            kept.append(("/".join(parts), size))

    return kept, dropped, symlinks


# ================================================================ scanning ===

def is_texty(path):
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return False
    try:
        if path.stat().st_size > MAX_SCAN_BYTES:
            return False
        with path.open("rb") as fh:
            return b"\x00" not in fh.read(4096)
    except OSError:
        return False


def iter_text_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "HANDOFF")]
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if is_texty(p):
                yield p


def read_text(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def looks_like_placeholder(value):
    low = value.lower()
    if any(h in low for h in PLACEHOLDER_HINTS):
        return True
    return len(set(value.strip("\"' "))) <= 3


def mask(value):
    v = value.strip()
    if len(v) <= 12:
        return v[:3] + "..."
    return "%s...%s (%d chars)" % (v[:6], v[-4:], len(v))


def scan_secrets(stage):
    findings = []
    rules = [("HIGH", n, re.compile(p)) for n, p in HIGH_PATTERNS]
    rules += [("MEDIUM", n, re.compile(p)) for n, p in MEDIUM_PATTERNS]

    for path in iter_text_files(stage):
        rel = path.relative_to(stage)
        if rel.name.endswith((".example", ".sample", ".template")):
            continue
        for lineno, line in enumerate(read_text(path).splitlines(), 1):
            if len(line) > 4000:
                continue
            stripped = line.lstrip()
            if stripped.startswith(("#", "//", "*")) and "BEGIN" not in stripped:
                continue
            if ENV_READ_RX.search(line):
                continue
            for severity, label, rx in rules:
                m = rx.search(line)
                if not m or looks_like_placeholder(m.group(0)):
                    continue
                findings.append({
                    "severity": severity, "label": label,
                    "file": str(rel), "line": lineno, "match": mask(m.group(0)),
                })
                break
    return findings


# ========================================================= env.example build ==

def collect_env_refs(stage):
    names = set()
    rules = [re.compile(p, re.M) for p in ENV_REF_PATTERNS]
    for path in iter_text_files(stage):
        text = read_text(path)
        for rx in rules:
            for m in rx.finditer(text):
                if m.group(1) not in SHELL_NOISE:
                    names.add(m.group(1))
    return names


def collect_env_keys(src):
    """Variable NAMES (never values) from the real .env files left in the source."""
    keys = set()
    candidates = list(Path(src).glob(".env*"))
    for sub in Path(src).iterdir() if Path(src).is_dir() else []:
        if sub.is_dir() and not sub.name.startswith("."):
            candidates += list(sub.glob(".env*"))
    for cand in candidates:
        if cand.name.endswith((".example", ".sample", ".template")) or not cand.is_file():
            continue
        for line in read_text(cand).splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.partition("=")[0].replace("export ", "").strip()
            if re.match(r"^[A-Z][A-Z0-9_]*$", key):
                keys.add(key)
    return keys


def write_env_example(stage, src):
    from_code = collect_env_refs(stage)
    from_env = collect_env_keys(src)
    names = sorted(from_code | from_env)
    if not names:
        return None, []

    lines = [
        "# Environment for video2brain - copy this to .env and fill in the values.",
        "#",
        "# Generated by package_video2brain.py. Names come from the code and from",
        "# the .env on the source machine; every value was left blank on purpose.",
        "",
    ]
    for name in names:
        origin = []
        if name in from_env:
            origin.append("set on the source machine")
        if name in from_code:
            origin.append("read by the code")
        lines.append("# " + " / ".join(origin))
        lines.append(name + "=")
        lines.append("")

    existing = Path(stage) / ".env.example"
    target = Path(stage) / ".env.example.generated" if existing.exists() else existing
    target.write_text("\n".join(lines), encoding="utf-8")
    return target, names


# ================================================================ detection ==

def detect(stage):
    stage = Path(stage)
    facts = {
        "python": False, "node": False, "docker": False, "python_version": None,
        "commands": [], "entrypoints": [], "tools": [], "has_tests": False,
    }

    if any((stage / n).exists() for n in ("requirements.txt", "pyproject.toml", "setup.py")) \
            or any(stage.rglob("*.py")):
        facts["python"] = True
    if (stage / "package.json").exists():
        facts["node"] = True
    if any((stage / n).exists() for n in ("Dockerfile", "docker-compose.yml", "compose.yaml")):
        facts["docker"] = True

    pyproject = stage / "pyproject.toml"
    if pyproject.exists():
        text = read_text(pyproject)
        m = re.search(r"requires-python\s*=\s*[\"']([^\"']+)", text)
        if m:
            facts["python_version"] = m.group(1)
        block = re.search(r"\[project\.scripts\](.*?)(?:\n\[|\Z)", text, re.S)
        if block:
            for m in re.finditer(r"^\s*([A-Za-z0-9_.\-]+)\s*=", block.group(1), re.M):
                facts["commands"].append((m.group(1), "pyproject console script"))

    makefile = stage / "Makefile"
    if makefile.exists():
        for m in re.finditer(r"^([a-zA-Z0-9_.\-]+):(?!=)", read_text(makefile), re.M):
            if m.group(1) not in (".PHONY", ".DEFAULT_GOAL", ".SILENT"):
                facts["commands"].append(("make " + m.group(1), "Makefile"))

    pkg = stage / "package.json"
    if pkg.exists():
        block = re.search(r"\"scripts\"\s*:\s*\{(.*?)\}", read_text(pkg), re.S)
        if block:
            for m in re.finditer(r"\"([^\"]+)\"\s*:", block.group(1)):
                facts["commands"].append(("npm run " + m.group(1), "package.json"))

    for sh in sorted(stage.glob("*.sh")):
        facts["commands"].append(("./" + sh.name, "shell script"))

    mains = []
    for path in sorted(stage.rglob("*.py")):
        rel = path.relative_to(stage)
        if len(rel.parts) > 3:
            continue
        if re.search(r"^if __name__ == [\"']__main__[\"']", read_text(path), re.M):
            mains.append(rel.as_posix())
    facts["entrypoints"] = mains[:12]

    blob, budget = [], 8_000_000
    for path in iter_text_files(stage):
        chunk = read_text(path)
        blob.append(chunk)
        budget -= len(chunk)
        if budget <= 0:
            break
    joined = "\n".join(blob)
    for tool, rx in EXTERNAL_TOOLS.items():
        if re.search(rx, joined, re.I | re.M):
            facts["tools"].append(tool)

    facts["has_tests"] = (stage / "tests").is_dir() or bool(next(stage.rglob("test_*.py"), None))
    return facts


def tree_summary(stage, max_depth=2):
    lines = []
    stage = str(stage)
    for dirpath, dirnames, _ in os.walk(stage):
        rel = os.path.relpath(dirpath, stage)
        depth = 0 if rel == "." else len(rel.split(os.sep))
        if depth > max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(dirnames)
        count = total = 0
        for dp, _, fns in os.walk(dirpath):
            for fn in fns:
                count += 1
                try:
                    total += os.lstat(os.path.join(dp, fn)).st_size
                except OSError:
                    pass
        label = "." if rel == "." else rel.split(os.sep)[-1] + "/"
        lines.append("%s%s  - %d files, %s" % ("  " * depth, label, count, human(total)))
        if depth == max_depth:
            dirnames[:] = []
    return lines[:80]


# ============================================================ doc generation ==

def bullets(items, empty="-"):
    return "\n".join("- `%s`" % i for i in items) if items else empty


def write_docs(stage, src, facts, env_names, dropped, symlinks, name, opts):
    stage = Path(stage)
    docs = stage / "HANDOFF"
    docs.mkdir(exist_ok=True)
    today = date.today().isoformat()

    tools = facts["tools"]
    needs_ffmpeg = "ffmpeg" in tools or "ffprobe" in tools
    needs_whispercpp = "whisper.cpp" in tools
    needs_ollama = "ollama" in tools
    uniq_cmds = list(dict.fromkeys(facts["commands"]))

    # ------------------------------------------------------- START_HERE.md ---
    (stage / "START_HERE.md").write_text("""# %(name)s - התחל כאן

חבילה נקייה של **%(name)s**, נארזה ב-%(today)s.

זה pipeline של וידאו ← תמלול ← כספת ידע. בחבילה יש את הקוד וההגדרות בלבד:
**אין סודות, מודלים, מדיה, בסיסי נתונים או כספות** - הכל נשלף לפני האריזה.

## שלושה צעדים

1. **התקנה** - `HANDOFF/SETUP.md`
2. **מפתחות** - העתק `.env.example` ל-`.env` ומלא ערכים
3. **הפעלה** - `HANDOFF/OPERATING.md`

להרצה על VPS חדש: `HANDOFF/CLOUD.md`.
פותח בסשן Claude Code חדש? יש `CLAUDE.md` בשורש, הוא נטען לבד.

## מה הוסר

| מה | למה |
|:--|:--|
| `.env`, מפתחות, `*.pem` | סודות לא נשלחים. השמות נשמרו ב-`.env.example`, הערכים לא |
| משקולות מודלים (`*.bin`, `*.gguf`, `models/`) | כבדים, ומורידים אותם מחדש בהתקנה |
| קבצי אודיו ווידאו | תוכן, לא קוד |
| בסיסי נתונים, כספות, לוגים, `out/` | מצב ריצה - נבנה מחדש בהרצה הראשונה |
| היסטוריית `.git` | קומיטים ישנים הם מקום נפוץ לסודות שנשכחו |

הפירוט המלא: `HANDOFF/MANIFEST.md`.
""" % {"name": name, "today": today}, encoding="utf-8")

    # ------------------------------------------------------------ SETUP.md ---
    mac_pkgs, deb_pkgs = [], []
    if needs_ffmpeg:
        mac_pkgs.append("ffmpeg"); deb_pkgs.append("ffmpeg")
    if facts["python"]:
        mac_pkgs.append("python@3.11"); deb_pkgs.append("python3 python3-venv python3-pip")
    if facts["node"]:
        mac_pkgs.append("node"); deb_pkgs.append("nodejs npm")
    if needs_whispercpp:
        mac_pkgs.append("cmake"); deb_pkgs.append("build-essential cmake git")
    if "yt-dlp" in tools:
        # deliberately not from apt: the packaged yt-dlp goes stale fast and
        # breaks against site changes. pip, inside the venv, below.
        mac_pkgs.append("yt-dlp")
    mac_line = " ".join(mac_pkgs) or "python@3.11"
    deb_line = " ".join(deb_pkgs) or "python3 python3-venv"

    parts = ["""# התקנה - %s

נבדק על macOS ועל Ubuntu 22.04/24.04. כל הפקודות מתוך תיקיית הפרויקט.

## 1. תלויות מערכת

**macOS**
```bash
brew install %s
```

**Ubuntu / Debian**
```bash
sudo apt update && sudo apt install -y %s
```
""" % (name, mac_line, deb_line)]

    if facts["python"]:
        if (stage / "requirements.txt").exists():
            install = "pip install -r requirements.txt"
        elif (stage / "pyproject.toml").exists() or (stage / "setup.py").exists():
            install = "pip install -e ."
        else:
            install = "# no requirements.txt or pyproject.toml - install dependencies manually"
        parts.append("""## 2. סביבת Python  (נדרש %s)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
%s%s
```
""" % (facts["python_version"] or "3.10+", install,
       "\npip install -U yt-dlp        # apt's build goes stale and breaks" if "yt-dlp" in tools else ""))

    if facts["node"]:
        parts.append("## תלויות Node\n\n```bash\nnpm install\n```\n")

    if needs_whispercpp:
        parts.append("""## whisper.cpp ומודל

הקוד מריץ whisper.cpp. הבינארי והמשקולות לא בחבילה - בנה והורד:

```bash
git clone https://github.com/ggml-org/whisper.cpp
cd whisper.cpp && cmake -B build && cmake --build build -j --config Release
bash ./models/download-ggml-model.sh large-v3-turbo    # ~1.6GB, האיכותי-מהיר
cd ..
```

בחירת מודל: `large-v3` הכי מדויק אבל איטי · `large-v3-turbo` כמעט אותה איכות
ופי כמה מהר · `medium` (~1.5GB) פשרה · `small` (~0.5GB) לבדיקות בלבד.

בדיקה:

```bash
./whisper.cpp/build/bin/whisper-cli \\
  -m whisper.cpp/models/ggml-large-v3-turbo.bin -f sample.wav -l he
```

לעברית העבר `-l he` במפורש. זיהוי שפה אוטומטי (`-l auto`) נוטה לטעות על
קטעים קצרים ועל עברית מהולה באנגלית. עדכן את הנתיב לבינארי ולמודל ב-`.env`.
""")

    if needs_ollama:
        parts.append("""## Ollama (מודל מקומי לשכבת הידע)

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.1:8b        # או המודל שמוגדר ב-.env
ollama serve &
```
""")

    if facts["has_tests"]:
        verify = "pytest -q"
    elif any(c == "make test" for c, _ in uniq_cmds):
        verify = "make test"
    else:
        verify = "# אין חבילת בדיקות - הרץ קובץ דוגמה קצר לפי HANDOFF/OPERATING.md"

    parts.append("""## 3. משתני סביבה

```bash
cp .env.example .env
$EDITOR .env
```

זוהו %d משתנים. אלה שהקוד קורא בפועל:

%s

## 4. בדיקה

```bash
%s
```
""" % (len(env_names), bullets(list(env_names)[:40]), verify))

    (docs / "SETUP.md").write_text("\n".join(parts), encoding="utf-8")

    # -------------------------------------------------------- OPERATING.md ---
    cmd_rows = "\n".join("| `%s` | %s |" % (c, o) for c, o in uniq_cmds) \
        or "| - | לא זוהו פקודות מוגדרות, ראה נקודות כניסה למטה |"
    transcriber = " / ".join(t for t in tools if "whisper" in t) or "מנוע התמלול המוגדר"
    audio_step = ("ffmpeg ל-WAV 16kHz mono, מה ש-whisper מצפה לו" if needs_ffmpeg
                  else "המרה לפורמט שהמתמלל מצפה לו")

    (docs / "OPERATING.md").write_text("""# הוראות הפעלה - %(name)s

## פקודות שזוהו

| פקודה | מקור |
|:--|:--|
%(cmds)s

## נקודות כניסה (קבצי python עם `__main__`)

%(entry)s

## כלים חיצוניים שהקוד מסתמך עליהם

%(tools)s

## מחזור הריצה

1. **קלט** - קובץ וידאו או אודיו%(dl)s
2. **חילוץ אודיו** - %(audio)s
3. **תמלול** - %(tr)s, עם חותמות זמן
4. **בניית ידע** - הטקסט נחתך לטענות, כל טענה נושאת את ציטוט המקור שלה
5. **פלט** - כספת מסודרת עם ציון QA

## איפה הפלט

תיקיות הפלט לא נכללו בחבילה, הן מצב ריצה. אחרי הרצה ראשונה הן נוצרות לפי
הנתיבים ב-`.env` - חפש שם משתנים כמו `*_DIR`, `*_PATH`, `VAULT*`, `OUTPUT*`.

## תקלות נפוצות

| תסמין | סיבה סבירה |
|:--|:--|
| `ffmpeg: command not found` | דילגת על תלויות המערכת ב-SETUP.md |
| `model file not found` | המשקולות לא בחבילה, הורד לפי SETUP.md |
| תמלול ריק או ג'יבריש | שפה שגויה. העבר `-l he` לעברית במקום זיהוי אוטומטי |
| `KeyError` על משתנה סביבה | חסר שדה ב-`.env`. השווה מול `.env.example` |
| איטי מאוד | מודל `large` על CPU. עבור ל-`large-v3-turbo`, או `small` לבדיקות |
| התהליך נהרג בלי הודעה | OOM. המודל גדול מדי ל-RAM שיש, רד בגודל |
| חותמות זמן זזות לאורך קובץ ארוך | קלט VBR. המר ל-WAV קבוע לפני התמלול |
""" % {
        "name": name, "cmds": cmd_rows,
        "entry": bullets(facts["entrypoints"]),
        "tools": bullets(tools),
        "dl": ", או קישור (yt-dlp בשימוש)" if "yt-dlp" in tools else "",
        "audio": audio_step, "tr": transcriber,
    }, encoding="utf-8")

    # ------------------------------------------------------------ CLOUD.md ---
    if facts["entrypoints"]:
        module = facts["entrypoints"][0][:-3].replace("/", ".") \
            if facts["entrypoints"][0].endswith(".py") else facts["entrypoints"][0]
        exec_start = "/home/v2b/%s/.venv/bin/python -m %s" % (name, module)
    else:
        exec_start = "/home/v2b/%s/.venv/bin/python -m CHANGE_ME" % name

    (docs / "CLOUD.md").write_text("""# הרצה על ענן חדש - %(name)s

## א. אבטחת השרת, לפני הכל

VPS טרי עם אימות סיסמה נסרק תוך דקות. סדר הפעולות:

```bash
# מהמחשב שלך
ssh-keygen -t ed25519 -C "$(whoami)@$(hostname)"
ssh-copy-id root@SERVER_IP

# על השרת
adduser v2b && usermod -aG sudo v2b
sudo sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo sed -i 's/^#\\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
sudo systemctl restart ssh
sudo apt install -y fail2ban
sudo ufw allow OpenSSH && sudo ufw enable
```

אל תדלג על זה, ואל תשלח סיסמאות שרת בצ'אט או במייל.

## ב. התקנה

```bash
sudo apt update && sudo apt install -y %(deb)s unzip
unzip %(name)s-handoff-*.zip && cd %(name)s
```

משם לפי `HANDOFF/SETUP.md`. שני דברים שנופלים עליהם בענן:

- **דיסק** - מודל `large` תופס ~1.6GB, ואודיו מצטבר. ודא 20GB+ פנויים (`df -h`)
- **RAM** - 8GB כדי לעבוד בנוח עם `large`. עם 4GB רד ל-`small`/`medium`,
  אחרת ה-OOM killer יהרוג את התהליך באמצע בלי הודעה ברורה

## ג. ריצה מתמשכת (systemd)

`/etc/systemd/system/%(name)s.service`:

```ini
[Unit]
Description=%(name)s - video to knowledge pipeline
After=network-online.target

[Service]
Type=simple
User=v2b
WorkingDirectory=/home/v2b/%(name)s
EnvironmentFile=/home/v2b/%(name)s/.env
ExecStart=%(exec)s
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now %(name)s
journalctl -u %(name)s -f      # לוגים חיים
```

לעיבוד מתוזמן במקום דמון שרץ תמיד, החלף ב-systemd timer או ב-cron.

## ד. פתיחה בסשן Claude Code חדש

יש `CLAUDE.md` בשורש, כל סשן חדש טוען אותו לבד.

**בענן** - דחוף לריפו פרטי, ואז [claude.ai/code](https://claude.ai/code) ובחר אותו:

```bash
git init && git add -A && git commit -m "Import %(name)s"
git remote add origin git@github.com:USER/%(name)s.git
git push -u origin main
```

ודא ש-`.gitignore` חוסם `.env`, `*.bin`, `*.gguf`, `out/`, `vault*/` **לפני**
הדחיפה. הריפו חייב להיות פרטי.

**על השרת עצמו**:

```bash
npm install -g @anthropic-ai/claude-code
cd ~/%(name)s && claude
```
""" % {"name": name, "deb": deb_line, "exec": exec_start}, encoding="utf-8")

    # --------------------------------------------------------- MANIFEST.md ---
    drop_rows = sorted(dropped.items(), key=lambda kv: kv[1][1], reverse=True)[:40]
    drop_table = "\n".join("| `%s` | %d | %s |" % (pat, c, human(b))
                           for pat, (c, b) in drop_rows) or "| - | - | - |"
    sym_block = bullets(symlinks[:20], "אין") if symlinks else "אין"

    (docs / "MANIFEST.md").write_text("""# מניפסט - %(name)s

נארז ב-%(today)s.

## מבנה החבילה

```
%(tree)s
```

## מה הושמט מהמקור

| כלל | קבצים | גודל |
|:--|--:|--:|
%(drops)s

## סימבוליים שלא הועתקו

%(syms)s

## מה שזוהה

- Python: %(py)s
- Node: %(node)s
- Docker: %(docker)s
- בדיקות: %(tests)s
- כלים חיצוניים: %(tools)s
- משתני סביבה: %(envs)d

## הגדרות האריזה

- היסטוריית git: %(git)s
- מודלים: %(models)s
- מדיה: %(media)s
- מצב ריצה: %(state)s
""" % {
        # the tree is substituted at the very end, once every file exists
        "name": name, "today": today, "tree": "@@TREE@@",
        "drops": drop_table, "syms": sym_block,
        "py": ("כן" + (" (דורש %s)" % facts["python_version"] if facts["python_version"] else "")) if facts["python"] else "לא",
        "node": "כן" if facts["node"] else "לא",
        "docker": "כן" if facts["docker"] else "לא",
        "tests": "כן" if facts["has_tests"] else "לא",
        "tools": ", ".join(tools) or "-",
        "envs": len(env_names),
        "git": "נשמרה" if opts.keep_git else "הושמטה",
        "models": "נכללו" if opts.include_models else "הושמטו",
        "media": "נכללה" if opts.include_media else "הושמטה",
        "state": "נכלל" if opts.include_data else "הושמט",
    }, encoding="utf-8")

    # ------------------------------------------------------------ CLAUDE.md --
    stack_bits = []
    if facts["python"]:
        stack_bits.append("Python" + (" " + facts["python_version"] if facts["python_version"] else ""))
    if facts["node"]:
        stack_bits.append("Node")
    if facts["docker"]:
        stack_bits.append("Docker")

    claude_md = """# %(name)s

Video to transcription to knowledge-vault pipeline. This copy is a handoff
package: secrets, model weights, media and generated state were stripped
before it was zipped, so it will not run until it is set up.

## Stack

- %(stack)s
- External tools: %(tools)s

## Commands

%(cmds)s

## Entry points

%(entry)s

## Before running anything

`.env` does not exist here, only `.env.example`. Copy and fill it: a missing key
surfaces as a `KeyError` deep inside a pipeline stage, not as a clean startup
error. Model weights are absent too - `HANDOFF/SETUP.md` has the downloads.

## Conventions

- Never commit `.env`, `*.bin`, `*.gguf`, vault directories or output directories.
- Every extracted claim carries its exact source quote. Do not introduce a claim
  that cannot point back at a timestamped line of the transcript.
- Missing data renders as missing. Do not substitute a plausible value.
- Hebrew audio needs the language passed explicitly; autodetect misfires on short
  clips and on Hebrew mixed with English.

## Docs

- `HANDOFF/SETUP.md` - install from scratch
- `HANDOFF/OPERATING.md` - how to run it, and what breaks
- `HANDOFF/CLOUD.md` - fresh VPS, systemd, new Claude session
- `HANDOFF/MANIFEST.md` - what is and is not in this package
""" % {
        "name": name,
        "stack": " / ".join(stack_bits) or "plain files",
        "tools": ", ".join(tools) or "none detected",
        "cmds": "\n".join("- `%s` (%s)" % (c, o) for c, o in uniq_cmds)
                or "- none defined; see entry points below",
        "entry": bullets(facts["entrypoints"]),
    }

    if (stage / "CLAUDE.md").exists():
        (docs / "CLAUDE.suggested.md").write_text(claude_md, encoding="utf-8")
    else:
        (stage / "CLAUDE.md").write_text(claude_md, encoding="utf-8")

    if not (stage / ".gitignore").exists():
        (stage / ".gitignore").write_text(
            ".env\n.env.*\n!.env.example\n*.bin\n*.gguf\n*.pt\n*.safetensors\n"
            "models/\nwhisper.cpp/\n__pycache__/\n*.pyc\n.venv/\nnode_modules/\n"
            "*.db\n*.sqlite3\n*.log\nlogs/\nout/\noutput/\nvault/\nvaults/\n"
            "transcripts/\n.DS_Store\n", encoding="utf-8")

    # Every file now exists, so the tree in the manifest can count itself.
    manifest = docs / "MANIFEST.md"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("@@TREE@@", "\n".join(tree_summary(stage))),
        encoding="utf-8")

    return docs


# ================================================================== source ===

SEARCH_DIRS = [
    "video2brain", "Projects/video2brain", "projects/video2brain", "dev/video2brain",
    "code/video2brain", "src/video2brain", "Developer/video2brain", "Documents/video2brain",
]


def autodetect_source():
    home = Path.home()
    found = [home / d for d in SEARCH_DIRS if (home / d).is_dir()]
    found += [Path(p) for p in ("/opt/video2brain", "/srv/video2brain", "/root/video2brain")
              if Path(p).is_dir()]
    if found:
        return found

    skip = {"Library", "node_modules", ".git", ".Trash", ".cache", "Applications"}
    for root, dirnames, _ in os.walk(home):
        depth = len(Path(root).relative_to(home).parts)
        if depth >= 4:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in skip]
        for d in list(dirnames):
            if d.lower().startswith("video2brain"):
                found.append(Path(root) / d)
        if len(found) >= 10:
            break
    return found


# ==================================================================== main ===

def build_parser():
    p = argparse.ArgumentParser(
        prog="package_video2brain.py",
        description="Build a clean, shareable ZIP of a video2brain installation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-s", "--src", help="project directory (auto-detected if omitted)")
    p.add_argument("-o", "--out", help="where to write the ZIP (default: ~/Desktop, else cwd)")
    p.add_argument("-N", "--name", help="name inside the ZIP (default: source directory name)")
    p.add_argument("-i", "--include", action="append", default=[], metavar="PATTERN",
                   help="re-include something the excludes would drop (repeatable)")
    p.add_argument("--keep-git", action="store_true",
                   help="keep .git (off by default: history often holds old secrets)")
    p.add_argument("--include-models", action="store_true", help="keep model weights")
    p.add_argument("--include-media", action="store_true", help="keep audio and video files")
    p.add_argument("--include-data", action="store_true",
                   help="keep databases, vaults, logs and output directories")
    p.add_argument("--allow-secrets", action="store_true",
                   help="package even if the secret scan finds something")
    p.add_argument("-n", "--dry-run", action="store_true", help="report only, write nothing")
    return p


def main(argv=None):
    opts = build_parser().parse_args(argv)

    # ------------------------------------------------------------- locate ---
    step("Locating the project")
    if opts.src:
        src = Path(opts.src).expanduser()
        if not src.is_dir():
            die("not a directory: %s" % src)
    else:
        found = autodetect_source()
        if not found:
            die("could not find a video2brain directory.\n"
                "       pass one explicitly:  python3 package_video2brain.py -s /path/to/video2brain")
        if len(found) > 1:
            print("Several candidates found - pick one with -s:")
            for f in found:
                print("    %s" % f)
            die("ambiguous source directory")
        src = found[0]

    src = src.resolve()
    if src == Path("/") or src == Path.home():
        die("refusing to package %s - point -s at the project directory itself" % src)
    ok("source: %s" % src)

    name = opts.name or src.name
    out_dir = Path(opts.out).expanduser() if opts.out else \
        (Path.home() / "Desktop" if (Path.home() / "Desktop").is_dir() else Path.cwd())
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir = out_dir.resolve()

    zip_base = "%s-handoff-%s" % (name, date.today().strftime("%Y%m%d"))
    zip_path = out_dir / (zip_base + ".zip")

    # -------------------------------------------------------------- stage ---
    tmp = Path(tempfile.mkdtemp(prefix="v2b-handoff-"))
    try:
        stage = tmp / zip_base / name
        stage.mkdir(parents=True)

        step("Staging a clean copy")
        filt = Filter(build_excludes(opts), opts.include)
        kept, dropped, symlinks = stage_project(str(src), str(stage), filt)
        if not kept:
            die("nothing survived the exclude list - check -s, or re-include with -i")
        dropped_files = sum(c for c, _ in dropped.values())
        dropped_bytes = sum(b for _, b in dropped.values())
        ok("%d files kept (%s)" % (len(kept), human(sum(s for _, s in kept))))
        ok("%d files dropped (%s)" % (dropped_files, human(dropped_bytes)))
        if symlinks:
            warn("%d symlink(s) not followed - listed in HANDOFF/MANIFEST.md" % len(symlinks))

        # ------------------------------------------------------ secrets ---
        step("Scanning for secrets")
        findings = scan_secrets(stage)
        high = [f for f in findings if f["severity"] == "HIGH"]
        if findings:
            warn("%d high, %d medium" % (len(high), len(findings) - len(high)))
            for f in findings[:25]:
                print("      [%-6s] %s:%d  %s  %s"
                      % (f["severity"], f["file"], f["line"], f["label"], f["match"]))
            if len(findings) > 25:
                note("... and %d more" % (len(findings) - 25))
            if high and not opts.allow_secrets:
                die("secrets found in the tree - nothing was packaged.\n"
                    "       remove them at the source, or re-run with --allow-secrets.")
        else:
            ok("clean")

        # --------------------------------------------------------- docs ---
        step("Generating documentation")
        env_path, env_names = write_env_example(stage, src)
        if env_path:
            ok("%s: %d variables, all values blank" % (env_path.name, len(env_names)))
        else:
            note("no environment variables detected")

        facts = detect(stage)
        kinds = [k for k, on in (("python", facts["python"]), ("node", facts["node"]),
                                 ("docker", facts["docker"])) if on]
        ok("detected: %s / %d commands / %d external tools"
           % (", ".join(kinds) or "plain files", len(facts["commands"]), len(facts["tools"])))
        had_claude_md = (stage / "CLAUDE.md").exists()
        write_docs(stage, src, facts, env_names, dropped, symlinks, name, opts)
        ok("START_HERE.md, HANDOFF/{SETUP,OPERATING,CLOUD,MANIFEST}.md")
        if had_claude_md:
            note("kept the project's own CLAUDE.md; mine is HANDOFF/CLAUDE.suggested.md")
        else:
            ok("CLAUDE.md written for a fresh Claude Code session")

        # ---------------------------------------------------------- zip ---
        if opts.dry_run:
            step("Dry run - nothing written")
            note("would have produced: %s" % zip_path)
            for rel, _ in kept[:60]:
                print("      %s/%s" % (name, rel))
            if len(kept) > 60:
                note("... and %d more files" % (len(kept) - 60))
            return 0

        step("Building the archive")
        if zip_path.exists():
            backup = zip_path.with_suffix(".zip.bak")
            zip_path.replace(backup)
            warn("previous archive moved to %s" % backup.name)

        root = tmp / zip_base
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames.sort()
                for fn in sorted(filenames):
                    full = Path(dirpath) / fn
                    zf.write(full, full.relative_to(tmp).as_posix())

        import hashlib
        digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()

        step("Done")
        ok("archive: %s" % zip_path)
        ok("size:    %s" % human(zip_path.stat().st_size))
        ok("sha256:  %s" % digest)
        print("""
  Before you send it, open the ZIP and confirm:
    - no .env  (only .env.example, values blank)
    - no *.bin / *.gguf / *.mp4
    - START_HERE.md, CLAUDE.md and HANDOFF/ are present

  Whoever receives it starts at START_HERE.md.
""")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
