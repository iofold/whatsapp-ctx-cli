# WACTX vs. PathRAG/GraphRAG — Algorithmic Comparison Framework

## Executive Summary

WACTX implements a **multi-pass graph-augmented retrieval** system with:
- **Retrieval**: BM25 + semantic search with RRF fusion
- **Graph Expansion**: 4-edge-type traversal with fixed weights
- **Ranking**: Additive scoring combining retrieval, graph, and conversation signals
- **Iteration**: Fixed 3-pass loop with exponential pruning

This document maps WACTX components to PathRAG/GraphRAG concepts for gap analysis.

---

## 1. Retrieval Stage Comparison

### WACTX Approach
```
BM25(query) → top_k×3
Vector(query_variants) → top_k×3 per variant
RRF_Fusion(BM25, Vector, k=60) → top_k×3
```

### PathRAG/GraphRAG Approach (Expected)
- Learned fusion weights (vs. fixed RRF k=60)
- Adaptive query expansion (vs. LLM-based variants)
- Learned similarity metrics (vs. cosine similarity)
- Ranking with learned parameters

### Gap Analysis
| Aspect | WACTX | Research | Gap |
|--------|-------|----------|-----|
| Fusion method | RRF (k=60) | Learned weights | Fixed vs. learned |
| Query variants | LLM-generated | Learned expansion | Heuristic vs. learned |
| Similarity metric | Cosine | Learned metric | Fixed vs. learned |
| Ranking | Additive | Learned combination | Heuristic vs. learned |

---

## 2. Graph Expansion Stage Comparison

### WACTX Approach
```
Seed Selection (top_n = 20 - 2i)
  ↓
Neighbor Discovery (4 edge types)
  ├─ DM: message_count × 3.0
  ├─ Group: message_count × 1.0
  ├─ Conversation: exchange_count × 5.0
  └─ Entity: mention_count × 2.0
  ↓
Weight Aggregation (SUM)
  ↓
Top 30 neighbors
  ↓
Message Retrieval (cosine similarity)
  ↓
Similarity Boost (1.0 + graph_weight/100, capped at 2.0)
```

### PathRAG/GraphRAG Approach (Expected)
- Learned edge weights (vs. fixed multipliers)
- Learned neighbor selection (vs. top-30 limit)
- Learned similarity boosting (vs. linear formula)
- Adaptive seed selection (vs. greedy top-N)
- Diversity-aware expansion (vs. greedy)

### Gap Analysis
| Aspect | WACTX | Research | Gap |
|--------|-------|----------|-----|
| Edge weights | Fixed (3.0, 1.0, 5.0, 2.0) | Learned | Heuristic vs. learned |
| Neighbor limit | Fixed (30) | Adaptive | Fixed vs. adaptive |
| Seed selection | Greedy top-N | Diversity-aware | Greedy vs. diverse |
| Similarity boost | Linear (1.0 + w/100) | Learned function | Heuristic vs. learned |
| Iteration strategy | Fixed (3 passes) | Adaptive | Fixed vs. adaptive |

---

## 3. Ranking Stage Comparison

### WACTX Approach
```
Person Aggregation
  ├─ message_count
  ├─ max_similarity
  ├─ max_rrf
  ├─ dm_volume
  ├─ shared_groups
  ├─ entities
  └─ conversation_boost

Person Scoring
  retrieval = max(max_rrf × 100, max_similarity)
  graph = min(1.0, 0.3×dm + 0.1×groups + 0.1×msgs + 0.05×entities)
  conv = conversation_boost
  score = 0.50×retrieval + 0.30×graph + 0.20×conv
```

### PathRAG/GraphRAG Approach (Expected)
- Learned aggregation functions (vs. max/sum)
- Learned scoring weights (vs. fixed 0.50/0.30/0.20)
- Multiplicative combination (vs. additive)
- Learned signal importance

### Gap Analysis
| Aspect | WACTX | Research | Gap |
|--------|-------|----------|-----|
| Aggregation | max/sum | Learned | Heuristic vs. learned |
| Weights | Fixed (0.50/0.30/0.20) | Learned | Heuristic vs. learned |
| Combination | Additive | Multiplicative/learned | Additive vs. learned |
| Signal importance | Equal per type | Learned | Heuristic vs. learned |

---

## 4. Iteration Strategy Comparison

### WACTX Approach
```
Pass 0: BM25 + Vector → 45 candidates
Pass 1: Graph expand (20 seeds) → RRF → 38 candidates (0.85^1)
Pass 2: Graph expand (18 seeds) → RRF → 32 candidates (0.85^2)
Pass 3: Graph expand (16 seeds) → RRF → 32 candidates (0.85^2)
```

**Pruning Formula**: `max(top_k×2, int(top_k×3 × 0.85^i))`

### PathRAG/GraphRAG Approach (Expected)
- Adaptive stopping criteria (vs. fixed iterations)
- Score-based pruning (vs. exponential decay)
- Diversity-aware selection (vs. greedy)
- Learned iteration count

### Gap Analysis
| Aspect | WACTX | Research | Gap |
|--------|-------|----------|-----|
| Iterations | Fixed (3) | Adaptive | Fixed vs. adaptive |
| Pruning | Exponential (0.85^i) | Score-based | Heuristic vs. learned |
| Stopping | Fixed count | Convergence-based | Fixed vs. adaptive |
| Seed selection | Greedy | Diversity-aware | Greedy vs. diverse |

---

## 5. Signal Combination Comparison

### WACTX Approach
```
Graph Signal = min(1.0,
    (0.3 if dm_volume > 0 else 0)
    + 0.1 × min(3, len(shared_groups))
    + 0.1 × min(3, message_count)
    + 0.05 × min(3, len(entities))
)
```

**Issues**:
- Additive combination (signals compete)
- Fixed weights (0.3, 0.1, 0.1, 0.05)
- Hard caps (min(3, ...))
- Binary DM signal (0 or 0.3)

### PathRAG/GraphRAG Approach (Expected)
- Multiplicative combination (signals reinforce)
- Learned weights
- Soft caps (sigmoid/tanh)
- Continuous signals

### Gap Analysis
| Aspect | WACTX | Research | Gap |
|--------|-------|----------|-----|
| Combination | Additive | Multiplicative | Additive vs. multiplicative |
| Weights | Fixed | Learned | Heuristic vs. learned |
| Caps | Hard (min(3)) | Soft (sigmoid) | Hard vs. soft |
| DM signal | Binary | Continuous | Binary vs. continuous |

---

## 6. Conversation Context Comparison

### WACTX Approach
```
Context Window: ±1 hour from message
Context Limit: 10 messages
Boost Formula: min(1.0, thread_length / 8.0)
```

**Issues**:
- Fixed time window (±1 hour)
- Fixed message limit (10)
- Simple length-based scoring
- No semantic relevance

### PathRAG/GraphRAG Approach (Expected)
- Learned context window
- Adaptive message selection
- Semantic relevance scoring
- Learned boost function

### Gap Analysis
| Aspect | WACTX | Research | Gap |
|--------|-------|----------|-----|
| Window | Fixed (±1h) | Learned | Fixed vs. learned |
| Limit | Fixed (10) | Adaptive | Fixed vs. adaptive |
| Scoring | Length-based | Semantic | Heuristic vs. learned |
| Boost | Linear (t/8) | Learned | Heuristic vs. learned |

---

## 7. Entity Handling Comparison

### WACTX Approach
```
Entity Signal = 0.05 × min(3, len(entities))
Entity Retrieval = Top 5 by mention_count
Entity Scoring = Mention count only
```

**Issues**:
- Mention count only (no importance)
- No entity centrality
- No entity type weighting
- Hard cap (min(3))

### PathRAG/GraphRAG Approach (Expected)
- Entity centrality/PageRank
- Entity type weighting
- Learned importance scoring
- Soft caps

### Gap Analysis
| Aspect | WACTX | Research | Gap |
|--------|-------|----------|-----|
| Scoring | Mention count | Centrality | Simple vs. learned |
| Type weighting | None | Learned | None vs. learned |
| Importance | Mention count | PageRank | Simple vs. learned |
| Caps | Hard (min(3)) | Soft | Hard vs. soft |

---

## 8. Hyperparameter Tuning Comparison

### WACTX Approach
```
Fixed Constants:
- RRF k = 60
- DM weight = 3.0
- Conversation weight = 5.0
- Group weight = 1.0
- Entity weight = 2.0
- Similarity boost cap = 2.0
- Pruning decay = 0.85
- Scoring weights = (0.50, 0.30, 0.20)
- Graph score weights = (0.3, 0.1, 0.1, 0.05)
```

**Issues**:
- All constants hardcoded
- No tuning mechanism
- No validation on held-out data
- No sensitivity analysis

### PathRAG/GraphRAG Approach (Expected)
- Learned parameters
- Validation on held-out data
- Sensitivity analysis
- Adaptive tuning

### Gap Analysis
| Aspect | WACTX | Research | Gap |
|--------|-------|----------|-----|
| Parameters | Hardcoded | Learned | Fixed vs. learned |
| Validation | None | Cross-validation | None vs. validated |
| Tuning | Manual | Automatic | Manual vs. automatic |
| Sensitivity | Unknown | Analyzed | Unknown vs. analyzed |

---

## 9. Failure Mode Handling Comparison

### WACTX Approach
```
No BM25 results → Use vector only
No graph table → Return empty
No new seeds → Break loop
Graph expansion fails → Log and break
Query expansion fails → Return original
Contact lookup fails → Use JID
Graph enrichment fails → Skip signal
```

**Issues**:
- Graceful degradation (good)
- No error recovery (bad)
- No fallback strategies (bad)
- Silent failures possible (bad)

### PathRAG/GraphRAG Approach (Expected)
- Robust error handling
- Multiple fallback strategies
- Explicit error logging
- Graceful degradation with alternatives

### Gap Analysis
| Aspect | WACTX | Research | Gap |
|--------|-------|----------|-----|
| Error handling | Basic | Robust | Basic vs. robust |
| Fallbacks | Limited | Multiple | Limited vs. multiple |
| Logging | Minimal | Detailed | Minimal vs. detailed |
| Recovery | None | Adaptive | None vs. adaptive |

---

## 10. Evaluation Metrics Comparison

### WACTX Approach
```
No explicit evaluation metrics
No ranking quality assessment
No diversity metrics
No coverage metrics
```

### PathRAG/GraphRAG Approach (Expected)
- NDCG (Normalized Discounted Cumulative Gain)
- MRR (Mean Reciprocal Rank)
- MAP (Mean Average Precision)
- Diversity metrics (Gini coefficient)
- Coverage metrics (recall)

### Gap Analysis
| Metric | WACTX | Research | Gap |
|--------|-------|----------|-----|
| Ranking quality | None | NDCG/MRR/MAP | None vs. measured |
| Diversity | None | Gini/diversity | None vs. measured |
| Coverage | None | Recall | None vs. measured |
| Evaluation | None | Systematic | None vs. systematic |

---

## Summary: Algorithmic Gaps

### Critical Gaps (High Impact)
1. **Fixed vs. Learned Weights**: All weights hardcoded (RRF k, edge weights, scoring weights)
2. **Greedy vs. Diverse Selection**: Seed selection is purely greedy top-N
3. **Fixed vs. Adaptive Iteration**: Always 3 passes regardless of convergence
4. **Additive vs. Multiplicative Signals**: Graph signals compete rather than reinforce
5. **No Evaluation**: No metrics to measure quality or guide optimization

### Important Gaps (Medium Impact)
6. **Simple vs. Learned Boosting**: Linear similarity boost formula
7. **Mention Count vs. Centrality**: Entity importance not measured
8. **Length vs. Semantic Context**: Conversation context scored by length only
9. **Fixed vs. Adaptive Pruning**: Exponential decay regardless of score distribution
10. **No Sensitivity Analysis**: Unknown impact of hyperparameter changes

### Minor Gaps (Low Impact)
11. **Hard vs. Soft Caps**: min(3, ...) instead of sigmoid/tanh
12. **Binary vs. Continuous Signals**: DM signal is 0 or 0.3
13. **Limited Error Recovery**: Graceful degradation but no alternatives
14. **No Diversity Metrics**: Unknown diversity of results

---

## Recommendations for Improvement

### Phase 1: Evaluation (Foundation)
- [ ] Implement NDCG, MRR, MAP metrics
- [ ] Create evaluation dataset with relevance judgments
- [ ] Establish baseline performance
- [ ] Add diversity and coverage metrics

### Phase 2: Learning (Core)
- [ ] Learn RRF k parameter
- [ ] Learn edge weights from training data
- [ ] Learn scoring weights (0.50/0.30/0.20)
- [ ] Learn graph signal weights (0.3/0.1/0.1/0.05)

### Phase 3: Adaptation (Advanced)
- [ ] Implement adaptive iteration stopping
- [ ] Add diversity-aware seed selection
- [ ] Learn similarity boosting function
- [ ] Implement score-based pruning

### Phase 4: Signals (Enhancement)
- [ ] Add entity centrality scoring
- [ ] Implement semantic conversation context
- [ ] Add entity type weighting
- [ ] Implement multiplicative signal combination

### Phase 5: Robustness (Polish)
- [ ] Add multiple fallback strategies
- [ ] Implement error recovery mechanisms
- [ ] Add detailed error logging
- [ ] Implement graceful degradation with alternatives

