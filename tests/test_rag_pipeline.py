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

    def test_delete_records_by_provider(self):
        """Valida que la eliminación de registros por proveedor elimine solo los productos de ese proveedor."""
        from app.core.ingestion.vector_store import PgVectorManager
        import psycopg
        from psycopg import sql

        manager = PgVectorManager(
            db_url=settings.get_db_url(),
            table_name=settings.DEFAULT_TABLE_NAME,
            dimension=settings.DEFAULT_EMBEDDING_DIM
        )
        test_prov = "TST"
        dummy_vector = [0.0] * settings.DEFAULT_EMBEDDING_DIM
        dummy_record = {
            "node_id": "test_node_tst_001",
            "text_content": "Producto de prueba temporal para test de eliminación",
            "embedding": dummy_vector,
            "metadata": {
                "codigo": "TST-001",
                "codigo_orig": "TST-001",
                "nombre_proveedor": "Proveedor Test",
                "codigo_proveedor": test_prov,
                "marca": "TestBrand",
                "precio": 99.99,
                "moneda": "USD",
                "pagina": 1,
                "archivo_origen": "test_catalog.pdf",
                "es_tabla": False
            }
        }

        # 1. Ingestar registro dummy
        manager.ingest_records([dummy_record], batch_size=10)

        # 2. Verificar que existe
        with psycopg.connect(settings.get_db_url(), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("SELECT COUNT(*) FROM {} WHERE codigo_proveedor = %s;").format(
                        sql.Identifier(settings.DEFAULT_TABLE_NAME)
                    ),
                    (test_prov,)
                )
                count_before = cur.fetchone()[0]
                self.assertGreaterEqual(count_before, 1)

        # 3. Ejecutar delete_records_by_provider
        deleted = manager.delete_records_by_provider(codigo_proveedor=test_prov)
        self.assertGreaterEqual(deleted, 1)

        # 4. Verificar que se eliminó
        with psycopg.connect(settings.get_db_url(), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("SELECT COUNT(*) FROM {} WHERE codigo_proveedor = %s;").format(
                        sql.Identifier(settings.DEFAULT_TABLE_NAME)
                    ),
                    (test_prov,)
                )
                count_after = cur.fetchone()[0]
                self.assertEqual(count_after, 0)


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

    def test_orchestrator_query_structured_json(self):
        """Valida que la consulta con structured_json emita la ficha técnica y comercial completa."""
        res = self.orchestrator.query(
            query_text="Llave de impacto 1/2 pulgada",
            top_n=2,
            structured_json=True,
            audit_sample=False
        )
        self.assertEqual(res.status, "SUCCESS")
        self.assertIsNotNone(res.structured_json)
        self.assertTrue(res.structured_json.get("consulta_respondida"))
        productos = res.structured_json.get("productos", [])
        self.assertGreater(len(productos), 0)
        p0 = productos[0]
        self.assertIn("codigo", p0)
        self.assertIn("marca", p0)
        self.assertIn("nombre", p0)
        self.assertIn("precio", p0)
        self.assertIn("moneda", p0)
        self.assertIn("archivo_origen", p0)
        self.assertIn("pagina", p0)
        self.assertIn("especificaciones", p0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
