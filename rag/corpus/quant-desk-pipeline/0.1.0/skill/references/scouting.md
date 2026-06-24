# Scout mode (fast interactive paper exploration)

Scout is a **fast, read-only, conversational browse loop** that runs *before* topic lock. **It is not part of the pipeline** — it is not a graph node, not a worker agent, and is never run by the orchestrator. It runs in the **main thread** as ordinary interactive skill behavior. Its only job is to give the user a quick overview of what's interesting/new/spicy in an area and a rough "could we even test this?" read — so they can pick a topic. It never extracts methodology, implements a signal, or runs a backtest. Nothing expensive happens until the user explicitly says "go deep on X", at which point the chosen topic string is written to `inputs/theme.md` and handed to **Phase 1 Step 0** of the deep pipeline.

**Use any core tools you have, broadly.** Scout is a brainstorm — reach for whatever read-only surfaces help the user see the landscape: `core_web_search` / `core_web_fetch` (academic skim, primary), `research_search_alpha` (scored alpha feed), the `research_*` Delta Lab screens (top APY / basis / funding / lending — to sanity-check whether a phenomenon even exists in the data we can reach), `research_crypto_sentiment`, and a glance at `MEMORY.md`. Stay read-only and stay fast; the point is breadth and conversation, not depth.

Scout is the front door for **broad / exploratory** asks that the deep pipeline's `topic-scope.md` would otherwise reject ("what's spicy in vol signals?", "find me something worth testing"). Instead of bouncing the user, Scout surfaces a menu and helps them choose.

## Speed budget (the point of this mode)

**Target: under ~3 minutes to first menu.** Scout is a skim, not a literature review. The hard rules that keep it fast:

- **EXA-first, main-thread.** Lead with 2-4 direct `core_web_search` calls for the academic skim; layer other core read-only tools only as they add signal. Do NOT spawn discovery subagents, do NOT fan out per-category, do NOT run the Phase 1b feasibility subagent, and do NOT invoke the pipeline orchestrator. Those belong to Deep mode.
- **Topics emerge from results — don't pre-bucket.** Do NOT start by inventing a fixed list of families and then searching each one (that was the slow, rigid failure mode). Search the area broadly, then *cluster whatever actually comes back* into 4-8 loose themes.
- **Abstracts from the search payload, not fetches.** `core_web_search(contentType="summary")` returns abstract-level text inline. Read that. Only `core_web_fetch` a specific paper when a cluster is genuinely ambiguous and the decision hinges on it — never as a default per-paper step.
- **Feasibility is a 4-line inline heuristic, not a deep prune** (see below).

## Read-only guarantees (locked)

- No methodology extraction, no signal files, no `evaluate_signal`, no fund movement.
- `category="research paper"` + an academic `includeDomains` list keeps citations academic; the discovery phase's source rules still govern what may later enter the pipeline (academic only, no SSRN). Practitioner/social hits are fine as *context* in Scout but are never promoted into Deep.

## Step 1 — Broad EXA sweep (2-4 searches, parallel)

Fire `core_web_search` directly with these settings:

- `category: "research paper"`
- `contentType: "summary"` (abstract-level text inline — this is what removes the per-paper fetch)
- `startPublishedDate: "<~24 months ago>"` for the recency/novelty bias (drop or widen only if the user wants seminal work)
- `numResults: 15-25`
- `includeDomains: "arxiv.org,nber.org,scholar.google.com,semanticscholar.org"` (academic skim)
- `type: "neural"` (concept search) or `"fast"` (latency-priority)

Run 2-4 queries that triangulate the area from different angles in one batch, e.g. for "volatility signals":
`"<area> return predictability trading signal"`, `"<area> regime crypto"`, `"<area> cross-sectional OR time-series strategy backtest"`. Let the wording follow the user's ask. Stop at ~3 searches unless results are thin.

Then **cluster the returned papers into 4-8 emergent themes** based on what actually came back — each theme should be narrow enough to later pass `topic-scope.md` (a reader could predict the signal-formula class). Don't force a predetermined taxonomy.

## Step 2 — Fast feasibility heuristic (inline, per theme)

For each theme, a one-line ✅/⚠️/❌ from a quick 4-point check — judged from the abstract snippets, no subagent, no deep reasoning. This is a *triage* read; the real prune is Deep's Phase 1b.

1. **Data** — inputs limited to what we have (crypto OHLCV via CCXT ~2017+, Hyperliquid funding/candles, on-chain APY/TVL)? `❌` if it needs equity-options surfaces, CRSP/Compustat, tick data, or other inaccessible inputs with no crypto proxy (the discovery phase applies the full feasibility prune).
2. **Frequency** — native horizon ≥ 15m? `❌` sub-15m (unmonetizable).
3. **Compute** — closed-form / rolling-stat / simple regression? `⚠️` if it smells like heavy ML, MCMC, or large cross-sectional optimization.
4. **Memory** — quick glance at `MEMORY.md`: already tested? Flag it (e.g. "⚠️ funding-MR REJECTED 2026-04-20") so the user doesn't re-burn a dead family.

If a theme is obviously `❌` on data or frequency, drop it from the menu or list it under a short "skipped — not testable here" note. Don't spend time explaining marginal cases — that's what the interactive loop is for.

## Step 3 — Present the menu (spice-ranked)

A compact table grouped by theme, each row: theme, 1-2 representative papers (title + year, pulled from the search payload), feasibility ✅/⚠️/❌, memory flag. Rank by a lightweight **spice** read — balanced and qualitative, not a computed citation pull:

- `novelty` — new angle, not a restatement; not already in memory.
- `recency` — favored by the `startPublishedDate` bound.
- `popularity` — use EXA's own relevance ranking + any citation hint already in the snippet. Do NOT make extra calls just to fetch citation counts during the skim.
- `buzz` — only if the user explicitly wants the practitioner pulse; one optional non-academic `core_web_search`.

The ranking just orders the menu. The user, not the score, decides what advances.

## Step 4 — Interactive loop (lazy, on demand)

Sit in a Q&A loop. Load more **only for what the user asks about**:

- *"tell me more about theme X"* / *"is the data really there?"* → now (and only now) `core_web_fetch` the 1-2 key papers for fuller abstracts, and walk the data path (which adapter/client, frequency, history depth — CCXT ~2017+, Hyperliquid ~7mo/200-day safe window).
- *"what's the catch with Y?"* → likely red-flags (`red-flag-taxonomy.md`), regime/universe risk — before any compute.
- *"compare X and Y"* → side-by-side on feasibility + spice + memory.

## Step 5 — Handoff to Deep

The user exits by choosing: *"run the full replication on X."* Only then:

1. Lock the theme as the topic string; confirm it passes `topic-scope.md` (a formality — Scout themes are pre-shaped to be narrow). Write the locked topic to `inputs/theme.md` — this is the pipeline's required input.
2. Hand to the Deep pipeline at **Phase 1 Step 0** (canonical seeding → discovery), unchanged. Scout's EXA hits for that theme can be passed in as a head start, but Phase 1b still runs its honest feasibility prune.

If the user picks nothing, Scout just ends. Optionally write the menu to `phase0_scout.json` so a later session can resume without re-searching.
