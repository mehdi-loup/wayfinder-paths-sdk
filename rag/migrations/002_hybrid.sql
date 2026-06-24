-- Hybrid search: vector cosine similarity + PostgreSQL full-text search (BM25 approximation).
-- Fused via Reciprocal Rank Fusion (RRF): score = 1/(k + rank_vector) + 1/(k + rank_bm25).
-- k=60 is the standard value from the original RRF paper (Cormack et al. 2009).
--
-- Why RRF over linear score combination?
-- Cosine similarity and ts_rank_cd are on incompatible scales (0-1 vs unbounded).
-- RRF sidesteps this by working on rank positions, which are always comparable.
-- A chunk missing from BM25 results still contributes its vector rank; it just gets
-- no BM25 bonus. Chunks strong in both lists get additive lift.
create or replace function hybrid_match_chunks(
  query_embedding vector(1024),
  query_text      text,
  match_count     int   default 5,
  rrf_k           float default 60.0
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
  with
  -- Rank all embedded chunks by vector cosine distance
  vector_ranked as (
    select
      c.id,
      row_number() over (order by c.embedding <=> query_embedding) as rank
    from chunks c
    where c.embedding is not null
  ),
  -- Build OR tsquery from query lexemes so ANY matching term scores the chunk.
  -- plainto_tsquery ANDs all terms — useless when query terms span multiple chunks.
  -- We extract stems from to_tsvector() and join with | for OR semantics.
  -- ts_rank_cd then scores by cover density of matched terms (tf-idf proxy).
  or_tsq as (
    select to_tsquery(
      'english',
      string_agg(lexeme, ' | ')
    ) as q
    from unnest(to_tsvector('english', query_text)) as t(lexeme, positions, weights)
  ),
  bm25_ranked as (
    select
      c.id,
      row_number() over (
        order by ts_rank_cd(
          to_tsvector('english', c.text),
          (select q from or_tsq)
        ) desc
      ) as rank
    from chunks c
    where c.embedding is not null
      and to_tsvector('english', c.text) @@ (select q from or_tsq)
  ),
  -- Fuse via RRF; chunks absent from BM25 get 0 BM25 contribution
  fused as (
    select
      coalesce(v.id, b.id) as id,
      coalesce(1.0 / (rrf_k + v.rank), 0.0)
        + coalesce(1.0 / (rrf_k + b.rank), 0.0) as rrf_score
    from vector_ranked v
    full outer join bm25_ranked b on v.id = b.id
    order by rrf_score desc
    limit match_count
  )
  select
    c.id          as chunk_id,
    c.document_id,
    d.slug,
    d.title,
    c.section,
    c.chunk_type,
    c.text,
    f.rrf_score   as similarity
  from fused f
  join chunks c on c.id = f.id
  join documents d on d.id = c.document_id
  order by f.rrf_score desc;
$$;
