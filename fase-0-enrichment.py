import os
import sys
import json
import logging
import argparse
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("RAG_Fase_0_Enrichment")


class AttributeItem(BaseModel):
    nombre: str = Field(description="Nombre del atributo en snake_case (ej: apertura, color, medida, unidad_venta, paquete_cantidad)")
    valor: str = Field(description="Valor del atributo (ej: 9/13 mm, Negro, 10)")


class EnrichedProduct(BaseModel):
    codigo: str = Field(description="Código de artículo o identificador único")
    nombre_comercial: str = Field(description="Título claro y estandarizado para catálogo / e-commerce, incluyendo el sustantivo rector y detalles clave (ej: Abrazadera Americana a Cremallera de Acero 9/13 mm)")
    categoria_padre: str = Field(description="Categoría principal de ferretería (ej: Fijaciones y Sujeciones, Herramientas, Cintas, Seguridad Industrial)")
    categoria: str = Field(description="Categoría específica (ej: Abrazaderas, Cintas Adhesivas, Calzado de Seguridad)")
    subcategoria: str = Field(description="Subcategoría o tipo de material/diseño (ej: Abrazaderas de Acero, Cintas Aisladoras PVC)")
    marca: str = Field(description="Marca normalizada del producto")
    descripcion_completa: str = Field(description="Descripción comercial fluida y completa pensada para búsqueda semántica / RAG y ficha de producto")
    atributos: List[AttributeItem] = Field(default_factory=list, description="Lista de atributos técnicos normalizados")


class BatchEnrichmentResponse(BaseModel):
    products: List[EnrichedProduct]


class CatalogEnricher:
    def __init__(self, model: str = "gpt-4o-mini", provider: str = "openai"):
        self.provider = provider.lower()
        self.model = model

        if self.provider == "openai":
            self.api_key = os.getenv("OPENAI_API_KEY")
            if not self.api_key:
                logger.error("No se encontró OPENAI_API_KEY en las variables de entorno ni en el archivo .env")
                logger.info("Podés configurar tu clave en un archivo .env: OPENAI_API_KEY=tu_clave_de_openai")
                sys.exit(1)
            from openai import OpenAI
            self.client = OpenAI(api_key=self.api_key)
            logger.info(f"Enricher inicializado con OpenAI (modelo: {self.model})")

        elif self.provider == "gemini":
            self.api_key = os.getenv("GEMINI_API_KEY")
            if not self.api_key:
                logger.error("No se encontró GEMINI_API_KEY en las variables de entorno ni en el archivo .env")
                logger.info("Podés configurar tu clave en un archivo .env: GEMINI_API_KEY=tu_api_key")
                sys.exit(1)
            from google import genai
            self.client = genai.Client(api_key=self.api_key)
            logger.info(f"Enricher inicializado con Gemini (modelo: {self.model})")
        else:
            logger.error(f"Proveedor no soportado: {self.provider}")
            sys.exit(1)

    def enrich_batch(self, raw_products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Envía un lote de productos crudos al LLM para su normalización taxonómica y comercial.
        """
        prompt = f"""Eres un experto en catalogación de e-commerce y arquitectura de datos para ferretería y suministros industriales.

A continuación tienes un lote de {len(raw_products)} registros extraídos directamente de tablas de un catálogo PDF.
Tu tarea es normalizar y enriquecer cada registro transformándolo en una ficha de producto profesional lista para e-commerce y búsqueda semántica (RAG).

Reglas de transformación:
1. 'nombre_comercial': Debe contener el sustantivo rector al inicio (ej: "Abrazadera", "Cinta Aisladora", "Guante"), seguido del tipo/modelo, material y la medida/variante específica de la fila.
2. 'categoria_padre', 'categoria', 'subcategoria': Genera una jerarquía taxonómica limpia y coherente de ferretería.
3. 'marca': Limpia cualquier ruido del OCR (ej: si dice "( \\TACSA'", normalízala a "TACSA"; si dice "CARBIZ", a "CARBIZ").
4. 'descripcion_completa': Redacta una oración fluida, profesional y descriptiva que integre el contexto de sección, la descripción general y las especificaciones particulares de la fila.
5. 'atributos': Estandariza las claves en snake_case (ej: "apertura", "ancho_fleje", "unidad_venta", "paquete_cantidad", "color", "medida").
6. Mantén el orden exacto de los productos del lote para preservar la correlación.

Datos de entrada (JSON crudo):
{json.dumps(raw_products, ensure_ascii=False, indent=2)}
"""

        try:
            if self.provider == "openai":
                response = self.client.beta.chat.completions.parse(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": "Eres un asistente de estructuración de catálogos y taxonomía para e-commerce ferretero."},
                        {"role": "user", "content": prompt}
                    ],
                    response_format=BatchEnrichmentResponse,
                    temperature=0.1
                )
                parsed = response.choices[0].message.parsed
                return [p.model_dump() for p in parsed.products]

            elif self.provider == "gemini":
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config={
                        "response_mime_type": "application/json",
                        "response_schema": BatchEnrichmentResponse,
                        "temperature": 0.1,
                    },
                )
                parsed_response = json.loads(response.text)
                return parsed_response.get("products", [])

        except Exception as e:
            logger.error(f"Error procesando lote con {self.provider}: {e}")
            return []

    def process_file(self, input_json_path: str, output_json_path: str, batch_size: int = 15, max_items: Optional[int] = None):
        if not os.path.exists(input_json_path):
            logger.error(f"El archivo de entrada {input_json_path} no existe.")
            sys.exit(1)

        with open(input_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        raw_products = data.get("products", [])
        if max_items:
            raw_products = raw_products[:max_items]

        total_products = len(raw_products)
        logger.info(f"Iniciando enriquecimiento de {total_products} productos en lotes de {batch_size}...")

        all_enriched = []

        for i in range(0, total_products, batch_size):
            batch = raw_products[i:i + batch_size]
            batch_num = (i // batch_size) + 1
            total_batches = (total_products + batch_size - 1) // batch_size
            
            logger.info(f"Procesando lote {batch_num}/{total_batches} ({len(batch)} productos)...")
            enriched_batch = self.enrich_batch(batch)

            if len(enriched_batch) == len(batch):
                # Preservamos los metadatos de origen (página, archivo)
                for orig, enriched in zip(batch, enriched_batch):
                    enriched_dict = enriched if isinstance(enriched, dict) else enriched.model_dump()
                    # Convertimos la lista de atributos a diccionario limpio key-value
                    if isinstance(enriched_dict.get("atributos"), list):
                        enriched_dict["atributos"] = {
                            attr.get("nombre"): attr.get("valor")
                            for attr in enriched_dict["atributos"]
                            if isinstance(attr, dict) and attr.get("nombre")
                        }
                    enriched_dict["metadata_origen"] = {
                        "pagina": orig.get("pagina"),
                        "fuente": data.get("metadata", {}).get("source_file", "catalogo.pdf"),
                        "datos_crudos": orig.get("especificaciones", {})
                    }
                    all_enriched.append(enriched_dict)
            else:
                logger.warning(f"Lote {batch_num}: Se recibieron {len(enriched_batch)} productos de {len(batch)} esperados.")
                for item in enriched_batch:
                    enriched_dict = item if isinstance(item, dict) else item.model_dump()
                    if isinstance(enriched_dict.get("atributos"), list):
                        enriched_dict["atributos"] = {
                            attr.get("nombre"): attr.get("valor")
                            for attr in enriched_dict["atributos"]
                            if isinstance(attr, dict) and attr.get("nombre")
                        }
                    all_enriched.append(enriched_dict)

        output_payload = {
            "metadata": {
                "source_file": data.get("metadata", {}).get("source_file"),
                "total_products_enriched": len(all_enriched),
                "provider": self.provider,
                "model_enrichment": self.model
            },
            "products": all_enriched
        }

        with open(output_json_path, "w", encoding="utf-8") as f_out:
            json.dump(output_payload, f_out, ensure_ascii=False, indent=2)

        logger.info(f"Enriquecimiento completado. Salida guardada en: {output_json_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fase 0 - Enriquecimiento Taxonómico y Síntesis E-Commerce con LLMs")
    parser.add_argument("--input", type=str, default="catalogo_estructurado.json", help="Ruta al JSON crudo de ingestión")
    parser.add_argument("--output", type=str, default="catalogo_ecommerce.json", help="Ruta al JSON final enriquecido")
    parser.add_argument("--batch_size", type=int, default=15, help="Cantidad de productos por llamada")
    parser.add_argument("--max_items", type=int, default=None, help="Límite de productos a enriquecer (útil para pruebas)")
    parser.add_argument("--provider", type=str, default="openai", choices=["openai", "gemini"], help="Proveedor de LLM")
    parser.add_argument("--model", type=str, default="gpt-4o-mini", help="Modelo a utilizar (ej: gpt-4o-mini, gemini-3.6-flash)")
    args = parser.parse_args()

    enricher = CatalogEnricher(model=args.model, provider=args.provider)
    enricher.process_file(args.input, args.output, batch_size=args.batch_size, max_items=args.max_items)
