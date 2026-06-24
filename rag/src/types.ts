export interface WfPathYaml {
  schema_version: string
  slug: string
  name: string
  version: string
  summary: string
  primary_kind: 'monitor' | 'strategy' | 'policy' | 'bundle'
  tags: string[]
  components?: Array<{ id: string; kind: string; path: string }>
  skill?: {
    enabled: boolean
    source: string
    name: string
    description: string
    instructions?: string
    runtime?: Record<string, unknown>
  }
  pipeline?: {
    archetype?: string
    entry_command?: string
    output_contract?: string[]
  }
  agents?: Array<{
    id: string
    phase: string
    description: string
    tools: string[]
    output: string
    host_mode?: string
  }>
  inputs?: {
    slots?: Record<string, {
      type: string
      required: boolean
      description?: string
    }>
  }
}

export interface Chunk {
  slug: string
  chunkIndex: number
  chunkType: 'metadata' | 'instructions' | 'agent' | 'reference'
  section: string | null
  text: string
  tokenCount: number
}

export interface ChunkWithEmbedding extends Chunk {
  embedding: number[]
}

export interface SearchResult {
  chunkId: string
  documentId: string
  slug: string
  title: string
  section: string | null
  chunkType: string
  text: string
  similarity: number
}
