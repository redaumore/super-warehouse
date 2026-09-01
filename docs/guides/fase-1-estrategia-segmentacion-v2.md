# Nota Técnica: Estrategia de Segmentación (Fase 1) para Catálogo de Productos (v2)

Esta nota técnica consolida la teoría fundamental de toma de decisiones operativas para la **Fase 1: Estrategia de Segmentación** de un pipeline de RAG (Retrieval-Augmented Generation) y detalla la aplicación metodológica sobre un catálogo estructurado de productos.

---

## 1. Fundamentos Teóricos: ¿Cómo Decidir Operativamente qué Estrategia de Segmentación Usar?

La segmentación (o *chunking*) no es simplemente cortar texto cada cierta cantidad de caracteres o tokens de forma aleatoria. En sistemas de producción, y especialmente en catálogos estructurados, la elección de la estrategia de segmentación define los límites de rendimiento del sistema y se evalúa bajo los siguientes escenarios principales:

### Escenario A: Segmentación Gruesa (Nivel de Página o Sección)
*   **En qué consiste:** Consiste en agrupar conjuntos de datos amplios, múltiples tablas o páginas completas de un catálogo bajo un único bloque de texto o vector.
*   **Cuándo usarla:** Es adecuada cuando los patrones de consulta esperados del usuario son analíticos, macro o comparativos. Por ejemplo, si el usuario suele preguntar: *"¿Qué marcas de herramientas de construcción manejás?"* o *"Hacé un resumen de las familias de abrazaderas de acero que vendés"*. 
*   **Limitación:** Si el catálogo tiene productos sumamente específicos y con códigos numéricos de parte, la información granular se "promedia" en el embedding gruesa, reduciendo drásticamente la capacidad de recuperar un ítem exacto.

### Escenario B: Segmentación Fina (Nivel de Producto Granular)
*   **En qué consiste:** Se extrae cada fila de datos, artículo, código o especificación individual de producto y se indexa como un fragmento (nodo) completamente independiente con su propio embedding.
*   **Cuándo usarla:** Es la estrategia sugerida para **catálogos técnicos, industriales o de ferretería**. Las búsquedas en estos entornos suelen ser de precisión milimétrica (ej: *"Abrazadera americana artículo 13"* o buscando directamente el código de producto *"100020"*). 
*   **Beneficio:** Evita la dilución de la firma semántica. El modelo de embeddings o la base de datos vectorial puede emparejar de forma directa el término de búsqueda con el nodo único que representa exactamente ese producto.

### Escenario C: Estrategia Híbrida / Jerárquica (Small-to-Big o Parent-Child)
*   **En qué consiste:** Se generan embeddings e índices de búsqueda sobre fragmentos muy pequeños y específicos (hijos - ej. el producto individual con sus códigos). Sin embargo, cuando se recupera un resultado coincidente, el pipeline RAG reemplaza o expande dicho fragmento por uno de contexto superior (padre - ej. la descripción de la sección completa, marca o página del catálogo) antes de enviárselo al LLM para la generación.
*   **Cuándo usarla:** Es el estándar de oro en producción cuando se necesita el máximo nivel de precisión matemática en la búsqueda, pero a la vez se requiere que el LLM entienda el panorama general y el contexto jerárquico de las marcas o familias de productos sin "perderse en las ramas".

---

## 2. Metodología: ¿Cómo Preparar el Entregable (Salida) de la Fase 1?

La salida de la Fase 1 sirve como insumo directo para el pipeline de Embeddings (Fase 2) y de Indexación Vectorial (Fase 3). Físicamente, este entregable debe consistir en una **lista estructurada de objetos "Node" o "Document"** (típicamente exportados en un JSON intermedio).

Para preparar esta salida, se deben ejecutar tres pasos operativos clave sobre cada registro del catálogo:

### A. Context Injection (Inyección de Contexto)
Si se vectoriza una fila técnica de forma aislada (ej: `{"Apertura": "78/101 mm", "Art": "13"}`), el embedding no tiene forma de saber de qué tipo de producto se trata porque la marca, el tipo de herramienta o la categoría principal estaban en niveles superiores del documento original. 
*   **Operativa:** Se debe concatenar programáticamente un string plano de búsqueda donde las capas superiores jerárquicas heredadas actúen como prefijo de las especificaciones concretas del producto. El formato óptimo recomendado es **YAML** (`clave: valor`) porque los transformadores modernos procesan y atienden estructuras de pares de datos significativamente mejor que el texto desordenado o el JSON plano escapado.

### B. Diccionario de Metadatos (`metadata`) para Pre-filtrado
El objeto "Node" debe llevar un diccionario de metadatos limpio y plano con atributos estables como `marca`, `categoria` o `codigo`.
*   **Operativa:** En la Fase 3, se configurarán índices tradicionales (B-Trees o índices invertidos) sobre estos metadatos en la base de datos vectorial. Esto permite que el pipeline ejecute un **pre-filtrado estricto** en tiempo de consulta. Por ejemplo, si el usuario busca *"Abrazadera de 13 mm CARBIZ"*, el sistema puede filtrar y aislar únicamente los vectores que tengan `metadata.marca == "CARBIZ"` antes de calcular similitudes vectoriales, aumentando drásticamente la velocidad de consulta y eliminando falsos positivos.

### C. Mapeo de Relaciones Jerárquicas
Si se opta por un enfoque híbrido, cada objeto de nodo debe documentar sus conexiones lógicas (`parent_id`, `next_node_id`, `previous_node_id`) para que el orquestador del RAG pueda navegar entre nodos hermanos o expandirse al nodo padre.

---

## 3. Aplicación Práctica: Segmentación Fina sobre tu Catálogo

### Diagnóstico de tu Fase 0
Tu catálogo estructurado cuenta con una ventaja competitiva clave: **en cada producto ya están explícitamente presentes todos los atributos de la jerarquía a la que pertenece** (`marca_pagina`, `categoria_padre`, `categoria`, `subcategoria`, `marca`, etc.). 

### Elección de Estrategia: **Segmentación Fina (Granular)**
Dado que ya resolviste el problema de la pérdida de contexto heredando las variables jerárquicas en cada elemento del JSON original, la mejor opción operativa es la **Segmentación Fina a Nivel de Producto Granular**. Esto significa que:
*   Cada ítem individual del array de productos de tu Fase 0 se convertirá en un único Nodo con su propio vector.
*   Esto garantiza que los códigos específicos de producto y medidas concretas queden grabados de forma limpia en el espacio vectorial, asegurando un **Recall impecable** para tus búsquedas técnicas.

---

## 4. Ejemplo de Transformación del Entregable (Fase 1)

A continuación se muestra cómo se aplica de forma concreta la teoría anterior tomando el producto que compartiste de tu Fase 0:

### 1. JSON de Entrada (Fase 0):
```json
{
  "pagina": 4,
  "marca_pagina": "CARBIZ",
  "codigo": "100020",
  "proveedor": "Ferretera del Norte",
  "nombre_comercial": "Abrazadera de acero Americana a Cremallera artículo 13, apertura 78/101 mm",
  "categoria_padre": "Fijaciones y sujeciones",
  "categoria": "Abrazaderas",
  "subcategoria": "Abrazaderas de acero a cremallera",
  "marca": "CARBIZ",
  "descripcion_completa": "Abrazadera de acero CARBIZ Americana a Cremallera, fleje de 13 mm de ancho, artículo 13 y apertura de 78/101 mm. Venta por unidad, paquete de 10 unidades.",
  "atributos": [
    {"nombre": "tipo", "valor": "Americana a Cremallera"},
    {"nombre": "fleje_ancho", "valor": "13 mm"},
    {"nombre": "articulo", "valor": "13"},
    {"nombre": "apertura", "valor": "78/101 mm"},
    {"nombre": "unidad_venta", "valor": "c/u"},
    {"nombre": "paquete_cantidad", "valor": "10"}
  ]
}
```

### 2. Objeto de Nodo de Salida (Entregable Fase 1):
```json
{
  "node_id": "node_prod_100020",
  "text_to_embed": "proveedor: Ferretera del Norte\ncategoria_padre: Fijaciones y sujeciones\ncategoria: Abrazaderas\nsubcategoria: Abrazaderas de acero a cremallera\nmarca: CARBIZ\nnombre: Abrazadera de acero Americana a Cremallera artículo 13, apertura 78/101 mm\ndescripcion: Abrazadera de acero CARBIZ Americana a Cremallera, fleje de 13 mm de ancho, artículo 13 y apertura de 78/101 mm. Venta por unidad, paquete de 10 unidades.\ntipo: Americana a Cremallera\nfleje_ancho: 13 mm\narticulo: 13\napertura: 78/101 mm\nunidad_venta: c/u\npaquete_cantidad: 10",
  "text_length_char": 558,
  "text_length_tokens": 128,
  "metadata": {
    "pagina": 4,
    "codigo": "100020",
    "proveedor": "Ferretera del Norte",
    "marca": "CARBIZ",
    "categoria": "Abrazaderas",
    "subcategoria": "Abrazaderas de acero a cremallera",
    "articulo": "13",
    "apertura": "78/101 mm"
  }
}
```

---

## 5. Próximos Pasos en tu Arquitectura RAG
1.  **Validar Límites de Contexto:** Garantizar que los strings YAML concatenados queden por debajo del límite máximo del modelo de embeddings elegido (ej: `text-embedding-3-large` soporta 8191 tokens; tus 128 tokens promedio por producto están en una zona de seguridad perfecta).
2.  **Generación de Embeddings (Fase 2):** Consumir de forma paralela o por batches el campo `text_to_embed` de tu lista de nodos estructurados.
3.  **Configuración de Índices e Híbridos (Fase 3):** Diseñar las colecciones en tu base de datos vectorial para soportar la búsqueda densa por similitud de vectores sobre `text_to_embed`, acompañada del filtrado de metadatos tradicionales utilizando tu diccionario `metadata`.
