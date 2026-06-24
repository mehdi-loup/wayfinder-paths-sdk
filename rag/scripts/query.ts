// CLI query runner. Usage: pnpm search "your question here"
// Embeds the query and returns top-k chunks with similarity scores.
import 'dotenv/config'
import { search } from '../src/search.js'

const query = process.argv.slice(2).join(' ')
if (!query) {
  console.error('Usage: pnpm search "<query>"')
  process.exit(1)
}

console.log(`Query: "${query}"\n`)

const results = await search(query, 5, 0.0)

if (results.length === 0) {
  console.log('No results found.')
  process.exit(0)
}

for (let i = 0; i < results.length; i++) {
  const r = results[i]
  const simPct = (r.similarity * 100).toFixed(1)
  console.log(`--- #${i + 1} [${simPct}%] ${r.title} / ${r.section ?? r.chunkType} (${r.slug})`)
  console.log(r.text.slice(0, 300) + (r.text.length > 300 ? '...' : ''))
  console.log()
}
