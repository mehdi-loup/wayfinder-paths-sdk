# Wayfinder Paths RAG — measured TypeScript retrieval library

<!-- VIDEO: drag ~/Desktop/rag-demo.mp4 into this line via GitHub's web README editor (Edit pencil → drop file). GitHub uploads and replaces the drop with a https://github.com/user-attachments/assets/<uuid> URL that renders as an inline playable video. -->

A standalone TypeScript RAG library — Supabase pgvector + Voyage AI embeddings, with a hybrid BM25 + vector RRF retrieval pipeline. **100% recall@3** on the 14-document Wayfinder Paths eval set. Exposed as a `pnpm search` CLI and as a `file:` dependency consumed by [day1-wallet-agent](https://github.com/mehdi-loup/day1-wallet-agent)'s agentic `searchCorpus` tool.

**Stack:** Supabase pgvector · Voyage AI (`voyage-3`, 1024 dims, cosine) · `tsvector` BM25 · Reciprocal Rank Fusion (k=60) · TypeScript / pnpm

## Prerequisites

- Node 20+, pnpm
- [Supabase](https://supabase.com) project (free tier works)
- [Voyage AI](https://dash.voyageai.com) API key (free tier: 50M tokens)

## Setup

```bash
cp .env.example .env
# Fill in VOYAGE_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

pnpm install

# Run the migration in your Supabase SQL editor (see output):
pnpm setup

# Embed the corpus and store in Supabase:
pnpm ingest
```

## Usage

```bash
# Semantic search
pnpm search "how do I convert a macro thesis into a Polymarket trade"

# Run eval harness (recall@1, @3, @5)
pnpm eval
```

## Corpus

4 example Wayfinder Paths from the [wayfinder-paths-sdk](https://github.com/WayfinderFoundation/wayfinder-paths-sdk) repo:

| Slug | Type | Summary |
|------|------|---------|
| `boros-carry-demo` | monitor | Boros carry vs underlying backtest |
| `conditional-router-reference` | policy | Conditional macro thesis → Polymarket job |
| `virtual-delta-neutral` | monitor | VIRTUAL delta-neutral (Moonwell + Hyperliquid) |
| `pt-carry-roller-demo` | strategy | Pendle PT carry selection + NAV rolling |

Add more paths: drop a `wfpath.yaml` + `skill/instructions.md` into `corpus/<slug>/` and re-run `pnpm ingest`.

## Architecture

```
corpus/<slug>/wfpath.yaml          ← structured metadata
corpus/<slug>/skill/instructions.md ← prose instructions

              ↓ chunker.ts
         2–5 chunks per path
         (1 metadata + N instruction sections)

              ↓ embed.ts (Voyage API, raw fetch)
         voyage-3, 1024 dims

              ↓ db.ts (Supabase pgvector)
         documents + chunks tables
         match_chunks() RPC function

              ↓ search.ts
         search(query, k) → SearchResult[]
```

## Chunking strategy

Each path produces two chunk types:

**Metadata chunk** — structured YAML fields as prose text (slug, name, type, tags,
summary, agent roles, pipeline archetype). One chunk per path, ~80–200 tokens.
Kept as a single unit because the fields are interdependent: separating "Tags:
delta-neutral" from "Summary: ..." loses the binding that makes both retrievable
together.

**Instructions chunks** — `skill/instructions.md` split on `##` headings. Each H2
section becomes one chunk. Why heading-based over fixed-token? Instructions are
organized by semantic task ("What this skill does", "Steps", "Rules"). A
fixed-token cut mid-section blends two contexts into one embedding, degrading
precision for specific queries. Overlap is 0 here because sections are already
short (50–300 tokens); overlap pays off when chunks are dense prose with no
structural boundaries.

**Failure modes at scale:**
- 10× chunk size → one chunk covers multiple distinct topics → retrieval returns
  tangentially-relevant context, hurting generation quality.
- 0 overlap on long dense prose (no headings) → sentences that straddle chunk
  boundaries lose context, dropping recall on queries that match the boundary area.

## Embedding model and distance metric

**Model:** `voyage-3` — 1024 dims, 32k context, general-purpose, L2-normalized outputs.

**Distance metric:** cosine (`<=>` in pgvector).

**Why cosine and not inner product (`<#>`)?**  
Voyage embeddings are unit-normalized, so cosine similarity ≡ dot-product similarity
(identical rankings). The inner product operator `<#>` returns *negative* dot-product
in pgvector (i.e., `ORDER BY embedding <#> query` gives the same ranking as cosine
but requires remembering the sign flip). Cosine is unambiguous and its [0,1] similarity
range makes threshold-setting intuitive.

**Why not L2 (`<->`)?**  
For normalized vectors, L2 and cosine produce identical rankings. L2 distance values
are harder to threshold meaningfully.

## Indexing

No vector index in the migration. At <1000 chunks, pgvector's sequential scan
(exact nearest-neighbor) is faster than an approximate index — IVFFlat or HNSW adds
build overhead and introduces approximation error without latency benefit.

**When to add an index:**
- IVFFlat: ~10k rows, batch workloads, recall loss acceptable. `lists = sqrt(N)`.
- HNSW: ~10k+ rows, latency-sensitive, recall ≥ 0.95 required. Higher memory cost.
- Managed vector DB (Pinecone, Weaviate): when ops burden of self-hosted pgvector
  (index tuning, vacuuming, connection pooling) exceeds cost of the managed service,
  typically at 100k+ vectors with SLA requirements.

## Eval

10 hand-written query/expected-slug pairs covering exact-keyword, semantic, protocol-
name, agent-role, and paraphrase queries. Run `pnpm eval` after ingest.

Gap addressed by k=1→3→5 progression: near-duplicate chunks from the same document
can push the correct doc out of @1. The `match_chunks` RPC does not deduplicate by
document — if top-k returns 3 chunks from one doc, other relevant docs are squeezed
out. **Cheapest fix:** post-process results to keep only the top-scoring chunk per
document (max-pooling by slug) before returning k results.

## Deferred (Day 12+)

- **Reranking / MMR:** cohere-rerank or custom max-marginal-relevance to reduce
  near-duplicate chunk surfacing without losing diversity.
- **Hybrid search:** BM25 + vector (Supabase `tsvector` full-text + pgvector). Catches
  exact keyword queries that semantic search misses on short unique strings (e.g. slug names).
- **HyDE (Hypothetical Document Embeddings):** generate a hypothetical answer, embed
  that, then retrieve. Improves recall for vague/abstract queries.
- **Wiring into Mastra agent:** export `search()` as a Mastra tool so the agent can
  ground answers in the path corpus alongside Zapper/MCP data.

---

## Related work

This repo is the **retrieval layer** of a three-part stack:

- **[day1-wallet-agent](https://github.com/mehdi-loup/day1-wallet-agent)** — the deployed TS AI agent that consumes this library via a `searchCorpus` tool and grounds answers in the Wayfinder Paths corpus.
- **[agentic-rag-evals](https://github.com/mehdi-loup/agentic-rag-evals)** — the Inspect AI suite that measures the retrieval-faithfulness behavior this library enables, end-to-end against the deployed agent.

More at [github.com/mehdi-loup](https://github.com/mehdi-loup).
