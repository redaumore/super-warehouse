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
    python fase-0-pdf-ingestion.py --pdf "data/FN Catalogo.pdf" --start_page 1 --max_pages 10 --skip_pages "1-3" --out_json "catalogo_luna.json"

    # 2. Procesar páginas específicas o con saltos:
    python fase-0-pdf-ingestion.py --pdf "data/FN Catalogo.pdf" --start_page 4 --max_pages 20 --skip_pages "7,9-11"

    # 3. Procesar únicamente capa de texto (sin visión multimodal):
    python fase-0-pdf-ingestion.py --pdf "data/FN Catalogo.pdf" --no_vision
"""

import os
import sys
import time
import json
import base64
import logging
import argparse
import hashlib
import unicodedata
import re
from collections import Counter
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


# Helper functions for code generation and brand normalization
BASE62_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def levenshtein_distance(s1: str, s2: str) -> int:
    """Calcula la distancia de edición de Levenshtein entre dos cadenas."""
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    previous_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]


def reconcile_brands_strict(results: List["PageExtractionResult"]) -> Dict[str, str]:
    """
    Analiza las marcas detectadas en todas las páginas y productos.
    Si detecta una variante infrecuente con distancia Levenshtein <= 1 respecto a una
    marca dominante con alta frecuencia, genera un mapa de corrección {variante_erronea: marca_canonica}.
    """
    brand_counts: Counter = Counter()

    for p in results:
        if p.marca_encabezado and p.marca_encabezado.strip():
            brand_counts[p.marca_encabezado.strip()] += 1
        for prod in p.productos:
            if prod.marca and prod.marca.strip():
                brand_counts[prod.marca.strip()] += 1

    if not brand_counts:
        return {}

    mapping: Dict[str, str] = {}
    sorted_brands = sorted(brand_counts.items(), key=lambda x: x[1], reverse=True)

    for rare_brand, rare_count in sorted_brands:
        rare_clean = rare_brand.strip()
        if len(rare_clean) < 4:
            continue

        for dom_brand, dom_count in sorted_brands:
            dom_clean = dom_brand.strip()
            if dom_clean == rare_clean or dom_count <= rare_count:
                continue

            if dom_count >= 2 * rare_count:
                dist = levenshtein_distance(rare_clean.lower(), dom_clean.lower())
                max_allowed_dist = 2 if len(dom_clean) > 8 else 1
                if dist <= max_allowed_dist:
                    mapping[rare_brand] = dom_brand
                    logger.info(
                        f"Reconciliación automática de marca: '{rare_brand}' ({rare_count} apariciones) "
                        f"-> '{dom_brand}' ({dom_count} apariciones) [Distancia Levenshtein: {dist}]"
                    )
                    break

    return mapping


def base62_encode_bytes(data: bytes, min_length: int = 10) -> str:
    """Codifica una secuencia de bytes en Base62 (0-9A-Za-z) con padding izquierdo a min_length."""
    num = int.from_bytes(data, byteorder="big")
    if num == 0:
        return BASE62_ALPHABET[0] * min_length
    chars = []
    base = len(BASE62_ALPHABET)
    while num > 0:
        num, rem = divmod(num, base)
        chars.append(BASE62_ALPHABET[rem])
    encoded = "".join(reversed(chars))
    if len(encoded) < min_length:
        encoded = BASE62_ALPHABET[0] * (min_length - len(encoded)) + encoded
    return encoded


def generate_internal_code(
    codigo_proveedor: str,
    codigo_orig: Optional[str],
    nombre_comercial: str,
    marca: Optional[str] = None
) -> str:
    """
    Genera el código propio del negocio con las siguientes reglas:
    1. Si existe código original: '{codigo_proveedor}-{codigo_orig}' (ej: 'PRF-1000001').
    2. Si no existe código original: '{codigo_proveedor}-{hash_base62}'
       donde hash_base62 es el hash SHA-256 del nombre de producto normalizado
       (en minúsculas y sin espacios extra) codificando los primeros 7 bytes (56 bits) en Base62.
    """
    prov_code = (codigo_proveedor or "PRF").strip().upper()[:3]

    cleaned_orig = codigo_orig.strip() if codigo_orig else ""
    if cleaned_orig and cleaned_orig.upper() not in ["N/D", "N/A", "NONE", "NULL", ""]:
        return f"{prov_code}-{cleaned_orig}"

    # Normalizar nombre: minúsculas y sin espacios extra
    norm_name = " ".join((nombre_comercial or "").strip().lower().split())
    if not norm_name:
        norm_name = "producto"

    # Hash SHA-256 y primeros 7 bytes (56 bits) en Base62
    digest_56bits = hashlib.sha256(norm_name.encode("utf-8")).digest()[:7]
    base62_str = base62_encode_bytes(digest_56bits, min_length=10)

    return f"{prov_code}-{base62_str}"


def generate_output_filename(codigo_proveedor: str, phase: str = "ingestion") -> str:
    """Genera el nombre de archivo automático: <id_proveedor>_<phase>_<YYMMDDHHmmSS>.json"""
    timestamp = datetime.now().strftime("%y%m%d%H%M%S")
    prov_code = (codigo_proveedor or "PRF").strip().upper()[:3]
    return f"{prov_code}_{phase}_{timestamp}.json"


# Pydantic Schemas for Structured Output
class AttributeItem(BaseModel):
    nombre: str = Field(description="Nombre del atributo en snake_case (ej: apertura, color, medida, unidad_venta, paquete_cantidad)")
    valor: str = Field(description="Valor del atributo (ej: 9/13 mm, Negro, 10)")


class ExtractedProduct(BaseModel):
    codigo_orig: Optional[str] = Field(default=None, description="Código de artículo o identificador original presente en el catálogo del proveedor (None o vacío si no existe)")
    codigo: Optional[str] = Field(default=None, description="Código interno propio del negocio generado según reglas de codificación")
    proveedor: Optional[str] = Field(default=None, description="Nombre descriptivo del proveedor asociado al producto")
    nombre_proveedor: Optional[str] = Field(default=None, description="Nombre descriptivo del proveedor asociado al producto")
    codigo_proveedor: Optional[str] = Field(default=None, description="Código de 3 caracteres del proveedor (ej: PRF)")
    precio: Optional[float] = Field(default=None, description="Precio unitario numérico del producto si figura en el catálogo")
    moneda: Optional[str] = Field(default=None, description="Moneda del precio ('ARS' para pesos argentinos, 'USD' para dólares estadounidenses o None si no hay precio)")
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
            "1. NO omitas variantes ni filas de las tablas.\n"
            "2. Extrae en 'codigo_orig' el código o número de artículo original del proveedor si figura en la tabla o página (o null si no existe).\n"
            "3. Extrae 'precio' (número float) y 'moneda' ('ARS' para pesos argentinos $, 'USD' para dólares U$S/USD) si figuran en el catálogo.\n"
            "4. Identifica la marca real de los productos en la página (ej: Fischer, Stanley, Tacsa, etc.).\n"
            "5. Genera un nombre comercial claro y una descripción técnica enriquecida para cada variante.\n"
            "6. Extrae atributos clave normalizados (medida, color, material, presentación, unidades, etc.).\n"
            "7. Conserva las especificaciones originales de la tabla en especificaciones_tabla."
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
        output_json: Optional[str] = None,
        start_page: int = 1,
        max_pages: Optional[int] = None,
        use_vision: bool = True,
        skip_pages: Optional[str] = None,
        proveedor: str = "Ferretera del Norte",
        nombre_proveedor: Optional[str] = None,
        codigo_proveedor: str = "FDN",
        marca: Optional[str] = None
    ):
        """Procesa el PDF página por página directamente con GPT-5.6 Luna."""
        if not os.path.exists(pdf_path):
            logger.error(f"El archivo PDF no existe: {pdf_path}")
            sys.exit(1)

        nom_prov = (nombre_proveedor or proveedor or "Ferretera del Norte").strip()

        if not output_json:
            output_json = generate_output_filename(codigo_proveedor, "ingestion")

        run_date_iso = datetime.now().astimezone().isoformat()
        doc = fitz.open(pdf_path)
        total_pdf_pages = len(doc)
        end_page = total_pdf_pages if max_pages is None else min(start_page + max_pages - 1, total_pdf_pages)
        pages_to_skip = self._parse_skip_pages(skip_pages)

        logger.info(
            f"Iniciando procesamiento directo de '{pdf_path}' (Páginas {start_page} a {end_page} de {total_pdf_pages}) | "
            f"Proveedor: {nom_prov} [{codigo_proveedor}]"
            + (f" | Marca forzada: '{marca}'" if marca else "")
        )
        if pages_to_skip:
            logger.info(f"Páginas a omitir: {sorted(list(pages_to_skip))}")
        
        all_pages_results: List[PageExtractionResult] = []
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
                all_pages_results.append(page_result)

                if page_result.metrics:
                    total_prompt_tokens += page_result.metrics.prompt_tokens
                    total_completion_tokens += page_result.metrics.completion_tokens
                    total_tokens += page_result.metrics.total_tokens

                logger.info(f"-> Página {pno}: Extraídos {len(page_result.productos)} productos. Marca detectada: '{page_result.marca_encabezado}'")
            except Exception as e:
                logger.error(f"Error procesando página {pno}: {e}")

        # Post-procesamiento y reconciliación de marcas
        if marca and marca.strip():
            forced_brand = marca.strip()
            logger.info(f"Aplicando marca forzada por parámetro: '{forced_brand}'")
            for page_res in all_pages_results:
                page_res.marca_encabezado = forced_brand
                for prod in page_res.productos:
                    prod.marca = forced_brand
        else:
            brand_corrections = reconcile_brands_strict(all_pages_results)
            if brand_corrections:
                for page_res in all_pages_results:
                    if page_res.marca_encabezado in brand_corrections:
                        page_res.marca_encabezado = brand_corrections[page_res.marca_encabezado]
                    for prod in page_res.productos:
                        if prod.marca in brand_corrections:
                            prod.marca = brand_corrections[prod.marca]

        # Asignar metadatos y resolver códigos de negocio internos
        total_products = 0
        for page_res in all_pages_results:
            total_products += len(page_res.productos)
            for prod in page_res.productos:
                prod.proveedor = nom_prov
                prod.nombre_proveedor = nom_prov
                prod.codigo_proveedor = codigo_proveedor
                if not prod.marca or not prod.marca.strip():
                    prod.marca = page_res.marca_encabezado
                prod.codigo = generate_internal_code(
                    codigo_proveedor=codigo_proveedor,
                    codigo_orig=prod.codigo_orig,
                    nombre_comercial=prod.nombre_comercial,
                    marca=prod.marca
                )

        total_elapsed = time.time() - start_benchmark
        pages_count = len(all_pages_results)

        # Estructura consolidada con métricas detalladas de consumo, proveedor y fecha de corrida
        output_payload = {
            "metadata": {
                "source_file": os.path.basename(pdf_path),
                "model": self.model,
                "proveedor": nom_prov,
                "nombre_proveedor": nom_prov,
                "codigo_proveedor": codigo_proveedor,
                "marca_forzada": marca if marca else None,
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
        logger.info(f"Fecha de Ejecución: {run_date_iso} | Proveedor: {nom_prov} [{codigo_proveedor}]")
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
  1. Extraer con nombre y código de proveedor explícitos:
     python fase-0-pdf-ingestion.py --pdf "data/FN Catalogo.pdf" --nombre_proveedor "PZ Force S.A." --codigo_proveedor "PZF" --marca "PZ Force" --start_page 1 --max_pages 5

  2. Extraer páginas 1 a 15 omitiendo portada e índice (páginas 1, 2 y 3):
     python fase-0-pdf-ingestion.py --pdf "data/FN Catalogo.pdf" --codigo_proveedor "FDN" --start_page 1 --max_pages 15 --skip_pages "1-3"

  3. Omitir múltiples rangos y páginas arbitrarias:
     python fase-0-pdf-ingestion.py --skip_pages "1-3, 7, 10-12" --out_json "resultado_luna.json"

  4. Modo texto puro sin visión (más económico y rápido si el PDF tiene texto limpio):
     python fase-0-pdf-ingestion.py --no_vision
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--pdf", type=str, default="data/FN Catalogo.pdf", help="Ruta al archivo PDF del catálogo")
    parser.add_argument("--out_json", type=str, default=None, help="Ruta del archivo JSON de salida (por defecto: automático <COD>_ingestion_<YYMMDDHHmmSS>.json)")
    parser.add_argument("--model", type=str, default="gpt-5.6-luna", help="Modelo de OpenAI a utilizar (por defecto: gpt-5.6-luna)")
    parser.add_argument("--nombre_proveedor", "--nombre-proveedor", "--proveedor", dest="nombre_proveedor", type=str, default="Ferretera del Norte", help="Nombre descriptivo del proveedor (por defecto: Ferretera del Norte)")
    parser.add_argument("--codigo_proveedor", "--cod_prov", "--codigo-proveedor", dest="codigo_proveedor", type=str, default="FDN", help="Código de 3 caracteres del proveedor (por defecto: FDN)")
    parser.add_argument("--marca", type=str, default=None, help="Nombre de marca forzado para todo el catálogo (ej: 'PZ Force')")
    parser.add_argument("--start_page", type=int, default=1, help="Número de página de inicio 1-indexed (por defecto: 1)")
    parser.add_argument("--max_pages", type=int, default=None, help="Cantidad máxima de páginas consecutivas a procesar (por defecto: procesa todo el catálogo hasta el final)")
    parser.add_argument("--skip_pages", type=str, default=None, help="Páginas o rangos a omitir separados por comas (ej: '1,2,3' o '1-3,5,8-10')")
    parser.add_argument("--no_vision", action="store_true", help="Desactivar renderizado visual en base64 (utiliza solo texto extraído)")

    args = parser.parse_args()

    cod_prov = (args.codigo_proveedor or "PRF").strip().upper()
    if len(cod_prov) > 3:
        logger.warning(f"El código de proveedor '{cod_prov}' supera los 3 caracteres. Se utilizará '{cod_prov[:3]}'.")
        cod_prov = cod_prov[:3]

    processor = DirectLunaCatalogProcessor(model=args.model)
    processor.process_catalog(
        pdf_path=args.pdf,
        output_json=args.out_json,
        start_page=args.start_page,
        max_pages=args.max_pages,
        use_vision=not args.no_vision,
        skip_pages=args.skip_pages,
        proveedor=args.nombre_proveedor,
        nombre_proveedor=args.nombre_proveedor,
        codigo_proveedor=cod_prov,
        marca=args.marca
    )
