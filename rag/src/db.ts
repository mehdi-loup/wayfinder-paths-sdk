import { createClient } from '@supabase/supabase-js'
import ws from 'ws'
import type { WfPathYaml, ChunkWithEmbedding, SearchResult } from './types.js'

function getClient() {
  const url = process.env.SUPABASE_URL
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY
  if (!url || !key) throw new Error('SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set')
  // Service role key: bypasses RLS, safe for server-side ingest scripts.
  // Never expose this key to the browser.
  // ws: Node 20 lacks native WebSocket; Supabase Realtime requires it even for REST-only use.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return createClient(url, key, { realtime: { transport: ws as any } })
}

export async function upsertDocument(path: WfPathYaml): Promise<string> {
  const client = getClient()

  const { data, error } = await client
    .from('documents')
    .upsert(
      {
        slug: path.slug,
        title: path.name,
        primary_kind: path.primary_kind,
        tags: path.tags,
        version: path.version,
        metadata: {
          schema_version: path.schema_version,
          summary: path.summary,
          pipeline_archetype: path.pipeline?.archetype ?? null,
        },
      },
      { onConflict: 'slug' },
    )
    .select('id')
    .single()

  if (error) throw new Error(`upsertDocument(${path.slug}): ${error.message}`)
  return data.id as string
}

export async function upsertChunks(
  documentId: string,
  chunks: ChunkWithEmbedding[],
): Promise<void> {
  const client = getClient()

  // Delete existing chunks for this document before re-inserting.
  // Simpler than diffing: at re-ingest time we always have the full set.
  const { error: deleteError } = await client
    .from('chunks')
    .delete()
    .eq('document_id', documentId)

  if (deleteError) throw new Error(`deleteChunks(${documentId}): ${deleteError.message}`)

  const rows = chunks.map((c) => ({
    document_id: documentId,
    chunk_index: c.chunkIndex,
    chunk_type: c.chunkType,
    section: c.section,
    text: c.text,
    token_count: c.tokenCount,
    // pgvector expects a string like "[0.1, 0.2, ...]" or a native vector type.
    // Supabase JS client accepts a plain number array for vector columns.
    embedding: JSON.stringify(c.embedding),
  }))

  const { error } = await client.from('chunks').insert(rows)
  if (error) throw new Error(`insertChunks(${documentId}): ${error.message}`)
}

type RpcRow = {
  chunk_id: string
  document_id: string
  slug: string
  title: string
  section: string | null
  chunk_type: string
  text: string
  similarity: number
}

function mapRow(row: RpcRow): SearchResult {
  return {
    chunkId: row.chunk_id,
    documentId: row.document_id,
    slug: row.slug,
    title: row.title,
    section: row.section,
    chunkType: row.chunk_type,
    text: row.text,
    similarity: row.similarity,
  }
}

export async function search(
  queryEmbedding: number[],
  k = 5,
  minSimilarity = 0.0,
): Promise<SearchResult[]> {
  const client = getClient()

  const { data, error } = await client.rpc('match_chunks', {
    query_embedding: JSON.stringify(queryEmbedding),
    match_count: k,
    min_similarity: minSimilarity,
  })

  if (error) throw new Error(`search rpc: ${error.message}`)

  return (data as RpcRow[]).map(mapRow)
}

// hybridSearch fuses vector cosine similarity with PostgreSQL full-text search
// via Reciprocal Rank Fusion (RRF). The returned `similarity` field holds the
// RRF score (not cosine similarity), so don't threshold it against cosine values.
export async function hybridSearch(
  queryEmbedding: number[],
  queryText: string,
  k = 5,
  rrfK = 60.0,
): Promise<SearchResult[]> {
  const client = getClient()

  const { data, error } = await client.rpc('hybrid_match_chunks', {
    query_embedding: JSON.stringify(queryEmbedding),
    query_text: queryText,
    match_count: k,
    rrf_k: rrfK,
  })

  if (error) throw new Error(`hybrid search rpc: ${error.message}`)

  return (data as RpcRow[]).map(mapRow)
}
