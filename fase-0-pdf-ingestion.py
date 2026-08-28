"""
Script de procesamiento y extracción directa de catálogos PDF utilizando OpenAI GPT-5.6 Luna.

Este pipeline procesa catálogos en un solo paso multimodal (End-to-End):
- Renderiza las páginas del PDF como imágenes de alta resolución e inspecciona la capa de texto vectorial.
- Utiliza GPT-5.6 Luna con Structured Outputs (esquema estricto Pydantic) para:
  * Identificar marcas reales del producto y descartar logotipos de la distribuidora.
  * Extraer cada fila de las tablas como una variante individual con su código de artículo.
  * Normalizar categorías y atributos clave (medida, apertura, material, cantidad por paquete, etc.).
  * Generar descripciones comerciales completas optimizadas para búsqueda semántica/RAG y e-commerce.

USO TÍPICO:
    # 1. Procesar un rango de páginas excluyendo carátula e índices:
    python fase-0-direct-luna.py --pdf "data/FN Catalogo.pdf" --start_page 1 --max_pages 10 --skip_pages "1-3" --out_json "catalogo_luna.json"

    # 2. Procesar páginas específicas o con saltos:
    python fase-0-direct-luna.py --pdf "data/FN Catalogo.pdf" --start_page 4 --max_pages 20 --skip_pages "7,9-11"

    # 3. Procesar únicamente capa de texto (sin visión multimodal):
    python fase-0-direct-luna.py --pdf "data/FN Catalogo.pdf" --no_vision
"""

import os
import sys
import time
import json
import base64
import logging
import argparse
from datetime import datetime
from typing import List, Dict, Any, Optional
import pymupdf as fitz
from pydantic import BaseModel, Field
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("RAG_Fase_0_Luna_Direct")


# Pydantic Schemas for Structured Output
class AttributeItem(BaseModel):
    nombre: str = Field(description="Nombre del atributo en snake_case (ej: apertura, color, medida, unidad_venta, paquete_cantidad)")
    valor: str = Field(description="Valor del atributo (ej: 9/13 mm, Negro, 10)")


class ExtractedProduct(BaseModel):
    codigo: Optional[str] = Field(default=None, description="Código de artículo o identificador único presente en el catálogo (None o vacío si no existe)")
    proveedor: Optional[str] = Field(default=None, description="Nombre o identificador del proveedor asociado al producto")
    nombre_comercial: str = Field(description="Título claro y estandarizado para catálogo / e-commerce, incluyendo el sustantivo rector y detalles clave")
    categoria_padre: str = Field(description="Categoría principal de ferretería (ej: Fijaciones y Sujeciones, Herramientas, Cintas, Seguridad Industrial)")
    categoria: str = Field(description="Categoría específica (ej: Abrazaderas, Cintas Adhesivas, Calzado de Seguridad)")
    subcategoria: str = Field(description="Subcategoría o tipo de material/diseño (ej: Abrazaderas de Acero, Cintas Aisladoras PVC)")
    marca: str = Field(description="Marca normalizada del producto detectada en la página o encabezado")
    descripcion_completa: str = Field(description="Descripción comercial fluida y completa para búsqueda semántica/RAG")
    atributos: List[AttributeItem] = Field(default_factory=list, description="Lista de atributos técnicos normalizados de esta variante/fila")
    especificaciones_tabla: List[AttributeItem] = Field(default_factory=list, description="Lista de especificaciones exactas columna-valor presentes en la fila de la tabla")


class PageUsageMetrics(BaseModel):
    prompt_tokens: int = Field(default=0, description="Tokens de entrada/prompt consumidos")
    completion_tokens: int = Field(default=0, description="Tokens de salida/completion generados")
    total_tokens: int = Field(default=0, description="Total de tokens consumidos en la página")
    elapsed_seconds: float = Field(default=0.0, description="Tiempo de respuesta en segundos para esta página")


class PageExtractionResult(BaseModel):
    pagina: int = Field(description="Número de página 1-indexed")
    marca_encabezado: str = Field(description="Marca del fabricante identificada en la cabecera/página (ignorar distribuidora)")
    productos: List[ExtractedProduct] = Field(default_factory=list, description="Lista de productos/variantes extraídos de la página")
    metrics: Optional[PageUsageMetrics] = Field(default=None, description="Métricas de consumo de tokens y latencia de la llamada")


class DirectLunaCatalogProcessor:
    def __init__(self, model: str = "gpt-5.6-luna"):
        self.model = model
        self.api_key = os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            logger.error("No se encontró OPENAI_API_KEY en las variables de entorno ni en el archivo .env")
            sys.exit(1)
        self.client = OpenAI(api_key=self.api_key)
        logger.info(f"DirectLunaCatalogProcessor inicializado con modelo: {self.model}")

    def render_page_to_base64(self, page: fitz.Page, dpi: int = 200) -> str:
        """Renderiza una página de PDF a imagen PNG en base64 para inspección visual."""
        pix = page.get_pixmap(dpi=dpi)
        img_bytes = pix.tobytes("png")
        return base64.b64encode(img_bytes).decode("utf-8")

    def process_page(self, page_num: int, page_text: str, image_b64: Optional[str] = None) -> PageExtractionResult:
        """Envía el contenido de la página directamente al modelo GPT-5.6 Luna."""
        system_prompt = (
            "Sos un extractor y estructurador experto de catálogos industriales y ferreteros para sistemas RAG y e-commerce.\n"
            "Tu tarea es analizar la página del catálogo provista (texto y/o imagen de la página), identificar las tablas de productos, "
            "los encabezados, marcas (distinguiendo la marca real del fabricante del logo de la distribuidora del catálogo) "
            "y extraer cada variante/fila de producto de forma completa y estructurada.\n\n"
            "REGLAS CRÍTICAS:\n"
            "1. NO omitas variantes ni códigos de producto de las tablas.\n"
            "2. Identifica la marca real de los productos en la página (ej: Fischer, Stanley, Tacsa, etc.).\n"
            "3. Genera un nombre comercial claro y una descripción técnica enriquecida para cada variante.\n"
            "4. Extrae atributos clave normalizados (medida, color, material, presentación, unidades, etc.).\n"
            "5. Conserva las especificaciones originales de la tabla en especificaciones_tabla."
        )

        user_content: List[Dict[str, Any]] = [
            {
                "type": "text",
                "text": f"--- INFORMACIÓN DE LA PÁGINA {page_num} ---\n\nTEXTO EXTRAÍDO POR CAPA VECTORIAL:\n{page_text or '[Página puramente visual/escaneada]'}"
            }
        ]

        if image_b64:
            user_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{image_b64}",
                    "detail": "high"
                }
            })

        logger.info(f"Procesando página {page_num} con {self.model}...")
        start_t = time.time()
        
        response = self.client.beta.chat.completions.parse(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}  # type: ignore[arg-type]
            ],
            response_format=PageExtractionResult
        )
        elapsed = time.time() - start_t
        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        total_tokens = usage.total_tokens if usage else (prompt_tokens + completion_tokens)

        logger.info(f"Página {page_num} completada en {elapsed:.2f}s (Tokens: Prompt={prompt_tokens}, Completion={completion_tokens}, Total={total_tokens})")

        parsed_res = response.choices[0].message.parsed
        if parsed_res is None:
            raise ValueError(f"Fallo al parsear la respuesta estructurada para la página {page_num}")

        parsed_res.pagina = page_num
        parsed_res.metrics = PageUsageMetrics(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            elapsed_seconds=round(elapsed, 2)
        )
        return parsed_res

    def _parse_skip_pages(self, skip_str: Optional[str]) -> set:
        """Parsea strings como '1,2,3' o '1-3,5,8-10' a un conjunto de enteros."""
        if not skip_str:
            return set()
        pages = set()
        for part in skip_str.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                try:
                    start, end = map(int, part.split("-"))
                    pages.update(range(start, end + 1))
                except ValueError:
                    logger.warning(f"Rango de páginas a omitir inválido: '{part}'")
            else:
                try:
                    pages.add(int(part))
                except ValueError:
                    logger.warning(f"Número de página a omitir inválido: '{part}'")
        return pages

    def process_catalog(
        self,
        pdf_path: str,
        output_json: str,
        start_page: int = 1,
        max_pages: Optional[int] = None,
        use_vision: bool = True,
        skip_pages: Optional[str] = None,
        proveedor: str = "Ferretera del Norte"
    ):
        """Procesa el PDF página por página directamente con GPT-5.6 Luna."""
        if not os.path.exists(pdf_path):
            logger.error(f"El archivo PDF no existe: {pdf_path}")
            sys.exit(1)

        run_date_iso = datetime.now().astimezone().isoformat()
        doc = fitz.open(pdf_path)
        total_pdf_pages = len(doc)
        end_page = total_pdf_pages if max_pages is None else min(start_page + max_pages - 1, total_pdf_pages)
        pages_to_skip = self._parse_skip_pages(skip_pages)

        logger.info(f"Iniciando procesamiento directo de '{pdf_path}' (Páginas {start_page} a {end_page} de {total_pdf_pages}) | Proveedor: {proveedor}")
        if pages_to_skip:
            logger.info(f"Páginas a omitir: {sorted(list(pages_to_skip))}")
        
        all_pages_results: List[PageExtractionResult] = []
        total_products = 0
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_tokens = 0
        start_benchmark = time.time()

        for pno in range(start_page, end_page + 1):
            if pno in pages_to_skip:
                logger.info(f"-> Página {pno}: Omitida según configuración.")
                continue

            page_idx = pno - 1
            page = doc[page_idx]
            page_text = str(page.get_text() or "")
            image_b64 = self.render_page_to_base64(page) if use_vision else None

            try:
                page_result = self.process_page(pno, page_text, image_b64)

                # Asignar proveedor y resolver ID/código de producto
                for prod in page_result.productos:
                    prod.proveedor = proveedor
                    # Si no tiene ID o código explícito (o es N/D / N/A), concatenar proveedor y nombre comercial
                    if not prod.codigo or prod.codigo.strip().upper() in ["N/D", "N/A", "NONE", "NULL", ""]:
                        prod.codigo = f"{proveedor}_{prod.nombre_comercial}"

                all_pages_results.append(page_result)
                total_products += len(page_result.productos)

                if page_result.metrics:
                    total_prompt_tokens += page_result.metrics.prompt_tokens
                    total_completion_tokens += page_result.metrics.completion_tokens
                    total_tokens += page_result.metrics.total_tokens

                logger.info(f"-> Página {pno}: Extraídos {len(page_result.productos)} productos. Marca detectada: '{page_result.marca_encabezado}'")
            except Exception as e:
                logger.error(f"Error procesando página {pno}: {e}")

        total_elapsed = time.time() - start_benchmark
        pages_count = len(all_pages_results)

        # Estructura consolidada con métricas detalladas de consumo, proveedor y fecha de corrida
        output_payload = {
            "metadata": {
                "source_file": os.path.basename(pdf_path),
                "model": self.model,
                "proveedor": proveedor,
                "run_date": run_date_iso,
                "execution_summary": {
                    "start_page": start_page,
                    "end_page": end_page,
                    "pages_requested": (end_page - start_page + 1),
                    "pages_processed": pages_count,
                    "skipped_pages": sorted(list(pages_to_skip)),
                    "total_products_extracted": total_products,
                    "total_elapsed_seconds": round(total_elapsed, 2),
                    "avg_seconds_per_page": round(total_elapsed / max(1, pages_count), 2)
                },
                "token_usage": {
                    "total_prompt_tokens": total_prompt_tokens,
                    "total_completion_tokens": total_completion_tokens,
                    "total_tokens": total_tokens,
                    "avg_prompt_tokens_per_page": round(total_prompt_tokens / max(1, pages_count), 2) if pages_count else 0,
                    "avg_completion_tokens_per_page": round(total_completion_tokens / max(1, pages_count), 2) if pages_count else 0,
                    "avg_total_tokens_per_page": round(total_tokens / max(1, pages_count), 2) if pages_count else 0
                }
            },
            "pages": [
                {
                    "pagina": p.pagina,
                    "marca_encabezado": p.marca_encabezado,
                    "metrics": p.metrics.model_dump() if p.metrics else None,
                    "productos": [prod.model_dump() for prod in p.productos]
                }
                for p in all_pages_results
            ],
            "products_flat": [
                {
                    "pagina": p.pagina,
                    "marca_pagina": p.marca_encabezado,
                    **prod.model_dump()
                }
                for p in all_pages_results
                for prod in p.productos
            ]
        }

        with open(output_json, "w", encoding="utf-8") as f_out:
            json.dump(output_payload, f_out, ensure_ascii=False, indent=2)

        logger.info("=" * 60)
        logger.info("PROCESAMIENTO DIRECTO FINALIZADO")
        logger.info(f"Fecha de Ejecución: {run_date_iso} | Proveedor: {proveedor}")
        logger.info(f"Tiempo Total: {total_elapsed:.2f}s | Páginas procesadas: {pages_count} | Productos: {total_products}")
        logger.info(f"Consumo Total de Tokens: {total_tokens:,} (Prompt: {total_prompt_tokens:,} | Completion: {total_completion_tokens:,})")
        logger.info(f"Resultado guardado en: {output_json}")
        logger.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="""
================================================================================
Procesamiento directo de Catálogos PDF con OpenAI GPT-5.6 Luna (Multimodal)
================================================================================
Extrae y estructura tablas complejas, normaliza especificaciones técnicas,
identifica marcas de fabricante y genera fichas de producto en un único paso.
        """,
        epilog="""
EJEMPLOS DE USO:
  1. Extraer 5 páginas a partir de la página 4:
     python fase-0-direct-luna.py --pdf "data/FN Catalogo.pdf" --start_page 4 --max_pages 5

  2. Extraer páginas 1 a 15 omitiendo portada e índice (páginas 1, 2 y 3):
     python fase-0-direct-luna.py --pdf "data/FN Catalogo.pdf" --start_page 1 --max_pages 15 --skip_pages "1-3"

  3. Omitir múltiples rangos y páginas arbitrarias:
     python fase-0-direct-luna.py --skip_pages "1-3, 7, 10-12" --out_json "resultado_luna.json"

  4. Modo texto puro sin visión (más económico y rápido si el PDF tiene texto limpio):
     python fase-0-direct-luna.py --no_vision
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--pdf", type=str, default="data/FN Catalogo.pdf", help="Ruta al archivo PDF del catálogo")
    parser.add_argument("--out_json", type=str, default="catalogo_directo_luna.json", help="Ruta del archivo JSON de salida consolidado")
    parser.add_argument("--model", type=str, default="gpt-5.6-luna", help="Modelo de OpenAI a utilizar (por defecto: gpt-5.6-luna)")
    parser.add_argument("--proveedor", type=str, default="Ferretera del Norte", help="Nombre o identificador del proveedor (por defecto: Ferretera del Norte)")
    parser.add_argument("--start_page", type=int, default=1, help="Número de página de inicio 1-indexed (por defecto: 1)")
    parser.add_argument("--max_pages", type=int, default=3, help="Cantidad máxima de páginas consecutivas a procesar (por defecto: 3)")
    parser.add_argument("--skip_pages", type=str, default=None, help="Páginas o rangos a omitir separados por comas (ej: '1,2,3' o '1-3,5,8-10')")
    parser.add_argument("--no_vision", action="store_true", help="Desactivar renderizado visual en base64 (utiliza solo texto extraído)")

    args = parser.parse_args()

    processor = DirectLunaCatalogProcessor(model=args.model)
    processor.process_catalog(
        pdf_path=args.pdf,
        output_json=args.out_json,
        start_page=args.start_page,
        max_pages=args.max_pages,
        use_vision=not args.no_vision,
        skip_pages=args.skip_pages,
        proveedor=args.proveedor
    )
