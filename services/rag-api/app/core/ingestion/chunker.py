#!/usr/bin/env python3
"""
fase-1-chunking.py

Pipeline de transformación RAG (Fase 1):
Convierte el catálogo de productos estructurado (Fase 0) en nodos/chunks
optimizados para embeddings y pre-filtrado estructurado.
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import tiktoken
except ImportError:
    tiktoken = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("fase-1-chunking")


def generate_output_filename(codigo_proveedor: str, phase: str = "nodes") -> str:
    """Genera el nombre de archivo automático: <id_proveedor>_<phase>_<YYMMDDHHmmSS>.json"""
    timestamp = datetime.now().strftime("%y%m%d%H%M%S")
    prov_code = (codigo_proveedor or "PROV").strip().upper()[:3]
    return f"{prov_code}_{phase}_{timestamp}.json"


def extract_codigo_proveedor(data: Any, fallback: str = "PROV") -> str:
    """Extrae el código de 3 caracteres del proveedor desde el payload o metadatos."""
    if isinstance(data, dict):
        meta = data.get("metadata", {})
        if isinstance(meta, dict) and meta.get("codigo_proveedor"):
            return str(meta["codigo_proveedor"]).strip().upper()[:3]
        products = data.get("products_flat") or []
        for p in products:
            if isinstance(p, dict) and p.get("codigo_proveedor"):
                return str(p["codigo_proveedor"]).strip().upper()[:3]
    elif isinstance(data, list):
        for p in data:
            if isinstance(p, dict) and p.get("codigo_proveedor"):
                return str(p["codigo_proveedor"]).strip().upper()[:3]
    return fallback


def get_token_encoder(model_or_encoding: str = "cl100k_base"):
    """
    Inicializa el encoder de tiktoken con fallback seguro.
    """
    if tiktoken is None:
        logger.warning("tiktoken no está instalado. El conteo de tokens usará aproximación heurística.")
        return None
    try:
        return tiktoken.get_encoding(model_or_encoding)
    except Exception:
        try:
            return tiktoken.encoding_for_model(model_or_encoding)
        except Exception as err:
            logger.warning("No se pudo cargar el encoding '%s' (%s). Usando 'cl100k_base'.", model_or_encoding, err)
            return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str, encoder: Any) -> int:
    """
    Calcula la cantidad de tokens exactos si el encoder está disponible.
    """
    if not text:
        return 0
    if encoder is not None:
        return len(encoder.encode(text))
    # Heurística de fallback aproximada (~4 caracteres por token)
    return max(1, len(text) // 4)


def clean_str(val: Any) -> Optional[str]:
    """
    Limpia y valida que un valor no sea nulo ni puramente espacios.
    """
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def build_text_to_embed(product: Dict[str, Any]) -> str:
    """
    Construye el string estructurado en formato YAML (clave: valor)
    respetando estrictamente el orden de inyección de contexto semántico:
    1. Jerarquía Macro
    2. Información del Producto
    3. Especificaciones Técnicas (Atributos aplanados sin duplicados)
    """
    lines: List[str] = []
    seen_keys = set()

    def add_line(key: str, val: Any):
        cleaned = clean_str(val)
        if cleaned is not None and key not in seen_keys:
            # Reemplazar saltos de línea internos para preservar formato de línea simple YAML
            sanitized_val = " ".join(cleaned.splitlines())
            lines.append(f"{key}: {sanitized_val}")
            seen_keys.add(key)

    # 1. Jerarquía Macro y Origen
    add_line("archivo_origen", product.get("archivo_origen"))
    add_line("proveedor", product.get("nombre_proveedor") or product.get("proveedor"))
    add_line("codigo_proveedor", product.get("codigo_proveedor"))
    add_line("categoria_padre", product.get("categoria_padre"))
    add_line("categoria", product.get("categoria"))
    add_line("subcategoria", product.get("subcategoria"))
    add_line("marca", product.get("marca"))

    # 2. Información Comercial y Códigos del Producto
    add_line("codigo", product.get("codigo") or product.get("sku_compuesto"))
    add_line("codigo_orig", product.get("codigo_orig"))
    add_line("nombre", product.get("nombre_comercial") or product.get("nombre_producto") or product.get("nombre"))
    add_line("descripcion", product.get("descripcion_completa") or product.get("descripcion_tecnica") or product.get("descripcion"))
    if product.get("precio") is not None:
        add_line("precio", str(product.get("precio")))
    add_line("moneda", product.get("moneda"))
    add_line("unidad_venta", product.get("unidad_venta"))
    add_line("empaque", product.get("empaque"))

    # 3. Especificaciones Técnicas (Atributos)
    atributos = product.get("atributos")
    if isinstance(atributos, list):
        for attr in atributos:
            if isinstance(attr, dict):
                attr_name = clean_str(attr.get("nombre"))
                attr_val = clean_str(attr.get("valor"))
                if attr_name and attr_val:
                    # Normalizar clave (reemplazar espacios por guion bajo)
                    attr_key = attr_name.replace(" ", "_").lower()
                    add_line(attr_key, attr_val)

    return "\n".join(lines)


def build_metadata(product: Dict[str, Any]) -> Dict[str, Any]:
    """
    Construye el diccionario plano de metadatos para pre-filtrado en base de datos vectorial.
    Solo contiene tipos primitivos (int, str, float, bool).
    """
    metadata: Dict[str, Any] = {}

    # Campo pagina (entero si es convertible)
    raw_page = product.get("pagina")
    if raw_page is not None:
        try:
            metadata["pagina"] = int(raw_page)
        except (ValueError, TypeError):
            metadata["pagina"] = str(raw_page)

    # Campos base primitivos
    base_fields = [
        ("archivo_origen", product.get("archivo_origen")),
        ("codigo", product.get("codigo") or product.get("sku_compuesto")),
        ("codigo_orig", product.get("codigo_orig")),
        ("sku_compuesto", product.get("sku_compuesto")),
        ("document_id", product.get("document_id")),
        ("nombre_proveedor", product.get("nombre_proveedor") or product.get("proveedor")),
        ("codigo_proveedor", product.get("codigo_proveedor")),
        ("precio", product.get("precio")),
        ("moneda", product.get("moneda")),
        ("unidad_venta", product.get("unidad_venta")),
        ("empaque", product.get("empaque")),
        ("marca", product.get("marca")),
        ("categoria_padre", product.get("categoria_padre")),
        ("categoria", product.get("categoria")),
        ("subcategoria", product.get("subcategoria")),
    ]

    for key, val in base_fields:
        cleaned = clean_str(val)
        if cleaned is not None:
            metadata[key] = cleaned

    # Atributo estructural es_tabla
    es_tab = product.get("es_tabla")
    if es_tab is None:
        specs = product.get("especificaciones_tabla") or []
        es_tab = len(specs) > 0
    metadata["es_tabla"] = bool(es_tab)

    # Atributos técnicos clave planos (e.g., articulo, apertura, tipo, etc.)
    key_attributes = {"articulo", "apertura", "tipo", "fleje_ancho", "rosca", "medida", "diametro"}
    atributos = product.get("atributos")
    if isinstance(atributos, list):
        for attr in atributos:
            if isinstance(attr, dict):
                attr_name = clean_str(attr.get("nombre"))
                attr_val = clean_str(attr.get("valor"))
                if attr_name and attr_val:
                    norm_key = attr_name.replace(" ", "_").lower()
                    if norm_key in key_attributes and norm_key not in metadata:
                        metadata[norm_key] = attr_val

    return metadata


def process_product(product: Dict[str, Any], encoder: Any) -> Optional[Dict[str, Any]]:
    """
    Transforma un producto individual en un Nodo RAG estructurado.
    """
    codigo = clean_str(product.get("codigo"))
    if not codigo:
        raise ValueError("El producto no contiene un 'codigo' válido.")

    node_id = f"node_prod_{codigo}"
    text_to_embed = build_text_to_embed(product)
    text_length_char = len(text_to_embed)
    text_length_tokens = count_tokens(text_to_embed, encoder)
    metadata = build_metadata(product)

    return {
        "node_id": node_id,
        "text_to_embed": text_to_embed,
        "text_length_char": text_length_char,
        "text_length_tokens": text_length_tokens,
        "metadata": metadata,
    }


def run_pipeline(
    input_path: str,
    output_path: Optional[str] = None,
    encoding_name: str = "cl100k_base",
    codigo_proveedor: Optional[str] = None
) -> Tuple[List[Dict[str, Any]], str]:
    """
    Ejecuta el pipeline completo de ingesta, transformación y persistencia.
    """
    in_file = Path(input_path)
    if not in_file.exists():
        logger.error("Archivo de entrada no encontrado: %s", in_file)
        sys.exit(1)

    logger.info("Cargando catálogo desde %s ...", in_file)
    with open(in_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Identificar código de proveedor y resolver nombre de salida si no fue provisto
    if not codigo_proveedor:
        codigo_proveedor = extract_codigo_proveedor(data)

    if not output_path:
        output_path = generate_output_filename(codigo_proveedor, "nodes")

    # Identificar la lista de productos (preferencia por products_flat)
    products_flat = []
    if isinstance(data, dict):
        if "products_flat" in data and isinstance(data["products_flat"], list):
            products_flat = data["products_flat"]
        elif "pages" in data and isinstance(data["pages"], list):
            for page in data["pages"]:
                pagina_num = page.get("pagina")
                marca_encabezado = page.get("marca_encabezado")
                for prod in page.get("productos", []):
                    prod_copy = dict(prod)
                    if "pagina" not in prod_copy:
                        prod_copy["pagina"] = pagina_num
                    if "marca_pagina" not in prod_copy and marca_encabezado:
                        prod_copy["marca_pagina"] = marca_encabezado
                    products_flat.append(prod_copy)
    elif isinstance(data, list):
        products_flat = data

    if not products_flat:
        logger.warning("No se encontraron productos para procesar en el archivo de entrada.")
        nodes = []
    else:
        logger.info("Procesando %d productos con codificación '%s' (Proveedor: %s)...", len(products_flat), encoding_name, codigo_proveedor)
        encoder = get_token_encoder(encoding_name)
        nodes = []
        errors = 0

        for idx, prod in enumerate(products_flat):
            try:
                node = process_product(prod, encoder)
                if node:
                    nodes.append(node)
            except Exception as e:
                errors += 1
                logger.warning("Error procesando producto #%d (código: %s): %s", idx, prod.get("codigo"), e)

        logger.info("Transformación completa: %d nodos generados exitosamente (%d errores).", len(nodes), errors)

    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Guardando %d nodos en %s ...", len(nodes), out_file)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(nodes, f, indent=2, ensure_ascii=False)

    logger.info("Archivo generado correctamente en %s", out_file)
    return nodes, str(out_file)


def main():
    parser = argparse.ArgumentParser(
        description="Fase 1 - Chunking y Construcción de Nodos RAG para Catálogo de Productos."
    )
    parser.add_argument(
        "--input", "-i",
        default="catalogo_directo_luna.json",
        help="Ruta al archivo JSON de entrada (ej: catalogo_directo_luna.json o FDN_ingestion_YYMMDDHHmmSS.json)."
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Ruta al archivo JSON de salida (por defecto: automático <COD>_nodes_<YYMMDDHHmmSS>.json)."
    )
    parser.add_argument(
        "--codigo_proveedor", "--cod_prov",
        default=None,
        help="Código de 3 caracteres del proveedor (opcional, se infiere del archivo de entrada)."
    )
    parser.add_argument(
        "--encoding", "-e",
        default="cl100k_base",
        help="Nombre de la codificación de tiktoken a utilizar (default: cl100k_base)."
    )

    args = parser.parse_args()
    run_pipeline(
        input_path=args.input,
        output_path=args.output,
        encoding_name=args.encoding,
        codigo_proveedor=args.codigo_proveedor
    )


if __name__ == "__main__":
    main()
