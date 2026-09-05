#!/usr/bin/env python3
"""
tests/test_refusal_fallback.py
=============================
Pruebas unitarias para el fallback "guard vs. retrieval":

Cuando el LLM rechaza la consulta (Directiva de Ausencia canónica o
`consulta_respondida=false` en salida estructurada) pero la recuperación
híbrida (BM25 + vector) produjo hits con score por encima del umbral de
relevancia configurado, el pipeline debe devolver esos hits como respuesta
SUCCESS (mismo esquema, sin rechazo). Solo se mantiene el rechazo cuando no
hay evidencia de retrieval por encima del umbral.

Los tests no requieren LLM externo ni base de datos:
- El motor determinista offline (`provider="offline"`) emula el LLM.
- `retrieve_and_rerank` se parchea con candidatos controlados.
"""

import unittest
from unittest.mock import patch

from app.core.retrieval.generator import (
    RETRIEVAL_FALLBACK_NARRATIVE,
    PromptBuilder,
    _should_fallback_to_retrieval,
    run_rag_pipeline,
)
from app.core.retrieval.reranker import RankedCandidate

THRESHOLD = 0.45


def _make_candidate(
    codigo: str,
    score: float,
    content: str,
    prompt_position: int = 1,
) -> RankedCandidate:
    """Candidato controlado de Fase 5 con metadatos completos, como en producción."""
    return RankedCandidate(
        node_id=f"CAT-{codigo}",
        codigo_producto=codigo,
        marca="RAPID-FIX",
        categoria="Acoples y Conexiones",
        text_content=content,
        metadata={
            "codigo": codigo,
            "codigo_orig": codigo.replace("AC-", ""),
            "codigo_proveedor": "FDN",
            "nombre_proveedor": "Ferretera del Norte",
            "precio": 8500.0,
            "moneda": "ARS",
            "unidad_venta": "c/u",
            "empaque": "Pack x 5",
            "pagina": 12,
            "archivo_origen": "catalogo_fdn.pdf",
        },
        normalized_score=score,
        prompt_position=prompt_position,
    )


def _acople_content() -> str:
    return (
        "codigo_producto: AC-X5\n"
        "marca: RAPID-FIX\n"
        "nombre: Acople rapido X 5 PZAS\n"
        "descripcion: Acople rapido neumatico universal pack x 5 piezas.\n"
        "conexion: 1/4 NPT\n"
        "material: Acero al carbono"
    )


# Consulta ajena al contenido de los candidatos: fuerza el rechazo canónico
# del motor determinista offline (equivalente al guard del LLM real).
_REFUSAL_QUERY = "bomba peristaltica dosificadora industrial"
# Consulta que coincide léxicamente con los candidatos: respuesta normal.
_MATCHING_QUERY = "acople rapido"


def _fake_rerank_result(candidates):
    """Stub mínimo del RerankResult de Fase 5 (solo se consume final_candidates)."""
    from types import SimpleNamespace
    return SimpleNamespace(final_candidates=candidates)


class TestShouldFallbackToRetrieval(unittest.TestCase):
    """Prueba la función de decisión pura (sin LLM ni DB)."""

    def test_returns_true_when_score_above_threshold(self):
        self.assertTrue(_should_fallback_to_retrieval([0.92, 0.87], THRESHOLD))

    def test_returns_true_when_any_score_above_threshold(self):
        self.assertTrue(_should_fallback_to_retrieval([0.30, 0.50], THRESHOLD))

    def test_returns_false_when_no_scores(self):
        self.assertFalse(_should_fallback_to_retrieval([], THRESHOLD))

    def test_returns_false_when_all_scores_below_threshold(self):
        self.assertFalse(_should_fallback_to_retrieval([0.40, 0.10], THRESHOLD))

    def test_returns_false_when_score_exactly_at_threshold(self):
        self.assertFalse(_should_fallback_to_retrieval([THRESHOLD], THRESHOLD))

    def test_returns_false_when_scores_are_none(self):
        self.assertFalse(_should_fallback_to_retrieval([None, None], THRESHOLD))


class TestRefusalFallbackPipeline(unittest.TestCase):
    """Pruebas end-to-end del pipeline con LLM offline determinista."""

    def _run(self, query: str, candidates, threshold: float = THRESHOLD):
        with patch(
            "app.core.retrieval.reranker.retrieve_and_rerank",
            return_value=_fake_rerank_result(candidates),
        ):
            return run_rag_pipeline(
                query=query,
                table_name="catalogo_test",
                top_n=3,
                threshold=threshold,
                llm_model="gpt-4o",
                provider="offline",  # fuerza motor determinista, sin red ni API keys
                structured_json=True,
                mock=False
            )

    def test_refusal_with_retrieval_hits_returns_products(self):
        """1. LLM rechaza + hits > umbral → SUCCESS con productos del retrieval."""
        candidates = [
            _make_candidate("AC-X5", 0.92, _acople_content(), prompt_position=1),
            _make_candidate("AC-X7", 0.87, _acople_content().replace("X 5", "X 7"), prompt_position=2),
        ]
        result = self._run(_REFUSAL_QUERY, candidates)

        self.assertFalse(result.is_refusal)
        self.assertEqual(result.status, "SUCCESS")
        self.assertIsNotNone(result.structured_json)
        self.assertTrue(result.structured_json["consulta_respondida"])
        self.assertEqual(result.structured_json["respuesta_narrativa"], RETRIEVAL_FALLBACK_NARRATIVE)

        productos = result.structured_json["productos"]
        self.assertGreater(len(productos), 0)
        self.assertEqual(productos[0]["codigo"], "AC-X5")
        self.assertEqual(productos[0]["codigo_proveedor"], "FDN")
        self.assertEqual(productos[0]["marca"], "RAPID-FIX")
        self.assertEqual(productos[0]["precio"], 8500.0)
        self.assertEqual(productos[0]["moneda"], "ARS")
        self.assertEqual(productos[0]["pagina"], 12)
        self.assertTrue(result.verification.is_fully_grounded)

    def test_refusal_without_retrieval_hits_keeps_refusal(self):
        """2. LLM rechaza + retrieval vacío → rechazo igual que antes (sin cambios)."""
        result = self._run(_REFUSAL_QUERY, [])

        self.assertTrue(result.is_refusal)
        self.assertEqual(result.status, "REFUSAL_GROUNDED")
        self.assertIsNotNone(result.structured_json)
        self.assertFalse(result.structured_json["consulta_respondida"])
        self.assertEqual(result.structured_json["productos"], [])
        self.assertIn(PromptBuilder.REFUSAL_CANONICAL_MESSAGE, result.structured_json["respuesta_narrativa"])

    def test_refusal_with_hits_at_threshold_keeps_refusal(self):
        """3. LLM rechaza + hits en/por debajo del umbral → rechazo (umbral respetado)."""
        candidates = [_make_candidate("AC-X5", THRESHOLD, _acople_content())]
        result = self._run(_REFUSAL_QUERY, candidates, threshold=THRESHOLD)

        self.assertTrue(result.is_refusal)
        self.assertEqual(result.status, "REFUSAL_GROUNDED")
        self.assertFalse(result.structured_json["consulta_respondida"])

        candidates_below = [_make_candidate("AC-X5", 0.44, _acople_content())]
        result_below = self._run(_REFUSAL_QUERY, candidates_below, threshold=THRESHOLD)
        self.assertTrue(result_below.is_refusal)
        self.assertEqual(result_below.status, "REFUSAL_GROUNDED")

    def test_normal_path_unchanged(self):
        """4. Camino normal (sin rechazo) queda intacto: respuesta del LLM con citas."""
        candidates = [
            _make_candidate("AC-X5", 0.92, _acople_content(), prompt_position=1),
            _make_candidate("AC-X7", 0.87, _acople_content().replace("X 5", "X 7"), prompt_position=2),
        ]
        result = self._run(_MATCHING_QUERY, candidates)

        self.assertFalse(result.is_refusal)
        self.assertEqual(result.status, "SUCCESS")
        self.assertIsNotNone(result.structured_json)
        self.assertTrue(result.structured_json["consulta_respondida"])
        productos = result.structured_json["productos"]
        self.assertGreater(len(productos), 0)
        # Respuesta sintetizada por el LLM (con citas), no la narrativa neutral del fallback.
        self.assertNotEqual(result.structured_json["respuesta_narrativa"], RETRIEVAL_FALLBACK_NARRATIVE)
        self.assertGreater(len(result.verification.citations_found), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
