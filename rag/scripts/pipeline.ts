// Full ingest pipeline: read corpus → parse YAML → chunk → embed → upsert to Supabase.
// Usage: pnpm ingest
//
// Supports two corpus layouts:
//   flat:      corpus/<slug>/wfpath.yaml
//   versioned: corpus/<slug>/<semver>/wfpath.yaml  (picks the latest version)
import 'dotenv/config'
import { readFileSync, readdirSync, existsSync } from 'fs'
import { join, basename } from 'path'
import { fileURLToPath } from 'url'
import { dirname } from 'path'
import { parse as parseYaml } from 'yaml'
import type { WfPathYaml } from '../src/types.js'
import { chunkPath, type PathFiles } from '../src/chunker.js'
import { embedMany, EMBEDDING_MODEL, EMBEDDING_DIMS } from '../src/embed.js'
import { upsertDocument, upsertChunks } from '../src/db.js'
import type { ChunkWithEmbedding } from '../src/types.js'

const __dirname = dirname(fileURLToPath(import.meta.url))
const CORPUS_DIR = join(__dirname, '../corpus')

// Compare two semver strings (e.g. "0.1.10" > "0.1.4").
// Splits on '.' and compares each part numerically.
function compareSemver(a: string, b: string): number {
  const pa = a.split('.').map(Number)
  const pb = b.split('.').map(Number)
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const diff = (pa[i] ?? 0) - (pb[i] ?? 0)
    if (diff !== 0) return diff
  }
  return 0
}

// Resolve the root directory for a slug: either the slug dir itself (flat)
// or the latest-semver subdirectory (versioned).
function resolvePathRoot(slugDir: string): string {
  if (existsSync(join(slugDir, 'wfpath.yaml'))) return slugDir

  const versions = readdirSync(slugDir, { withFileTypes: true })
    .filter((d) => d.isDirectory() && /^\d/.test(d.name))
    .map((d) => d.name)
    .sort((a, b) => compareSemver(a, b))

  if (versions.length === 0) throw new Error(`No wfpath.yaml or version dirs in ${slugDir}`)

  // Use latest version
  return join(slugDir, versions.at(-1)!)
}

function readOptional(filePath: string): string | null {
  return existsSync(filePath) ? readFileSync(filePath, 'utf8') : null
}

function readMarkdownDir(dir: string): Record<string, string> {
  if (!existsSync(dir)) return {}
  return Object.fromEntries(
    readdirSync(dir)
      .filter((f) => f.endsWith('.md'))
      .map((f) => [basename(f, '.md'), readFileSync(join(dir, f), 'utf8')]),
  )
}

function loadPathFiles(slugDir: string): PathFiles {
  const root = resolvePathRoot(slugDir)
  const yaml = parseYaml(readFileSync(join(root, 'wfpath.yaml'), 'utf8')) as WfPathYaml
  return {
    yaml,
    instructions: readOptional(join(root, 'skill', 'instructions.md')),
    agents: readMarkdownDir(join(root, 'skill', 'agents')),
    references: readMarkdownDir(join(root, 'skill', 'references')),
  }
}

async function run() {
  console.log(`Model: ${EMBEDDING_MODEL} (${EMBEDDING_DIMS} dims)`)
  console.log(`Corpus: ${CORPUS_DIR}\n`)

  const slugDirs = readdirSync(CORPUS_DIR, { withFileTypes: true })
    .filter((d) => d.isDirectory())
    .map((d) => join(CORPUS_DIR, d.name))

  // Load all paths and chunk them
  const allChunks: Array<{
    slug: string
    files: PathFiles
    chunkIndex: number
    chunkType: 'metadata' | 'instructions' | 'agent' | 'reference'
    section: string | null
    text: string
    tokenCount: number
  }> = []

  const pathFilesList: PathFiles[] = []

  for (const slugDir of slugDirs) {
    let files: PathFiles
    try {
      files = loadPathFiles(slugDir)
    } catch (err) {
      console.warn(`  skip ${basename(slugDir)}: ${(err as Error).message}`)
      continue
    }

    const chunks = chunkPath(files)
    pathFilesList.push(files)

    const agentCount = Object.keys(files.agents).length
    const refCount = Object.keys(files.references).length
    const totalTokens = chunks.reduce((s, c) => s + c.tokenCount, 0)
    console.log(
      `  ${files.yaml.slug} v${files.yaml.version}: ${chunks.length} chunks` +
      ` (agents:${agentCount} refs:${refCount}) ~${totalTokens} tokens`,
    )

    for (const c of chunks) {
      allChunks.push({ files, ...c })
    }
  }

  console.log(`\nEmbedding ${allChunks.length} chunks total...`)

  const { embeddings, totalTokens } = await embedMany(
    allChunks.map((c) => c.text),
    (done, total, tokens) => {
      process.stdout.write(`  ${done}/${total} embedded, ${tokens} tokens used\r`)
    },
  )
  console.log(`\nDone. Total tokens: ${totalTokens}`)
  const costUsd = (totalTokens / 1_000_000) * 0.06
  console.log(`Estimated cost: $${costUsd.toFixed(6)} (voyage-3 @ $0.06/1M tokens)\n`)

  // Group by slug, upsert documents then chunks
  const bySlug = new Map<string, { files: PathFiles; chunksWithEmbeddings: ChunkWithEmbedding[] }>()
  for (let i = 0; i < allChunks.length; i++) {
    const c = allChunks[i]
    if (!bySlug.has(c.slug)) bySlug.set(c.slug, { files: c.files, chunksWithEmbeddings: [] })
    bySlug.get(c.slug)!.chunksWithEmbeddings.push({
      slug: c.slug,
      chunkIndex: c.chunkIndex,
      chunkType: c.chunkType,
      section: c.section,
      text: c.text,
      tokenCount: c.tokenCount,
      embedding: embeddings[i],
    })
  }

  console.log('Upserting to Supabase...')
  for (const [slug, { files, chunksWithEmbeddings }] of bySlug) {
    const docId = await upsertDocument(files.yaml)
    await upsertChunks(docId, chunksWithEmbeddings)
    console.log(`  ✓ ${slug} (${chunksWithEmbeddings.length} chunks)`)
  }

  console.log('\nIngest complete.')
}

run().catch((err) => {
  console.error(err)
  process.exit(1)
})
