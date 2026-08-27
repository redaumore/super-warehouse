import os
import sys
import json
import logging
import argparse
from typing import List, Dict, Any
import pymupdf as fitz
import pytesseract
from PIL import Image
import io

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("RAG_Fase_0_Ferreteria")


class HardwareCatalogProcessor:
    def __init__(self):
        try:
            from docling.document_converter import DocumentConverter
            self.converter = DocumentConverter()
            logger.info("Docling inicializado correctamente.")
        except ImportError:
            logger.error("Docling no instalado. Ejecuta: pip install docling pymupdf pytesseract pillow")
            sys.exit(1)

    DISTRIBUTOR_STOPWORDS = [
        "FERRETERA", "DEL NORTE", "DISTRIBUIDORA", "CATALOGO", "CATÁLOGO", "AYUDAMOS", "CRECER"
    ]

    def _ocr_box(self, page, clip_rect: fitz.Rect) -> str:
        try:
            pix = page.get_pixmap(clip=clip_rect, dpi=200)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            text = pytesseract.image_to_string(img, config="--psm 6").strip()
            lines = [l.strip().upper() for l in text.split("\n") if len(l.strip()) > 1]
            return " ".join(lines) if lines else ""
        except Exception:
            return ""

    def _is_distributor(self, text: str) -> bool:
        if not text:
            return False
        return any(stopword in text for stopword in self.DISTRIBUTOR_STOPWORDS)

    def _extract_header_brand_ocr(self, pdf_path: str, page_num: int) -> str:
        """
        Escanea ambas esquinas superiores (izquierda y derecha) para manejar el diseño
        alternado entre páginas pares e impares, descartando el logo de la distribuidora.
        """
        try:
            doc = fitz.open(pdf_path)
            page = doc[page_num]
            rect = page.rect

            # Recorte enfocado estrictamente en la franja del logo superior (0% a 11% del alto)
            left_box = fitz.Rect(0, 0, rect.width * 0.45, rect.height * 0.11)
            right_box = fitz.Rect(rect.width * 0.55, 0, rect.width, rect.height * 0.11)

            left_text = self._ocr_box(page, left_box)
            right_text = self._ocr_box(page, right_box)

            left_is_dist = self._is_distributor(left_text)
            right_is_dist = self._is_distributor(right_text)

            # Si la izquierda es distribuidora y la derecha es la marca
            if left_is_dist and not right_is_dist and len(right_text) > 1:
                return right_text
            # Si la derecha es distribuidora y la izquierda es la marca
            elif right_is_dist and not left_is_dist and len(left_text) > 1:
                return left_text
            # Si ninguna es distribuidora pero una tiene texto
            elif not left_is_dist and len(left_text) > 1:
                return left_text
            elif not right_is_dist and len(right_text) > 1:
                return right_text

            # Fallback
            return left_text or right_text or "GENÉRICO/DISTRIBUIDORA"

        except Exception as e:
            logger.warning(f"No se pudo extraer logo por OCR en página {page_num + 1}: {e}")
            return "NO_DETECTADA"

    def process_catalog(self, pdf_path: str, output_json: str, output_md: str, qtables: int = None, max_pages: int = None):
        target_pdf_path = pdf_path
        temp_pdf_path = None
        pdf_fitz = fitz.open(pdf_path)
        total_pages = len(pdf_fitz)

        if max_pages is not None and max_pages < total_pages:
            total_pages = max_pages
            logger.info(f"Extrayendo las primeras {max_pages} páginas para procesar...")
            temp_pdf_path = f"temp_subset_{max_pages}_pages.pdf"
            doc_subset = fitz.open()
            doc_subset.insert_pdf(pdf_fitz, from_page=0, to_page=max_pages - 1)
            doc_subset.save(temp_pdf_path)
            doc_subset.close()
            target_pdf_path = temp_pdf_path

        try:
            logger.info(f"Convirtiendo catálogo con Docling: {target_pdf_path}")
            conv_result = self.converter.convert(target_pdf_path)
            docling_doc = conv_result.document
        finally:
            if temp_pdf_path and os.path.exists(temp_pdf_path):
                try:
                    os.remove(temp_pdf_path)
                except Exception:
                    pass

        extracted_products = []
        full_markdown_corpus = []
        tables_processed = 0

        logger.info(f"Procesando {total_pages} páginas...")

        for page_idx in range(total_pages):
            if qtables is not None and tables_processed >= qtables:
                logger.info(f"Límite de qtables ({qtables}) alcanzado. Deteniendo procesamiento.")
                break

            # 1. Obtener la marca desde el logo de la página
            brand = self._extract_header_brand_ocr(pdf_path, page_idx)
            
            # 2. Contexto de página
            page_header = f"# PÁGINA {page_idx + 1} | MARCA: {brand}\n\n"
            full_markdown_corpus.append(page_header)

            # 3. Extraer elementos de Docling correspondientes a la página
            # Docling expone los elementos jerárquicamente en el exportador
            current_category = "GENERAL"
            current_product = ""
            current_desc = ""

            # Extraemos los nodos de la página para vincular textos y tablas
            for item, _level in docling_doc.iterate_items():
                # Filtrar items por página
                item_page = getattr(item, "page_no", None)
                if item_page is None and hasattr(item, "prov") and item.prov:
                    first_prov = item.prov[0]
                    item_page = getattr(first_prov, "page_no", None) if not isinstance(first_prov, dict) else first_prov.get("page_no")

                if item_page != (page_idx + 1):
                    continue

                item_type = item.__class__.__name__

                # Encabezados de Sección (Categorías o Productos)
                if "SectionHeader" in item_type or "Heading" in item_type:
                    header_text = item.text.strip()
                    if header_text.isupper() and len(header_text) > 4:
                        current_category = header_text
                    else:
                        current_product = header_text

                # Texto de descripción (ej: "Fleje de 13 mm de Ancho")
                elif "Text" in item_type or "Paragraph" in item_type:
                    txt = item.text.strip()
                    if not txt.startswith("<!--") and txt != current_product:
                        current_desc = txt

                # Tablas de productos
                elif "Table" in item_type:
                    if qtables is not None and tables_processed >= qtables:
                        break

                    table_df = item.export_to_dataframe(doc=docling_doc)
                    table_md = item.export_to_markdown(doc=docling_doc)

                    # Inyección de contexto al Markdown para RAG
                    enriched_table_md = (
                        f"### Categoría: {current_category}\n"
                        f"**Marca:** {brand}\n"
                        f"**Producto:** {current_product}\n"
                        f"**Descripción:** {current_desc}\n\n"
                        f"{table_md}\n\n---\n"
                    )
                    full_markdown_corpus.append(enriched_table_md)

                    # Estructuración fina a nivel de fila (JSON)
                    for _, row in table_df.iterrows():
                        row_dict = {str(k).strip(): str(v).strip() for k, v in row.to_dict().items() if v is not None}
                        
                        # Extraer código si existe en las columnas
                        codigo = row_dict.get("Código") or row_dict.get("Codigo") or row_dict.get("Cód.", "N/A")
                        
                        product_record = {
                            "pagina": page_idx + 1,
                            "marca": brand,
                            "categoria": current_category,
                            "producto": current_product,
                            "descripcion_general": current_desc,
                            "codigo": codigo,
                            "especificaciones": row_dict
                        }
                        extracted_products.append(product_record)

                    tables_processed += 1
                    if qtables is not None and tables_processed >= qtables:
                        logger.info(f"Se alcanzó la cantidad solicitada de tablas ({qtables}).")
                        break

        # 4. Guardar archivo Markdown consolidado enriquecido
        with open(output_md, "w", encoding="utf-8") as f_md:
            f_md.write("\n".join(full_markdown_corpus))
        logger.info(f"Markdown enriquecido guardado en: {output_md}")

        # 5. Guardar archivo JSON estructurado (Nivel de Producto Granular)
        output_payload = {
            "metadata": {
                "source_file": os.path.basename(pdf_path),
                "total_pages": total_pages,
                "total_products_indexed": len(extracted_products),
                "tables_processed": tables_processed
            },
            "products": extracted_products
        }
        
        with open(output_json, "w", encoding="utf-8") as f_json:
            json.dump(output_payload, f_json, ensure_ascii=False, indent=2)
        logger.info(f"JSON estructurado guardado en: {output_json}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Parser de Catálogos Ferreteros para RAG")
    parser.add_argument("--pdf", type=str, default="data/FN Catalogo.pdf", help="Ruta al PDF")
    parser.add_argument("--out_json", type=str, default="catalogo_estructurado.json", help="Salida JSON")
    parser.add_argument("--out_md", type=str, default="catalogo_enriquecido.md", help="Salida Markdown")
    parser.add_argument("--qtables", type=int, default=None, help="Cantidad máxima de tablas a procesar en orden de aparición")
    parser.add_argument("--max_pages", type=int, default=None, help="Cantidad máxima de páginas a convertir y procesar")
    args = parser.parse_args()

    processor = HardwareCatalogProcessor()
    processor.process_catalog(args.pdf, args.out_json, args.out_md, qtables=args.qtables, max_pages=args.max_pages)