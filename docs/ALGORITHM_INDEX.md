# WACTX Algorithm Documentation Index

**Generated**: 2026-03-21  
**Source**: `wactx/search.py` (694 lines)  
**Purpose**: Complete algorithmic specification for comparison with PathRAG/GraphRAG

---

## Quick Navigation

### For Algorithm Details
→ **[ALGORITHM_SPEC.md](ALGORITHM_SPEC.md)** (852 lines, 25KB)
- Complete breakdown of all functions
- Exact code with line numbers
- All formulas and constants
- Detailed parameter explanations

### For Quick Reference
→ **[ALGORITHM_FORMULAS.md](ALGORITHM_FORMULAS.md)** (164 lines, 5KB)
- 10 core scoring formulas
- Edge weights table
- Depth presets
- Candidate limits throughout pipeline
- Iteration dynamics
- Key constants
- Failure modes & fallbacks

### For Research Comparison
→ **[ALGORITHM_COMPARISON.md](ALGORITHM_COMPARISON.md)** (394 lines, 12KB)
- WACTX vs. PathRAG/GraphRAG comparison
- 10 detailed gap analyses
- Critical vs. important vs. minor gaps
- Improvement recommendations (5 phases)

---

## Document Structure

### ALGORITHM_SPEC.md — Complete Specification

**Sections**:
1. **run_search** (Lines 583-694)
   - Initialization & parameters
   - Query expansion
   - Candidate generation (BM25 + vector fusion)
   - RRF fusion formula
   - Graph expansion loop
   - Seed selection
   - Pruning formula
   - Enrichment & context
   - People aggregation

2. **graph_expand_candidates** (Lines 158-277)
   - Seed validation
   - Neighbor discovery (4 edge types)
   - Weight aggregation
   - Message retrieval
   - Similarity boosting
   - Deduplication

3. **find_related_people** (Lines 387-432)
   - Message aggregation by sender
   - Person scoring (retrieval + graph + conversation)
   - Sorting

4. **enrich_results** (Lines 280-356)
   - Contact enrichment
   - Graph signal initialization
   - DM volume
   - Shared groups
   - Entities

5. **fetch_conversation_context** (Lines 359-384)
   - Context retrieval
   - Conversation boost formula

6. **Supporting Functions**
   - BM25 search
   - Semantic search
   - RRF fusion
   - Query expansion
   - Embedding

7. **Constants & Hyperparameters** (Table)
   - 30+ constants with locations and purposes

8. **Algorithmic Gaps vs. PathRAG/GraphRAG**
   - 10 known limitations
   - Potential improvements

### ALGORITHM_FORMULAS.md — Quick Reference

**Sections**:
1. **Core Scoring Formulas** (10 formulas)
   - RRF fusion
   - Graph similarity boost
   - Person retrieval score
   - Person graph score
   - Person conversation score
   - Final person score
   - Conversation boost
   - Candidate pruning
   - Seed selection
   - Graph expansion limit

2. **Edge Weights in Graph Expansion** (Table)
   - DM, Group, Conversation, Entity weights

3. **Depth Presets** (Table)
   - fast, balanced, deep configurations

4. **Candidate Limits Throughout Pipeline** (Table)
   - BM25, Vector, RRF, Graph iterations, Final

5. **Iteration Dynamics** (Table)
   - Iteration 0, 1, 2 parameters

6. **Key Constants** (Table)
   - 30+ constants

7. **Scoring Weight Breakdown**
   - Person score composition
   - Graph score components

8. **Algorithmic Complexity** (Table)
   - Time complexity of each operation

9. **Failure Modes & Fallbacks** (Table)
   - 9 failure scenarios and fallbacks

### ALGORITHM_COMPARISON.md — Research Comparison

**Sections**:
1. **Executive Summary**
   - WACTX architecture overview

2. **10 Detailed Comparisons**
   - Retrieval stage
   - Graph expansion stage
   - Ranking stage
   - Iteration strategy
   - Signal combination
   - Conversation context
   - Entity handling
   - Hyperparameter tuning
   - Failure mode handling
   - Evaluation metrics

3. **Gap Analysis Tables** (10 tables)
   - WACTX vs. Research approach
   - Identified gaps

4. **Summary: Algorithmic Gaps**
   - 5 critical gaps (high impact)
   - 5 important gaps (medium impact)
   - 4 minor gaps (low impact)

5. **Recommendations for Improvement**
   - Phase 1: Evaluation (Foundation)
   - Phase 2: Learning (Core)
   - Phase 3: Adaptation (Advanced)
   - Phase 4: Signals (Enhancement)
   - Phase 5: Robustness (Polish)

---

## Key Findings

### Algorithm Overview
WACTX implements a **multi-pass graph-augmented retrieval** system:
- **Pass 0**: BM25 + semantic search with RRF fusion (k=60)
- **Pass 1-2**: Graph expansion with 4 edge types (DM 3x, Conversation 5x, Entity 2x, Group 1x)
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

---

## How to Use This Documentation

### For Implementation
1. Read **ALGORITHM_SPEC.md** Section 1 for complete run_search flow
2. Reference **ALGORITHM_FORMULAS.md** for exact formulas
3. Check line numbers in source code for verification

### For Optimization
1. Review **ALGORITHM_COMPARISON.md** for gap analysis
2. Identify critical gaps (Section 4)
3. Follow improvement recommendations (Section 5)

### For Debugging
1. Check **ALGORITHM_FORMULAS.md** Section 9 for failure modes
2. Trace through **ALGORITHM_SPEC.md** with actual data
3. Verify constants match source code

### For Research
1. Start with **ALGORITHM_COMPARISON.md** Executive Summary
2. Review gap analysis tables (Sections 1-10)
3. Compare against PathRAG/GraphRAG papers
4. Use improvement recommendations as research directions

---

## Source Code Reference

**File**: `wactx/search.py`  
**Lines**: 694 total

### Function Locations
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

### Constants
| Constant | Lines | Value |
|----------|-------|-------|
| DEPTH_PRESETS | 16-20 | Preset configurations |
| QUERY_EXPANSION_PROMPT | 22-33 | LLM prompt template |

---

## Verification Checklist

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

---

## Document Statistics

| Document | Lines | Size | Focus |
|----------|-------|------|-------|
| ALGORITHM_SPEC.md | 852 | 25KB | Complete specification |
| ALGORITHM_FORMULAS.md | 164 | 5KB | Quick reference |
| ALGORITHM_COMPARISON.md | 394 | 12KB | Research comparison |
| **Total** | **1410** | **42KB** | **Complete documentation** |

---

## Next Steps

### For Immediate Use
1. Share ALGORITHM_SPEC.md with research team
2. Use ALGORITHM_FORMULAS.md as quick reference
3. Compare against PathRAG/GraphRAG papers using ALGORITHM_COMPARISON.md

### For Implementation
1. Implement evaluation metrics (ALGORITHM_COMPARISON.md Phase 1)
2. Learn RRF k parameter (ALGORITHM_COMPARISON.md Phase 2)
3. Learn edge weights (ALGORITHM_COMPARISON.md Phase 2)

### For Research
1. Identify which gaps are most impactful
2. Design experiments to measure impact
3. Implement improvements in priority order

---

## Questions & Clarifications

### Q: Why is RRF k=60?
A: Hardcoded constant. No justification in code. Should be learned from data.

### Q: Why are edge weights 3.0/1.0/5.0/2.0?
A: Heuristic values. Conversation (5.0) > DM (3.0) > Entity (2.0) > Group (1.0). Should be learned.

### Q: Why is pruning decay 0.85?
A: Exponential decay formula. No justification. Should be adaptive based on score distribution.

### Q: Why are scoring weights 0.50/0.30/0.20?
A: Retrieval (50%) > Graph (30%) > Conversation (20%). Should be learned from training data.

### Q: Why only 3 iterations?
A: Fixed in DEPTH_PRESETS. Should be adaptive based on convergence.

### Q: Why is similarity boost capped at 2.0?
A: Heuristic limit. Should be learned or adaptive.

---

## Contact & Attribution

**Documentation Generated**: 2026-03-21  
**Source Code**: `wactx/search.py`  
**Purpose**: Algorithmic specification for PathRAG/GraphRAG comparison

