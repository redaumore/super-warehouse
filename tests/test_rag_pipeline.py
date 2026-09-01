#!/usr/bin/env python3
"""
tests/test_rag_pipeline.py
==========================
Pruebas unitarias para el orquestador y los componentes del Core RAG.
"""

import unittest
from app.core.orchestrator import RAGOrchestrator
from app.config import settings


class TestRAGPipeline(unittest.TestCase):
    """Test suite para el orquestador maestro y el pipeline RAG."""

    def setUp(self):
        self.orchestrator = RAGOrchestrator(table_name=settings.DEFAULT_TABLE_NAME)

    def test_orchestrator_initialization(self):
        """Valida que el orquestador se inicialice con la configuración por defecto."""
        self.assertEqual(self.orchestrator.table_name, settings.DEFAULT_TABLE_NAME)
        self.assertEqual(self.orchestrator.llm_model, settings.DEFAULT_LLM_MODEL)
        self.assertIsNotNone(self.orchestrator.evaluator)

    def test_orchestrator_query_online(self):
        """Valida una consulta real a la base de datos PostgreSQL/pgvector."""
        res = self.orchestrator.query(
            query_text="Llave de impacto 1/2 pulgada",
            top_n=2,
            audit_sample=False
        )
        self.assertEqual(res.status, "SUCCESS")
        self.assertTrue(res.is_fully_grounded)
        self.assertGreater(len(res.response_text), 10)
        self.assertGreater(len(res.citations), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
