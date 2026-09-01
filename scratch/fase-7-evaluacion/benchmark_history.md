# Bitácora de Evolución de Benchmarks (Closed-Loop Evaluation)

Historial acumulativo de iteraciones de calibración sobre el pipeline RAG y la Tríada de Evaluación ([fase_7_evaluator.py](file:///Users/rolandodaumas/Development/projects/super-warehouse-data-processing/fase-0-pdf-parsing/fase_7_evaluator.py)).

---

## Umbrales de Aceptación (Quality Gates)

| Métrica | Umbral Mínimo | Objetivo |
| :--- | :---: | :---: |
| **Context Relevance** | $\ge 70\%$ | $\ge 85\%$ |
| **Faithfulness** | $\ge 90\%$ | $100\%$ |
| **Answer Relevance** | $\ge 80\%$ | $\ge 90\%$ |
| **Recall@K** | $\ge 90\%$ | $100\%$ |
| **Gate Global** | **PASS** (100% muestras aprobadas) | **PASS** |

---

## Historial de Iteraciones

| Iteración | Timestamp | Módulo / Fase Modificada | Hipótesis / Acción de Calibración | Context Rel | Faithfulness | Ans Rel | Recall@K | Gate | Snapshot JSON |
| :---: | :---: | :--- | :--- | :---: | :---: | :---: | :---: | :---: | :--- |
| **0 (Baseline)** | 2026-09-01 12:50 | Estado inicial | Baseline de referencia previo a calibraciones | 81.00% | 80.00% | 92.06% | 80.00% | ❌ FAIL (2/5 - 40%) | [baseline-iter-0.json](file:///Users/rolandodaumas/Development/projects/super-warehouse-data-processing/fase-0-pdf-parsing/scratch/fase-7-evaluacion/baseline-iter-0.json) |
| **1** | 2026-09-01 13:00 | **Fase 6 (System Prompt & Generator)** | Endurecimiento de Directiva de Ausencia, prohibición estricta de extrapolación de SKUs y calibración de fidelidad | 81.00% | **100.00%** | **94.06%** | 80.00% | ❌ FAIL (3/5 - 60%) | [iter-1.json](file:///Users/rolandodaumas/Development/projects/super-warehouse-data-processing/fase-0-pdf-parsing/scratch/fase-7-evaluacion/iter-1.json) |
| **2** | 2026-09-01 13:03 | **Fase 5 (Cross-Encoder) & Fase 1 (Chunking)** | Aumento de umbral de corte del Cross-Encoder a `0.45`, eliminación de ruido distractor y compresión de contexto | 91.00% | **100.00%** | **94.06%** | 80.00% | ❌ FAIL (4/5 - 80%) | [iter-2.json](file:///Users/rolandodaumas/Development/projects/super-warehouse-data-processing/fase-0-pdf-parsing/scratch/fase-7-evaluacion/iter-2.json) |
| **3 (Óptimo)** | 2026-09-01 13:06 | **Fase 3 (HNSW Indexing) & Fase 4 (Hybrid Search)** | Calibración de `ef_search=128`, conectividad $M=32$, $ef_{construction}=200$ e indexación exhaustiva | **100.00%** | **100.00%** | **96.39%** | **100.00%** | ✅ **PASS (5/5 - 100%)** | [iter-3.json](file:///Users/rolandodaumas/Development/projects/super-warehouse-data-processing/fase-0-pdf-parsing/scratch/fase-7-evaluacion/iter-3.json) |

---

## Diagnósticos Resueltos (Iteración 3 — Quality Gate PASS)

### ✅ Todos los Diagnósticos Resueltos:
1. **[CRITICAL] Fase 6 (System Prompt & LLM Generation):** Directivas anti-alucinación y alineación alfanumérica reforzadas. **Faithfulness: 100.00%**.
2. **[WARNING] Fase 1 (Chunking) & Fase 5 (Reranking):** Umbral Cross-Encoder elevado a `0.45` y compresión limpia de contexto. **Context Relevance: 100.00%**.
3. **[CRITICAL] Fase 3 (HNSW Indexing) & Fase 4 (Hybrid Search):** Amplitud de búsqueda HNSW ajustada (`ef_search=128`, $M=32$, $ef_{construction}=200$). **Recall@K: 100.00%**.
