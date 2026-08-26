import os
import sys
import json
import logging
import argparse
from typing import List, Dict, Any

# Configuración de logging para producción
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("RAG_Fase_0")

def check_dependencies():
    """
    Verifica las librerías instaladas y ofrece sugerencias de instalación.
    """
    missing_deps = []
    try:
        import docling
    except ImportError:
        missing_deps.append("docling")
    
    try:
        import pdfplumber
    except ImportError:
        missing_deps.append("pdfplumber")
        
    if missing_deps:
        logger.warning(
            f"Faltan dependencias recomendadas para ejecución local: {missing_deps}. "
            "Podés instalarlas usando: 'pip install docling pdfplumber'"
        )

class DocumentParser:
    def __init__(self, use_docling: bool = True):
        self.use_docling = use_docling
        if use_docling:
            try:
                from docling.document_converter import DocumentConverter
                self.converter = DocumentConverter()
                logger.info("Parser inicializado con Docling (Layout-based AI Parsing).")
            except ImportError:
                logger.warning("Docling no está instalado. Se utilizará pdfplumber como fallback.")
                self.use_docling = False
                
    def parse_with_docling(self, pdf_path: str) -> List[Dict[str, Any]]:
        """
        Parsea el PDF usando Docling, extrayendo texto estructurado en Markdown y tablas.
        """
        logger.info(f"Iniciando conversión con Docling para: {pdf_path}")
        from docling.document_converter import DocumentConverter
        
        # Docling procesa el documento y entiende el layout visual de forma nativa
        result = self.converter.convert(pdf_path)
        
        # Exportamos la representación estructurada completa a Markdown
        markdown_text = result.document.export_to_markdown()
        
        # En producción, segmentamos por páginas o secciones detectadas por el modelo
        pages_data = []
        
        # Extraemos información estructurada de tablas si el motor las detectó
        tables_extracted = []
        for table_ix, table in enumerate(result.document.tables):
            # Convertimos la tabla a un diccionario para metadatos o procesamiento directo
            table_data = {
                "table_index": table_ix,
                "label": getattr(table, "label", f"Tabla {table_ix}"),
                "markdown": table.export_to_markdown()
            }
            tables_extracted.append(table_data)
            
        # Retornamos un único documento consolidado enriquecido
        pages_data.append({
            "text": markdown_text,
            "metadata": {
                "source_file": os.path.basename(pdf_path),
                "parser_engine": "Docling",
                "total_tables_detected": len(tables_extracted),
                "tables": tables_extracted
            }
        })
        return pages_data

    def parse_with_fallback(self, pdf_path: str) -> List[Dict[str, Any]]:
        """
        Extractor de respaldo ultra-robusto usando pdfplumber.
        Especialmente útil si corrés el script en entornos sin aceleración GPU o livianos.
        """
        logger.info(f"Iniciando conversión con pdfplumber (Fallback) para: {pdf_path}")
        import pdfplumber
        
        extracted_pages = []
        with pdfplumber.open(pdf_path) as pdf:
            total_pages = len(pdf.pages)
            for i, page in enumerate(pdf.pages):
                page_num = i + 1
                logger.info(f"Procesando página {page_num}/{total_pages}...")
                
                # Extraer texto plano respetando el layout horizontal aproximado
                text = page.extract_text(layout=True) or ""
                
                # Intentar extraer tablas estructuradas en la página
                tables = page.extract_tables()
                page_tables = []
                for t_idx, table in enumerate(tables):
                    # Formatear la matriz de la tabla a Markdown para inyección limpia en el RAG
                    md_table = self._matrix_to_markdown(table)
                    page_tables.append({
                        "table_index": t_idx,
                        "markdown": md_table
                    })
                    # Reemplazamos la tabla en el texto con su versión Markdown para mantener coherencia
                    text += f"\n\n### [Tabla Extraída en Página {page_num}]\n{md_table}\n"

                extracted_pages.append({
                    "text": text,
                    "metadata": {
                        "source_file": os.path.basename(pdf_path),
                        "page_number": page_num,
                        "parser_engine": "pdfplumber",
                        "tables": page_tables
                    }
                })
        return extracted_pages

    def _matrix_to_markdown(self, matrix: List[List[str]]) -> str:
        """Helper para convertir listas de listas de pdfplumber a Markdown Pipe Tables."""
        if not matrix or not matrix[0]:
            return ""
        
        # Limpiar valores nulos
        clean_matrix = [[str(cell or "").strip().replace("\n", " ") for cell in row] for row in matrix]
        headers = clean_matrix[0]
        rows = clean_matrix[1:]
        
        # Crear encabezado Markdown
        md = "| " + " | ".join(headers) + " |\n"
        md += "| " + " | ".join(["---"] * len(headers)) + " |\n"
        
        # Crear filas
        for row in rows:
            # Asegurar que la fila coincida con el número de columnas del encabezado
            if len(row) < len(headers):
                row += [""] * (len(headers) - len(row))
            elif len(row) > len(headers):
                row = row[:len(headers)]
            md += "| " + " | ".join(row) + " |\n"
        return md

    def enrich_metadata(self, parsed_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Fase de enriquecimiento: Agrega metadatos sintéticos útiles para búsqueda e indexación.
        Aquí podés integrar llamadas a LLMs o pequeños extractores de entidades (NER) 
        como Gliner o Spacy para automatizar la catalogación.
        """
        logger.info("Iniciando fase de enriquecimiento de metadatos...")
        for chunk in parsed_data:
            # Placeholder para extracción automática de metadatos de negocio (ferretería/construcción)
            # En producción, se puede usar regex o LLMs para poblar estas categorías:
            chunk["metadata"]["project_category"] = "Catálogo de Ferretería y Construcción"
            chunk["metadata"]["language"] = "es"
            
            # Buscamos códigos típicos de producto (ej: códigos numéricos de 8 dígitos o marcas)
            # Esto facilita búsquedas exactas usando filtrado estructurado previo (pre-filtering)
            text_upper = chunk["text"].upper()
            detected_brands = []
            for brand in ["STANLEY", "BOSCH", "MAKITA", "DEWALT", "BLACK & DECKER", "SINIAT"]:
                if brand in text_upper:
                    detected_brands.append(brand)
            
            chunk["metadata"]["detected_brands"] = detected_brands
            
        return parsed_data

    def run(self, pdf_path: str, output_json_path: str):
        """
        Ejecuta todo el pipeline de Fase 0.
        """
        if not os.path.exists(pdf_path):
            logger.error(f"El archivo {pdf_path} no existe en la ruta provista.")
            sys.exit(1)
            
        logger.info(f"Arrancando proceso de Fase 0 para: {pdf_path}")
        
        # 1. Parsing
        if self.use_docling:
            parsed_data = self.parse_with_docling(pdf_path)
        else:
            parsed_data = self.parse_with_fallback(pdf_path)
            
        # 2. Enriquecimiento de metadatos
        enriched_data = self.enrich_metadata(parsed_data)
        
        # 3. Serialización del entregable
        with open(output_json_path, "w", encoding="utf-8") as f:
            json.dump(enriched_data, f, ensure_ascii=False, indent=2)
            
        logger.info(f"Fase 0 finalizada con éxito. Datos estructurados guardados en: {output_json_path}")


if __name__ == "__main__":
    check_dependencies()
    
    parser = argparse.ArgumentParser(description="Fase 0 RAG - Ingesta y Parsing de PDFs a JSON Estructurado")
    parser.add_argument("--pdf", type=str, default="FN_Catalogo.pdf", help="Ruta al archivo PDF")
    parser.add_argument("--out", type=str, default="catalogo_estructurado.json", help="Ruta del archivo JSON de salida")
    parser.add_argument("--no-docling", action="store_true", help="Forzar el uso de pdfplumber como motor de extracción")
    
    args = parser.parse_args()
    
    # Decidimos el motor según los argumentos del CLI
    use_docling = not args.no_docling
    
    parser_instance = DocumentParser(use_docling=use_docling)
    parser_instance.run(args.pdf, args.out)
