# WACTX Algorithm Formulas — Quick Reference

## Core Scoring Formulas

### 1. RRF Fusion (Line 145)
```
score[doc_id] = Σ 1 / (k + rank + 1)
where k = 60
```

### 2. Graph Similarity Boost (Line 267)
```
boosted_similarity = original_similarity × min(2.0, 1.0 + graph_weight / 100.0)
```

### 3. Person Retrieval Score (Line 421)
```
retrieval = max(max_rrf × 100, max_similarity)
```

### 4. Person Graph Score (Lines 422-428)
```
graph = min(1.0,
    (0.3 if dm_volume > 0 else 0)
    + 0.1 × min(3, len(shared_groups))
    + 0.1 × min(3, message_count)
    + 0.05 × min(3, len(entities))
)
```

### 5. Person Conversation Score (Line 429)
```
conv = conversation_boost
```

### 6. Final Person Score (Line 430)
```
score = 0.50 × retrieval + 0.30 × graph + 0.20 × conv
```

### 7. Conversation Boost (Line 383)
```
conversation_boost = min(1.0, thread_length / 8.0)
```

### 8. Candidate Pruning (Line 660)
```
keep_count = max(top_k × 2, int(top_k × 3 × 0.85^i))
```

### 9. Seed Selection (Line 634)
```
top_n = max(5, 20 - i × 2)
```

### 10. Graph Expansion Limit (Line 646)
```
expansion_limit = max(5, top_k × 2 - i × 3)
```

## Edge Weights in Graph Expansion

| Edge Type | Weight Formula | Location |
|-----------|---|---|
| DM (Direct Message) | `message_count × 3.0` | Line 182 |
| Group Co-membership | `message_count × 1.0` | Line 188 |
| Conversation | `exchange_count × 5.0` | Line 198 |
| Entity Mention | `mention_count × 2.0` | Line 204 |

## Depth Presets

| Preset | Variants | Top-K | Graph | Iterations |
|--------|----------|-------|-------|------------|
| fast | 1 | 10 | No | 1 |
| balanced | 5 | 15 | Yes | 3 |
| deep | 8 | 30 | Yes | 3 |

## Candidate Limits Throughout Pipeline

| Stage | Formula | For "balanced" |
|-------|---------|---|
| BM25 search | `top_k × 3` | 45 |
| Vector search | `top_k × 3` | 45 |
| After RRF fusion | `top_k × 3` | 45 |
| After graph iter 0 | `max(30, 45 × 0.85^0) = 45` | 45 |
| After graph iter 1 | `max(30, 45 × 0.85^1) = 38` | 38 |
| After graph iter 2 | `max(30, 45 × 0.85^2) = 32` | 32 |
| Final truncation | `top_k × 4` | 60 |

## Iteration Dynamics (for "balanced" with top_k=15)

### Iteration 0
- Seed selection: `top_n = max(5, 20 - 0×2) = 20`
- Graph expansion: `max(5, 15×2 - 0×3) = 30`
- Pruning: `max(30, 45 × 0.85^0) = 45`

### Iteration 1
- Seed selection: `top_n = max(5, 20 - 1×2) = 18`
- Graph expansion: `max(5, 15×2 - 1×3) = 27`
- Pruning: `max(30, 45 × 0.85^1) = 38`

### Iteration 2
- Seed selection: `top_n = max(5, 20 - 2×2) = 16`
- Graph expansion: `max(5, 15×2 - 2×3) = 24`
- Pruning: `max(30, 45 × 0.85^2) = 32`

## Key Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| RRF k | 60 | Reciprocal rank fusion denominator |
| Similarity boost cap | 2.0 | Max multiplier for graph-boosted similarity |
| Pruning decay | 0.85 | Exponential decay per iteration |
| Pruning min | `top_k × 2` | Minimum candidates to keep |
| Seed decay | 2 | Reduce top_n by 2 per iteration |
| Seed min | 5 | Minimum seeds to select |
| Seed max | 20 | Maximum seeds in iteration 0 |
| Graph vectors | 2 | Only first 2 query vectors for graph |
| Graph neighbor limit | 30 | Max neighbors to expand from |
| Conversation window | ±1 hour | Time range for context |
| Conversation limit | 10 | Max messages in context thread |
| Conversation divisor | 8.0 | Normalize thread length to 0-1 |
| Entity limit | 5 | Max entities per person |
| Graph insights limit | 15 | Max people for insights |

## Scoring Weight Breakdown

### Person Score Composition
- **Retrieval (50%)**: Semantic relevance (RRF + similarity)
- **Graph (30%)**: Relationship strength (DMs, groups, entities)
- **Conversation (20%)**: Contextual richness (thread length)

### Graph Score Components
- **DM Signal**: 0.3 (if dm_volume > 0)
- **Shared Groups**: 0.1 per group (max 0.3 for 3+ groups)
- **Message Count**: 0.1 per message (max 0.3 for 3+ messages)
- **Entities**: 0.05 per entity (max 0.15 for 3+ entities)
- **Total Cap**: 1.0

## Algorithmic Complexity

| Operation | Complexity | Notes |
|-----------|-----------|-------|
| BM25 search | O(n log n) | DuckDB FTS index |
| Vector search | O(n) | HNSW index, cosine similarity |
| RRF fusion | O(m log m) | m = total results |
| Graph expansion | O(k × e) | k = seeds, e = edges per person |
| Person aggregation | O(m) | m = candidate messages |
| Person scoring | O(p) | p = unique people |

## Failure Modes & Fallbacks

| Scenario | Fallback |
|----------|----------|
| No BM25 results | Use vector results only, set `rrf_score = similarity` |
| No graph table | Return empty graph expansion |
| No new seeds (iter < 2) | Break graph loop |
| No new seeds (iter >= 2) | Expand to top_n×2, take top_n//2 |
| No graph expansion results | Log "no new graph neighbours", break |
| Query expansion fails | Return original query only |
| Embedding fails | Exception propagates |
| Contact lookup fails | Use JID as display name |
| Graph enrichment fails | Skip that signal (dm_volume=0, etc.) |

