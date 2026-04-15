# WACTX Search Algorithm Specification

**Source**: `wactx/search.py` (694 lines)
**Date**: 2026-03-21
**Purpose**: Complete algorithmic breakdown for comparison with PathRAG/GraphRAG

---

## 1. RUN_SEARCH — Main Iteration Flow

**Location**: Lines 583-694

### 1.1 Initialization & Parameters

```python
# Lines 594-598: Depth preset resolution
preset = DEPTH_PRESETS.get(depth, DEPTH_PRESETS["balanced"])
n_variants = variants if variants is not None else preset["variants"]
top_k = top if top is not None else preset["top"]
use_graph = preset["graph"] and not no_graph
n_iterations = iterations if iterations is not None else preset.get("iterations", 1)
```

**DEPTH_PRESETS** (Lines 16-20):
```python
DEPTH_PRESETS = {
    "fast":     {"variants": 1, "top": 10, "graph": False, "iterations": 1},
    "balanced": {"variants": 5, "top": 15, "graph": True,  "iterations": 3},
    "deep":     {"variants": 8, "top": 30, "graph": True,  "iterations": 3},
}
```

### 1.2 Query Expansion (Lines 609-612)

```python
queries = expand_query(client, config, query, n_variants)
vectors = embed_queries(client, config, queries)
dims = config.api.embedding_dims
progress.append({"pass": 0, "label": f"Query → {len(queries)} variants"})
```

**expand_query** (Lines 43-62):
- If `n <= 1`: return `[query]` (no expansion)
- Otherwise: Call LLM with `QUERY_EXPANSION_PROMPT` (Lines 22-33)
- LLM generates `n-1` alternative queries
- Returns: `[original_query] + [n-1 alternatives]`
- Fallback: Returns `[query]` on any exception

### 1.3 Candidate Generation — BM25 + Vector Fusion (Lines 614-627)

#### BM25 Search (Lines 76-104)
```python
bm25_results = bm25_search(conn, query, top_k=top_k * 3)
```

**Algorithm**:
```sql
SELECT m.id, m.text_content, m.push_name, m.sender_jid, m.chat_jid,
       m.timestamp, m.media_type, m.media_path,
       fts_main_messages.match_bm25(m.id, ?, fields := 'text_content') AS score
FROM messages m
WHERE score IS NOT NULL
ORDER BY score
LIMIT ?
```

- Searches on `text_content` field only
- Returns top `top_k * 3` results
- Score: DuckDB's native BM25 implementation

#### Vector Search (Lines 107-136)
```python
vector_results = semantic_search(conn, vectors, dims, top_k * 3)
```

**Algorithm**:
```sql
SELECT id, text_content, push_name, sender_jid, chat_jid, timestamp,
       media_type, media_path,
       array_cosine_similarity(embedding, ?::FLOAT[{dims}]) AS similarity
FROM messages WHERE embedding IS NOT NULL
ORDER BY similarity DESC LIMIT ?
```

- Runs for **each query vector** independently
- Cosine similarity: `dot(embedding, query_vec) / (||embedding|| * ||query_vec||)`
- Deduplication: If message appears in multiple query results, keeps **highest similarity**
- Returns top `top_k * 3` unique messages

#### RRF Fusion (Lines 139-155)

```python
if bm25_results:
    candidates = rrf_fuse([("bm25", bm25_results), ("vector", vector_results)])
else:
    candidates = vector_results
    for doc in candidates:
        doc["rrf_score"] = doc.get("similarity", 0.0)
```

**RRF Formula** (Line 145):
```python
scores[doc_id] += 1.0 / (k + rank + 1)
```

Where:
- `k = 60` (constant, Line 139)
- `rank` = 0-indexed position in ranking
- Formula: `1 / (60 + rank + 1)`

**Example**:
- Rank 0: `1/61 ≈ 0.0164`
- Rank 1: `1/62 ≈ 0.0161`
- Rank 10: `1/71 ≈ 0.0141`

**Fusion Process**:
1. For each ranking (BM25, Vector), iterate through results
2. Add RRF score to document
3. Store document metadata (first occurrence)
4. Sort by total RRF score descending
5. Return fused list

**Fallback**: If no BM25 results, use vector results directly with `rrf_score = similarity`

### 1.4 Candidate Truncation (Line 624)

```python
candidates = candidates[: top_k * 3]
```

- Keeps top `top_k * 3` fused candidates
- For "balanced" (top_k=15): keeps top 45 candidates

### 1.5 Graph Expansion Loop (Lines 629-666)

**Condition**: `if use_graph and n_iterations >= 2 and candidates:`

```python
seen_seeds: set[str] = set()
graph_passes = n_iterations - 1

for i in range(graph_passes):
    # Iteration logic
```

#### Seed Selection (Lines 634-643)

```python
top_n = max(5, 20 - i * 2)
seed_jids = list({c["sender_jid"] for c in candidates[:top_n]})
new_seeds = [j for j in seed_jids if j not in seen_seeds]
seen_seeds.update(seed_jids)

if not new_seeds and i >= 2:
    all_jids = list({c["sender_jid"] for c in candidates[: top_n * 2]})
    new_seeds = all_jids[: max(3, top_n // 2)]
if not new_seeds:
    break
```

**Seed Selection Formula**:
- Iteration 0: `top_n = max(5, 20 - 0*2) = 20`
- Iteration 1: `top_n = max(5, 20 - 1*2) = 18`
- Iteration 2: `top_n = max(5, 20 - 2*2) = 16`

**New Seed Logic**:
1. Extract unique sender JIDs from top `top_n` candidates
2. Filter to only unseen JIDs
3. If no new seeds AND iteration >= 2:
   - Expand to top `top_n * 2` candidates
   - Take first `max(3, top_n // 2)` unique JIDs
4. If still no new seeds: break loop

#### Graph Expansion (Lines 645-650)

```python
expanded = graph_expand_candidates(
    conn, new_seeds, vectors, dims, top_k=max(5, top_k * 2 - i * 3)
)
if not expanded:
    progress.append({"pass": i + 2, "label": "no new graph neighbours"})
    break
```

**top_k parameter for graph expansion**:
- Iteration 0: `max(5, 15*2 - 0*3) = 30`
- Iteration 1: `max(5, 15*2 - 1*3) = 27`
- Iteration 2: `max(5, 15*2 - 2*3) = 24`

#### RRF Re-fusion (Lines 652-658)

```python
prev_count = len(candidates)
candidates = rrf_fuse(
    [
        (f"pass{i + 1}", candidates),
        (f"graph_{i + 1}", expanded),
    ]
)
```

- Re-fuses previous candidates with newly expanded graph results
- Uses same RRF formula (k=60)

#### Pruning (Line 660)

```python
candidates = candidates[: max(top_k * 2, int(top_k * 3 * (0.85**i)))]
```

**Pruning Formula**:
```
max(top_k * 2, int(top_k * 3 * (0.85^i)))
```

For "balanced" (top_k=15):
- Iteration 0: `max(30, int(45 * 0.85^0)) = max(30, 45) = 45`
- Iteration 1: `max(30, int(45 * 0.85^1)) = max(30, 38) = 38`
- Iteration 2: `max(30, int(45 * 0.85^2)) = max(30, 32) = 32`

**Bounds**:
- Minimum: `top_k * 2` (30 for balanced)
- Maximum: `top_k * 3 * 0.85^i` (decays exponentially)

### 1.6 Final Candidate Truncation (Line 667)

```python
candidates = candidates[: top_k * 4]
```

- Keeps top `top_k * 4` candidates
- For "balanced": keeps top 60 candidates

### 1.7 Enrichment & Context (Lines 669-675)

```python
candidates = enrich_results(conn, candidates, config.search.owner_name, use_graph)
progress.append(
    {"pass": "enrich", "label": f"Enriched {len(candidates)} with graph signals"}
)

if n_iterations >= 3:
    candidates = fetch_conversation_context(conn, candidates, limit=top_k)
```

- Enriches with graph signals (see Section 4)
- If deep search (n_iterations >= 3): fetches conversation context

### 1.8 People Aggregation & Scoring (Lines 677-682)

```python
people = find_related_people(candidates)
insights = (
    compute_graph_insights(conn, people[:15], config.search.owner_name)
    if use_graph
    else {}
)
```

- Aggregates messages by sender (see Section 3)
- Computes graph insights from top 15 people

---

## 2. GRAPH_EXPAND_CANDIDATES — Graph Traversal

**Location**: Lines 158-277

### 2.1 Seed Validation (Lines 167-168)

```python
if not table_exists(conn, "graph_persons") or not seed_jids:
    return []
```

- Returns empty if graph not built or no seeds provided

### 2.2 Neighbor Discovery via 4 Edge Types (Lines 173-228)

#### Via DM (Direct Messages) — Lines 178-182

```sql
via_dm AS (
    SELECT CASE
        WHEN epm.sender_person_id IN (SELECT person_id FROM seed)
        THEN epm.receiver_person_id ELSE epm.sender_person_id
    END AS person_id, epm.message_count * 3.0 AS weight
    FROM edge_person_messaged epm
    WHERE epm.sender_person_id IN (SELECT person_id FROM seed)
       OR epm.receiver_person_id IN (SELECT person_id FROM seed)
)
```

**Weight**: `message_count * 3.0`

#### Via Group — Lines 187-192

```sql
via_group AS (
    SELECT e2.person_id, e2.message_count * 1.0 AS weight
    FROM edge_person_in_group e1
    JOIN edge_person_in_group e2 ON e1.group_jid = e2.group_jid
    WHERE e1.person_id IN (SELECT person_id FROM seed)
      AND e2.person_id NOT IN (SELECT person_id FROM seed)
)
```

**Weight**: `message_count * 1.0`

#### Via Conversation — Lines 194-201

```sql
via_conversation AS (
    SELECT CASE
        WHEN epc.person1_id IN (SELECT person_id FROM seed)
        THEN epc.person2_id ELSE epc.person1_id
    END AS person_id, epc.exchange_count * 5.0 AS weight
    FROM edge_person_conversed epc
    WHERE epc.person1_id IN (SELECT person_id FROM seed)
       OR epc.person2_id IN (SELECT person_id FROM seed)
)
```

**Weight**: `exchange_count * 5.0`

#### Via Entity Mention — Lines 203-208

```sql
via_entity AS (
    SELECT e2.person_id, e2.mention_count * 2.0 AS weight
    FROM edge_person_mentions_entity e1
    JOIN edge_person_mentions_entity e2 ON e1.entity_id = e2.entity_id
    WHERE e1.person_id IN (SELECT person_id FROM seed)
      AND e2.person_id NOT IN (SELECT person_id FROM seed)
)
```

**Weight**: `mention_count * 2.0`

### 2.3 Weight Aggregation (Lines 210-225)

```sql
all_neighbours AS (
    SELECT person_id, SUM(weight) AS total_weight
    FROM (
        SELECT * FROM via_dm
        UNION ALL SELECT * FROM via_group
        UNION ALL SELECT * FROM via_conversation
        UNION ALL SELECT * FROM via_entity
    ) combined
    WHERE person_id NOT IN (SELECT person_id FROM seed)
    GROUP BY person_id
)
SELECT gp.source_id, an.total_weight
FROM all_neighbours an
JOIN graph_persons gp ON an.person_id = gp.person_id
ORDER BY an.total_weight DESC
LIMIT 30
```

**Aggregation**:
- Sums weights across all edge types
- Excludes seed persons
- Returns top 30 neighbors by total weight

### 2.4 Message Retrieval from Neighbors (Lines 240-269)

```python
neighbour_jids = [r[0] for r in neighbour_rows]
neighbour_weights = {r[0]: r[1] for r in neighbour_rows}

expanded = []
for qvec in vectors[:2]:  # Only first 2 query vectors
    rows = conn.execute(
        f"""SELECT id, text_content, push_name, sender_jid, chat_jid, timestamp,
                   media_type, media_path,
                   array_cosine_similarity(embedding, ?::FLOAT[{dims}]) AS similarity
            FROM messages
            WHERE embedding IS NOT NULL
              AND sender_jid IN ({n_placeholders})
            ORDER BY similarity DESC LIMIT ?""",
        [qvec] + neighbour_jids + [top_k],
    ).fetchall()
```

**Retrieval**:
- Uses only **first 2 query vectors** (not all variants)
- Searches messages from neighbor JIDs
- Cosine similarity ranking
- Returns top `top_k` per vector

### 2.5 Similarity Boosting by Graph Weight (Lines 255-269)

```python
for r in rows:
    graph_weight = float(neighbour_weights.get(r[3], 1.0))
    expanded.append(
        {
            "id": r[0],
            "text": r[1],
            "sender": r[2],
            "sender_jid": r[3],
            "chat_jid": r[4],
            "time": r[5],
            "media_type": r[6],
            "media_path": r[7],
            "similarity": float(r[8]) * min(2.0, 1.0 + graph_weight / 100.0),
        }
    )
```

**Similarity Boost Formula**:
```
boosted_similarity = original_similarity * min(2.0, 1.0 + graph_weight / 100.0)
```

**Bounds**:
- Minimum multiplier: `1.0` (when graph_weight = 0)
- Maximum multiplier: `2.0` (when graph_weight >= 100)

**Examples**:
- graph_weight = 0: multiplier = 1.0 (no boost)
- graph_weight = 50: multiplier = 1.5 (50% boost)
- graph_weight = 100: multiplier = 2.0 (100% boost, capped)
- graph_weight = 200: multiplier = 2.0 (capped at 2.0)

### 2.6 Deduplication (Lines 271-277)

```python
seen = set()
unique = []
for doc in sorted(expanded, key=lambda x: x["similarity"], reverse=True):
    if doc["id"] not in seen:
        seen.add(doc["id"])
        unique.append(doc)
return unique
```

- Sorts by boosted similarity descending
- Keeps first occurrence of each message ID
- Returns deduplicated list

---

## 3. FIND_RELATED_PEOPLE — Person Aggregation & Scoring

**Location**: Lines 387-432

### 3.1 Message Aggregation by Sender (Lines 388-418)

```python
by_person: dict[str, dict] = {}
for r in results:
    jid = r["sender_jid"]
    if jid not in by_person:
        by_person[jid] = {
            "display_name": r.get("display_name", r.get("sender", "?")),
            "phone": r.get("phone", ""),
            "sender_jid": jid,
            "max_similarity": r.get("similarity", 0.0),
            "max_rrf": r.get("rrf_score", 0.0),
            "message_count": 0,
            "dm_volume": r.get("dm_volume", 0),
            "shared_groups": r.get("shared_groups", []),
            "entities": r.get("entities", []),
            "conversation_boost": r.get("conversation_boost", 0.0),
            "messages": [],
        }
    p = by_person[jid]
    p["message_count"] += 1
    p["max_similarity"] = max(p["max_similarity"], r.get("similarity", 0.0))
    p["max_rrf"] = max(p["max_rrf"], r.get("rrf_score", 0.0))
    p["conversation_boost"] = max(
        p["conversation_boost"], r.get("conversation_boost", 0.0)
    )
    if r.get("dm_volume", 0) > p["dm_volume"]:
        p["dm_volume"] = r["dm_volume"]
    if len(r.get("shared_groups", [])) > len(p["shared_groups"]):
        p["shared_groups"] = r["shared_groups"]
    if len(r.get("entities", [])) > len(p["entities"]):
        p["entities"] = r["entities"]
    p["messages"].append(r)
```

**Aggregation Strategy**:
- Group messages by `sender_jid`
- Per person, track:
  - `message_count`: Total messages from this person
  - `max_similarity`: Highest cosine similarity
  - `max_rrf`: Highest RRF score
  - `dm_volume`: DM message count (from graph)
  - `shared_groups`: Groups shared with owner
  - `entities`: Extracted entities mentioned
  - `conversation_boost`: Max conversation context boost

### 3.2 Person Scoring (Lines 420-430)

```python
for p in by_person.values():
    retrieval = max(p["max_rrf"] * 100, p["max_similarity"])
    graph = min(
        1.0,
        (0.3 if p["dm_volume"] > 0 else 0)
        + 0.1 * min(3, len(p["shared_groups"]))
        + 0.1 * min(3, p["message_count"])
        + 0.05 * min(3, len(p["entities"])),
    )
    conv = p["conversation_boost"]
    p["score"] = 0.50 * retrieval + 0.30 * graph + 0.20 * conv
```

#### Retrieval Score (Line 421)

```
retrieval = max(max_rrf * 100, max_similarity)
```

**Components**:
- `max_rrf * 100`: RRF score scaled to 0-1 range (assuming max RRF ≈ 0.01)
- `max_similarity`: Cosine similarity (0-1 range)
- Takes maximum of both

#### Graph Score (Lines 422-428)

```
graph = min(1.0,
    (0.3 if dm_volume > 0 else 0)
    + 0.1 * min(3, len(shared_groups))
    + 0.1 * min(3, message_count)
    + 0.05 * min(3, len(entities))
)
```

**Components**:
1. **DM Signal**: 0.3 if `dm_volume > 0`, else 0
2. **Shared Groups**: `0.1 * min(3, len(shared_groups))`
   - 0 groups: 0.0
   - 1 group: 0.1
   - 2 groups: 0.2
   - 3+ groups: 0.3
3. **Message Count**: `0.1 * min(3, message_count)`
   - 0 messages: 0.0
   - 1 message: 0.1
   - 2 messages: 0.2
   - 3+ messages: 0.3
4. **Entities**: `0.05 * min(3, len(entities))`
   - 0 entities: 0.0
   - 1 entity: 0.05
   - 2 entities: 0.1
   - 3+ entities: 0.15

**Maximum**: Capped at 1.0

#### Conversation Score (Line 429)

```
conv = conversation_boost
```

- Direct pass-through of max conversation boost
- Computed in `fetch_conversation_context` (see Section 4.2)

#### Final Person Score (Line 430)

```
score = 0.50 * retrieval + 0.30 * graph + 0.20 * conv
```

**Weights**:
- Retrieval: 50% (semantic relevance)
- Graph: 30% (relationship strength)
- Conversation: 20% (contextual richness)

### 3.3 Sorting (Line 432)

```python
return sorted(by_person.values(), key=lambda x: x["score"], reverse=True)
```

- Returns people sorted by score descending

---

## 4. ENRICH_RESULTS — Graph Signal Computation

**Location**: Lines 280-356

### 4.1 Contact Enrichment (Lines 288-304)

```python
for r in results:
    row = conn.execute(
        "SELECT COALESCE(group_name, push_name, jid), is_group FROM contacts WHERE jid = ?",
        [r["chat_jid"]],
    ).fetchone()
    r["group_name"] = (row[0] if row[1] else "DM") if row else r["chat_jid"]

    row = conn.execute(
        "SELECT push_name, full_name, jid FROM contacts WHERE jid = ?",
        [r["sender_jid"]],
    ).fetchone()
    if row:
        r["display_name"] = row[1] or row[0] or row[2]
        r["phone"] = _phone_from_jid(row[2])
    else:
        r["display_name"] = r["sender"] or "?"
        r["phone"] = _phone_from_jid(r["sender_jid"])
```

**Enrichment**:
- `group_name`: Group name or "DM" if direct message
- `display_name`: Full name > push name > JID
- `phone`: Extracted from JID (format: +{digits})

### 4.2 Graph Signal Initialization (Lines 306-308)

```python
r.setdefault("dm_volume", 0)
r.setdefault("shared_groups", [])
r.setdefault("entities", [])
```

- Initializes graph signals to defaults

### 4.3 Conditional Graph Enrichment (Lines 310-354)

```python
if not use_graph:
    continue
```

- Only enriches if graph is enabled

#### DM Volume (Lines 313-323)

```python
try:
    dm = conn.execute(
        """SELECT COALESCE(SUM(message_count), 0)
           FROM edge_person_messaged epm
           JOIN graph_persons gp ON (epm.sender_person_id = gp.person_id OR epm.receiver_person_id = gp.person_id)
           WHERE gp.source_id = ?""",
        [r["sender_jid"]],
    ).fetchone()
    r["dm_volume"] = dm[0] if dm else 0
except Exception:
    pass
```

- Sums all DM message counts for this person
- Fallback: 0 on error

#### Shared Groups (Lines 325-339)

```python
try:
    if owner_name:
        shared = conn.execute(
            """SELECT LIST(DISTINCT gg.group_name)
               FROM edge_person_in_group e1
               JOIN edge_person_in_group e2 ON e1.group_jid = e2.group_jid
               JOIN graph_groups gg ON e1.group_jid = gg.group_jid
               JOIN graph_persons gp1 ON e1.person_id = gp1.person_id
               JOIN graph_persons gp2 ON e2.person_id = gp2.person_id
               WHERE gp1.source_id = ? AND gp2.display_name = ?""",
            [r["sender_jid"], owner_name],
        ).fetchone()
        r["shared_groups"] = shared[0] if shared and shared[0] else []
except Exception:
    pass
```

- Finds groups where both person and owner are members
- Returns list of group names
- Only if `owner_name` is configured

#### Entities (Lines 341-354)

```python
try:
    if table_exists(conn, "edge_person_mentions_entity"):
        entities = conn.execute(
            """SELECT ge.entity_type, ge.entity_value, epm.mention_count
               FROM edge_person_mentions_entity epm
               JOIN graph_entities ge ON epm.entity_id = ge.entity_id
               JOIN graph_persons gp ON epm.person_id = gp.person_id
               WHERE gp.source_id = ?
               ORDER BY epm.mention_count DESC LIMIT 5""",
            [r["sender_jid"]],
        ).fetchall()
        r["entities"] = [(t, v, c) for t, v, c in entities]
except Exception:
    pass
```

- Retrieves top 5 entities mentioned by this person
- Format: `(entity_type, entity_value, mention_count)`
- Sorted by mention count descending

---

## 5. FETCH_CONVERSATION_CONTEXT — Contextual Enrichment

**Location**: Lines 359-384

### 5.1 Context Retrieval (Lines 362-381)

```python
for r in results[:limit]:
    try:
        thread = conn.execute(
            """SELECT push_name, text_content, timestamp
               FROM messages
               WHERE chat_jid = ?
                 AND timestamp BETWEEN ?::TIMESTAMPTZ - INTERVAL '1 hour'
                                   AND ?::TIMESTAMPTZ + INTERVAL '1 hour'
                 AND text_content IS NOT NULL
               ORDER BY timestamp
               LIMIT 10""",
            [r["chat_jid"], r["time"], r["time"]],
        ).fetchall()
        r["conversation_thread"] = (
            [{"sender": t[0] or "?", "text": t[1], "time": t[2]} for t in thread]
            if thread
            else []
        )
    except Exception:
        r["conversation_thread"] = []
```

**Context Window**:
- Time range: ±1 hour from message timestamp
- Limit: 10 messages
- Only messages with text content

### 5.2 Conversation Boost (Line 383)

```python
r["conversation_boost"] = min(1.0, len(r.get("conversation_thread", [])) / 8.0)
```

**Boost Formula**:
```
conversation_boost = min(1.0, thread_length / 8.0)
```

**Examples**:
- 0 messages: 0.0
- 4 messages: 0.5
- 8 messages: 1.0
- 10 messages: 1.0 (capped)

---

## 6. SUPPORTING FUNCTIONS

### 6.1 BM25 Search (Lines 76-104)

**Query**: Full-text search on `text_content` field
**Scoring**: DuckDB native BM25
**Returns**: Top `top_k` results with BM25 score

### 6.2 Semantic Search (Lines 107-136)

**Query**: Cosine similarity against query embeddings
**Deduplication**: Keeps highest similarity per message across all query vectors
**Returns**: Top `top_k` unique results

### 6.3 RRF Fusion (Lines 139-155)

**Formula**: `1 / (k + rank + 1)` where k=60
**Aggregation**: Sums scores across rankings
**Returns**: Sorted by total RRF score

### 6.4 Query Expansion (Lines 43-62)

**Method**: LLM-based (Claude/GPT)
**Prompt**: Generates semantic variants (Lines 22-33)
**Returns**: Original query + n-1 alternatives

### 6.5 Embedding (Lines 65-73)

**Method**: OpenAI-compatible API
**Batch**: All queries in single call
**Returns**: List of embedding vectors

---

## 7. CONSTANTS & HYPERPARAMETERS

| Parameter | Value | Location | Purpose |
|-----------|-------|----------|---------|
| RRF k | 60 | Line 139 | Reciprocal rank fusion constant |
| DM weight | 3.0 | Line 182 | Graph edge weight for DMs |
| Group weight | 1.0 | Line 188 | Graph edge weight for group co-membership |
| Conversation weight | 5.0 | Line 198 | Graph edge weight for conversations |
| Entity weight | 2.0 | Line 204 | Graph edge weight for entity mentions |
| Graph neighbor limit | 30 | Line 225 | Max neighbors to expand from |
| Graph vectors used | 2 | Line 240 | Only first 2 query vectors for graph |
| Similarity boost cap | 2.0 | Line 267 | Max multiplier for graph-boosted similarity |
| Pruning decay | 0.85 | Line 660 | Exponential decay per iteration |
| Pruning min | top_k * 2 | Line 660 | Minimum candidates to keep |
| Pruning max | top_k * 3 * 0.85^i | Line 660 | Maximum candidates per iteration |
| Candidate truncation | top_k * 4 | Line 667 | Final candidate limit |
| Retrieval weight | 0.50 | Line 430 | Person score: semantic relevance |
| Graph weight | 0.30 | Line 430 | Person score: relationship strength |
| Conversation weight | 0.20 | Line 430 | Person score: contextual richness |
| DM signal | 0.3 | Line 424 | Graph score: DM presence |
| Shared groups signal | 0.1 | Line 425 | Graph score: per group (max 0.3) |
| Message count signal | 0.1 | Line 426 | Graph score: per message (max 0.3) |
| Entity signal | 0.05 | Line 427 | Graph score: per entity (max 0.15) |
| Graph score cap | 1.0 | Line 428 | Max graph signal |
| Conversation boost divisor | 8.0 | Line 383 | Normalize thread length to 0-1 |
| Conversation boost cap | 1.0 | Line 383 | Max conversation boost |
| Seed selection decay | 2 | Line 634 | Reduce top_n by 2 per iteration |
| Seed min | 5 | Line 634 | Minimum seeds to select |
| Seed max | 20 | Line 634 | Maximum seeds in iteration 0 |
| Fallback seed ratio | 0.5 | Line 641 | When no new seeds: take top_n // 2 |
| Fallback seed min | 3 | Line 641 | Minimum fallback seeds |
| Graph top_k decay | 3 | Line 646 | Reduce graph expansion by 3 per iteration |
| Graph top_k min | 5 | Line 646 | Minimum graph expansion limit |

---

## 8. ALGORITHMIC GAPS vs. PATHRAG/GRAPHRAG

### Known Limitations

1. **RRF Fusion**: Simple reciprocal rank fusion (k=60) vs. learned fusion weights
2. **Graph Weights**: Fixed multipliers (3x DM, 5x conversation) vs. learned edge weights
3. **Similarity Boosting**: Linear formula `1.0 + graph_weight/100` vs. learned boosting
4. **Seed Selection**: Greedy top-N vs. diversity-aware sampling
5. **Pruning**: Exponential decay (0.85^i) vs. adaptive thresholding
6. **Person Scoring**: Fixed weights (0.50/0.30/0.20) vs. learned combination
7. **Graph Signals**: Additive (0.3 + 0.1*groups + ...) vs. multiplicative or learned
8. **Conversation Context**: Simple thread length / 8 vs. semantic relevance scoring
9. **Entity Signals**: Mention count only vs. entity importance/centrality
10. **Iteration Strategy**: Fixed iterations vs. adaptive stopping criteria

### Potential Improvements

- Learn RRF k parameter from training data
- Learn edge weights from relationship importance
- Use learned similarity boosting function
- Implement diversity-aware seed selection
- Adaptive pruning based on score distribution
- Learn person scoring weights
- Multiplicative graph signal combination
- Semantic conversation context scoring
- Entity centrality/PageRank scoring
- Adaptive iteration stopping

