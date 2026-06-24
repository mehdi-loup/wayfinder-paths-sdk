// Eval harness: recall@k comparison between pure vector and hybrid (BM25+vector RRF) search.
// Usage: pnpm eval
import 'dotenv/config'
import { readFileSync } from 'fs'
import { join, dirname } from 'path'
import { fileURLToPath } from 'url'
import { search, searchVectorDeduped, searchDeduped } from '../src/search.js'

const __dirname = dirname(fileURLToPath(import.meta.url))
const evalSet = JSON.parse(
  readFileSync(join(__dirname, '../eval-set.json'), 'utf8'),
) as Array<{ query: string; expectedSlug: string; note: string }>

const KS = [1, 3, 5]
const MAX_K = Math.max(...KS)

type EvalRow = {
  query: string
  expectedSlug: string
  note: string
  vectorSlugs: string[]
  hybridSlugs: string[]
  vectorHit: Record<number, boolean>
  hybridHit: Record<number, boolean>
}

console.log(`Eval set: ${evalSet.length} queries\n`)
console.log('Running vector and hybrid retrieval for each query...\n')

const rows: EvalRow[] = []
const vectorHits: Record<number, number> = Object.fromEntries(KS.map((k) => [k, 0]))
const hybridHits: Record<number, number> = Object.fromEntries(KS.map((k) => [k, 0]))

for (const item of evalSet) {
  const [vectorResults, hybridResults] = await Promise.all([
    searchVectorDeduped(item.query, MAX_K, 0.0),
    searchDeduped(item.query, MAX_K),
  ])

  const vectorSlugs = vectorResults.map((r) => r.slug)
  const hybridSlugs = hybridResults.map((r) => r.slug)

  const vectorHit: Record<number, boolean> = {}
  const hybridHit: Record<number, boolean> = {}

  for (const k of KS) {
    vectorHit[k] = vectorSlugs.slice(0, k).includes(item.expectedSlug)
    hybridHit[k] = hybridSlugs.slice(0, k).includes(item.expectedSlug)
    if (vectorHit[k]) vectorHits[k]++
    if (hybridHit[k]) hybridHits[k]++
  }

  rows.push({ ...item, vectorSlugs, hybridSlugs, vectorHit, hybridHit })
}

// Per-query table
const hitStr = (hit: Record<number, boolean>) =>
  KS.map((k) => `@${k}:${hit[k] ? '✓' : '✗'}`).join(' ')

for (const r of rows) {
  const changed = KS.some((k) => r.vectorHit[k] !== r.hybridHit[k])
  const marker = changed ? ' ◀ changed' : ''
  console.log(`vector ${hitStr(r.vectorHit)}  hybrid ${hitStr(r.hybridHit)}${marker}`)
  console.log(`  query:    "${r.query}"`)
  console.log(`  expected: ${r.expectedSlug}`)
  if (!r.vectorHit[MAX_K] || !r.hybridHit[MAX_K]) {
    console.log(`  vector:   ${r.vectorSlugs.slice(0, MAX_K).join(', ')}`)
    console.log(`  hybrid:   ${r.hybridSlugs.slice(0, MAX_K).join(', ')}`)
  }
  if (changed) {
    console.log(`  note:     ${r.note}`)
  }
  console.log()
}

// Summary table
console.log('=== recall@k: vector vs hybrid ===')
for (const k of KS) {
  const vr = vectorHits[k] / evalSet.length
  const hr = hybridHits[k] / evalSet.length
  const vBar = '█'.repeat(Math.round(vr * 20)).padEnd(20, '░')
  const hBar = '█'.repeat(Math.round(hr * 20)).padEnd(20, '░')
  const delta = hybridHits[k] - vectorHits[k]
  const deltaStr = delta > 0 ? `+${delta}` : delta < 0 ? `${delta}` : '='
  console.log(`  @${k}  vector: ${vBar} ${(vr * 100).toFixed(0)}%  (${vectorHits[k]}/${evalSet.length})`)
  console.log(`       hybrid: ${hBar} ${(hr * 100).toFixed(0)}%  (${hybridHits[k]}/${evalSet.length})  delta: ${deltaStr}`)
}
