-- Enable pgvector. In Supabase this extension is pre-installed; this is a no-op if already enabled.
-- What CREATE EXTENSION vector installs: a new data type (vector), three operator classes
-- (vector_l2_ops, vector_ip_ops, vector_cosine_ops), and index methods (IVFFlat, HNSW).
create extension if not exists vector;

-- documents: one row per Wayfinder Path (the source unit)
-- Storing metadata here avoids duplicating it across every chunk row.
create table if not exists documents (
  id           uuid primary key default gen_random_uuid(),
  slug         text unique not null,
  title        text not null,
  primary_kind text,
  tags         text[],
  version      text,
  metadata     jsonb,
  created_at   timestamptz default now()
);

-- chunks: the embeddable units. Each chunk belongs to a document.
-- chunk_type: 'metadata' (structured YAML fields rendered as text) or 'instructions' (prose from skill/instructions.md)
-- section: heading label from the source markdown, or 'metadata' for the structured chunk
create table if not exists chunks (
  id          uuid primary key default gen_random_uuid(),
  document_id uuid references documents(id) on delete cascade,
  chunk_index integer not null,
  chunk_type  text not null check (chunk_type in ('metadata', 'instructions')),
  section     text,
  text        text not null,
  token_count integer,
  -- voyage-3 outputs 1024-dimensional normalized vectors.
  -- We use cosine distance (<=>). Since vectors are unit-normalized,
  -- cosine and dot-product rankings are identical; cosine is clearer to read.
  -- No index yet: at <1000 chunks, sequential scan is faster than IVFFlat/HNSW
  -- (index overhead exceeds savings). Add HNSW at ~10k+ chunks.
  embedding   vector(1024),
  created_at  timestamptz default now()
);

-- Retrieval function: embed query externally, pass as argument, get ranked chunks back.
-- Returns chunk text + document metadata so callers don't need a second JOIN.
create or replace function match_chunks(
  query_embedding vector(1024),
  match_count     int default 5,
  min_similarity  float default 0.0
)
returns table (
  chunk_id    uuid,
  document_id uuid,
  slug        text,
  title       text,
  section     text,
  chunk_type  text,
  text        text,
  similarity  float
)
language sql stable
as $$
  select
    c.id          as chunk_id,
    c.document_id,
    d.slug,
    d.title,
    c.section,
    c.chunk_type,
    c.text,
    1 - (c.embedding <=> query_embedding) as similarity
  from chunks c
  join documents d on d.id = c.document_id
  where c.embedding is not null
    and 1 - (c.embedding <=> query_embedding) >= min_similarity
  order by c.embedding <=> query_embedding
  limit match_count;
$$;
