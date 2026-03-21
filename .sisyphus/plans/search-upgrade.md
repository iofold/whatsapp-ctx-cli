# Search Upgrade: Post-Sync Pipeline + Conversation Context + Multi-Iteration Search

## Phase 1: Post-Sync Pipeline (auto index + enrich + graph)

### 1.1 Fix enrich bug (entities.py)
- Line 174: check `classifications` has rows, not just exists
- `has_cl = table_exists(conn, "classifications") and conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] > 0`
- When no classifications, extract from ALL group messages with text

### 1.2 Add pipeline orchestrator (new: wactx/pipeline.py)
```python
async def post_sync_pipeline(config: Config) -> dict:
    """Run index + enrich in parallel, then graph."""
    conn = get_connection(config)
    
    # Phase A: parallel index + enrich
    index_result = await run_pipeline(config)           # embed.py
    enrich_result = await extract_entities(conn, config, process_all=True)
    
    # Phase B: graph (depends on both)
    graph_result = build_graph(conn, config)
    conn.close()
    return {"indexed": index_result, "entities": enrich_result, "graph": graph_result}
```

### 1.3 Wire into sync command (cli.py)
- After `sync_whatsapp()` returns, auto-run `post_sync_pipeline()`
- Add `--no-post-process` flag to skip
- `init` command uses same pipeline

---

## Phase 2: Conversation-Aware Embeddings (embed.py)

### 2.1 Context window at embed time
Instead of embedding bare `text_content`, prepend/append N surrounding messages from same chat:

```sql
SELECT m.id, m.chat_jid, m.text_content, m.timestamp,
       (SELECT STRING_AGG(m2.push_name || ': ' || m2.text_content, '\n' ORDER BY m2.timestamp)
        FROM messages m2
        WHERE m2.chat_jid = m.chat_jid
          AND m2.timestamp BETWEEN m.timestamp - INTERVAL '30 minutes' AND m.timestamp
          AND m2.id != m.id
          AND m2.text_content IS NOT NULL
        LIMIT 3) AS context_before,
       (SELECT STRING_AGG(m2.push_name || ': ' || m2.text_content, '\n' ORDER BY m2.timestamp)
        FROM messages m2
        WHERE m2.chat_jid = m.chat_jid
          AND m2.timestamp BETWEEN m.timestamp AND m.timestamp + INTERVAL '30 minutes'
          AND m2.id != m.id
          AND m2.text_content IS NOT NULL
        LIMIT 3) AS context_after
FROM messages m
WHERE m.embedding IS NULL AND m.text_content IS NOT NULL AND TRIM(m.text_content) != ''
ORDER BY m.id
```

Embed text becomes: `[context_before]\n---\n[message]\n---\n[context_after]`

Keeps within 8000 char limit by truncating context, prioritizing the message itself.

---

## Phase 3: Multi-Iteration Search (search.py)

### Current pipeline:
```
expand_query → embed_queries → semantic_search (HNSW) → enrich → score → render
```

### New pipeline (3 iterations):
```
ITERATION 1: Candidate Generation
  ├─ BM25 keyword search (new) → top 50
  └─ Vector HNSW search (existing) → top 50
  └─ RRF fusion → top 100 candidates

ITERATION 2: Graph Neighbour Expansion  
  ├─ From top candidates, find their graph neighbours
  ├─ 1-hop: DMs, shared groups, entity co-mentions
  ├─ Fetch messages from graph neighbours not yet in pool
  └─ Add to candidate pool

ITERATION 3: Reranking
  ├─ For each candidate, compute:
  │   - retrieval_score (from RRF)
  │   - dm_volume with owner
  │   - shared_group_count with owner
  │   - entity_overlap with query
  │   - conversation_context (surrounding messages)
  └─ Final: 0.55 * retrieval + 0.25 * graph + 0.20 * conversation_relevance
```

### 3.1 BM25 Search (new function)
DuckDB has `fts` extension. Create FTS index on messages.text_content:
```sql
INSTALL fts; LOAD fts;
PRAGMA create_fts_index('messages', 'id', 'text_content');
```

Query:
```sql
SELECT id, text_content, fts_main_messages.match_bm25(id, ?) AS bm25_score
FROM messages
WHERE bm25_score IS NOT NULL
ORDER BY bm25_score DESC
LIMIT 50
```

### 3.2 RRF Fusion
```python
def rrf_fuse(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    scores = defaultdict(float)
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking):
            scores[doc_id] += 1.0 / (k + rank + 1)
    return scores
```

### 3.3 Graph Neighbour Expansion
From top-K people in iteration 1 results:
```sql
-- Find people who DM'd the same people, share groups, or mention same entities
WITH seed_persons AS (
    SELECT DISTINCT gp.person_id
    FROM graph_persons gp
    WHERE gp.source_id IN (?)  -- top candidate sender_jids
),
neighbours AS (
    -- Via DMs
    SELECT CASE WHEN epm.sender_person_id IN (SELECT person_id FROM seed_persons)
                THEN epm.receiver_person_id ELSE epm.sender_person_id END AS person_id,
           epm.message_count AS weight
    FROM edge_person_messaged epm
    WHERE epm.sender_person_id IN (SELECT person_id FROM seed_persons)
       OR epm.receiver_person_id IN (SELECT person_id FROM seed_persons)
    UNION ALL
    -- Via shared groups
    SELECT e2.person_id, e2.message_count AS weight
    FROM edge_person_in_group e1
    JOIN edge_person_in_group e2 ON e1.group_jid = e2.group_jid
    WHERE e1.person_id IN (SELECT person_id FROM seed_persons)
      AND e2.person_id NOT IN (SELECT person_id FROM seed_persons)
)
SELECT gp.source_id, gp.display_name, SUM(weight) AS expansion_score
FROM neighbours n
JOIN graph_persons gp ON n.person_id = gp.person_id
GROUP BY gp.source_id, gp.display_name
ORDER BY expansion_score DESC
LIMIT 20
```

Then fetch top messages from expanded people via vector similarity.

### 3.4 Conversation Context at Search Time
For top results, fetch surrounding messages for display (not re-embedding):
```sql
SELECT push_name, text_content, timestamp
FROM messages
WHERE chat_jid = ?
  AND timestamp BETWEEN ? - INTERVAL '1 hour' AND ? + INTERVAL '1 hour'
ORDER BY timestamp
LIMIT 10
```

Attach as `conversation_thread` to each search result for richer display.

### 3.5 Reranking Score
```python
final_score = (
    0.55 * retrieval_score +      # RRF from iteration 1
    0.25 * graph_proximity +       # DM volume + shared groups + entity overlap
    0.20 * conversation_boost      # messages in active conversations score higher
)
```

`conversation_boost` = normalized count of messages in the same chat within ±1 hour window.

---

## Files to modify

| File | Changes |
|------|---------|
| `wactx/pipeline.py` | NEW — post-sync orchestrator |
| `wactx/entities.py` | Fix classifications bug (line 174) |
| `wactx/embed.py` | Add context window to embedding text |
| `wactx/search.py` | Rewrite to 3-iteration pipeline |
| `wactx/db.py` | Add FTS index creation |
| `wactx/cli.py` | Wire post-sync pipeline, add --no-post-process |
| `wactx/render.py` | Add conversation thread display |

## Estimated scope
- Phase 1 (pipeline): ~30 min
- Phase 2 (context embeddings): ~20 min
- Phase 3 (search upgrade): ~60 min
