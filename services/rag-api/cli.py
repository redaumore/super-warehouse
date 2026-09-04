#!/usr/bin/env python3
"""
cli.py
======
Línea de Comandos Unificada (CLI) para Gestión del Sistema RAG y API REST.

Comandos y Modos de Uso:
  1. Iniciar Servidor API REST:
     python cli.py serve --port 8000 --reload

  2. Ingesta Batch de Catálogo PDF (Fases 0 a 3):
     python cli.py ingest "data/raw_pdfs/FN Catalogo.pdf" --cod-prov "FDN" --recreate-table

  3. Consulta en Tiempo Real (Fases 4 a 6):
     python cli.py query "Llave de impacto 1/2 pulgada" --top-n 3

  4. Auditoría y Evaluación de Calidad (Fase 7):
     python cli.py evaluate

  5. Verificación de Salud de Base de Datos:
     python cli.py health
"""

import sys
import json
import argparse
import logging
import uvicorn

from app.config import settings
from app.core.orchestrator import RAGOrchestrator
from app.api.v1.endpoints.health import get_health

logger = logging.getLogger("CLI")


def main():
    parser = argparse.ArgumentParser(
        description="RAG Master CLI: Herramienta unificada de gestión para Catálogos Industriales y API REST.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EJEMPLOS DE USO:
  python cli.py serve --port 8000 --reload
  python cli.py query "Llave de impacto 1/2 pulgada" --table "catalogo_productos_rag"
  python cli.py ingest "data/raw_pdfs/FN Catalogo.pdf" --cod-prov "FDN" --max-pages 5
  python cli.py evaluate
  python cli.py health
        """
    )
    subparsers = parser.add_subparsers(dest="command", help="Comando a ejecutar")

    # 1. Subcomando: SERVE
    serve_parser = subparsers.add_parser("serve", help="Iniciar el servidor API REST con Uvicorn.")
    serve_parser.add_argument("--host", type=str, default="0.0.0.0", help="Host del servidor (default: 0.0.0.0).")
    serve_parser.add_argument("--port", "-p", type=int, default=8000, help="Puerto del servidor (default: 8000).")
    serve_parser.add_argument("--reload", action="store_true", help="Recarga automática en caliente para desarrollo.")

    # 2. Subcomando: QUERY
    query_parser = subparsers.add_parser("query", help="Ejecutar consulta RAG en tiempo real.")
    query_parser.add_argument("query_pos", nargs="?", default=None, help="Texto de la consulta.")
    query_parser.add_argument("--query", "-q", type=str, default=None, help="Texto de la consulta.")
    query_parser.add_argument("--table", "-t", type=str, default=settings.DEFAULT_TABLE_NAME, help="Tabla en PostgreSQL.")
    query_parser.add_argument("--top-n", "-n", type=int, default=3, help="Candidatos finalistas tras reranking.")
    query_parser.add_argument("--structured", "-s", action="store_true", help="Salida JSON estructurada.")
    query_parser.add_argument("--audit", "-a", action="store_true", help="Auditar con la Tríada RAG.")
    query_parser.add_argument("--mock", action="store_true", help="Modo sintético offline.")
    query_parser.add_argument("--json", "-j", action="store_true", help="Formato de salida JSON.")

    # 3. Subcomando: INGEST
    ingest_parser = subparsers.add_parser("ingest", help="Ingestar un catálogo PDF completo (Fases 0 a 3).")
    ingest_parser.add_argument("pdf_pos", nargs="?", default=None, help="Ruta al archivo PDF.")
    ingest_parser.add_argument("--pdf", "-f", type=str, default=None, help="Ruta al archivo PDF.")
    ingest_parser.add_argument("--codigo-proveedor", "--cod-prov", dest="codigo_proveedor", type=str, default="FDN", help="Código de 3 caracteres del proveedor.")
    ingest_parser.add_argument("--nombre-proveedor", "--proveedor", dest="nombre_proveedor", type=str, default="Ferretera del Norte", help="Nombre del proveedor.")
    ingest_parser.add_argument("--proveedor-id", dest="proveedor_id", type=str, default=None, help="Slug único del proveedor.")
    ingest_parser.add_argument("--marca", type=str, default=None, help="Marca forzada para los productos.")
    ingest_parser.add_argument("--start-page", type=int, default=1, help="Página de inicio (1-indexed).")
    ingest_parser.add_argument("--max-pages", type=int, default=None, help="Cantidad máxima de páginas a procesar.")
    ingest_parser.add_argument("--skip-pages", type=str, default=None, help="Páginas o rangos a omitir (ej: '1-2,4').")
    ingest_parser.add_argument("--no-vision", action="store_true", help="Desactivar visión multimodal (usar solo texto).")
    ingest_parser.add_argument("--recreate-table", action="store_true", help="Elimina y recrea la tabla antes de ingestar.")
    ingest_parser.add_argument("--table", "-t", type=str, default=settings.DEFAULT_TABLE_NAME, help="Tabla destino en PostgreSQL.")
    ingest_parser.add_argument("--output-dir", type=str, default=None, help="Directorio de destino de JSONs intermediarios.")
    ingest_parser.add_argument("--json", "-j", action="store_true", help="Formato de salida JSON.")

    # 4. Subcomando: EVALUATE
    eval_parser = subparsers.add_parser("evaluate", help="Ejecutar suite de evaluación continua (Tríada RAG).")
    eval_parser.add_argument("--table", "-t", type=str, default=settings.DEFAULT_TABLE_NAME, help="Tabla a evaluar.")
    eval_parser.add_argument("--output-dir", "-o", type=str, default=None, help="Directorio de exportación de reportes.")

    # 5. Subcomando: HEALTH
    health_parser = subparsers.add_parser("health", help="Verificar estado de salud de PostgreSQL y pgvector.")
    health_parser.add_argument("--table", "-t", type=str, default=settings.DEFAULT_TABLE_NAME, help="Tabla a verificar.")

    # Compatibilidad con flags legacy de primer nivel
    parser.add_argument("--ingest", "-i", type=str, default=None, help="Modo compatibilidad: Ingesta directa de PDF.")
    parser.add_argument("--query", "-q", type=str, default=None, help="Modo compatibilidad: Consulta RAG.")
    parser.add_argument("--evaluate", "-e", action="store_true", help="Modo compatibilidad: Evaluación continua.")
    parser.add_argument("--table", "-t", type=str, default=settings.DEFAULT_TABLE_NAME, help="Tabla en PostgreSQL.")
    parser.add_argument("query_compat", nargs="*", default=None, help="Consulta directa posicional.")

    args = parser.parse_args()

    # Resolver subcomando
    if args.command == "serve":
        logger.info(f"Iniciando API REST en http://{args.host}:{args.port}...")
        uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)
        return

    elif args.command == "health":
        h = get_health(table_name=args.table)
        print("\n" + "=" * 60)
        print("ESTADO DE SALUD DEL SISTEMA RAG")
        print("=" * 60)
        print(f"Estado Global          : {h.status}")
        print(f"Conexión PostgreSQL    : {'✓ Conectado' if h.db_connected else '✗ Desconectado'}")
        print(f"Extensión pgvector     : {'✓ Habilitada' if h.pgvector_enabled else '✗ No disponible'}")
        print(f"Tabla '{args.table}' : {'✓ Existe' if h.target_table_exists else '✗ No encontrada'}")
        print(f"Productos Indexados    : {h.total_products_indexed}")
        print(f"Proveedores Activos    : {', '.join(h.active_providers) if h.active_providers else 'Ninguno'}")
        print("=" * 60 + "\n")
        return

    elif args.command == "ingest" or args.ingest:
        pdf_path = getattr(args, "pdf_pos", None) or getattr(args, "pdf", None) or args.ingest
        if not pdf_path:
            logger.error("Debe especificar la ruta al archivo PDF a ingestar.")
            sys.exit(1)

        orchestrator = RAGOrchestrator(table_name=args.table)
        res = orchestrator.ingest_catalog_pdf(
            pdf_path=pdf_path,
            codigo_proveedor=getattr(args, "codigo_proveedor", "FDN"),
            nombre_proveedor=getattr(args, "nombre_proveedor", "Ferretera del Norte"),
            proveedor_id=getattr(args, "proveedor_id", None),
            marca=getattr(args, "marca", None),
            start_page=getattr(args, "start_page", 1),
            max_pages=getattr(args, "max_pages", None),
            skip_pages=getattr(args, "skip_pages", None),
            use_vision=not getattr(args, "no_vision", False),
            recreate_table=getattr(args, "recreate_table", False),
            output_dir=getattr(args, "output_dir", None)
        )

        if getattr(args, "json", False):
            print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
        else:
            print("\n" + "=" * 80)
            print("RESUMEN DE INGESTA INTEGRAL")
            print("=" * 80)
            print(f"Estado                 : {res.status}")
            print(f"Documento Origen       : {res.source_document}")
            print(f"Tabla PostgreSQL       : {res.target_table}")
            print(f"Páginas Procesadas     : {res.pages_processed}")
            print(f"Productos Extraídos    : {res.total_products_extracted}")
            print(f"Nodos Generados        : {res.total_nodes_generated}")
            print(f"Embeddings Creados     : {res.total_embeddings_created}")
            print(f"Registros Indexados    : {res.total_records_indexed}")
            print(f"Tokens Consumidos      : {res.total_tokens_used:,}")
            print(f"Tiempo Total           : {res.total_elapsed_seconds:.2f} s")
            if res.error:
                print(f"Error                  : {res.error}")
            print("=" * 80 + "\n")
        return

    elif args.command == "evaluate" or getattr(args, "evaluate", False):
        orchestrator = RAGOrchestrator(table_name=args.table)
        report = orchestrator.evaluate_pipeline(output_dir=getattr(args, "output_dir", None))
        print("\n" + "=" * 80)
        print("RESUMEN DE EVALUACIÓN CONTINUA (TRÍADA RAG)")
        print("=" * 80)
        print(f"Total Muestras Evaluadas : {report.total_samples}")
        print(f"Muestras Aprobadas       : {report.passed_samples} ({report.pass_rate:.1%})")
        print(f"Context Relevance Media  : {report.mean_context_relevance:.2%}")
        print(f"Faithfulness Media       : {report.mean_faithfulness:.2%}")
        print(f"Answer Relevance Media   : {report.mean_answer_relevance:.2%}")
        print(f"Recall@K Media           : {report.mean_recall_at_k:.2%}")
        print(f"Quality Gate Global      : {'✅ PASS' if report.all_passed_quality_gate else '❌ FAIL'}")
        print("=" * 80 + "\n")
        return

    # Modo Query por defecto o subcomando
    query_text = (
        getattr(args, "query_pos", None)
        or getattr(args, "query", None)
        or (" ".join(args.query_compat) if args.query_compat else None)
    )
    if not query_text:
        query_text = "Llave de impacto neumática 1/2 pulgada"

    orchestrator = RAGOrchestrator(table_name=args.table, auto_audit=getattr(args, "audit", False))
    response = orchestrator.query(
        query_text=query_text,
        top_n=getattr(args, "top_n", 3),
        structured_json=getattr(args, "structured", False),
        audit_sample=getattr(args, "audit", False),
        mock=getattr(args, "mock", False)
    )

    if getattr(args, "json", False):
        print(json.dumps(response.to_dict(), ensure_ascii=False, indent=2))
        return

    print("\n" + "=" * 80)
    print("RESPUESTA DEL ORQUESTADOR RAG")
    print(f"Consulta: \"{query_text}\"")
    print(f"Estado: {response.status} | Latencia Total: {response.total_latency_ms:.2f} ms | Modelo: {response.model_name}")
    print(f"Citas: {response.citations} | Grounded: {response.is_fully_grounded}")
    print("=" * 80)
    print(response.response_text)
    print("=" * 80)

    if response.evaluation:
        ev = response.evaluation
        print("\nAUDITORÍA TRÍADA RAG (FASE 7):")
        print(f" * Context Relevance : {ev['context_relevance']['score']:.2%}")
        print(f" * Faithfulness      : {ev['faithfulness']['score']:.2%}")
        print(f" * Answer Relevance  : {ev['answer_relevance']['score']:.2%}")
        print(f" * Quality Gate      : {'✅ PASS' if ev['overall_passed'] else '❌ FAIL'}")
        print("-" * 80 + "\n")


if __name__ == "__main__":
    main()
