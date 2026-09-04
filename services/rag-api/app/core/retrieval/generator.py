#!/usr/bin/env python3
"""
fase_6_generator.py
===================
Fase 6: Síntesis del LLM, Prompt Engineering y Control de Alucinaciones
Pipeline de producción RAG para Catálogos Industriales y Dominios Técnicos.

Componentes del Módulo:
1. Ensamblador de Contexto Estructurado (XML Delimited Packaging):
   - Envoltorio de fragmentos con etiquetas XML explícitas (<fragmento id="N" ...>).
   - Inyección de metadatos de trazabilidad (página, marca, código de producto/SKU, categoría).
   - Delimitación estricta para inmunizar contra Prompt Injection e interpretaciones ambiguas.

2. Motor de Prompt Engineering con Directivas Anti-Alucinación:
   - Directiva de Ausencia (Grounded Refusal): Rechazo honesto y determinista ante falta de datos.
     Mensaje canónico: "No cuento con información factual en mis fuentes de conocimiento para responder a esta consulta."
   - Directiva de Alineación Alfanumérica Rígida: Cero extrapolación de códigos, SKUs o medidas.
   - Directiva de Citación Atómica Obligatoria: Formato estricto [Fragmento N] al final de cada afirmación.
   - Contextualización Consciente de la Consulta (Query-Aware): Mitigación del sesgo "Lost in the Middle".

3. Motor de Inferencia Determinista (LLM Engine):
   - Temperatura = 0.0 (Greedy Decoding) para máxima reproducibilidad y fidelidad factual.
   - Compatible con OpenAI Chat API (gpt-4o, gpt-4o-mini, modelos razonadores), Google GenAI y fallback determinista offline.
   - Soporte automático para parámetros modernos (max_completion_tokens / max_tokens).

4. Verificador de Fidelidad Sintáctica y Salida Estructurada (Post-Generation Audit):
   - Extracción y validación cruzada de citaciones [Fragmento N].
   - Detección de "citas fantasma" (citas que no existen en los fragmentos provistos).
   - Soporte para salida en Lenguaje Natural auditado o JSON estructurado (Structured Outputs).
   - Preparación de la Tripleta RAG fundamental (Q, C, A) para evaluación continua en Fase 7.

EJEMPLOS DE USO / CLI EXECUTION EXAMPLES:

1. Ejecución de la suite completa de validación y benchmarking:
   $ python fase_6_generator.py --benchmark

2. Consulta directa end-to-end sobre PostgreSQL (Fase 4 -> Fase 5 -> Fase 6):
   $ python fase_6_generator.py "Llave de impacto neumática de 1/2 pulgada" --table catalogo_amx_rag --top-n 3

3. Consulta con salida en JSON Estructurado (Structured Outputs para APIs / ERPs):
   $ python fase_6_generator.py "Llaves de impacto neumáticas de 3/4" --structured

4. Búsqueda con modelo OpenAI específico:
   $ python fase_6_generator.py "AMX-AT-5044" --model gpt-4o-mini

5. Consulta en modo sintético / offline (sin conexión a base de datos externa):
   $ python fase_6_generator.py "Llave de impacto CARBIZ-099" --mock
"""

import os
import sys
import re
import time
import json
import logging
import argparse
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional, Union, Sequence
from dotenv import load_dotenv

# Cargar variables de entorno desde .env
load_dotenv()

# Configuración de Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("RAG_Fase_6_Generator")


# ============================================================================
# 1. ESTRUCTURAS DE DATOS DE LA FASE 6
# ============================================================================

@dataclass
class InputContextChunk:
    """Representa un fragmento finalista recibido desde la Fase 5 con su ficha técnica y comercial completa."""
    fragment_id: int  # Identificador secuencial en el prompt: 1, 2, 3...
    node_id: str
    codigo_producto: Optional[str] = None
    codigo_orig: Optional[str] = None
    marca: Optional[str] = None
    nombre_proveedor: Optional[str] = None
    codigo_proveedor: Optional[str] = None
    categoria_padre: Optional[str] = None
    categoria: Optional[str] = None
    subcategoria: Optional[str] = None
    precio: Optional[float] = None
    moneda: Optional[str] = None
    unidad_venta: Optional[str] = None
    empaque: Optional[str] = None
    archivo_origen: Optional[str] = None
    pagina: Optional[int] = None
    content: str = ""
    relevance_score: float = 0.0
    prompt_position: int = 1
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CitationVerificationResult:
    """Auditoría sintáctica de las citas emitidas por el LLM."""
    citations_found: List[str]  # ej. ["[Fragmento 1]", "[Fragmento 2]"]
    referenced_fragment_ids: List[int]  # ej. [1, 2]
    valid_fragment_ids: List[int]  # Fragmentos realmente presentes en el prompt
    invalid_fragment_ids: List[int]  # Fragmentos citados que no existen ("citas fantasma")
    is_fully_grounded: bool
    citation_ratio: float  # Porcentaje de oraciones o aseveraciones que contienen citas
    total_sentences: int = 0
    cited_sentences: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StructuredProductItem:
    """Elemento individual de producto para salida estructurada completa (ERP y confirmación cliente)."""
    codigo: str
    codigo_orig: Optional[str] = None
    codigo_proveedor: Optional[str] = None
    nombre_proveedor: Optional[str] = None
    marca: Optional[str] = None
    nombre: str = ""
    categoria_padre: Optional[str] = None
    categoria: Optional[str] = None
    subcategoria: Optional[str] = None
    precio: Optional[float] = None
    moneda: Optional[str] = None
    unidad_venta: Optional[str] = None
    empaque: Optional[str] = None
    especificaciones: Optional[str] = None
    archivo_origen: Optional[str] = None
    pagina: Optional[int] = None
    fragmento_id: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StructuredRAGOutput:
    """Esquema JSON tipado para APIs / ERPs."""
    respuesta_narrativa: str
    consulta_respondida: bool
    productos: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class GenerationResult:
    """Entregable final consolidado de la Fase 6."""
    query: str
    response_text: str
    is_refusal: bool  # True si el sistema activó la directiva de ausencia canónica
    status: str  # "SUCCESS" | "REFUSAL_GROUNDED" | "EMPTY_CONTEXT" | "CITATION_MISMATCH"
    verification: CitationVerificationResult
    structured_json: Optional[Dict[str, Any]]
    total_context_tokens_approx: int
    latency_ms: float
    model_name: str
    temperature: float
    rag_triplet: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["verification"] = self.verification.to_dict()
        return d


@dataclass
class BenchmarkScenario:
    """Escenario de prueba para benchmarks y validación."""
    id: str
    titulo: str
    query: str
    contexto: List[Dict[str, Any]]
    structured: bool = False


# ============================================================================
# 2. CONSTRUCTOR DEL CONTEXTO Y PROMPTS DEL SISTEMA (XML DELIMITED)
# ============================================================================

class PromptBuilder:
    """
    Construye los payloads de entrada para el LLM garantizando encapsulación XML
    y directivas de comportamiento rígidas según la Nota Técnica de Fase 6.
    """
    REFUSAL_CANONICAL_MESSAGE = (
        "No cuento con información factual en mis fuentes de conocimiento para responder a esta consulta."
    )

    @staticmethod
    def build_system_prompt() -> str:
        """
        Retorna el System Prompt de producción con directivas anti-alucinación reforzadas.
        """
        return (
            "Eres un Asistente Técnico y Especialista en Catálogos Industriales y Ferretería de alta precisión.\n"
            "Tu única fuente de verdad y conocimiento es la información factual contenida exclusivamente dentro de las "
            "etiquetas <contexto_conocimiento>.\n\n"
            "### DIRECTIVAS OBLIGATORIAS DE SEGURIDAD Y PRECISIÓN FACTUAL:\n\n"
            "1. DIRECTIVA DE AUSENCIA (RECHAZO HONESTO Y DETERMINISTA):\n"
            f"   - Si la respuesta precisa, el producto consultado, o el código/SKU específico NO se encuentra explícitamente detallado en el contexto provisto, "
            f"debes responder exactamente con la siguiente frase canónica:\n"
            f"     \"{PromptBuilder.REFUSAL_CANONICAL_MESSAGE}\"\n"
            "   - Tienes terminantemente prohibido utilizar tu memoria pre-entrenada para suponer, inventar o extrapolar información ausente.\n\n"
            "2. DIRECTIVA DE INTEGRIDAD ALFANUMÉRICA ESTRICTA (CERO EXTRAPOLACIÓN):\n"
            "   - Prohibido inferir, inventar, aproximar o extrapolar números de pieza, códigos de catálogo (ej. 'AMX-AT-5044', 'CARBIZ-099', '205020'), "
            "dimensiones en milímetros/pulgadas, materiales, torques o valores de presión si no figuran de forma textual exacta en los fragmentos.\n"
            "   - Jamás inventes códigos numéricos ficticios ni deduzcas variantes no listadas en las tablas técnicas.\n"
            "   - Si el usuario consulta por una variante, medida o precio que no figura explícitamente en el catálogo, aplica la Directiva de Ausencia o declara textualmente que dicha medida no se encuentra listada.\n\n"
            "3. DIRECTIVA DE CITACIÓN ATÓMICA OBLIGATORIA:\n"
            "   - Toda afirmación, dato técnico, precio o especificación que declares DEBE finalizar obligatoriamente con la citación "
            "del fragmento de origen usando el formato exacto `[Fragmento N]`, donde N es el identificador numérico del fragmento.\n"
            "   - Si respondes con una lista de viñetas, incluye la cita `[Fragmento N]` al final de cada viñeta o especificación técnica.\n"
            "   - Si sintetizas datos de dos fragmentos en una misma oración, cita ambos (ej. `[Fragmento 1, Fragmento 3]`).\n\n"
            "4. DIRECTIVA DE FORMATO Y ESTILO:\n"
            "   - Responde de forma concisa, profesional, estructurada y directa (empleando viñetas con los datos técnicos clave).\n"
            "   - Jamás menciones términos internos como 'contexto XML', 'prompt', 'embeddings' o 'pipeline'."
        )

    @staticmethod
    def build_user_prompt(
        query: str,
        chunks: Sequence[InputContextChunk],
        query_aware: bool = True
    ) -> str:
        """
        Construye el User Prompt empaquetando los fragmentos en XML y aplicando
        el principio de contextualización consciente de la consulta (Query-Aware, Liu et al.).
        """
        if not chunks:
            return (
                f"CONSULTA A RESPONDER:\n\"{query}\"\n\n"
                f"<contexto_conocimiento estado=\"VACIO\">\n"
                f"<!-- No se recuperaron fragmentos relevantes para esta búsqueda -->\n"
                f"</contexto_conocimiento>\n\n"
                f"INSTRUCCIONES FINALES:\n"
                f"Aplica la Directiva de Ausencia canónica: \"{PromptBuilder.REFUSAL_CANONICAL_MESSAGE}\""
            )

        lines: List[str] = []

        # Zona de Primacía: Consulta del usuario al inicio
        if query_aware:
            lines.append("=== CONSULTA A RESPONDER ===")
            lines.append(f"Pregunta del Usuario: \"{query}\"\n")

        lines.append("=== CONTEXTO DE CONOCIMIENTO AUTORIZADO ===")
        lines.append("<contexto_conocimiento>")
        lines.append("  <!-- Fragmentos técnicos de catálogo verificados y recuperados -->")

        for c in chunks:
            attrs: List[str] = [f'id="{c.fragment_id}"']
            if c.archivo_origen:
                attrs.append(f'archivo="{c.archivo_origen}"')
            if c.pagina is not None:
                attrs.append(f'pagina="{c.pagina}"')
            if c.codigo_producto:
                attrs.append(f'codigo="{c.codigo_producto}"')
            if c.codigo_orig:
                attrs.append(f'codigo_orig="{c.codigo_orig}"')
            if c.codigo_proveedor:
                attrs.append(f'cod_prov="{c.codigo_proveedor}"')
            if c.nombre_proveedor:
                attrs.append(f'proveedor="{c.nombre_proveedor}"')
            if c.marca:
                attrs.append(f'marca="{c.marca}"')
            if c.categoria:
                attrs.append(f'categoria="{c.categoria}"')
            if c.precio is not None:
                attrs.append(f'precio="{c.precio}"')
            if c.moneda:
                attrs.append(f'moneda="{c.moneda}"')
            if c.unidad_venta:
                attrs.append(f'unidad_venta="{c.unidad_venta}"')
            if c.empaque:
                attrs.append(f'empaque="{c.empaque}"')
            
            header = f"  <fragmento {' '.join(attrs)}>"
            lines.append(header)

            # Contenido sangrado
            for row in c.content.strip().splitlines():
                lines.append(f"    {row}")

            lines.append("  </fragmento>")

        lines.append("</contexto_conocimiento>\n")

        # Zona de Recencia: Instrucciones finales y anclaje inmediato
        lines.append("=== INSTRUCCIONES FINALES DE RESPUESTA ===")
        lines.append(f"Responde con precisión técnica y comercial a la consulta: \"{query}\".")
        lines.append(
            "Recuerda:\n"
            "1. Cita obligatoriamente cada afirmación, precio o dato técnico con `[Fragmento N]`.\n"
            f"2. Si la información solicitada no está presente en los fragmentos, responde exactamente:\n"
            f"   \"{PromptBuilder.REFUSAL_CANONICAL_MESSAGE}\""
        )

        return "\n".join(lines)

    @staticmethod
    def build_structured_json_system_prompt() -> str:
        """
        System prompt para cuando se solicita salida estructurada en JSON.
        """
        return (
            "Eres un Asistente Técnico y Extractor Estructurado de Catálogos Industriales.\n"
            "Tu única fuente de verdad es la información dentro de <contexto_conocimiento>.\n"
            "Debes responder EXCLUSIVAMENTE con un objeto JSON válido que cumpla la siguiente estructura:\n"
            "{\n"
            "  \"respuesta_narrativa\": \"Explicación técnica en texto natural con citas [Fragmento N] incluyendo precio, moneda, empaque y detalles\",\n"
            "  \"consulta_respondida\": true | false,\n"
            "  \"productos\": [\n"
            "    {\n"
            "      \"codigo\": \"Código o SKU exacto de negocio (ej. 'FDN-XMAX-AT-5044')\",\n"
            "      \"codigo_orig\": \"Código original de catálogo/fábrica (ej. 'AT-5044') o null\",\n"
            "      \"codigo_proveedor\": \"Código de 3 letras del proveedor (ej. 'FDN') o null\",\n"
            "      \"nombre_proveedor\": \"Nombre del proveedor o null\",\n"
            "      \"marca\": \"Marca del fabricante\",\n"
            "      \"nombre\": \"Nombre / Título comercial del producto\",\n"
            "      \"categoria_padre\": \"Categoría principal o null\",\n"
            "      \"categoria\": \"Categoría específica\",\n"
            "      \"subcategoria\": \"Subcategoría o tipo o null\",\n"
            "      \"precio\": 185000.0 (número float exacto o null si no figura),\n"
            "      \"moneda\": \"ARS\" | \"USD\" | null,\n"
            "      \"unidad_venta\": \"c/u\" | \"metro\" | \"paquete\" | null,\n"
            "      \"empaque\": \"Caja x 100\" | \"Blister\" | null,\n"
            "      \"especificaciones\": \"Medidas, torque, rosca, especificaciones técnicas clave\",\n"
            "      \"archivo_origen\": \"Nombre del archivo PDF fuente de catálogo\",\n"
            "      \"pagina\": número entero de página 1-indexed,\n"
            "      \"fragmento_id\": 1\n"
            "    }\n"
            "  ]\n"
            "}\n\n"
            f"Si la información no existe, \"consulta_respondida\" debe ser false, \"productos\" una lista vacía [], "
            f"y \"respuesta_narrativa\" exactamente \"{PromptBuilder.REFUSAL_CANONICAL_MESSAGE}\"."
        )


# ============================================================================
# 3. VERIFICADOR SINTÁCTICO DE CITAS Y GROUNDEDNESS
# ============================================================================

class CitationVerifier:
    """
    Audita sintácticamente que las respuestas del LLM cumplan con las reglas de citación.
    Detecta si el modelo inventó citas a fragmentos que no se encontraban en el prompt.
    """
    CITATION_PATTERN = re.compile(r'\[Fragmento\s*(\d+)\]', re.IGNORECASE)

    @classmethod
    def verify(
        cls,
        response_text: str,
        valid_chunk_ids: Sequence[int]
    ) -> CitationVerificationResult:
        """
        Analiza el texto de respuesta y contrasta las citas con los fragmentos suministrados.
        """
        # Extraer todas las menciones
        raw_matches = cls.CITATION_PATTERN.findall(response_text)
        found_ids = [int(m) for m in raw_matches]
        citations_found = [f"[Fragmento {fid}]" for fid in found_ids]

        valid_set = set(valid_chunk_ids)
        invalid_ids = [fid for fid in set(found_ids) if fid not in valid_set]
        valid_referenced = [fid for fid in set(found_ids) if fid in valid_set]

        # Comprobar si el texto es un rechazo canónico
        is_refusal = PromptBuilder.REFUSAL_CANONICAL_MESSAGE.lower() in response_text.lower()

        # Ratio de cobertura de citas (calculado sobre párrafos / bloques / oraciones)
        raw_blocks = [b.strip() for b in response_text.splitlines() if b.strip()]
        substantive_blocks = [
            b for b in raw_blocks
            if len(b) > 10 and not b.startswith("==") and not b.startswith("---") and not b.startswith("```")
        ]
        cited_blocks = [b for b in substantive_blocks if cls.CITATION_PATTERN.search(b)]

        total_s = len(substantive_blocks)
        cited_s = len(cited_blocks)
        ratio = (cited_s / total_s) if total_s > 0 else (1.0 if citations_found else 0.0)

        # Es grounded si no hay citas falsas y, en caso de no ser rechazo, contiene al menos una cita válida
        if is_refusal:
            is_grounded = True
        elif not valid_chunk_ids:
            is_grounded = False
        else:
            is_grounded = (len(invalid_ids) == 0) and (len(valid_referenced) > 0)

        return CitationVerificationResult(
            citations_found=citations_found,
            referenced_fragment_ids=sorted(list(set(found_ids))),
            valid_fragment_ids=sorted(valid_referenced),
            invalid_fragment_ids=sorted(invalid_ids),
            is_fully_grounded=is_grounded,
            citation_ratio=round(ratio, 2),
            total_sentences=total_s,
            cited_sentences=cited_s
        )


# ============================================================================
# 4. MOTOR DE INFERENCIA DETERMINISTA (OPENAI API + OFFLINE DETERMINISTA)
# ============================================================================

class LLMInferenceEngine:
    """
    Abstrae la invocación al modelo de lenguaje con hiperparámetros estrictos:
    - Temperatura = 0.0 (Greedy Decoding)
    - Soporte multi-proveedor: OpenAI (gpt-4o, gpt-4o-mini, o1/o3/gpt-5.x), Google GenAI, y
      motor determinista offline de precisión para entornos sin conexión o sin API keys.
    """
    def __init__(
        self,
        model_name: str = "gpt-4o",
        temperature: float = 0.0,
        max_tokens: int = 800,
        provider: str = "auto",
        use_mock_fallback: bool = True
    ):
        self.model_name = model_name
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.provider = provider
        self.use_mock_fallback = use_mock_fallback

        self.openai_client: Any = None
        self.gemini_client: Any = None
        self._init_clients()

    def _init_clients(self) -> None:
        """Inicializa los clientes disponibles según las API keys."""
        # 1. OpenAI
        openai_key = os.getenv("OPENAI_API_KEY")
        if openai_key and (self.provider in ["auto", "openai"]):
            try:
                from openai import OpenAI
                self.openai_client = OpenAI(api_key=openai_key)
                logger.info(f"[✓] Cliente OpenAI activo para modelo '{self.model_name}' (Temp={self.temperature}).")
            except Exception as e:
                logger.warning(f"No se pudo inicializar OpenAI: {e}")

        # 2. Gemini / Google GenAI
        gemini_key = os.getenv("GEMINI_API_KEY")
        if gemini_key and (self.provider in ["auto", "gemini"]):
            try:
                from google import genai
                self.gemini_client = genai.Client(api_key=gemini_key)
                logger.info("[✓] Cliente Google GenAI disponible.")
            except Exception as e:
                logger.debug(f"Google GenAI no inicializado: {e}")

        if not self.openai_client and not self.gemini_client:
            if not self.use_mock_fallback:
                raise ValueError("No se encontraron clientes de API activos y mock_fallback está desactivado.")
            logger.info("Operando en Modo Determinista Offline de Precisión.")

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        chunks: Sequence[InputContextChunk],
        json_mode: bool = False
    ) -> str:
        """
        Ejecuta la inferencia sobre el LLM con temperatura 0.0 y manejo robusto de parámetros.
        """
        # Prioridad 1: OpenAI
        if self.openai_client and (self.provider in ["auto", "openai"]):
            try:
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
                kwargs: Dict[str, Any] = {
                    "model": self.model_name,
                    "messages": messages,
                }
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}

                # Intentar con max_completion_tokens primero si es modelo reciente / o-series
                is_reasoning_or_newer = any(
                    x in self.model_name.lower()
                    for x in ["o1", "o3", "gpt-5", "2024-08", "2024-11", "preview"]
                )

                if is_reasoning_or_newer:
                    kwargs["max_completion_tokens"] = self.max_tokens
                else:
                    kwargs["max_tokens"] = self.max_tokens
                    kwargs["temperature"] = self.temperature

                try:
                    response = self.openai_client.chat.completions.create(**kwargs)
                    content = response.choices[0].message.content
                    return (content or "").strip()
                except Exception as api_err:
                    err_msg = str(api_err).lower()
                    # Si falla por max_tokens -> reintentar con max_completion_tokens
                    if "max_tokens" in err_msg or "max_completion_tokens" in err_msg or "unsupported_parameter" in err_msg:
                        logger.info("Reintentando con compatibilidad de tokens para OpenAI...")
                        kwargs.pop("max_tokens", None)
                        kwargs["max_completion_tokens"] = self.max_tokens
                        if "temperature" in err_msg:
                            kwargs.pop("temperature", None)
                        response = self.openai_client.chat.completions.create(**kwargs)
                        content = response.choices[0].message.content
                        return (content or "").strip()
                    raise api_err

            except Exception as e:
                logger.error(f"Error en OpenAI Chat Completion: {e}")
                if not self.use_mock_fallback:
                    raise e
                logger.info("Derivando inferencia al generador de respaldo determinista offline...")

        # Prioridad 2: Gemini
        if self.gemini_client and (self.provider in ["auto", "gemini"]):
            try:
                prompt_full = f"{system_prompt}\n\n{user_prompt}"
                res = self.gemini_client.models.generate_content(
                    model=self.model_name if "gemini" in self.model_name else "gemini-3.5-flash",
                    contents=prompt_full
                )
                if res and res.text:
                    return str(res.text).strip()
            except Exception as e:
                logger.warning(f"Error en Google GenAI: {e}")
                if not self.use_mock_fallback:
                    raise e

        # Prioridad 3: Motor Offline Determinista
        return self._generate_offline_synthesis(user_prompt, chunks, json_mode=json_mode)

    def _generate_offline_synthesis(
        self,
        user_prompt: str,
        chunks: Sequence[InputContextChunk],
        json_mode: bool = False
    ) -> str:
        """
        Motor de inferencia determinista offline de alta fidelidad:
        - Si no hay fragmentos o la consulta es ajena: emite la Directiva de Ausencia Canónica.
        - Si hay datos: sintetiza cada atributo asociando obligatoriamente [Fragmento N].
        """
        if not chunks:
            if json_mode:
                return json.dumps({
                    "respuesta_narrativa": PromptBuilder.REFUSAL_CANONICAL_MESSAGE,
                    "consulta_respondida": False,
                    "productos": []
                }, ensure_ascii=False, indent=2)
            return PromptBuilder.REFUSAL_CANONICAL_MESSAGE

        # Extraer query del prompt
        q_match = re.search(r'Pregunta del Usuario:\s*"(.*?)"', user_prompt, re.IGNORECASE)
        query = q_match.group(1).lower() if q_match else ""

        # Verificar coincidencia básica entre palabras de la query y fragmentos
        query_words = [w for w in re.findall(r'\b\w+\b', query) if len(w) > 2]
        matching_chunks: List[InputContextChunk] = []
        for c in chunks:
            c_text = (c.content + " " + (c.codigo_producto or "") + " " + (c.marca or "")).lower()
            if any(word in c_text for word in query_words):
                matching_chunks.append(c)

        if not matching_chunks:
            if json_mode:
                return json.dumps({
                    "respuesta_narrativa": PromptBuilder.REFUSAL_CANONICAL_MESSAGE,
                    "consulta_respondida": False,
                    "productos": []
                }, ensure_ascii=False, indent=2)
            return PromptBuilder.REFUSAL_CANONICAL_MESSAGE

        # Síntesis estructurada
        productos_json: List[Dict[str, Any]] = []
        response_lines: List[str] = ["En base a la información técnica y comercial verificada en el catálogo:\n"]

        for c in matching_chunks:
            # Parsear pares clave: valor
            attr_dict: Dict[str, str] = {}
            for row in c.content.strip().splitlines():
                if ":" in row:
                    parts = row.split(":", 1)
                    if len(parts) == 2:
                        attr_dict[parts[0].strip().lower()] = parts[1].strip()

            nombre = (
                attr_dict.get("nombre") or
                attr_dict.get("nombre_producto") or
                attr_dict.get("titulo") or
                attr_dict.get("producto") or
                attr_dict.get("descripcion", "Herramienta de Catálogo")
            )
            codigo = c.codigo_producto or attr_dict.get("codigo") or attr_dict.get("codigo_producto") or "N/D"
            codigo_orig = c.codigo_orig or attr_dict.get("codigo_orig")
            marca = c.marca or attr_dict.get("marca") or "Sin Marca"

            precio_val = c.precio
            if precio_val is None and "precio" in attr_dict:
                try:
                    precio_val = float(attr_dict["precio"])
                except Exception:
                    precio_val = None

            moneda_val = c.moneda or attr_dict.get("moneda")
            unidad_venta_val = c.unidad_venta or attr_dict.get("unidad_venta")
            empaque_val = c.empaque or attr_dict.get("empaque")
            archivo_val = c.archivo_origen or attr_dict.get("archivo_origen")
            cod_prov_val = c.codigo_proveedor or attr_dict.get("codigo_proveedor")
            nom_prov_val = c.nombre_proveedor or attr_dict.get("proveedor") or attr_dict.get("nombre_proveedor")
            cat_padre_val = c.categoria_padre or attr_dict.get("categoria_padre")
            subcat_val = c.subcategoria or attr_dict.get("subcategoria")

            detalles: List[str] = []
            for k in ["encastre", "torque", "torque_maximo", "velocidad", "medida", "apertura", "rosca", "diametro", "presion_aire", "conexion", "peso"]:
                if k in attr_dict:
                    detalles.append(f"{k.replace('_', ' ').capitalize()}: {attr_dict[k]}")

            medidas_str = ", ".join(detalles) if detalles else "Consultar ficha técnica"

            bullet = f"* **{nombre}** (Código: `{codigo}`"
            if codigo_orig and codigo_orig != codigo:
                bullet += f" | Cód. Fábrica: `{codigo_orig}`"
            bullet += f" | Marca: {marca}"
            if precio_val is not None:
                mon_str = moneda_val or "ARS"
                bullet += f" | Precio: {mon_str} {precio_val:,.2f}"
            if c.pagina is not None:
                bullet += f" | Pág. {c.pagina}"
            bullet += ")"
            if detalles:
                bullet += f": {', '.join(detalles)}. [Fragmento {c.fragment_id}]"
            else:
                bullet += f": Especificaciones detalladas en catálogo. [Fragmento {c.fragment_id}]"

            response_lines.append(bullet)

            productos_json.append({
                "codigo": codigo,
                "codigo_orig": codigo_orig,
                "codigo_proveedor": cod_prov_val,
                "nombre_proveedor": nom_prov_val,
                "marca": marca,
                "nombre": nombre,
                "categoria_padre": cat_padre_val,
                "categoria": c.categoria or attr_dict.get("categoria"),
                "subcategoria": subcat_val,
                "precio": precio_val,
                "moneda": moneda_val,
                "unidad_venta": unidad_venta_val,
                "empaque": empaque_val,
                "especificaciones": medidas_str,
                "archivo_origen": archivo_val,
                "pagina": c.pagina,
                "fragmento_id": c.fragment_id
            })

        narrativa = "\n".join(response_lines)

        if json_mode:
            return json.dumps({
                "respuesta_narrativa": narrativa,
                "consulta_respondida": True,
                "productos": productos_json
            }, ensure_ascii=False, indent=2)

        return narrativa


# ============================================================================
# 5. CONSTRUCTOR DE SALIDAS ESTRUCTURADAS (JSON SCHEMA / APIS)
# ============================================================================

class StructuredOutputBuilder:
    """
    Parsea y normaliza respuestas en el esquema estructurado JSON tipado según
    la Sección 5 de la guía técnica.
    """
    @classmethod
    def parse_or_build(
        cls,
        raw_response: str,
        query: str,
        chunks: Sequence[InputContextChunk],
        is_refusal: bool
    ) -> Dict[str, Any]:
        """
        Intenta parsear JSON directo emitido por el LLM o sintetiza el esquema desde texto narrativo.
        """
        # Intento 1: Parsear directamente si es un bloque JSON
        try:
            cleaned = raw_response.strip()
            if "```json" in cleaned:
                match = re.search(r'```json\s*(.*?)\s*```', cleaned, re.DOTALL)
                if match:
                    cleaned = match.group(1)
            elif "```" in cleaned:
                match = re.search(r'```\s*(.*?)\s*```', cleaned, re.DOTALL)
                if match:
                    cleaned = match.group(1)

            parsed = json.loads(cleaned)
            if isinstance(parsed, dict) and "respuesta_narrativa" in parsed:
                # Enriquecer productos que falten metadatos usando los chunks fuente
                chunk_map = {c.fragment_id: c for c in chunks}
                prods = parsed.get("productos", [])
                if isinstance(prods, list):
                    for p in prods:
                        if isinstance(p, dict):
                            f_id = p.get("fragmento_id")
                            if f_id in chunk_map:
                                src_c = chunk_map[f_id]
                                if not p.get("archivo_origen") and src_c.archivo_origen:
                                    p["archivo_origen"] = src_c.archivo_origen
                                if p.get("pagina") is None and src_c.pagina is not None:
                                    p["pagina"] = src_c.pagina
                                if p.get("precio") is None and src_c.precio is not None:
                                    p["precio"] = src_c.precio
                                if not p.get("moneda") and src_c.moneda:
                                    p["moneda"] = src_c.moneda
                                if not p.get("codigo_proveedor") and src_c.codigo_proveedor:
                                    p["codigo_proveedor"] = src_c.codigo_proveedor
                                if not p.get("nombre_proveedor") and src_c.nombre_proveedor:
                                    p["nombre_proveedor"] = src_c.nombre_proveedor
                                if not p.get("unidad_venta") and src_c.unidad_venta:
                                    p["unidad_venta"] = src_c.unidad_venta
                                if not p.get("empaque") and src_c.empaque:
                                    p["empaque"] = src_c.empaque
                return parsed
        except Exception:
            pass

        # Intento 2: Estructurar determinísticamente desde los fragmentos citados
        if is_refusal or not chunks:
            return {
                "respuesta_narrativa": raw_response if raw_response else PromptBuilder.REFUSAL_CANONICAL_MESSAGE,
                "consulta_respondida": False,
                "productos": []
            }

        # Extraer productos de los chunks suministrados
        productos: List[Dict[str, Any]] = []
        for c in chunks:
            attr_dict: Dict[str, str] = {}
            for line in c.content.strip().splitlines():
                if ":" in line:
                    p = line.split(":", 1)
                    if len(p) == 2:
                        attr_dict[p[0].strip().lower()] = p[1].strip()

            nombre = (
                attr_dict.get("nombre") or
                attr_dict.get("nombre_producto") or
                attr_dict.get("titulo") or
                attr_dict.get("producto") or
                "Artículo de Catálogo"
            )
            medidas_parts: List[str] = []
            for k in ["encastre", "torque", "torque_maximo", "velocidad", "medida", "apertura", "rosca", "diametro", "presion_aire", "conexion", "peso"]:
                if k in attr_dict:
                    medidas_parts.append(f"{k.replace('_', ' ').capitalize()}: {attr_dict[k]}")

            precio_val = c.precio
            if precio_val is None and "precio" in attr_dict:
                try:
                    precio_val = float(attr_dict["precio"])
                except Exception:
                    precio_val = None

            productos.append({
                "codigo": c.codigo_producto or attr_dict.get("codigo") or "N/D",
                "codigo_orig": c.codigo_orig or attr_dict.get("codigo_orig"),
                "codigo_proveedor": c.codigo_proveedor or attr_dict.get("codigo_proveedor"),
                "nombre_proveedor": c.nombre_proveedor or attr_dict.get("proveedor"),
                "marca": c.marca or attr_dict.get("marca") or "N/D",
                "nombre": nombre,
                "categoria_padre": c.categoria_padre or attr_dict.get("categoria_padre"),
                "categoria": c.categoria or attr_dict.get("categoria"),
                "subcategoria": c.subcategoria or attr_dict.get("subcategoria"),
                "precio": precio_val,
                "moneda": c.moneda or attr_dict.get("moneda"),
                "unidad_venta": c.unidad_venta or attr_dict.get("unidad_venta"),
                "empaque": c.empaque or attr_dict.get("empaque"),
                "especificaciones": ", ".join(medidas_parts) if medidas_parts else None,
                "archivo_origen": c.archivo_origen or attr_dict.get("archivo_origen"),
                "pagina": c.pagina,
                "fragmento_id": c.fragment_id
            })

        return {
            "respuesta_narrativa": raw_response,
            "consulta_respondida": True,
            "productos": productos
        }


# ============================================================================
# 6. ORQUESTADOR PRINCIPAL DE LA FASE 6
# ============================================================================

class Fase6Generator:
    """
    Orquestador principal de la Fase 6:
    Toma los fragmentos refinados de la Fase 5, ensambla el prompt con encapsulación XML,
    ejecuta el LLM con temperatura 0.0 y audita las citaciones de salida.
    """
    def __init__(
        self,
        model_name: str = "gpt-4o",
        temperature: float = 0.0,
        max_tokens: int = 800,
        provider: str = "auto",
        query_aware: bool = True
    ):
        self.model_name = model_name
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.provider = provider
        self.query_aware = query_aware

        self.inference_engine = LLMInferenceEngine(
            model_name=model_name,
            temperature=temperature,
            max_tokens=max_tokens,
            provider=provider
        )
        logger.info(
            f"Fase 6 Inicializada: Modelo='{model_name}' | Temp={temperature} | "
            f"Provider='{provider}' | Query-Aware={query_aware}"
        )

    def generate_response(
        self,
        query: str,
        fase_5_candidates: Union[List[Any], Sequence[Any], Any],
        structured_json_mode: bool = False
    ) -> GenerationResult:
        """
        Ejecuta el ciclo de síntesis aumentada y verificación de fidelidad.
        """
        t0 = time.perf_counter()

        # Si el input es un objeto RerankResult de la Fase 5
        candidates_list: Sequence[Any]
        if hasattr(fase_5_candidates, "final_candidates"):
            candidates_list = getattr(fase_5_candidates, "final_candidates")
        elif isinstance(fase_5_candidates, (list, tuple)):
            candidates_list = fase_5_candidates
        elif isinstance(fase_5_candidates, Sequence):
            candidates_list = list(fase_5_candidates)
        else:
            candidates_list = [fase_5_candidates]

        # 1. Empaquetar candidatos de Fase 5 en InputContextChunks secuenciales
        chunks: List[InputContextChunk] = []
        for idx, item in enumerate(candidates_list, start=1):
            if isinstance(item, dict):
                score_raw = item.get("normalized_score")
                if score_raw is None:
                    score_raw = item.get("score", 0.0)
                relevance_score = float(score_raw) if score_raw is not None else 0.0

                pos_raw = item.get("prompt_position")
                prompt_pos = int(pos_raw) if pos_raw is not None else idx

                meta = item.get("metadata") or {}
                pagina_val: Optional[int] = None
                if isinstance(meta, dict):
                    p = meta.get("pagina") or meta.get("pagina_origen")
                    pagina_val = int(p) if p is not None else None
                if pagina_val is None and item.get("pagina") is not None:
                    pagina_val = int(item["pagina"])
                elif pagina_val is None and item.get("pagina_origen") is not None:
                    pagina_val = int(item["pagina_origen"])

                raw_precio = item.get("precio") if item.get("precio") is not None else (meta.get("precio") if isinstance(meta, dict) else None)
                precio_val: Optional[float] = None
                if raw_precio is not None:
                    try:
                        precio_val = float(raw_precio)
                    except Exception:
                        precio_val = None

                moneda_val = item.get("moneda") or (meta.get("moneda") if isinstance(meta, dict) else None)
                cod_orig = item.get("codigo_orig") or (meta.get("codigo_orig") if isinstance(meta, dict) else None)
                cod_prov = item.get("codigo_proveedor") or (meta.get("codigo_proveedor") if isinstance(meta, dict) else None)
                nom_prov = item.get("nombre_proveedor") or (meta.get("nombre_proveedor") or meta.get("proveedor") if isinstance(meta, dict) else None)
                cat_padre = item.get("categoria_padre") or (meta.get("categoria_padre") if isinstance(meta, dict) else None)
                subcat = item.get("subcategoria") or (meta.get("subcategoria") if isinstance(meta, dict) else None)
                u_venta = item.get("unidad_venta") or (meta.get("unidad_venta") if isinstance(meta, dict) else None)
                emp = item.get("empaque") or (meta.get("empaque") if isinstance(meta, dict) else None)
                arch_orig = item.get("archivo_origen") or (meta.get("archivo_origen") if isinstance(meta, dict) else None)

                c = InputContextChunk(
                    fragment_id=idx,
                    node_id=str(item.get("node_id") or f"node_{idx}"),
                    codigo_producto=item.get("codigo_producto") or (meta.get("codigo") if isinstance(meta, dict) else None),
                    codigo_orig=cod_orig,
                    marca=item.get("marca") or (meta.get("marca") if isinstance(meta, dict) else None),
                    nombre_proveedor=nom_prov,
                    codigo_proveedor=cod_prov,
                    categoria_padre=cat_padre,
                    categoria=item.get("categoria") or (meta.get("categoria") if isinstance(meta, dict) else None),
                    subcategoria=subcat,
                    precio=precio_val,
                    moneda=moneda_val,
                    unidad_venta=u_venta,
                    empaque=emp,
                    archivo_origen=arch_orig,
                    pagina=pagina_val,
                    content=str(item.get("text_content") or ""),
                    relevance_score=relevance_score,
                    prompt_position=prompt_pos,
                    metadata=meta if isinstance(meta, dict) else {}
                )
            else:
                score_raw = getattr(item, "normalized_score", None)
                if score_raw is None:
                    score_raw = getattr(item, "score", 0.0)
                relevance_score = float(score_raw) if score_raw is not None else 0.0

                pos_raw = getattr(item, "prompt_position", None)
                prompt_pos = int(pos_raw) if pos_raw is not None else idx

                meta = getattr(item, "metadata", None) or {}
                pagina_val = None
                if isinstance(meta, dict):
                    p = meta.get("pagina") or meta.get("pagina_origen")
                    pagina_val = int(p) if p is not None else None
                if pagina_val is None and getattr(item, "pagina", None) is not None:
                    pagina_val = int(getattr(item, "pagina"))
                elif pagina_val is None and getattr(item, "pagina_origen", None) is not None:
                    pagina_val = int(getattr(item, "pagina_origen"))

                raw_precio = getattr(item, "precio", None) if getattr(item, "precio", None) is not None else (meta.get("precio") if isinstance(meta, dict) else None)
                precio_val = None
                if raw_precio is not None:
                    try:
                        precio_val = float(raw_precio)
                    except Exception:
                        precio_val = None

                moneda_val = getattr(item, "moneda", None) or (meta.get("moneda") if isinstance(meta, dict) else None)
                cod_orig = getattr(item, "codigo_orig", None) or (meta.get("codigo_orig") if isinstance(meta, dict) else None)
                cod_prov = getattr(item, "codigo_proveedor", None) or (meta.get("codigo_proveedor") if isinstance(meta, dict) else None)
                nom_prov = getattr(item, "nombre_proveedor", None) or (meta.get("nombre_proveedor") or meta.get("proveedor") if isinstance(meta, dict) else None)
                cat_padre = getattr(item, "categoria_padre", None) or (meta.get("categoria_padre") if isinstance(meta, dict) else None)
                subcat = getattr(item, "subcategoria", None) or (meta.get("subcategoria") if isinstance(meta, dict) else None)
                u_venta = getattr(item, "unidad_venta", None) or (meta.get("unidad_venta") if isinstance(meta, dict) else None)
                emp = getattr(item, "empaque", None) or (meta.get("empaque") if isinstance(meta, dict) else None)
                arch_orig = getattr(item, "archivo_origen", None) or (meta.get("archivo_origen") if isinstance(meta, dict) else None)

                c = InputContextChunk(
                    fragment_id=idx,
                    node_id=str(getattr(item, "node_id", f"node_{idx}")),
                    codigo_producto=getattr(item, "codigo_producto", None) or (meta.get("codigo") if isinstance(meta, dict) else None),
                    codigo_orig=cod_orig,
                    marca=getattr(item, "marca", None) or (meta.get("marca") if isinstance(meta, dict) else None),
                    nombre_proveedor=nom_prov,
                    codigo_proveedor=cod_prov,
                    categoria_padre=cat_padre,
                    categoria=getattr(item, "categoria", None) or (meta.get("categoria") if isinstance(meta, dict) else None),
                    subcategoria=subcat,
                    precio=precio_val,
                    moneda=moneda_val,
                    unidad_venta=u_venta,
                    empaque=emp,
                    archivo_origen=arch_orig,
                    pagina=pagina_val,
                    content=str(getattr(item, "text_content", "")),
                    relevance_score=relevance_score,
                    prompt_position=prompt_pos,
                    metadata=meta if isinstance(meta, dict) else {}
                )
            chunks.append(c)

        valid_chunk_ids: List[int] = [c.fragment_id for c in chunks]

        # 2. Construcción de Prompts (XML Delimited + Query-Aware)
        if structured_json_mode:
            system_prompt = PromptBuilder.build_structured_json_system_prompt()
        else:
            system_prompt = PromptBuilder.build_system_prompt()

        user_prompt = PromptBuilder.build_user_prompt(
            query=query,
            chunks=chunks,
            query_aware=self.query_aware
        )

        # Estimación rápida de tokens de contexto (1 token ≈ 4 caracteres)
        approx_tokens = (len(system_prompt) + len(user_prompt)) // 4

        # 3. Inferencia Determinista del Modelo
        raw_response = self.inference_engine.generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            chunks=chunks,
            json_mode=structured_json_mode
        )

        # 4. Verificación de Citaciones y Groundedness
        is_refusal = PromptBuilder.REFUSAL_CANONICAL_MESSAGE.lower() in raw_response.lower()

        # Si viene en JSON, extraer texto narrativo para verificar citas
        structured_json: Optional[Dict[str, Any]] = None
        text_for_audit = raw_response
        if structured_json_mode:
            structured_json = StructuredOutputBuilder.parse_or_build(
                raw_response=raw_response,
                query=query,
                chunks=chunks,
                is_refusal=is_refusal
            )
            text_for_audit = structured_json.get("respuesta_narrativa", raw_response)
            is_refusal = not structured_json.get("consulta_respondida", True) or is_refusal

        verification = CitationVerifier.verify(text_for_audit, valid_chunk_ids)

        # 5. Determinación del Estado Operativo
        if is_refusal:
            status = "REFUSAL_GROUNDED"
        elif not chunks:
            status = "EMPTY_CONTEXT"
        elif not verification.is_fully_grounded:
            status = "CITATION_MISMATCH"
        else:
            status = "SUCCESS"

        t1 = time.perf_counter()
        latency_ms = (t1 - t0) * 1000.0

        # 6. Preparación de la Tripleta RAG para Fase 7 (Evaluación)
        rag_triplet: Dict[str, Any] = {
            "query": query,
            "context": [c.to_dict() for c in chunks],
            "response": text_for_audit,
            "is_refusal": is_refusal,
            "citations": verification.citations_found,
            "is_grounded": verification.is_fully_grounded
        }

        logger.info(
            f"Fase 6 Completada en {latency_ms:.2f}ms | Chunks={len(chunks)} | "
            f"Status={status} | Citaciones={len(verification.citations_found)} | Grounded={verification.is_fully_grounded}"
        )

        return GenerationResult(
            query=query,
            response_text=raw_response,
            is_refusal=is_refusal,
            status=status,
            verification=verification,
            structured_json=structured_json,
            total_context_tokens_approx=approx_tokens,
            latency_ms=round(latency_ms, 2),
            model_name=self.model_name,
            temperature=self.temperature,
            rag_triplet=rag_triplet
        )


# ============================================================================
# 7. INTEGRACIÓN COMPLETA PIPELINE RAG (FASE 4 -> FASE 5 -> FASE 6)
# ============================================================================

def run_rag_pipeline(
    query: str,
    table_name: str = "catalogo_amx_rag",
    k_input: int = 20,
    top_n: int = 3,
    threshold: float = 0.45,
    reranker_model: str = "BAAI/bge-reranker-v2-m3",
    llm_model: str = "gpt-4o",
    provider: str = "auto",
    structured_json: bool = False,
    mock: bool = False
) -> GenerationResult:
    """
    Ejecuta el pipeline RAG completo de producción:
    1. Fase 4: Recuperación Híbrida Dual (Dense + Sparse/BM25 con RRF).
    2. Fase 5: Cross-Encoder Reranking, Thresholding y U-Shape Context Slicing.
    3. Fase 6: XML Context Packaging, LLM Synthesis (Temp 0.0) y Citation Verification.
    """
    logger.info(f"Iniciando Pipeline RAG completo para consulta: '{query}'")

    # Invocación de Fase 4 y Fase 5 mediante helper integrado
    candidates: Sequence[Any]
    try:
        try:
            from app.core.retrieval.reranker import retrieve_and_rerank
        except ImportError:
            from fase_5_reranker import retrieve_and_rerank
        rerank_result = retrieve_and_rerank(
            query=query,
            table_name=table_name,
            k_input=k_input,
            top_n=top_n,
            threshold=threshold,
            model_name=reranker_model,
            mock=mock
        )
        candidates = rerank_result.final_candidates
    except Exception as e:
        logger.warning(f"Error al encadenar Fase 4/5 ({e}). Utilizando pool sintético de respaldo.")
        try:
            from app.core.retrieval.reranker import generar_pool_sintetico_fase_4, Fase5RerankerCompressor
        except ImportError:
            from fase_5_reranker import generar_pool_sintetico_fase_4, Fase5RerankerCompressor
        pool = generar_pool_sintetico_fase_4()
        reranker = Fase5RerankerCompressor(score_threshold=threshold, top_n=top_n)
        rerank_result = reranker.process(query, pool)
        candidates = rerank_result.final_candidates

    # Fase 6: Generador
    generator = Fase6Generator(
        model_name=llm_model,
        temperature=0.0,
        provider=provider,
        query_aware=True
    )

    return generator.generate_response(
        query=query,
        fase_5_candidates=candidates,
        structured_json_mode=structured_json
    )


# ============================================================================
# 8. SUITE DE BENCHMARKS Y VALIDACIÓN FACTUAL
# ============================================================================

def ejecutar_benchmarks() -> None:
    """
    Ejecuta la suite de pruebas representativa para validar:
    1. Escenario A: Búsqueda factual de producto existente con citación atómica.
    2. Escenario B: Búsqueda fuera de catálogo (activación de Directiva de Ausencia Canónica).
    3. Escenario C: Consulta de medida/torque inexistente (verificación de no-extrapolación).
    4. Escenario D: Emisión de JSON estructurado tipado para APIs / ERPs.
    """
    print("\n" + "=" * 90)
    print(" SUITE DE BENCHMARKS Y VALIDACIÓN FACTUAL: FASE 6 (GENERACIÓN AUMENTADA Y CITACIONES)")
    print("=" * 90)

    # Pool de fragmentos provenientes de Fase 5 para pruebas controladas
    candidatos_controlados: List[Dict[str, Any]] = [
        {
            "node_id": "node_prod_AMX-AT-5044",
            "codigo_producto": "AMX-AT-5044",
            "marca": "XMAX",
            "categoria": "Llaves de Impacto Neumáticas",
            "normalized_score": 0.9922,
            "pagina": 3,
            "text_content": (
                "codigo_producto: AMX-AT-5044\n"
                "marca: XMAX\n"
                "nombre: Llave de impacto neumática XMAX de 1/2\" AT-5044\n"
                "descripcion: Llave de impacto neumática XMAX AT-5044 de 1/2 pulgada con mecanismo doble martillo.\n"
                "encastre: 1/2 pulgada\n"
                "torque: 520 lb/ft (700 Nm)\n"
                "velocidad: 7500 rpm\n"
                "conexion: 1/4 NPT\n"
                "peso: 2,7 kg"
            )
        },
        {
            "node_id": "node_prod_AMX-AT-330-2",
            "codigo_producto": "AMX-AT-330-2",
            "marca": "XMAX",
            "categoria": "Llaves de Impacto Neumáticas",
            "normalized_score": 0.9850,
            "pagina": 4,
            "text_content": (
                "codigo_producto: AMX-AT-330-2\n"
                "marca: XMAX\n"
                "nombre: Llave de impacto neumática XMAX de 3/4\" AT-330-2\n"
                "descripcion: Llave de impacto neumática XMAX AT-330-2 de 3/4 pulgada con mecanismo doble martillo.\n"
                "encastre: 3/4 pulgada\n"
                "torque: 900 lb/ft (1120 Nm)\n"
                "velocidad: 4500 rpm\n"
                "conexion: 3/8 NPT\n"
                "peso: 4,8 kg"
            )
        }
    ]

    generator = Fase6Generator(
        model_name="gpt-4o",
        temperature=0.0,
        query_aware=True
    )

    escenarios: List[BenchmarkScenario] = [
        BenchmarkScenario(
            id="A",
            titulo="Escenario A: Consulta Factual de Producto Existente (AMX-AT-5044)",
            query="¿Cuáles son las especificaciones técnicas y torque de la llave de impacto AMX-AT-5044 de 1/2 pulgada?",
            contexto=candidatos_controlados,
            structured=False
        ),
        BenchmarkScenario(
            id="B",
            titulo="Escenario B: Consulta Fuera de Catálogo (Prueba de Directiva de Ausencia)",
            query="¿Tienen stock de paneles solares fotovoltaicos de 450W monocristalinos?",
            contexto=[],  # Bloqueado por la Fase 5
            structured=False
        ),
        BenchmarkScenario(
            id="C",
            titulo="Escenario C: Consulta de Medida / Especificación Inexistente (Anti-Extrapolación)",
            query="¿Cuál es el torque de la llave AMX-AT-5044 en encastre de 1 pulgada y qué precio tiene?",
            contexto=candidatos_controlados,
            structured=False
        ),
        BenchmarkScenario(
            id="D",
            titulo="Escenario D: Generación de Salida Estructurada Tipada (Structured JSON)",
            query="¿Qué opciones de llaves de impacto XMAX tienen disponibles?",
            contexto=candidatos_controlados,
            structured=True
        )
    ]

    for esc in escenarios:
        print(f"\n>>> [{esc.id}] {esc.titulo}")
        print(f"    Consulta: \"{esc.query}\"")
        print(f"    Fragmentos inyectados: {len(esc.contexto)}")

        resultado = generator.generate_response(
            query=esc.query,
            fase_5_candidates=esc.contexto,
            structured_json_mode=esc.structured
        )

        print(f"    -> Estado: {resultado.status}")
        print(f"    -> ¿Activó Rechazo Honesto?: {resultado.is_refusal}")
        print(f"    -> Latencia: {resultado.latency_ms:.2f} ms")
        print(f"    -> Citas Detectadas: {resultado.verification.citations_found}")
        print(f"    -> Ratio de Citas: {resultado.verification.citation_ratio * 100:.0f}%")
        print(f"    -> ¿Grounded Verificado?: {resultado.verification.is_fully_grounded}")

        print("\n    [RESPUESTA GENERADA]:")
        for linea in resultado.response_text.splitlines():
            print(f"       {linea}")

        if resultado.structured_json:
            print("\n    [SALIDA ESTRUCTURADA JSON (Structured Output)]:")
            print(json.dumps(resultado.structured_json, indent=4, ensure_ascii=False))

    print("\n" + "=" * 90)
    print("EJEMPLO DE PROMPT CONTEXTUALIZADO QUERY-AWARE (ZONAS DE PRIMACÍA Y RECENCIA):")
    print("=" * 90)
    user_prompt_ejemplo = PromptBuilder.build_user_prompt(
        query="Llave neumática AMX-AT-5044",
        chunks=[
            InputContextChunk(
                fragment_id=1,
                node_id="node_prod_AMX-AT-5044",
                codigo_producto="AMX-AT-5044",
                marca="XMAX",
                categoria="Herramientas Neumáticas",
                pagina=3,
                content="codigo_producto: AMX-AT-5044\ntorque: 700 Nm\nencastre: 1/2 pulgada"
            )
        ],
        query_aware=True
    )
    print(user_prompt_ejemplo)
    print("=" * 90)


# ============================================================================
# 9. INTERFAZ DE LÍNEA DE COMANDOS (CLI)
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fase 6: Síntesis del LLM, Prompt Engineering y Control de Alucinaciones.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EJEMPLOS DE USO:
  # 1. Benchmark y suite de pruebas:
  python fase_6_generator.py --benchmark

  # 2. Consulta end-to-end sobre catálogo en PostgreSQL:
  python fase_6_generator.py "Llave de impacto neumática de 1/2 pulgada" --top-n 3

  # 3. Salida en formato JSON estructurado (Structured Output para APIs):
  python fase_6_generator.py "Llaves de impacto XMAX" --structured

  # 4. Consulta por código / SKU exacto:
  python fase_6_generator.py "AMX-AT-5044" --model gpt-4o-mini

  # 5. Modo emulado/sintético offline:
  python fase_6_generator.py "Llave de impacto CARBIZ-099" --mock
        """
    )
    parser.add_argument("query_pos", nargs="?", default=None, help="Consulta a procesar (posicional).")
    parser.add_argument("--query", "-q", type=str, default=None, help="Consulta de búsqueda.")
    parser.add_argument("--table", "-t", type=str, default="catalogo_amx_rag", help="Tabla en PostgreSQL (default: catalogo_amx_rag).")
    parser.add_argument("--k-input", "-k", type=int, default=20, help="Candidatos a recuperar en Fase 4 (default: 20).")
    parser.add_argument("--top-n", "-n", type=int, default=3, help="Candidatos finalistas de Fase 5 (default: 3).")
    parser.add_argument("--threshold", type=float, default=0.45, help="Umbral de corte de score sigmoide en Fase 5 (default: 0.45).")
    parser.add_argument("--reranker-model", type=str, default="BAAI/bge-reranker-v2-m3", help="Modelo de Reranking Cross-Encoder.")
    parser.add_argument("--model", "-m", type=str, default="gpt-4o", help="Modelo de LLM para Fase 6 (default: gpt-4o).")
    parser.add_argument("--provider", type=str, default="auto", choices=["auto", "openai", "gemini"], help="Proveedor de LLM.")
    parser.add_argument("--structured", "-s", action="store_true", help="Formato de salida JSON estructurado.")
    parser.add_argument("--json", "-j", action="store_true", help="Imprimir resultado completo en JSON.")
    parser.add_argument("--benchmark", action="store_true", help="Ejecutar suite de pruebas y benchmark.")
    parser.add_argument("--mock", action="store_true", help="Usar catálogo sintético sin conexión externa.")

    args = parser.parse_args()

    if args.benchmark:
        ejecutar_benchmarks()
        return

    query_text = args.query or args.query_pos
    if not query_text:
        query_text = "Llave de impacto neumática XMAX de 1/2 pulgada AT-5044"

    resultado = run_rag_pipeline(
        query=query_text,
        table_name=args.table,
        k_input=args.k_input,
        top_n=args.top_n,
        threshold=args.threshold,
        reranker_model=args.reranker_model,
        llm_model=args.model,
        provider=args.provider,
        structured_json=args.structured,
        mock=args.mock
    )

    if args.json:
        print(json.dumps(resultado.to_dict(), ensure_ascii=False, indent=2))
        return

    print("\n" + "=" * 90)
    print(" RESULTADO FINAL PIPELINE RAG: FASE 6 (GENERACIÓN AUMENTADA Y AUDITORÍA)")
    print(f" Consulta: \"{query_text}\"")
    print(f" Estado: {resultado.status} | Latencia: {resultado.latency_ms:.2f} ms | Modelo: {resultado.model_name}")
    print(f" Tokens Contexto (Aprox): {resultado.total_context_tokens_approx} | Rechazo Honesto: {resultado.is_refusal}")
    print(f" Citaciones Encontradas: {len(resultado.verification.citations_found)} | Grounded: {resultado.verification.is_fully_grounded}")
    print("=" * 90)

    if args.structured and resultado.structured_json:
        print("\n[SALIDA ESTRUCTURADA JSON (Structured Output)]:")
        print(json.dumps(resultado.structured_json, ensure_ascii=False, indent=2))
    else:
        print("\n[RESPUESTA GENERADA]:\n")
        print(resultado.response_text)

    print("\n" + "-" * 90)
    print("AUDITORÍA DE CITACIONES:")
    print(f" * Citas detectadas: {resultado.verification.citations_found}")
    print(f" * IDs referenciados: {resultado.verification.referenced_fragment_ids}")
    print(f" * Citas válidas: {resultado.verification.valid_fragment_ids}")
    print(f" * Citas inválidas / fantasmas: {resultado.verification.invalid_fragment_ids}")
    print(f" * Ratio de cobertura: {resultado.verification.citation_ratio * 100:.0f}% de oraciones con cita")
    print("=" * 90 + "\n")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        ejecutar_benchmarks()
    else:
        main()
