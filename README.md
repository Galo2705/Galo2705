<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/hero-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="assets/hero-light.svg">
  <img src="assets/hero-dark.svg" alt="Yehonatan Galo — trading systems, knowledge engines, agent orchestration" width="100%">
</picture>

I build autonomous research infrastructure for markets: knowledge engines that read
video, filings and tick data; a backtesting lab that prices every claimed edge with
real costs; and a fleet of agents that review each other's work before anything
reaches a decision.

Most of it runs privately, 24/7, on a Mac and a small VPS — local-first, with $0
inference wherever a local model is good enough. The rule that holds it all
together: **nothing trades live without a measured edge, and no claim survives
without its source.**

<br>

<img src="assets/system-map.svg" alt="System map — from raw data to verified decisions" width="100%">

<br>

## Selected work

| System | What it does |
|:--|:--|
| **Cockpit** | Unified command center for every engine — live dashboards, alert routing with dedup and budgets, margin guard, performance analytics that show `—` instead of inventing a number. |
| **Backtest Lab** | Nightly, fully automated backtesting with per-asset-class cost models. Strategy changes pass 10 gates before adoption. It once found that a strategy's entire "edge" was the commission model — verdict flipped to `NO_EDGE`. That's the point. |
| **Council** | Multi-engine deliberation: one ticker, five independent research engines in parallel, a contradiction matrix, then a designated skeptic. Disagreement between engines is the signal, not a bug. |
| **video2brain** | Anchor-first pipeline: video → whisper.cpp → five-layer knowledge vault. Every extracted claim carries the exact quote it came from, scored 0–100 by a QA pass. No anchor, no claim. |
| **graphify** | Local-first code knowledge-graph CLI — maps a codebase into a queryable graph with god-node detection, community clustering, and an EXTRACTED / INFERRED / AMBIGUOUS audit trail. Deterministic and free in code-only mode. |
| **Execution layer** | Paper-first broker execution behind an edge gate: an engine gets live capital only after its verdict survives statistical review. 57 tests, and the gate has said "no" more often than "yes". |

<br>

## How I work

- **Measured over assumed.** Every edge claim gets a backtest with real costs. Gross vs. net has killed more of my strategies than the market has.
- **Honest UX.** Dashboards show missing data as missing. A small sample gets flagged as a small sample. Numbers the system can't defend don't get rendered.
- **Local-first, $0 by default.** Ollama, whisper.cpp and SQLite before any paid API. The nightly loops cost nothing to run.
- **Adversarial review.** Agents audit agents; a skeptic gets the last word. My favorite bug reports are the ones my own review fleet files against me.

<br>

<img src="assets/stack.svg" alt="Stack: Python, TypeScript, asyncio, SQLite, Redis, Ollama, whisper.cpp, Claude agents, launchd, systemd, Tailscale, Obsidian" width="100%">

<br>

<sub>Jerusalem · Hebrew &amp; English · most repos are private — happy to talk about any of the above.</sub>
