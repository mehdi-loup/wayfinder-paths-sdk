import { embedOne } from './embed.js'
import { search as dbSearch, hybridSearch as dbHybridSearch } from './db.js'
import type { SearchResult } from './types.js'

function dedup(results: SearchResult[], k: number): SearchResult[] {
  const seen = new Set<string>()
  return results.filter((r) => !seen.has(r.slug) && seen.add(r.slug)).slice(0, k)
}

// Pure vector search — useful for baseline comparison against hybrid.
// Returns raw chunks; may include multiple chunks from the same document.
export async function search(
  query: string,
  k = 5,
  minSimilarity = 0.3,
): Promise<SearchResult[]> {
  const queryEmbedding = await embedOne(query)
  return dbSearch(queryEmbedding, k, minSimilarity)
}

// Hybrid: vector cosine + BM25 full-text fused via Reciprocal Rank Fusion.
// `similarity` on results holds RRF score — different scale from cosine similarity.
export async function searchHybrid(
  query: string,
  k = 5,
): Promise<SearchResult[]> {
  const queryEmbedding = await embedOne(query)
  return dbHybridSearch(queryEmbedding, query, k)
}

// Pure-vector deduped — kept for eval baseline comparison only.
export async function searchVectorDeduped(
  query: string,
  k = 5,
  minSimilarity = 0.0,
): Promise<SearchResult[]> {
  const raw = await search(query, k * 4, minSimilarity)
  return dedup(raw, k)
}

// Hybrid BM25+vector deduped — one result per document, swapped internals.
// Same signature as the old searchDeduped; searchCorpus tool is unchanged.
export async function searchDeduped(
  query: string,
  k = 5,
): Promise<SearchResult[]> {
  const raw = await searchHybrid(query, k * 4)
  return dedup(raw, k)
}

export type { SearchResult }
