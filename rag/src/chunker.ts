import type { WfPathYaml, Chunk } from './types.js'

// Rough token estimate: 1 token ≈ 4 chars (GPT/Voyage rule of thumb).
// Good enough for budgeting; use a real tokenizer if precision matters.
function estimateTokens(text: string): number {
  return Math.ceil(text.length / 4)
}

// Build the metadata chunk: structured YAML fields rendered as human-readable prose.
// This chunk answers "what is this path?" — slug, type, tags, summary, agent roles.
// Keeping it as one chunk (not split further) because it's small (<200 tokens) and
// the fields are interdependent: splitting "Tags: delta-neutral" from "Summary: ..."
// would lose the context that makes both meaningful together.
function buildMetadataChunk(path: WfPathYaml, chunkIndex: number): Chunk {
  const lines: string[] = [
    `Name: ${path.name}`,
    `Slug: ${path.slug}`,
    `Type: ${path.primary_kind}`,
    `Tags: ${path.tags.join(', ')}`,
    `Version: ${path.version}`,
    ``,
    `Summary: ${path.summary}`,
  ]

  if (path.skill?.description && path.skill.description !== path.summary) {
    lines.push(``, `Skill: ${path.skill.description}`)
  }

  if (path.pipeline?.archetype) {
    lines.push(``, `Pipeline archetype: ${path.pipeline.archetype}`)
  }

  if (path.pipeline?.output_contract?.length) {
    lines.push(`Output contract: ${path.pipeline.output_contract.join(', ')}`)
  }

  if (path.agents?.length) {
    lines.push(``, `Agents:`)
    for (const agent of path.agents) {
      lines.push(`  - ${agent.phase}: ${agent.description}`)
    }
  }

  if (path.inputs?.slots) {
    const slots = Object.entries(path.inputs.slots)
    if (slots.length) {
      lines.push(``, `Inputs: ${slots.map(([k]) => k).join(', ')}`)
    }
  }

  const text = lines.join('\n')
  return {
    slug: path.slug,
    chunkIndex,
    chunkType: 'metadata',
    section: 'metadata',
    text,
    tokenCount: estimateTokens(text),
  }
}

// Shared heading-based markdown splitter used by instructions, agents, and references.
// Splits on ## (H2) boundaries. H1 preamble becomes an 'overview' section.
// Returns raw {section, text} pairs — caller sets chunkType and indexes.
function splitByH2(markdown: string): Array<{ section: string; text: string }> {
  const normalized = markdown.replace(/\r\n/g, '\n').trim()
  if (!normalized) return []

  const sections = normalized.split(/^## /m)
  const result: Array<{ section: string; text: string }> = []

  // Preamble before first ## (strip H1 title line)
  const preamble = sections[0].replace(/^# .+\n+/, '').trim()
  if (preamble) result.push({ section: 'overview', text: preamble })

  for (let i = 1; i < sections.length; i++) {
    const [headingLine, ...bodyLines] = sections[i].split('\n')
    const heading = headingLine.trim()
    const body = bodyLines.join('\n').trim()
    if (!heading && !body) continue
    result.push({
      section: heading,
      text: body ? `## ${heading}\n\n${body}` : `## ${heading}`,
    })
  }

  return result
}

function buildInstructionChunks(
  slug: string,
  markdown: string,
  startIndex: number,
): Chunk[] {
  const sections = splitByH2(markdown)

  if (sections.length === 0) return []

  // If no ## headings were found, splitByH2 returns one 'overview' section.
  // Treat it as a single chunk rather than splitting further.
  return sections.map((s, i) => ({
    slug,
    chunkIndex: startIndex + i,
    chunkType: 'instructions' as const,
    section: s.section,
    text: s.text,
    tokenCount: estimateTokens(s.text),
  }))
}

// Agent files (skill/agents/*.md) describe a single agent's responsibilities,
// data sources, and rules. Each file becomes its own chunk set (split by ##).
// The agent name (filename without .md) is prepended to each chunk's section label
// so retrieval results identify which agent the chunk came from.
function buildAgentChunks(
  slug: string,
  agentName: string,
  markdown: string,
  startIndex: number,
): Chunk[] {
  const sections = splitByH2(markdown)
  if (sections.length === 0) return []

  return sections.map((s, i) => ({
    slug,
    chunkIndex: startIndex + i,
    chunkType: 'agent' as const,
    section: `${agentName}${s.section !== 'overview' ? ` / ${s.section}` : ''}`,
    text: `Agent: ${agentName}\n\n${s.text}`,
    tokenCount: estimateTokens(`Agent: ${agentName}\n\n${s.text}`),
  }))
}

// Reference files (skill/references/*.md) are shared context docs read by agents
// before running (pipeline topology, signal vocabulary, risk thresholds, data sources).
// Each file split by ## — reference name prepended to section label.
function buildReferenceChunks(
  slug: string,
  refName: string,
  markdown: string,
  startIndex: number,
): Chunk[] {
  const sections = splitByH2(markdown)
  if (sections.length === 0) return []

  return sections.map((s, i) => ({
    slug,
    chunkIndex: startIndex + i,
    chunkType: 'reference' as const,
    section: `${refName}${s.section !== 'overview' ? ` / ${s.section}` : ''}`,
    text: `Reference: ${refName}\n\n${s.text}`,
    tokenCount: estimateTokens(`Reference: ${refName}\n\n${s.text}`),
  }))
}

export interface PathFiles {
  yaml: WfPathYaml
  instructions: string | null
  // filename (without .md) → content
  agents: Record<string, string>
  references: Record<string, string>
}

export function chunkPath(files: PathFiles): Chunk[] {
  const { yaml, instructions, agents, references } = files
  const chunks: Chunk[] = []

  chunks.push(buildMetadataChunk(yaml, 0))

  if (instructions) {
    chunks.push(...buildInstructionChunks(yaml.slug, instructions, chunks.length))
  }

  for (const [name, content] of Object.entries(agents)) {
    chunks.push(...buildAgentChunks(yaml.slug, name, content, chunks.length))
  }

  for (const [name, content] of Object.entries(references)) {
    chunks.push(...buildReferenceChunks(yaml.slug, name, content, chunks.length))
  }

  return chunks
}
