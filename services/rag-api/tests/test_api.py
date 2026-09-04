#!/usr/bin/env python3
"""
tests/test_api.py
=================
Suite de pruebas automatizadas para los endpoints REST de la aplicación FastAPI.
"""

import unittest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


class TestRAGApi(unittest.TestCase):
    """Pruebas de integración sobre la API REST."""

    def test_health_root_endpoint(self):
        """Valida el endpoint /health."""
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("status", data)
        self.assertIn("db_connected", data)
        self.assertIn("pgvector_enabled", data)
        self.assertIn("target_table_exists", data)
        self.assertTrue(data["db_connected"])
        self.assertTrue(data["pgvector_enabled"])
        self.assertTrue(data["target_table_exists"])

    def test_health_v1_endpoint(self):
        """Valida el endpoint /api/v1/health."""
        response = client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "HEALTHY")
        self.assertGreater(data["total_products_indexed"], 0)
        self.assertIn("AMX", data["active_providers"])
        self.assertIn("PZF", data["active_providers"])

    def test_catalogs_list_endpoint(self):
        """Valida el inventario consolidado /api/v1/catalogs."""
        response = client.get("/api/v1/catalogs")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertGreaterEqual(data["total_proveedores"], 2)
        self.assertGreaterEqual(data["total_productos"], 100)
        codes = [c["codigo_proveedor"] for c in data["catalogs"]]
        self.assertIn("AMX", codes)
        self.assertIn("PZF", codes)

    def test_query_rag_endpoint(self):
        """Valida la consulta en tiempo real /api/v1/query."""
        payload = {
            "query": "Llave de impacto neumática 1/2 pulgada",
            "table_name": "catalogo_productos_rag",
            "top_n": 2,
            "threshold": 0.45,
            "audit": False
        }
        response = client.post("/api/v1/query", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "SUCCESS")
        self.assertTrue(len(data["response_text"]) > 10)
        self.assertTrue(data["is_fully_grounded"])
        self.assertGreaterEqual(len(data["citations"]), 1)
        self.assertGreater(data["total_latency_ms"], 0)

    def test_job_not_found(self):
        """Valida el manejo de 404 para un job inexistente."""
        response = client.get("/api/v1/jobs/job-noexistente999")
        self.assertEqual(response.status_code, 404)
        data = response.json()
        self.assertIn("detail", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
