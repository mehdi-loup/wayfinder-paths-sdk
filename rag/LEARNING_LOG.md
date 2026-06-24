# Day 11 Learning Log — RAG / pgvector

## Checkpoint questions

### 1. Chunking defense
**Strategy:** One metadata chunk (structured YAML fields as prose) + heading-based
splits of `skill/instructions.md` per path.

**Why this size?** Instructions sections are naturally 50–300 tokens, well under the
512-token target. The heading boundary is the semantic unit, not a token count.
Forcing a fixed-size split would override a document structure that's already
expressing the right granularity.

**What breaks if I 10× the chunk size?** A single chunk would span multiple H2
sections (e.g., "What this skill does" + "Steps" + "Rules" fused into one vector).
A query about "orchestration rules" would match a chunk that also contains unrelated
step-by-step instructions, diluting the embedding toward an average topic.
Retrieval returns contextually-mixed chunks that hurt answer quality downstream.

**What breaks if I drop overlap to 0?** Already 0. Safe here because sections are
short and structurally bounded. In long prose documents (papers, contracts) with no
headers, zero overlap means sentences straddling a chunk boundary lose half their
context. A query matching the second half of a sentence would miss the first half,
degrading retrieval for that boundary region.

**What corpus property would force a different choice?**
Long-form prose paths (e.g., detailed protocol specs with no heading structure)
would require fixed-token chunking (~512 tokens) with ~50-token overlap to preserve
sentence context across boundaries.

---

### 2. Embedding model + distance metric

**Model:** `voyage-3` (1024 dims, 32k context)

**Why Voyage and not OpenAI text-embedding-3-small?**  
Voyage is Anthropic's recommended embedding partner (docs.anthropic.com explicitly
references them). The sprint rule is Anthropic-ecosystem only. Additionally,
voyage-3 has a 32k context window (vs. 8k for text-embedding-3-small), which
matters for long instructions documents.

**Distance operator:** `<=>` (cosine distance in pgvector).

**Why cosine and not inner product (`<#>`)?**  
Voyage vectors are L2-normalized (unit length). For unit vectors:
  `cosine_similarity(a,b) = dot_product(a,b)` (numerically identical)
  `cosine_distance = 1 - cosine_similarity`
  
pgvector's `<#>` returns negative dot product, so results need a sign flip
(`1 - (embedding <#> query)` would be wrong — it would need `-(embedding <#> query)`).
Cosine is unambiguous, human-readable, and the [0,1] similarity range makes
thresholding intuitive.

**How would I know if I picked the wrong operator?**  
Wrong choice would return high-similarity scores for semantically distant chunks
and low scores for close ones — retrieval would look like random noise. With 4
documents, the effect would be immediately visible: `pnpm search "delta neutral"`
returning the policy path ahead of the delta-neutral monitor.

---

### 3. Eval numbers

*(Fill in after running `pnpm eval` against the live Supabase + Voyage setup)*

| k | recall@k | hits/total |
|---|----------|-----------|
| 1 | ?% | ?/10 |
| 3 | ?% | ?/10 |
| 5 | ?% | ?/10 |

**Expected:** recall@5 should be high (≥0.8) on 4 documents — the corpus is small
enough that near-duplicates from the same doc are the main failure mode, not actual
retrieval failure. The interesting gap to watch is @1 vs @3: if @1 is low but @3
is high, the correct chunk is being outranked by a near-duplicate from the same
document, not a wrong document.

**Next lever if recall@5 < 0.7:** Chunking (check if metadata chunks are overly
generic and outranking specific instruction chunks). Not model — model upgrade helps
by ~5-15%, chunking improvements can be 30-50%.

---

### 4. The near-duplicate chunk problem

**Did top-k surface near-duplicates?** Likely yes at k=5 with 4 paths. Each path
has 3–6 chunks. A query about "conditional router policy" might return chunks 1, 2,
and 3 from `conditional-router-reference`, leaving only 2 slots for other documents.

**What's the cheapest fix?**  
Post-process `match_chunks` output to keep only the highest-scoring chunk per
document (slug), then return the top-k documents:

```typescript
function deduplicateByDocument(results: SearchResult[], k: number): SearchResult[] {
  const seen = new Set<string>()
  return results
    .filter(r => !seen.has(r.slug) && seen.add(r.slug))
    .slice(0, k)
}
```

This is max-pooling by document. It requires fetching more than k chunks initially
(e.g., fetch 20, dedup to 5). The `match_chunks` RPC already supports `match_count`
— pass `k * 4` and dedup client-side.

**Why I didn't fix it today:** 4-document corpus means the failure only appears at
k≥3. Worth documenting for Day 12 when the prod corpus has 50+ paths.

---

### 5. Scaling thresholds

**When does sequential scan become unacceptable?**  
pgvector sequential scan is O(N) over all vectors. Rule of thumb:
- <10k vectors: sequential scan, no index needed, query time <10ms
- 10k–100k: IVFFlat (`lists = sqrt(N)`), ~10ms with minor recall loss
- 100k+: HNSW (`m=16, ef_construction=64`), sub-5ms with high recall (0.95+)

The 4-path example corpus produces ~20 chunks. Sequential scan finishes in <1ms.

**When does self-hosted pgvector lose to a managed vector DB?**  
Three axes:
1. **Ops burden:** pgvector requires you to manage vacuuming, index rebuilds (IVFFlat
   degrades as rows are deleted/inserted), connection pooling (pgBouncer), and
   backups. Pinecone/Weaviate eliminate this entirely.
2. **Latency:** at 1M+ vectors, HNSW on self-hosted pgvector needs careful tuning to
   hit <50ms. Managed DBs are optimized for exactly this.
3. **Cost crossover:** Supabase free tier covers ~500MB. Pinecone free tier covers
   100k vectors. At ~1M vectors (1024 dims × 4 bytes = ~4GB), Supabase Pro (~$25/mo)
   vs Pinecone Starter (~$70/mo) — pgvector wins on cost until you need sub-10ms
   latency SLAs or multi-tenant isolation without schema gymnastics.

**Conclusion:** pgvector (Supabase) is the right choice for this sprint. The
crossover to a managed vector DB happens when the operational simplicity of the
managed service is worth $50–100/mo and you've hit the latency ceiling of pgvector
with a proper HNSW index.

---

## What surprised me

*(Fill in after running the full pipeline)*

---

## Open threads (Day 12+)

- [ ] Reranking: Cohere Rerank or custom MMR to suppress near-duplicate chunks
- [ ] Hybrid search: `tsvector` BM25 + pgvector cosine for better slug/keyword recall
- [ ] HyDE: generate hypothetical answer → embed it → retrieve (helps abstract queries)
- [ ] Wire `search()` into Mastra agent as a Tool
- [ ] Eval expansion: more queries from prod paths when corpus is loaded
- [ ] HNSW index: add when prod corpus exceeds 10k chunks
