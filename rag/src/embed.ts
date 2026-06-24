// Voyage AI embedding via raw fetch — no SDK wrapper.
// Why not voyage-ai-provider (the community AI SDK package)?
// It depends on @ai-sdk/provider@^3.0.0 while we're on AI SDK v5 internals;
// version mismatch risk isn't worth it for a 10-line API call.
//
// Model: voyage-3
//   - 1024 dimensions
//   - L2-normalized outputs (unit vectors)
//   - All three distance metrics (cosine, dot product, L2) give identical rankings
//     on normalized vectors. We use cosine (<=> in pgvector) for readability.
//   - 32k token context window — plenty for our ~512-token chunks

const VOYAGE_API_URL = 'https://api.voyageai.com/v1/embeddings'
export const EMBEDDING_MODEL = 'voyage-3'
export const EMBEDDING_DIMS = 1024

// Free-tier limits: 3 RPM, 10K TPM.
// We batch by token count (not input count) so agent/reference chunks (500-2000 tokens)
// don't silently bust the per-minute token cap. Each batch stays under MAX_TOKENS_PER_BATCH.
// After each successful batch we wait INTER_BATCH_MS so the next batch starts in a fresh
// 60-second window.
//
// To unlock standard rate limits: add a payment method at dash.voyageai.com.
// Free tokens (200M for voyage-3) still apply after adding a card.
// Then set: MAX_TOKENS_PER_BATCH = 80_000, INTER_BATCH_MS = 0
const MAX_TOKENS_PER_BATCH = 8_000
const INTER_BATCH_MS = 62_000  // full 60s window + 2s safety buffer

// Rough token estimate: 1 token ≈ 4 chars. Same heuristic used in chunker.ts.
function estimateTokens(text: string): number {
  return Math.ceil(text.length / 4)
}

// Split texts into token-capped batches. Each batch stays under MAX_TOKENS_PER_BATCH.
// A single text exceeding the cap gets its own batch (edge case for very long chunks).
function makeBatches(texts: string[]): string[][] {
  const batches: string[][] = []
  let current: string[] = []
  let currentTokens = 0

  for (const text of texts) {
    const tokens = estimateTokens(text)
    if (current.length > 0 && currentTokens + tokens > MAX_TOKENS_PER_BATCH) {
      batches.push(current)
      current = []
      currentTokens = 0
    }
    current.push(text)
    currentTokens += tokens
  }
  if (current.length > 0) batches.push(current)
  return batches
}

interface VoyageResponse {
  object: string
  data: Array<{ object: string; embedding: number[]; index: number }>
  model: string
  usage: { total_tokens: number }
}

async function callVoyage(texts: string[], attempt = 0): Promise<{ embeddings: number[][]; totalTokens: number }> {
  const apiKey = process.env.VOYAGE_API_KEY
  if (!apiKey) throw new Error('VOYAGE_API_KEY not set')

  const response = await fetch(VOYAGE_API_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({ model: EMBEDDING_MODEL, input: texts }),
  })

  if (response.status === 429 && attempt < 6) {
    const waitMs = 62_000 + attempt * 10_000
    process.stderr.write(`  rate limited (attempt ${attempt + 1}), waiting ${waitMs / 1000}s...\n`)
    await new Promise((r) => setTimeout(r, waitMs))
    return callVoyage(texts, attempt + 1)
  }

  if (!response.ok) {
    const body = await response.text()
    throw new Error(`Voyage API ${response.status}: ${body}`)
  }

  const data = (await response.json()) as VoyageResponse
  const sorted = data.data.sort((a, b) => a.index - b.index)
  return {
    embeddings: sorted.map((d) => d.embedding),
    totalTokens: data.usage.total_tokens,
  }
}

// embedMany batches by token count and paces between batches to stay under free-tier limits.
export async function embedMany(
  texts: string[],
  onProgress?: (done: number, total: number, tokens: number) => void,
): Promise<{ embeddings: number[][]; totalTokens: number }> {
  const batches = makeBatches(texts)
  const allEmbeddings: number[][] = []
  let totalTokens = 0
  let done = 0

  for (let b = 0; b < batches.length; b++) {
    const batch = batches[b]
    const batchTokens = batch.reduce((s, t) => s + estimateTokens(t), 0)
    process.stderr.write(`  batch ${b + 1}/${batches.length}: ${batch.length} texts, ~${batchTokens} tokens\n`)

    const result = await callVoyage(batch)
    allEmbeddings.push(...result.embeddings)
    totalTokens += result.totalTokens
    done += batch.length
    onProgress?.(done, texts.length, totalTokens)

    // Wait between batches so the next one starts in a fresh 60s window.
    if (INTER_BATCH_MS > 0 && b < batches.length - 1) {
      process.stderr.write(`  waiting ${INTER_BATCH_MS / 1000}s before next batch...\n`)
      await new Promise((r) => setTimeout(r, INTER_BATCH_MS))
    }
  }

  return { embeddings: allEmbeddings, totalTokens }
}

export async function embedOne(text: string): Promise<number[]> {
  const { embeddings } = await embedMany([text])
  return embeddings[0]
}
