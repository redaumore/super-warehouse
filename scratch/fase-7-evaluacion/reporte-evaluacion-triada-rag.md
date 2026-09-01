# Reporte de Evaluación Continua RAG (Fase 7 - Tríada RAG & Recall@K)

**Fecha y Hora de Ejecución:** 2026-09-01 16:06:20 UTC
**Estado General del Pipeline:** ✅ APROBADO (CI PASS)
**Tasa de Aprobación Global:** 100.0% (5/5 muestras)

---

## 1. Métricas Cuantitativas Consolidadas

| Métrica | Promedio Obtenido | Umbral Mínimo Requerido | Estado |
| :--- | :---: | :---: | :---: |
| **Context Relevance** | 100.00% | 70.00% | ✅ OK |
| **Faithfulness (Groundedness)** | 100.00% | 90.00% | ✅ OK |
| **Answer Relevance** | 96.39% | 80.00% | ✅ OK |
| **Recall@K (Recuperador)** | 100.00% | 90.00% | ✅ OK |

---

## 2. Resultados Detallados por Muestra (Golden Dataset)

| ID | Consulta | Context Rel. | Faithfulness | Answer Rel. | Recall@K | Gate |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| `SAMPLE-01-SKU-EXACTO` | ¿Cuál es el código de producto para l... | 100.00% | 100.00% | 93.64% | 100.00% | ✅ PASS |
| `SAMPLE-02-PRODUCTO-INEXISTENTE` | ¿Tienen abrazaderas de titanio quirúr... | 100.00% | 100.00% | 100.00% | 100.00% | ✅ PASS |
| `SAMPLE-03-SKU-TACSA-CALIBRADO` | ¿Qué código tiene la cinta aisladora ... | 100.00% | 100.00% | 100.00% | 100.00% | ✅ PASS |
| `SAMPLE-04-CONTEXTO-COMPRIMIDO` | ¿Cuál es la apertura del artículo 00 ... | 100.00% | 100.00% | 88.33% | 100.00% | ✅ PASS |
| `SAMPLE-05-RECALL-CALIBRADO` | ¿Cuáles son las medidas disponibles d... | 100.00% | 100.00% | 100.00% | 100.00% | ✅ PASS |

---

## 3. Acciones de Diagnóstico y Bucle de Retroalimentación Operativa

No se registraron alertas ni degradaciones. El pipeline cumple todos los estándares de producción.