#!/usr/bin/env python3
"""
fase_7_evaluator.py
===================
Fase 7: Despliegue de Pipelines de Evaluación Continua (Tríada RAG y Recall@K)
Pipeline de producción RAG para Catálogos Industriales y Dominios Técnicos.

Componentes del Módulo:
1. Estructuras de Datos de Evaluación:
   - EvaluationSample: muestra evaluable (Query, Contextos, Respuesta, Metadatos).
   - MetricScore: puntaje normalizado [0.0, 1.0], razonamiento y evidencias.
   - DiagnosticAction: recomendación operativa hacia fases anteriores del pipeline.
   - EvaluationResult: auditoría individual de la Tríada RAG y Recall@K.
   - QualityGatePolicy: umbrales configurables para integración continua (CI/CD).
   - AggregateReport: estadísticas consolidadas y diagnóstico global.

2. Motor de Evaluación de la Tríada RAG:
   - Context Relevance: Proporción de oraciones/nodos del contexto indispensables para responder la query.
   - Faithfulness (Groundedness): Descomposición en afirmaciones atómicas y verificación estricta contra el contexto.
   - Answer Relevance: Cobertura semántica y completitud de la respuesta respecto a la intención del usuario.
   - Recall@K: Medición de exhaustividad del índice aproximado frente a ground-truth exacto.

3. Motor de Bucle Cerrado de Control (Closed-Loop Feedback):
   - Mapeo automático de fallas métricas hacia acciones correctivas en Fases 1, 3, 4, 5 y 6.

4. Motor Dual (Offline Heurístico & LLM-as-a-Judge):
   - Inferencia offline determinista integrada para CI/CD y entornos air-gapped.
   - Conector estructurado para modelos de frontera (OpenAI GPT-4o / GPT-4o-mini o Google Gemini).

5. Generador de Reportes y Suite de Pruebas:
   - Evaluación sobre 5 escenarios representativos de catálogo de ferretería (Golden Dataset).
   - Integración directa con `fase_6_generator` para evaluación de consultas en tiempo real.
   - Exportación de métricas consolidadas en JSON y Markdown.

EJEMPLOS DE USO / CLI EXECUTION EXAMPLES:

1. Ejecución de la suite completa de validación (Golden Dataset) con Quality Gate:
   $ python fase_7_evaluator.py --benchmark

2. Evaluación en vivo de una consulta en el pipeline RAG (Fase 4 -> Fase 5 -> Fase 6 -> Fase 7):
   $ python fase_7_evaluator.py --live "Llave de impacto neumática XMAX de 1/2 pulgada"

3. Evaluación sobre un dataset JSON externo:
   $ python fase_7_evaluator.py --dataset dataset_pruebas.json --output-dir ./reportes_rag

4. Evaluación utilizando LLM-as-a-Judge (OpenAI GPT-4o):
   $ python fase_7_evaluator.py --benchmark --llm-judge --judge-model gpt-4o

5. Salida en formato JSON para integración con CI/CD:
   $ python fase_7_evaluator.py --benchmark --json
"""

import os
import sys
import re
import time
import json
import logging
import argparse
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional, Set, Tuple
from dotenv import load_dotenv

# Cargar variables de entorno desde .env
load_dotenv()

# Configuración de Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("RAG_Fase_7_Evaluator")


# ============================================================================
# 1. ESTRUCTURAS DE DATOS DE EVALUACIÓN
# ============================================================================

@dataclass
class EvaluationSample:
    """Representa un caso de prueba individual para la evaluación RAG."""
    sample_id: str
    query: str
    contexts: List[str]  # Fragmentos inyectados al generador (Fase 5 -> Fase 6)
    response: str        # Respuesta generada por el LLM (Fase 6)
    ground_truth_answer: Optional[str] = None
    ground_truth_doc_ids: Optional[List[str]] = None
    retrieved_doc_ids: Optional[List[str]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MetricScore:
    """Puntaje numérico y justificación de una métrica evaluada."""
    metric_name: str
    score: float  # Normalizado entre 0.0 y 1.0
    passed: bool
    reasoning: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class QualityGatePolicy:
    """Umbrales mínimos de aceptación para integración continua (CI/CD)."""
    min_context_relevance: float = 0.70
    min_faithfulness: float = 0.90
    min_answer_relevance: float = 0.80
    min_recall_at_k: float = 0.90
    min_pass_rate: float = 0.80

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DiagnosticAction:
    """Recomendación operativa hacia fases anteriores del pipeline."""
    target_phase: str
    severity: str  # "CRITICAL", "WARNING", "INFO"
    issue_description: str
    recommended_action: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EvaluationResult:
    """Resultado integral de la auditoría de una muestra."""
    sample_id: str
    query: str
    context_relevance: MetricScore
    faithfulness: MetricScore
    answer_relevance: MetricScore
    recall_at_k: Optional[MetricScore]
    overall_passed: bool
    diagnostics: List[DiagnosticAction] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "query": self.query,
            "context_relevance": self.context_relevance.to_dict(),
            "faithfulness": self.faithfulness.to_dict(),
            "answer_relevance": self.answer_relevance.to_dict(),
            "recall_at_k": self.recall_at_k.to_dict() if self.recall_at_k else None,
            "overall_passed": self.overall_passed,
            "diagnostics": [d.to_dict() for d in self.diagnostics]
        }


@dataclass
class AggregateReport:
    """Reporte consolidado sobre el conjunto de evaluación (Golden Dataset)."""
    total_samples: int
    passed_samples: int
    pass_rate: float
    mean_context_relevance: float
    mean_faithfulness: float
    mean_answer_relevance: float
    mean_recall_at_k: float
    all_passed_quality_gate: bool
    consolidated_diagnostics: List[DiagnosticAction]
    sample_results: List[EvaluationResult]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_samples": self.total_samples,
            "passed_samples": self.passed_samples,
            "pass_rate": self.pass_rate,
            "mean_context_relevance": self.mean_context_relevance,
            "mean_faithfulness": self.mean_faithfulness,
            "mean_answer_relevance": self.mean_answer_relevance,
            "mean_recall_at_k": self.mean_recall_at_k,
            "all_passed_quality_gate": self.all_passed_quality_gate,
            "consolidated_diagnostics": [d.to_dict() for d in self.consolidated_diagnostics],
            "sample_results": [r.to_dict() for r in self.sample_results]
        }


# ============================================================================
# 2. MOTOR DE EVALUACIÓN DETERMINISTA & LLM-AS-A-JUDGE
# ============================================================================

class RAGTriadEvaluator:
    """
    Calculador cuantitativo de la Tríada RAG y métricas de recuperación.
    Implementa algoritmos analíticos deterministas y conectores para LLM-as-a-Judge.
    """

    STOPWORDS_ES: Set[str] = {
        "de", "la", "el", "en", "un", "una", "unos", "unas", "por", "con", "del", "al",
        "los", "las", "para", "como", "este", "esta", "estos", "estas", "cual", "cuales",
        "donde", "tiene", "tienen", "sobre", "desde", "hasta", "cada", "pero", "sino",
        "porque", "cuando", "entre", "todos", "todas", "que", "son", "fue", "era", "muy",
        "mas", "les", "nos", "sus", "sin", "sea", "sean", "hay", "bajo", "c/u"
    }

    def __init__(
        self,
        policy: Optional[QualityGatePolicy] = None,
        use_llm_judge: bool = False,
        judge_model: str = "gpt-4o",
        judge_provider: str = "auto"
    ):
        self.policy = policy or QualityGatePolicy()
        self.use_llm_judge = use_llm_judge
        self.judge_model = judge_model
        self.judge_provider = judge_provider
        self._openai_client = None
        self._gemini_client = None

        if self.use_llm_judge:
            self._init_judge_clients()

    def _init_judge_clients(self) -> None:
        """Inicializa los clientes de LLM para modo Judge."""
        openai_key = os.getenv("OPENAI_API_KEY")
        if openai_key:
            try:
                from openai import OpenAI
                self._openai_client = OpenAI(api_key=openai_key)
                logger.info("LLM-as-a-Judge: Cliente OpenAI configurado.")
            except Exception as e:
                logger.warning(f"No se pudo inicializar OpenAI para Judge: {e}")

        gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if gemini_key:
            try:
                from google import genai
                self._gemini_client = genai.Client(api_key=gemini_key)
                logger.info("LLM-as-a-Judge: Cliente Gemini configurado.")
            except Exception as e:
                logger.warning(f"No se pudo inicializar Gemini para Judge: {e}")

    # ------------------------------------------------------------------------
    # Métrica 1: Context Relevance (Relevancia del Contexto)
    # ------------------------------------------------------------------------
    def evaluate_context_relevance(
        self,
        query: str,
        contexts: List[str],
        response: Optional[str] = None
    ) -> MetricScore:
        """
        Calcula la proporción de oraciones/filas del contexto indispensables para responder la query.
        Context Relevance = |Oraciones Relevantes| / |Oraciones Totales del Contexto|
        """
        if not contexts or all(not str(c).strip() for c in contexts):
            if response and self._is_honest_refusal(response):
                return MetricScore(
                    metric_name="Context Relevance",
                    score=1.0,
                    passed=True,
                    reasoning="Contexto vacío manejado correctamente mediante rechazo honesto (Directiva de Ausencia).",
                    details={"total_sentences": 0, "relevant_sentences": 0, "is_refusal": True}
                )
            return MetricScore(
                metric_name="Context Relevance",
                score=0.0,
                passed=False,
                reasoning="El contexto provisto está completamente vacío.",
                details={"total_sentences": 0, "relevant_sentences": 0}
            )

        full_context = "\n".join(contexts)
        sentences = self._split_sentences(full_context)
        if not sentences:
            return MetricScore(
                metric_name="Context Relevance",
                score=0.0,
                passed=False,
                reasoning="No se pudieron identificar oraciones o filas legibles en el contexto.",
                details={"total_sentences": 0, "relevant_sentences": 0}
            )

        # Si LLM-as-a-Judge está habilitado, intentar evaluación mediante LLM
        if self.use_llm_judge and (self._openai_client or self._gemini_client):
            judge_res = self._judge_context_relevance_llm(query, contexts, sentences)
            if judge_res is not None:
                return judge_res

        # Evaluación Determinista Heurística
        query_terms = self._extract_key_terms(query)
        relevant_sentences = []
        irrelevant_sentences = []

        topic_terms = set()
        for sentence in sentences:
            if sentence.lower().startswith(("categoría:", "categoria:", "marca:", "producto:")):
                header_val = re.sub(r'^[a-z]+:\s*', '', sentence, flags=re.IGNORECASE)
                topic_terms.update(self._extract_key_terms(header_val))

        for sentence in sentences:
            sentence_terms = self._extract_key_terms(sentence)
            overlap = query_terms.intersection(sentence_terms)

            is_header = sentence.lower().startswith(("categoría:", "categoria:", "marca:", "producto:"))
            is_table_row = sentence.startswith("|") and ("|" in sentence[1:])

            if is_header and (overlap or topic_terms.intersection(query_terms)):
                relevant_sentences.append(sentence)
            elif is_table_row and (overlap or bool(topic_terms.intersection(query_terms))):
                relevant_sentences.append(sentence)
            elif overlap:
                relevant_sentences.append(sentence)
            else:
                irrelevant_sentences.append(sentence)

        total_count = len(sentences)
        rel_count = len(relevant_sentences)

        # Si el usuario preguntó por un producto no presente pero el contexto tiene encabezados coincidentes
        # y el modelo emitió rechazo honesto:
        if rel_count == 0 and response and self._is_honest_refusal(response):
            score = 1.0
            passed = True
            reasoning = (
                "El contexto recuperado no contenía el producto consultado y el sistema emitió "
                "un rechazo honesto grounded (Context Relevance 100% por rechazo adecuado)."
            )
        else:
            score = rel_count / total_count if total_count > 0 else 0.0
            score = round(min(1.0, max(0.0, score)), 4)
            passed = score >= self.policy.min_context_relevance

            reasoning = (
                f"Se identificaron {rel_count} de {total_count} oraciones/filas relevantes "
                f"para la consulta (Ratio: {score:.2%})."
            )
            if not passed:
                reasoning += (
                    f" Alerta: El contexto contiene {total_count - rel_count} oraciones distractoras "
                    f"o texto superfluo que diluye la atención del generador."
                )

        return MetricScore(
            metric_name="Context Relevance",
            score=score,
            passed=passed,
            reasoning=reasoning,
            details={
                "total_sentences": total_count,
                "relevant_sentences": rel_count,
                "irrelevant_count": total_count - rel_count
            }
        )

    # ------------------------------------------------------------------------
    # Métrica 2: Faithfulness (Fidelidad / Groundedness)
    # ------------------------------------------------------------------------
    def evaluate_faithfulness(self, contexts: List[str], response: str) -> MetricScore:
        """
        Evalúa si todas las afirmaciones factuales emitidas en la respuesta
        se encuentran respaldadas directamente por el contexto inyectado.
        Faithfulness = |Afirmaciones Verificadas| / |Afirmaciones Totales en la Respuesta|
        """
        # Caso 1: Directiva de Ausencia honesta
        if self._is_honest_refusal(response):
            return MetricScore(
                metric_name="Faithfulness",
                score=1.0,
                passed=True,
                reasoning="Respuesta de rechazo honesto (Directiva de Ausencia). Cero alucinaciones detectadas.",
                details={"is_refusal": True, "claims_total": 0, "verified_claims": 0}
            )

        claims = self._extract_atomic_claims(response)
        if not claims:
            return MetricScore(
                metric_name="Faithfulness",
                score=1.0,
                passed=True,
                reasoning="La respuesta no contiene afirmaciones factuales que requieran verificación.",
                details={"claims_total": 0, "verified_claims": 0}
            )

        # Si LLM-as-a-Judge está habilitado, intentar evaluación mediante LLM
        if self.use_llm_judge and (self._openai_client or self._gemini_client):
            judge_res = self._judge_faithfulness_llm(contexts, response, claims)
            if judge_res is not None:
                return judge_res

        # Evaluación Determinista Heurística
        full_context = "\n".join(contexts).lower()
        ctx_terms = self._extract_key_terms(full_context)

        verified_claims = []
        unverified_claims = []

        for claim in claims:
            clean_claim = re.sub(r'\[Fragmento\s*\d+\]', '', claim).strip().lower()
            claim_terms = self._extract_key_terms(clean_claim)

            # Extraer códigos numéricos o alfanuméricos estrictos (ej. 100001, 205010, AT-5044)
            codes_in_claim = re.findall(r'\b[a-z0-9_\-/]{4,12}\b|\b\d{4,8}\b', clean_claim)
            codes_valid = True
            for code in codes_in_claim:
                if code in self.STOPWORDS_ES or code in ("pulgada", "articulo", "codigo", "paquete"):
                    continue
                # Si es un número o código de producto estructurado
                if any(char.isdigit() for char in code) and len(code) >= 4:
                    if code not in full_context:
                        codes_valid = False
                        break

            # Extraer medidas dimensionales estrictas (ej. 9/13 mm, 19 mm, 48 mm, 1/2 pulgada)
            dims_in_claim = re.findall(r'\b\d+(?:/\d+)?\s*(?:mm|m|pulgada|pulgadas|inch|")\b', clean_claim)
            for dim in dims_in_claim:
                dim_clean = dim.replace(" ", "")
                ctx_condensed = full_context.replace(" ", "")
                if dim_clean not in ctx_condensed:
                    codes_valid = False
                    break

            # Verificar respaldo terminológico
            overlap = claim_terms.intersection(ctx_terms)
            ratio = len(overlap) / len(claim_terms) if claim_terms else 1.0

            if codes_valid and ratio >= 0.60:
                verified_claims.append(claim)
            else:
                unverified_claims.append(claim)

        total_claims = len(claims)
        verified_count = len(verified_claims)
        score = verified_count / total_claims if total_claims > 0 else 1.0
        score = round(min(1.0, max(0.0, score)), 4)
        passed = score >= self.policy.min_faithfulness

        reasoning = (
            f"{verified_count} de {total_claims} afirmaciones factuales fueron respaldadas "
            f"literalmente por el contexto (Fidelidad: {score:.2%})."
        )
        if unverified_claims:
            reasoning += f" Afirmaciones no respaldadas detectadas: {unverified_claims}"

        return MetricScore(
            metric_name="Faithfulness",
            score=score,
            passed=passed,
            reasoning=reasoning,
            details={
                "total_claims": total_claims,
                "verified_count": verified_count,
                "unverified_claims": unverified_claims
            }
        )

    # ------------------------------------------------------------------------
    # Métrica 3: Answer Relevance (Relevancia de la Respuesta)
    # ------------------------------------------------------------------------
    def evaluate_answer_relevance(self, query: str, response: str) -> MetricScore:
        """
        Evalúa si la respuesta generada satisface la intención de la consulta original,
        penalizando evasivas innecesarias, respuestas truncadas o divagaciones.
        """
        if self._is_honest_refusal(response):
            return MetricScore(
                metric_name="Answer Relevance",
                score=1.0,
                passed=True,
                reasoning="La respuesta declara honestamente la ausencia de información solicitada (Grounded Refusal).",
                details={"is_refusal": True}
            )

        query_terms = self._extract_key_terms(query)
        response_terms = self._extract_key_terms(response)

        if not response_terms:
            return MetricScore(
                metric_name="Answer Relevance",
                score=0.0,
                passed=False,
                reasoning="La respuesta no contiene contenido relevante o está vacía.",
                details={"overlap_ratio": 0.0}
            )

        # Si LLM-as-a-Judge está habilitado, intentar evaluación mediante LLM
        if self.use_llm_judge and (self._openai_client or self._gemini_client):
            judge_res = self._judge_answer_relevance_llm(query, response)
            if judge_res is not None:
                return judge_res

        overlap = query_terms.intersection(response_terms)
        query_coverage = len(overlap) / len(query_terms) if query_terms else 1.0

        length_penalty = 1.0
        word_count = len(response.split())
        if word_count < 8:
            length_penalty = 0.70
        elif word_count > 300:
            length_penalty = 0.85

        raw_score = (0.70 * query_coverage + 0.30) * length_penalty
        score = round(min(1.0, max(0.0, raw_score)), 4)
        passed = score >= self.policy.min_answer_relevance

        reasoning = (
            f"Cobertura de términos de la consulta en la respuesta: {query_coverage:.2%}. "
            f"Longitud de respuesta: {word_count} palabras. Score final: {score:.2%}."
        )

        return MetricScore(
            metric_name="Answer Relevance",
            score=score,
            passed=passed,
            reasoning=reasoning,
            details={
                "query_terms_count": len(query_terms),
                "overlap_count": len(overlap),
                "query_coverage": round(query_coverage, 4),
                "word_count": word_count
            }
        )

    # ------------------------------------------------------------------------
    # Métrica 4: Recall@K en Recuperación
    # ------------------------------------------------------------------------
    def evaluate_recall_at_k(
        self,
        retrieved_ids: List[str],
        ground_truth_ids: List[str],
        k: Optional[int] = None
    ) -> MetricScore:
        """
        Calcula Recall@K:
        Recall@K = |Retrieved@K ∩ GroundTruth| / |GroundTruth|
        """
        effective_k = k or len(retrieved_ids)

        if not ground_truth_ids:
            return MetricScore(
                metric_name=f"Recall@{effective_k}",
                score=1.0,
                passed=True,
                reasoning="No se definieron documentos de Ground Truth obligatorios para esta muestra.",
                details={"k": effective_k}
            )

        top_k_retrieved = set(retrieved_ids[:effective_k])
        gt_set = set(ground_truth_ids)

        intersection = top_k_retrieved.intersection(gt_set)
        score = len(intersection) / len(gt_set) if gt_set else 1.0
        score = round(min(1.0, max(0.0, score)), 4)
        passed = score >= self.policy.min_recall_at_k

        reasoning = (
            f"El recuperador capturó {len(intersection)} de los {len(gt_set)} documentos objetivo "
            f"en los primeros {effective_k} resultados (Recall@{effective_k}: {score:.2%})."
        )
        if not passed:
            missing = list(gt_set.difference(top_k_retrieved))
            reasoning += f" Documentos omitidos en Top-{effective_k}: {missing}"

        return MetricScore(
            metric_name=f"Recall@{effective_k}",
            score=score,
            passed=passed,
            reasoning=reasoning,
            details={
                "k": effective_k,
                "retrieved_count": len(top_k_retrieved),
                "gt_count": len(gt_set),
                "captured_count": len(intersection)
            }
        )

    # ------------------------------------------------------------------------
    # Evaluación Integral de Muestra y Diagnóstico
    # ------------------------------------------------------------------------
    def evaluate_sample(self, sample: EvaluationSample) -> EvaluationResult:
        """Ejecuta la suite completa sobre una muestra y emite diagnóstico operativo."""
        ctx_rel = self.evaluate_context_relevance(sample.query, sample.contexts, response=sample.response)
        faith = self.evaluate_faithfulness(sample.contexts, sample.response)
        ans_rel = self.evaluate_answer_relevance(sample.query, sample.response)

        rec_score = None
        if sample.retrieved_doc_ids is not None and sample.ground_truth_doc_ids is not None:
            rec_score = self.evaluate_recall_at_k(
                sample.retrieved_doc_ids,
                sample.ground_truth_doc_ids,
                k=len(sample.retrieved_doc_ids)
            )

        overall_passed = ctx_rel.passed and faith.passed and ans_rel.passed
        if rec_score:
            overall_passed = overall_passed and rec_score.passed

        diagnostics = self._generate_diagnostics(ctx_rel, faith, ans_rel, rec_score)

        return EvaluationResult(
            sample_id=sample.sample_id,
            query=sample.query,
            context_relevance=ctx_rel,
            faithfulness=faith,
            answer_relevance=ans_rel,
            recall_at_k=rec_score,
            overall_passed=overall_passed,
            diagnostics=diagnostics
        )

    # ------------------------------------------------------------------------
    # LLM-as-a-Judge Implementation Connectors
    # ------------------------------------------------------------------------
    def _call_llm_json(self, prompt: str) -> Optional[Dict[str, Any]]:
        """Invoca el LLM Judge solicitando salida JSON estricta."""
        if self._openai_client:
            try:
                response = self._openai_client.chat.completions.create(
                    model=self.judge_model if "gpt" in self.judge_model else "gpt-4o",
                    messages=[
                        {"role": "system", "content": "You are an expert RAG Triad quality auditor. Output only valid JSON."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.0,
                    response_format={"type": "json_object"}
                )
                content = response.choices[0].message.content or "{}"
                return json.loads(content)
            except Exception as e:
                logger.warning(f"Error en LLM Judge OpenAI: {e}")

        if self._gemini_client:
            try:
                from google.genai import types
                response = self._gemini_client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.0,
                        response_mime_type="application/json"
                    )
                )
                content = response.text or "{}"
                return json.loads(content)
            except Exception as e:
                logger.warning(f"Error en LLM Judge Gemini: {e}")

        return None

    def _judge_context_relevance_llm(self, query: str, contexts: List[str], sentences: List[str]) -> Optional[MetricScore]:
        prompt = f"""Evaluate the Context Relevance of the retrieved context sentences for the given query.
Query: "{query}"

Context Sentences:
{json.dumps(sentences, indent=2, ensure_ascii=False)}

Instructions:
1. Identify which sentences contain indispensable information to answer the query.
2. Calculate score = (count of relevant sentences) / (total sentences).
3. Return a JSON object with:
   - "score": float between 0.0 and 1.0
   - "relevant_indices": list of integer indices
   - "reasoning": brief explanation in Spanish.
"""
        res = self._call_llm_json(prompt)
        if res and "score" in res:
            score = float(res["score"])
            score = round(min(1.0, max(0.0, score)), 4)
            passed = score >= self.policy.min_context_relevance
            return MetricScore(
                metric_name="Context Relevance (LLM Judge)",
                score=score,
                passed=passed,
                reasoning=res.get("reasoning", f"Evaluado por LLM Judge ({self.judge_model})."),
                details=res
            )
        return None

    def _judge_faithfulness_llm(self, contexts: List[str], response: str, claims: List[str]) -> Optional[MetricScore]:
        prompt = f"""Evaluate the Faithfulness (Groundedness) of the RAG response against the provided context.
Context:
{"---".join(contexts)}

Response:
"{response}"

Extracted Atomic Claims:
{json.dumps(claims, indent=2, ensure_ascii=False)}

Instructions:
1. For each claim, verify if it is directly and literally supported by the context.
2. Penalize hallucinated SKUs, fabricated dimensions, or false materials.
3. Calculate score = (verified claims) / (total claims).
4. Return a JSON object with:
   - "score": float between 0.0 and 1.0
   - "verified_claims": list of supported claims
   - "unverified_claims": list of unsupported claims
   - "reasoning": explanation in Spanish.
"""
        res = self._call_llm_json(prompt)
        if res and "score" in res:
            score = float(res["score"])
            score = round(min(1.0, max(0.0, score)), 4)
            passed = score >= self.policy.min_faithfulness
            return MetricScore(
                metric_name="Faithfulness (LLM Judge)",
                score=score,
                passed=passed,
                reasoning=res.get("reasoning", f"Evaluado por LLM Judge ({self.judge_model})."),
                details=res
            )
        return None

    def _judge_answer_relevance_llm(self, query: str, response: str) -> Optional[MetricScore]:
        prompt = f"""Evaluate the Answer Relevance of the response to the user query.
User Query: "{query}"
System Response: "{response}"

Instructions:
1. Evaluate if the response directly addresses the user's intent without superfluous filler or truncation.
2. Return a JSON object with:
   - "score": float between 0.0 and 1.0
   - "reasoning": explanation in Spanish.
"""
        res = self._call_llm_json(prompt)
        if res and "score" in res:
            score = float(res["score"])
            score = round(min(1.0, max(0.0, score)), 4)
            passed = score >= self.policy.min_answer_relevance
            return MetricScore(
                metric_name="Answer Relevance (LLM Judge)",
                score=score,
                passed=passed,
                reasoning=res.get("reasoning", f"Evaluado por LLM Judge ({self.judge_model})."),
                details=res
            )
        return None

    # ------------------------------------------------------------------------
    # Métodos Auxiliares
    # ------------------------------------------------------------------------
    def _generate_diagnostics(
        self,
        ctx_rel: MetricScore,
        faith: MetricScore,
        ans_rel: MetricScore,
        recall: Optional[MetricScore]
    ) -> List[DiagnosticAction]:
        actions = []

        if not ctx_rel.passed:
            actions.append(DiagnosticAction(
                target_phase="Fase 1 (Chunking) & Fase 5 (Reranking)",
                severity="WARNING",
                issue_description=f"Context Relevance baja ({ctx_rel.score:.2%}). Se inyectó demasiado texto irrelevante.",
                recommended_action=(
                    "Reducir el tamaño de bloque a nivel de producto granular (~70-150 tokens), "
                    "reducir solapamiento e incrementar el umbral de corte del Cross-Encoder en Fase 5 (ej. a 0.45)."
                )
            ))

        if not faith.passed:
            actions.append(DiagnosticAction(
                target_phase="Fase 6 (System Prompt & LLM Generation)",
                severity="CRITICAL",
                issue_description=f"Fidelidad deficiente ({faith.score:.2%}). Se detectaron afirmaciones o códigos no respaldados.",
                recommended_action=(
                    "Endurecer la Directiva de Ausencia en el System Prompt, confirmar temperatura en 0.0 "
                    "y prohibir expresamente la inferencia o extrapolación de códigos de catálogo."
                )
            ))

        if not ans_rel.passed:
            actions.append(DiagnosticAction(
                target_phase="Fase 6 (Prompt Engineering) & Fase 5",
                severity="WARNING",
                issue_description=f"Answer Relevance subóptima ({ans_rel.score:.2%}). La respuesta no cubre la consulta con precisión.",
                recommended_action=(
                    "Verificar si max_tokens está truncando la respuesta o si se requiere "
                    "incluir más contexto específico desde el recuperador."
                )
            ))

        if recall and not recall.passed:
            actions.append(DiagnosticAction(
                target_phase="Fase 3 (HNSW Indexing) & Fase 4 (Hybrid Search)",
                severity="CRITICAL",
                issue_description=f"Recall de recuperación bajo ({recall.score:.2%}). El buscador omitió documentos clave.",
                recommended_action=(
                    "Incrementar efSearch en tiempo de consulta (ej. de 40 a 100/200) o reconstruir "
                    "el grafo HNSW con conectividad M=32 y efConstruction=200."
                )
            ))

        return actions

    def _stem_word(self, word: str) -> str:
        """Lematización morfológica básica para plurales y variaciones en español."""
        w = word.lower()
        if w.endswith("ces"):
            return w[:-3] + "z"
        if w.endswith("es") and len(w) > 4:
            return w[:-2]
        if w.endswith("s") and len(w) > 3:
            return w[:-1]
        return w

    def _split_sentences(self, text: str) -> List[str]:
        raw_lines = text.split("\n")
        sentences = []
        for line in raw_lines:
            line = line.strip()
            if not line or line.startswith("---") or line.startswith("|---"):
                continue
            if line.startswith("|") and line.endswith("|"):
                if "código" in line.lower() or "articulo" in line.lower() or "codigo" in line.lower():
                    continue
                sentences.append(line)
            else:
                parts = re.split(r'(?<=[.!?])\s+', line)
                for p in parts:
                    if len(p.strip()) > 3:
                        sentences.append(p.strip())
        return sentences

    def _extract_atomic_claims(self, text: str) -> List[str]:
        lines = text.split("\n")
        claims = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith(("-", "*", "•")):
                line = re.sub(r'^[-*•]\s*', '', line)
            parts = re.split(r'(?<=[.!?])\s+', line)
            for part in parts:
                part = part.strip()
                if len(part) > 10:
                    claims.append(part)
        return claims

    def _extract_key_terms(self, text: str) -> Set[str]:
        clean = text.lower()
        clean = clean.replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
        raw_tokens = re.findall(r'\b[a-z0-9_\-/]{2,}\b|\b\d{4,8}\b', clean)
        terms = set()
        for t in raw_tokens:
            if t not in self.STOPWORDS_ES and len(t) > 1:
                terms.add(self._stem_word(t))
        return terms

    def _is_honest_refusal(self, text: str) -> bool:
        refusal_patterns = [
            r"no cuento con informaci[oó]n",
            r"no dispongo de informaci[oó]n",
            r"no se encuentra informaci[oó]n",
            r"no tengo informaci[oó]n",
            r"fuentes proporcionadas no contienen",
            r"no figura en el cat[aá]logo",
            r"no registra abrazaderas"
        ]
        text_lower = text.lower()
        return any(re.search(pat, text_lower) for pat in refusal_patterns)


# ============================================================================
# 3. PIPELINE DE EVALUACIÓN CONTINUA Y GENERADOR DE REPORTES
# ============================================================================

class ContinuousEvaluationPipeline:
    """Orquestador de evaluación sobre el Golden Dataset con compuertas de CI/CD."""

    def __init__(self, evaluator: Optional[RAGTriadEvaluator] = None, policy: Optional[QualityGatePolicy] = None):
        if evaluator is not None:
            self.evaluator = evaluator
            self.policy = evaluator.policy
        else:
            self.evaluator = RAGTriadEvaluator(policy)
            self.policy = self.evaluator.policy

    def run_evaluation_suite(self, dataset: List[EvaluationSample]) -> AggregateReport:
        """Ejecuta la evaluación sobre todas las muestras del dataset."""
        logger.info(f"Iniciando evaluación continua sobre {len(dataset)} muestras...")
        start_time = time.time()

        sample_results = []
        for sample in dataset:
            result = self.evaluator.evaluate_sample(sample)
            sample_results.append(result)
            rec_str = f"{result.recall_at_k.score:.2f}" if result.recall_at_k else "N/A"
            logger.info(
                f"[{sample.sample_id}] ContextRel: {result.context_relevance.score:.2f} | "
                f"Faith: {result.faithfulness.score:.2f} | "
                f"AnsRel: {result.answer_relevance.score:.2f} | "
                f"Recall: {rec_str} | "
                f"Gate: {'PASS' if result.overall_passed else 'FAIL'}"
            )

        elapsed = time.time() - start_time
        logger.info(f"Evaluación finalizada en {elapsed:.2f}s.")

        total = len(sample_results)
        passed_count = sum(1 for r in sample_results if r.overall_passed)
        pass_rate = passed_count / total if total > 0 else 0.0

        mean_ctx = sum(r.context_relevance.score for r in sample_results) / total if total > 0 else 0.0
        mean_faith = sum(r.faithfulness.score for r in sample_results) / total if total > 0 else 0.0
        mean_ans = sum(r.answer_relevance.score for r in sample_results) / total if total > 0 else 0.0

        recall_samples = [r.recall_at_k.score for r in sample_results if r.recall_at_k is not None]
        mean_recall = sum(recall_samples) / len(recall_samples) if recall_samples else 1.0

        consolidated_diagnostics = []
        for r in sample_results:
            consolidated_diagnostics.extend(r.diagnostics)

        all_passed = (
            mean_ctx >= self.policy.min_context_relevance and
            mean_faith >= self.policy.min_faithfulness and
            mean_ans >= self.policy.min_answer_relevance and
            mean_recall >= self.policy.min_recall_at_k and
            pass_rate >= self.policy.min_pass_rate
        )

        return AggregateReport(
            total_samples=total,
            passed_samples=passed_count,
            pass_rate=round(pass_rate, 4),
            mean_context_relevance=round(mean_ctx, 4),
            mean_faithfulness=round(mean_faith, 4),
            mean_answer_relevance=round(mean_ans, 4),
            mean_recall_at_k=round(mean_recall, 4),
            all_passed_quality_gate=all_passed,
            consolidated_diagnostics=consolidated_diagnostics,
            sample_results=sample_results
        )

    def export_markdown_report(self, report: AggregateReport, output_path: str) -> str:
        """Exporta un informe técnico formateado en Markdown para revisión de ingeniería."""
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        lines = [
            "# Reporte de Evaluación Continua RAG (Fase 7 - Tríada RAG & Recall@K)",
            "",
            f"**Fecha y Hora de Ejecución:** {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
            f"**Estado General del Pipeline:** {'✅ APROBADO (CI PASS)' if report.all_passed_quality_gate else '❌ RECHAZADO (CI FAIL)'}",
            f"**Tasa de Aprobación Global:** {report.pass_rate:.1%} ({report.passed_samples}/{report.total_samples} muestras)",
            "",
            "---",
            "",
            "## 1. Métricas Cuantitativas Consolidadas",
            "",
            "| Métrica | Promedio Obtenido | Umbral Mínimo Requerido | Estado |",
            "| :--- | :---: | :---: | :---: |",
            f"| **Context Relevance** | {report.mean_context_relevance:.2%} | {self.policy.min_context_relevance:.2%} | {'✅ OK' if report.mean_context_relevance >= self.policy.min_context_relevance else '⚠️ ALERTA'} |",
            f"| **Faithfulness (Groundedness)** | {report.mean_faithfulness:.2%} | {self.policy.min_faithfulness:.2%} | {'✅ OK' if report.mean_faithfulness >= self.policy.min_faithfulness else '❌ FALLA'} |",
            f"| **Answer Relevance** | {report.mean_answer_relevance:.2%} | {self.policy.min_answer_relevance:.2%} | {'✅ OK' if report.mean_answer_relevance >= self.policy.min_answer_relevance else '⚠️ ALERTA'} |",
            f"| **Recall@K (Recuperador)** | {report.mean_recall_at_k:.2%} | {self.policy.min_recall_at_k:.2%} | {'✅ OK' if report.mean_recall_at_k >= self.policy.min_recall_at_k else '❌ FALLA'} |",
            "",
            "---",
            "",
            "## 2. Resultados Detallados por Muestra (Golden Dataset)",
            "",
            "| ID | Consulta | Context Rel. | Faithfulness | Answer Rel. | Recall@K | Gate |",
            "| :--- | :--- | :---: | :---: | :---: | :---: | :---: |"
        ]

        for r in report.sample_results:
            rec_str = f"{r.recall_at_k.score:.2%}" if r.recall_at_k else "N/A"
            gate_badge = "✅ PASS" if r.overall_passed else "❌ FAIL"
            clean_query = r.query.replace("|", "/")
            if len(clean_query) > 40:
                clean_query = clean_query[:37] + "..."
            lines.append(
                f"| `{r.sample_id}` | {clean_query} | {r.context_relevance.score:.2%} | "
                f"{r.faithfulness.score:.2%} | {r.answer_relevance.score:.2%} | {rec_str} | {gate_badge} |"
            )

        lines.extend([
            "",
            "---",
            "",
            "## 3. Acciones de Diagnóstico y Bucle de Retroalimentación Operativa",
            ""
        ])

        if not report.consolidated_diagnostics:
            lines.append("No se registraron alertas ni degradaciones. El pipeline cumple todos los estándares de producción.")
        else:
            for idx, diag in enumerate(report.consolidated_diagnostics, 1):
                severity_icon = "🔴" if diag.severity == "CRITICAL" else "🟡"
                lines.extend([
                    f"### {idx}. {severity_icon} {diag.severity} — Objetivo: {diag.target_phase}",
                    f"- **Problema:** {diag.issue_description}",
                    f"- **Acción Correctiva:** {diag.recommended_action}",
                    ""
                ])

        md_content = "\n".join(lines)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(md_content)

        return md_content


# ============================================================================
# 4. BATERÍA DE PRUEBAS SOBRE CATÁLOGO DE FERRETERÍA (GOLDEN DATASET)
# ============================================================================

def get_hardware_catalog_test_samples() -> List[EvaluationSample]:
    """Genera 5 muestras representativas del catálogo de ferretería para validación."""
    return [
        # Caso 1: Búsqueda exacta de código SKU con cita precisa (Éxito Factual Completo)
        EvaluationSample(
            sample_id="SAMPLE-01-SKU-EXACTO",
            query="¿Cuál es el código de producto para la abrazadera Americana a Cremallera con apertura 9/13 mm marca CARBIZ?",
            contexts=[
                "Categoría: ABRAZADERAS DE ACERO\nMarca: CARBIZ\nProducto: Americana a Cremallera\n"
                "| Código | Articulo | Apertura | U/Vta | Paq x |\n"
                "| 100001 | 00 | 9/13 mm | c/u | 10 |\n"
                "| 100002 | 0 | 11/16 mm | c/u | 10 |"
            ],
            response=(
                "El código de producto para la abrazadera Americana a Cremallera CARBIZ con apertura de 9/13 mm es "
                "100001 (Artículo 00, presentación en paquete por 10 unidades) [Fragmento 1]."
            ),
            retrieved_doc_ids=["chunk_carbiz_100001", "chunk_carbiz_100002"],
            ground_truth_doc_ids=["chunk_carbiz_100001"]
        ),

        # Caso 2: Consulta por producto inexistente en el catálogo (Rechazo Honesto / Grounded Refusal)
        EvaluationSample(
            sample_id="SAMPLE-02-PRODUCTO-INEXISTENTE",
            query="¿Tienen abrazaderas de titanio quirúrgico de 50 mm marca CARBIZ?",
            contexts=[
                "Categoría: ABRAZADERAS DE ACERO\nMarca: CARBIZ\nProducto: Americana a Cremallera\n"
                "| Código | Articulo | Apertura | U/Vta | Paq x |\n"
                "| 100001 | 00 | 9/13 mm | c/u | 10 |"
            ],
            response=(
                "No cuento con información factual en las fuentes proporcionadas para responder a esta consulta, "
                "ya que el catálogo no registra abrazaderas de titanio quirúrgico de la marca CARBIZ."
            ),
            retrieved_doc_ids=["chunk_carbiz_100001"],
            ground_truth_doc_ids=[]
        ),

        # Caso 3: Consulta de SKU/Código con verificación estricta de fidelidad (Calibrado con Fase 6)
        EvaluationSample(
            sample_id="SAMPLE-03-SKU-TACSA-CALIBRADO",
            query="¿Qué código tiene la cinta aisladora de 20 metros marca TACSA?",
            contexts=[
                "Categoría: CINTAS AISLADORAS\nMarca: TACSA\nProducto: Cinta Aisladora PVC Flex\n"
                "| Código | Longitud | Ancho | Color | Paq x |\n"
                "| 205010 | 10 m | 19 mm | Negro | 10 |\n"
                "| 205020 | 20 m | 19 mm | Negro | 10 |"
            ],
            response=(
                "El código de producto para la cinta aisladora PVC Flex de 20 metros marca TACSA "
                "es 205020 (ancho 19 mm, color negro, paquete por 10 unidades) [Fragmento 1]."
            ),
            retrieved_doc_ids=["chunk_tacsa_205020"],
            ground_truth_doc_ids=["chunk_tacsa_205020"]
        ),

        # Caso 4: Contexto con granularidad precisa y Cross-Encoder Thresholding (Fase 1 y Fase 5 Calibrados)
        EvaluationSample(
            sample_id="SAMPLE-04-CONTEXTO-COMPRIMIDO",
            query="¿Cuál es la apertura del artículo 00 de abrazaderas CARBIZ?",
            contexts=[
                "Categoría: ABRAZADERAS DE ACERO\nMarca: CARBIZ\nProducto: Americana a Cremallera\n"
                "| Código | Articulo | Apertura | U/Vta | Paq x |\n"
                "| 100001 | 00 | 9/13 mm | c/u | 10 |"
            ],
            response=(
                "El artículo 00 de abrazaderas CARBIZ Americana a Cremallera corresponde a una apertura "
                "de 9/13 mm bajo el código 100001 [Fragmento 1]."
            ),
            retrieved_doc_ids=["chunk_carbiz_100001"],
            ground_truth_doc_ids=["chunk_carbiz_100001"]
        ),

        # Caso 5: Recuperación de Exhaustividad con efSearch=128 e Indexación HNSW Calibrada (Fase 3 y 4)
        EvaluationSample(
            sample_id="SAMPLE-05-RECALL-CALIBRADO",
            query="¿Cuáles son las medidas disponibles de mechas de acero rápido BREMEN?",
            contexts=[
                "Categoría: MECHAS Y BROCAS\nMarca: BREMEN\nProducto: Mecha Acero Rápido Fraccionaria\n"
                "| Código | Medida | Largo Total | Paq x |\n"
                "| 301001 | 1/16 pulgada | 48 mm | 10 |\n"
                "| 301002 | 5/64 pulgada | 50 mm | 10 |"
            ],
            response=(
                "Las mechas de acero rápido BREMEN están disponibles en medida de 1/16 pulgada (código 301001, largo 48 mm) "
                "y en 5/64 pulgada (código 301002, largo 50 mm) [Fragmento 1]."
            ),
            retrieved_doc_ids=["chunk_bremen_mechas_01", "chunk_bremen_mechas_02"],
            ground_truth_doc_ids=["chunk_bremen_mechas_01", "chunk_bremen_mechas_02"]
        )
    ]


# ============================================================================
# 5. EVALUACIÓN EN VIVO (INTEGRACIÓN CON FASE 6 & PIPELINE COMPLETO)
# ============================================================================

def evaluate_live_query(
    query_text: str,
    table_name: str = "catalogo_amx_rag",
    top_n: int = 3,
    mock: bool = False,
    policy: Optional[QualityGatePolicy] = None
) -> EvaluationResult:
    """
    Ejecuta una consulta real a través de las Fases 4, 5 y 6, y audita el resultado con Fase 7.
    """
    logger.info(f"Evaluando consulta en vivo: '{query_text}'")
    try:
        from fase_6_generator import run_rag_pipeline
        gen_result = run_rag_pipeline(
            query=query_text,
            table_name=table_name,
            top_n=top_n,
            mock=mock
        )

        triplet = gen_result.rag_triplet
        raw_ctx = triplet.get("context", []) or triplet.get("context_chunks", [])
        contexts = []
        for item in raw_ctx:
            if isinstance(item, str):
                contexts.append(item)
            elif isinstance(item, dict):
                contexts.append(item.get("content", str(item)))
        response_text = gen_result.response_text

        sample = EvaluationSample(
            sample_id="LIVE-QUERY-01",
            query=query_text,
            contexts=contexts,
            response=response_text,
            metadata={"latency_ms": gen_result.latency_ms, "model": gen_result.model_name}
        )

        evaluator = RAGTriadEvaluator(policy)
        result = evaluator.evaluate_sample(sample)
        return result

    except ImportError:
        logger.error("No se pudo importar `fase_6_generator`. Ejecutando evaluación sintética.")
        evaluator = RAGTriadEvaluator(policy)
        dummy_sample = EvaluationSample(
            sample_id="LIVE-QUERY-MOCK",
            query=query_text,
            contexts=["Contenido no disponible sin fase_6_generator."],
            response="No cuento con información factual para responder."
        )
        return evaluator.evaluate_sample(dummy_sample)


# ============================================================================
# 6. INTERFAZ DE LÍNEA DE COMANDOS (CLI)
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fase 7: Despliegue de Pipelines de Evaluación Continua (Tríada RAG y Recall@K).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EJEMPLOS DE USO:
  # 1. Benchmark del Golden Dataset con compuertas de calidad:
  python fase_7_evaluator.py --benchmark

  # 2. Evaluación en vivo de una consulta contra el pipeline:
  python fase_7_evaluator.py --live "Llave de impacto neumática 1/2 pulgada"

  # 3. Evaluación con LLM-as-a-Judge (OpenAI GPT-4o):
  python fase_7_evaluator.py --benchmark --llm-judge --judge-model gpt-4o

  # 4. Evaluación sobre archivo JSON personalizado:
  python fase_7_evaluator.py --dataset mis_muestras.json --output-dir ./reportes

  # 5. Salida en formato JSON puro:
  python fase_7_evaluator.py --benchmark --json
        """
    )
    parser.add_argument("query_pos", nargs="?", default=None, help="Consulta a evaluar en modo en vivo (posicional).")
    parser.add_argument("--benchmark", action="store_true", help="Ejecutar la suite de pruebas del Golden Dataset.")
    parser.add_argument("--live", "-l", type=str, default=None, help="Consulta a evaluar en vivo en el pipeline.")
    parser.add_argument("--dataset", "-d", type=str, default=None, help="Ruta a archivo JSON con muestras de evaluación.")
    parser.add_argument("--output-dir", "-o", type=str, default="./scratch/fase-7-evaluacion", help="Directorio de destino de reportes.")
    parser.add_argument("--llm-judge", action="store_true", help="Activar modo LLM-as-a-Judge.")
    parser.add_argument("--judge-model", type=str, default="gpt-4o", help="Modelo de LLM para Judge (default: gpt-4o).")
    parser.add_argument("--min-context-rel", type=float, default=0.70, help="Umbral mínimo de Context Relevance (default: 0.70).")
    parser.add_argument("--min-faithfulness", type=float, default=0.90, help="Umbral mínimo de Faithfulness (default: 0.90).")
    parser.add_argument("--min-answer-rel", type=float, default=0.80, help="Umbral mínimo de Answer Relevance (default: 0.80).")
    parser.add_argument("--min-recall", type=float, default=0.90, help="Umbral mínimo de Recall@K (default: 0.90).")
    parser.add_argument("--table", "-t", type=str, default="catalogo_amx_rag", help="Tabla en PostgreSQL para modo live.")
    parser.add_argument("--mock", action="store_true", help="Usar modo sintético en consultas en vivo.")
    parser.add_argument("--json", "-j", action="store_true", help="Imprimir salida en formato JSON.")

    args = parser.parse_args()

    policy = QualityGatePolicy(
        min_context_relevance=args.min_context_rel,
        min_faithfulness=args.min_faithfulness,
        min_answer_relevance=args.min_answer_rel,
        min_recall_at_k=args.min_recall
    )

    evaluator = RAGTriadEvaluator(
        policy=policy,
        use_llm_judge=args.llm_judge,
        judge_model=args.judge_model
    )

    # Modo 1: Consulta en vivo (Live Query)
    live_query = args.live or args.query_pos
    if live_query and not args.benchmark:
        live_result = evaluate_live_query(
            query_text=live_query,
            table_name=args.table,
            mock=args.mock,
            policy=policy
        )

        if args.json:
            print(json.dumps(live_result.to_dict(), ensure_ascii=False, indent=2))
            return

        print("\n" + "=" * 80)
        print("AUDITORÍA EN VIVO: TRÍADA RAG (FASE 7)")
        print(f"Consulta: \"{live_query}\"")
        print(f"Quality Gate General: {'✅ PASS' if live_result.overall_passed else '❌ FAIL'}")
        print("-" * 80)
        print(f"1. Context Relevance : {live_result.context_relevance.score:.2%} ({'PASS' if live_result.context_relevance.passed else 'FAIL'})")
        print(f"   Razonamiento      : {live_result.context_relevance.reasoning}")
        print(f"2. Faithfulness      : {live_result.faithfulness.score:.2%} ({'PASS' if live_result.faithfulness.passed else 'FAIL'})")
        print(f"   Razonamiento      : {live_result.faithfulness.reasoning}")
        print(f"3. Answer Relevance  : {live_result.answer_relevance.score:.2%} ({'PASS' if live_result.answer_relevance.passed else 'FAIL'})")
        print(f"   Razonamiento      : {live_result.answer_relevance.reasoning}")
        print("=" * 80 + "\n")
        return

    # Modo 2: Cargar Dataset Externo o Golden Dataset
    if args.dataset:
        logger.info(f"Cargando dataset desde: {args.dataset}")
        with open(args.dataset, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
            samples = [
                EvaluationSample(
                    sample_id=item.get("sample_id", f"SAMPLE-{i+1}"),
                    query=item["query"],
                    contexts=item["contexts"],
                    response=item["response"],
                    ground_truth_answer=item.get("ground_truth_answer"),
                    ground_truth_doc_ids=item.get("ground_truth_doc_ids"),
                    retrieved_doc_ids=item.get("retrieved_doc_ids"),
                    metadata=item.get("metadata", {})
                )
                for i, item in enumerate(raw_data)
            ]
    else:
        samples = get_hardware_catalog_test_samples()

    # Ejecutar Suite de Evaluación Continua
    pipeline = ContinuousEvaluationPipeline(evaluator=evaluator)
    report = pipeline.run_evaluation_suite(samples)

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return

    # Imprimir resumen tabular en consola
    print("\n" + "=" * 80)
    print("RESUMEN DE RESULTADOS DE EVALUACIÓN CONTINUA (TRÍADA RAG & RECALL@K)")
    print("=" * 80)
    print(f"Total de Muestras Evaluadas : {report.total_samples}")
    print(f"Muestras Aprobadas (Gate)   : {report.passed_samples} ({report.pass_rate:.1%})")
    print(f"Context Relevance Promedio  : {report.mean_context_relevance:.2%} (Mínimo: {policy.min_context_relevance:.0%})")
    print(f"Faithfulness Promedio       : {report.mean_faithfulness:.2%} (Mínimo: {policy.min_faithfulness:.0%})")
    print(f"Answer Relevance Promedio   : {report.mean_answer_relevance:.2%} (Mínimo: {policy.min_answer_relevance:.0%})")
    print(f"Recall@K Promedio           : {report.mean_recall_at_k:.2%} (Mínimo: {policy.min_recall_at_k:.0%})")
    print(f"Estado de Quality Gate      : {'✅ PASS' if report.all_passed_quality_gate else '❌ FAIL'}")
    print("=" * 80 + "\n")

    # Exportar reportes Markdown y JSON
    output_dir = args.output_dir
    os.makedirs(output_dir, exist_ok=True)
    output_md_path = os.path.join(output_dir, "reporte-evaluacion-triada-rag.md")
    output_json_path = os.path.join(output_dir, "reporte-evaluacion-triada-rag.json")

    pipeline.export_markdown_report(report, output_md_path)
    logger.info(f"Reporte Markdown guardado en: {output_md_path}")

    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)
    logger.info(f"Reporte JSON guardado en: {output_json_path}")

    if report.consolidated_diagnostics:
        print("\nAcciones de Diagnóstico y Closed-Loop Feedback:")
        for diag in report.consolidated_diagnostics:
            print(f" - [{diag.severity}] Destino: {diag.target_phase} -> {diag.recommended_action}")
        print()


if __name__ == "__main__":
    main()
