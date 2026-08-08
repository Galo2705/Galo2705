# video2brain handoff packager

Packages a running `video2brain` install into a clean ZIP you can send to
someone else: code and configuration only, with setup and operating docs
generated from what the project actually contains.

Single file, stdlib only. No `rsync`, no `zip` binary, no pip install.
Python 3.8+, macOS and Linux.

## Run it

```bash
python3 package_video2brain.py -s ~/video2brain
```

The archive lands on `~/Desktop` (or the working directory if there is no
Desktop) as `video2brain-handoff-YYYYMMDD.zip`.

Check what it would do first:

```bash
python3 package_video2brain.py -s ~/video2brain --dry-run
```

## What it strips

| Category | Examples |
|:--|:--|
| Secrets | `.env`, `*.pem`, `*.key`, `credentials.json`, `.ssh/`, `.aws/` |
| Model weights | `*.bin`, `*.gguf`, `*.safetensors`, `models/`, `whisper.cpp/` |
| Media | `*.mp4`, `*.wav`, `*.mp3`, `*.mkv` … |
| Generated state | `*.db`, `*.log`, `logs/`, `out/`, `vault/`, `transcripts/`, `cache/` |
| Environments | `.venv/`, `node_modules/`, `__pycache__/`, `.pytest_cache/` |
| Git history | `.git/` — old commits are a common place for forgotten keys |

Symlinks are never followed; they are listed in the manifest instead.

## What it generates

- `.env.example` — every variable the code reads plus every key from the
  source machine's `.env`, with all values blank
- `START_HERE.md` — orientation for whoever opens the ZIP
- `HANDOFF/SETUP.md` — install steps derived from the detected stack
- `HANDOFF/OPERATING.md` — detected commands, entry points, failure modes
- `HANDOFF/CLOUD.md` — fresh VPS setup, systemd unit, new Claude Code session
- `HANDOFF/MANIFEST.md` — the tree, and exactly what was dropped and why
- `CLAUDE.md` — project context for a new Claude Code session (an existing
  one is never overwritten; the generated version goes to
  `HANDOFF/CLAUDE.suggested.md`)
- `.gitignore` — only if the project has none

## Secret scan

Before zipping, every text file is scanned for Anthropic / OpenAI / GitHub /
AWS / Google / Slack / Stripe / HuggingFace keys, JWTs, private key blocks,
credentials embedded in connection URLs, and hardcoded `password = "…"`
assignments. A high-severity hit aborts the run and writes nothing.

Lines that read a value from the environment are not flagged — that is the
correct pattern, not a leak.

## Options

```
-s, --src DIR         project directory (auto-detected if omitted)
-o, --out DIR         where to write the ZIP
-N, --name NAME       name inside the ZIP
-i, --include PATTERN re-include something the excludes would drop (repeatable)
    --keep-git        keep .git
    --include-models  keep model weights
    --include-media   keep audio and video
    --include-data    keep databases, vaults, logs, output directories
    --allow-secrets   package anyway after a secret-scan hit
-n, --dry-run         report only, write nothing
```

`--include` wins over every exclude, so a config file that happens to live
under an excluded directory can be rescued:

```bash
python3 package_video2brain.py -s ~/video2brain -i "data/config.yaml"
```

Prefer an anchored path like that. A bare filename pattern (`-i "*.yaml"`)
matches at any depth, which forces the walk to descend into directories it
would otherwise skip whole — including `.venv/` and `node_modules/`.

## Verify before sending

```bash
unzip -l video2brain-handoff-*.zip | grep -E '\.env$|\.pem|\.bin|\.gguf|\.mp4'
```

That should print nothing.
