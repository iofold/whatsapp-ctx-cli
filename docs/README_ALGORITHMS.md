# WACTX Algorithm Documentation

Complete algorithmic specification of the WACTX search system for comparison with PathRAG/GraphRAG research.

## 📋 Quick Start

**Start here**: [ALGORITHM_INDEX.md](ALGORITHM_INDEX.md) — Master index with navigation guide

**For details**: [ALGORITHM_SPEC.md](ALGORITHM_SPEC.md) — Complete specification (852 lines)

**For quick ref**: [ALGORITHM_FORMULAS.md](ALGORITHM_FORMULAS.md) — Formulas & constants (164 lines)

**For research**: [ALGORITHM_COMPARISON.md](ALGORITHM_COMPARISON.md) — Gap analysis vs. PathRAG/GraphRAG (394 lines)

## 🎯 What's Inside

### ALGORITHM_SPEC.md (25KB)
Complete breakdown of all 10 functions with:
- Exact code with line numbers
- All 10 scoring formulas
- All 30+ constants and hyperparameters
- Edge weights and iteration dynamics
- Failure modes and fallbacks

### ALGORITHM_FORMULAS.md (5KB)
Quick reference card with:
- 10 core scoring formulas
- Edge weights table
- Depth presets (fast/balanced/deep)
- Candidate limits throughout pipeline
- Iteration dynamics (Iter 0/1/2)
- Key constants (30+)
- Failure modes & fallbacks

### ALGORITHM_COMPARISON.md (12KB)
Research comparison framework with:
- WACTX vs. PathRAG/GraphRAG comparison
- 10 detailed gap analyses
- Critical gaps (5 high-impact)
- Important gaps (5 medium-impact)
- Minor gaps (4 low-impact)
- Improvement recommendations (5 phases)

### ALGORITHM_INDEX.md (9KB)
Master index with:
- Quick navigation guide
- Document structure overview
- Key findings summary
- How to use documentation
- Source code reference
- Verification checklist

## 🔍 Key Findings

### Algorithm Overview
Multi-pass graph-augmented retrieval system:
- **Pass 0**: BM25 + semantic search with RRF fusion (k=60)
- **Pass 1-2**: Graph expansion with 4 edge types
- **Ranking**: Additive scoring (50% retrieval + 30% graph + 20% conversation)
- **Pruning**: Exponential decay (0.85^i per iteration)

### Critical Constants
| Constant | Value | Impact |
|----------|-------|--------|
| RRF k | 60 | Controls fusion weight distribution |
| Pruning decay | 0.85 | Controls candidate reduction per iteration |
| Scoring weights | 0.50/0.30/0.20 | Controls signal importance |
| Edge weights | 3.0/1.0/5.0/2.0 | Controls graph signal strength |
| Similarity boost cap | 2.0 | Controls max graph boost |

### Critical Gaps vs. Research
1. **All weights are hardcoded** (vs. learned)
2. **Seed selection is greedy** (vs. diversity-aware)
3. **Iteration count is fixed** (vs. adaptive)
4. **Signals are additive** (vs. multiplicative)
5. **No evaluation metrics** (vs. NDCG/MRR/MAP)

## 📊 Functions Documented

| Function | Lines | Purpose |
|----------|-------|---------|
| run_search | 583-694 | Main search orchestration |
| graph_expand_candidates | 158-277 | Graph traversal and expansion |
| find_related_people | 387-432 | Person aggregation and scoring |
| enrich_results | 280-356 | Graph signal computation |
| fetch_conversation_context | 359-384 | Contextual enrichment |
| bm25_search | 76-104 | BM25 full-text search |
| semantic_search | 107-136 | Vector similarity search |
| rrf_fuse | 139-155 | Reciprocal rank fusion |
| expand_query | 43-62 | LLM-based query expansion |
| embed_queries | 65-73 | Embedding generation |

## 📐 10 Core Formulas

1. **RRF Fusion** (Line 145)
   ```
   score[doc_id] = Σ 1 / (k + rank + 1), where k = 60
   ```

2. **Graph Similarity Boost** (Line 267)
   ```
   boosted_similarity = original_similarity × min(2.0, 1.0 + graph_weight / 100.0)
   ```

3. **Person Retrieval Score** (Line 421)
   ```
   retrieval = max(max_rrf × 100, max_similarity)
   ```

4. **Person Graph Score** (Lines 422-428)
   ```
   graph = min(1.0,
       (0.3 if dm_volume > 0 else 0)
       + 0.1 × min(3, len(shared_groups))
       + 0.1 × min(3, message_count)
       + 0.05 × min(3, len(entities))
   )
   ```

5. **Person Conversation Score** (Line 429)
   ```
   conv = conversation_boost
   ```

6. **Final Person Score** (Line 430)
   ```
   score = 0.50 × retrieval + 0.30 × graph + 0.20 × conv
   ```

7. **Conversation Boost** (Line 383)
   ```
   conversation_boost = min(1.0, thread_length / 8.0)
   ```

8. **Candidate Pruning** (Line 660)
   ```
   keep_count = max(top_k × 2, int(top_k × 3 × 0.85^i))
   ```

9. **Seed Selection** (Line 634)
   ```
   top_n = max(5, 20 - i × 2)
   ```

10. **Graph Expansion Limit** (Line 646)
    ```
    expansion_limit = max(5, top_k × 2 - i × 3)
    ```

## 🔗 Edge Weights

| Edge Type | Weight Formula | Location |
|-----------|---|---|
| DM (Direct Message) | `message_count × 3.0` | Line 182 |
| Group Co-membership | `message_count × 1.0` | Line 188 |
| Conversation | `exchange_count × 5.0` | Line 198 |
| Entity Mention | `mention_count × 2.0` | Line 204 |

## 🎛️ Depth Presets

| Preset | Variants | Top-K | Graph | Iterations |
|--------|----------|-------|-------|------------|
| fast | 1 | 10 | No | 1 |
| balanced | 5 | 15 | Yes | 3 |
| deep | 8 | 30 | Yes | 3 |

## 📈 Candidate Limits (for "balanced" with top_k=15)

| Stage | Formula | Count |
|-------|---------|-------|
| BM25 search | `top_k × 3` | 45 |
| Vector search | `top_k × 3` | 45 |
| After RRF fusion | `top_k × 3` | 45 |
| After graph iter 0 | `max(30, 45 × 0.85^0)` | 45 |
| After graph iter 1 | `max(30, 45 × 0.85^1)` | 38 |
| After graph iter 2 | `max(30, 45 × 0.85^2)` | 32 |
| Final truncation | `top_k × 4` | 60 |

## 🔄 Iteration Dynamics

### Iteration 0
- Seed selection: `top_n = 20`
- Graph expansion: `30` neighbors
- Pruning: `45` candidates

### Iteration 1
- Seed selection: `top_n = 18`
- Graph expansion: `27` neighbors
- Pruning: `38` candidates

### Iteration 2
- Seed selection: `top_n = 16`
- Graph expansion: `24` neighbors
- Pruning: `32` candidates

## ⚠️ Failure Modes & Fallbacks

| Scenario | Fallback |
|----------|----------|
| No BM25 results | Use vector results only, set `rrf_score = similarity` |
| No graph table | Return empty graph expansion |
| No new seeds (iter < 2) | Break graph loop |
| No new seeds (iter >= 2) | Expand to top_n×2, take top_n//2 |
| No graph expansion | Log "no new graph neighbours", break |
| Query expansion fails | Return original query only |
| Embedding fails | Exception propagates |
| Contact lookup fails | Use JID as display name |
| Graph enrichment fails | Skip that signal (dm_volume=0, etc.) |

## 🎓 Improvement Recommendations

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

## 📚 Document Statistics

| Document | Lines | Size | Focus |
|----------|-------|------|-------|
| ALGORITHM_SPEC.md | 852 | 25KB | Complete specification |
| ALGORITHM_FORMULAS.md | 164 | 5KB | Quick reference |
| ALGORITHM_COMPARISON.md | 394 | 12KB | Research comparison |
| ALGORITHM_INDEX.md | 318 | 9KB | Master index |
| **Total** | **1728** | **51KB** | **Complete documentation** |

## ✅ Verification Checklist

- [x] All functions documented with line numbers
- [x] All formulas extracted with exact code
- [x] All constants identified and listed
- [x] All edge weights documented
- [x] All scoring weights documented
- [x] All hyperparameters listed
- [x] Iteration dynamics calculated
- [x] Failure modes identified
- [x] Gaps vs. research identified
- [x] Improvement recommendations provided

## 🚀 Next Steps

1. **For Implementation**: Read ALGORITHM_SPEC.md Section 1 for complete run_search flow
2. **For Optimization**: Review ALGORITHM_COMPARISON.md for gap analysis
3. **For Debugging**: Check ALGORITHM_FORMULAS.md Section 9 for failure modes
4. **For Research**: Start with ALGORITHM_COMPARISON.md Executive Summary

## 📖 How to Use

### For Algorithm Details
→ **[ALGORITHM_SPEC.md](ALGORITHM_SPEC.md)** — Complete breakdown with exact code and line numbers

### For Quick Reference
→ **[ALGORITHM_FORMULAS.md](ALGORITHM_FORMULAS.md)** — Formulas, constants, and quick lookup tables

### For Research Comparison
→ **[ALGORITHM_COMPARISON.md](ALGORITHM_COMPARISON.md)** — Gap analysis vs. PathRAG/GraphRAG with improvement recommendations

### For Navigation
→ **[ALGORITHM_INDEX.md](ALGORITHM_INDEX.md)** — Master index with document structure and verification checklist

---

**Source**: `wactx/search.py` (694 lines)  
**Generated**: 2026-03-21  
**Purpose**: Complete algorithmic specification for PathRAG/GraphRAG comparison
