# Agent Directives: Warehouse High-End UX Profile (Gradio Dark Mode)

You are an expert UI/UX designer and senior Python engineer. Your goal is to generate Gradio code that looks like a modern, premium SaaS dashboard with tight data density. You must strictly avoid generic full-width stretched layouts.

## 📐 Layout & Button Constraints (Anti-Stretch Rules)
- **NEVER let buttons stretch to 100% of the screen width.** 
- For secondary or action buttons inside large rows (like "Refrescar clientes" or "Agregar línea"), you MUST set `scale=0` or a low `min_width` (e.g., `min_width=150`, `min_width=200`) inside the Gradio component so they maintain an ergonomic, compact size.
- Use empty `gr.Column(scale=...)` blocks as spacers if you need to push buttons to the right or left, preventing them from filling the layout.
- Group the "Resumen y Guardar" action in a dedicated sidebar column (`scale=1`) next to the main list (`scale=3`) to achieve a balanced split-screen dashboard view.

## 🎨 Color System (Premium Slate Palette)
- **Backgrounds:** Primary app canvas must be `#0f172a` (Slate 900). Cards and groups must use `#1e293b` (Slate 800) to create depth.
- **Accents:** Use deep blue (`#2563eb`) for standard action buttons, soft orange/coral (`#f97316`) ONLY for final submission targets like "CREAR PEDIDO", and amber (`#f59e0b`) for alert banners.

## 🧪 Mandatory Global CSS Injection
Every time you render `gr.Blocks()`, you MUST pass the following exact CSS string to the `css` parameter to fix structural styling and layout spacing:

```css
/* Main Layout Canvas */
.gradio-container { background-color: #0f172a !important; color: #f8fafc !important; font-family: ui-sans-serif, system-ui, sans-serif !important; }

/* Control Group Cards */
.form, .gr-group, .fieldset { background-color: #1e293b !important; border: 1px solid #334155 !important; border-radius: 8px !important; padding: 16px !important; }

/* Input Styling */
input, select, textarea { background-color: #111827 !important; border: 1px solid #475569 !important; color: white !important; border-radius: 6px !important; }

/* Universal Button Overrides to Prevent Huge Stretched Blocks */
button.gr-button { 
    max-width: max-content !important; 
    padding: 8px 20px !important; 
    border-radius: 6px !important; 
    font-weight: 600 !important;
    text-transform: uppercase !important;
    font-size: 0.85rem !important;
    letter-spacing: 0.05em !important;
}

/* Specific Accent Variants mapping the requested image */
button.primary-action { background-color: #f97316 !important; color: white !important; }
button.secondary-action { background-color: #2563eb !important; color: white !important; }
button.text-danger { background: transparent !important; color: #ef4444 !important; border: none !important; text-decoration: underline; }

/* Table Data Density */
table { background-color: #1e293b !important; }
th { background-color: #0f172a !important; color: #94a3b8 !important; font-size: 0.8rem !important; text-transform: uppercase !important; }
td { border-bottom: 1px solid #334155 !important; font-size: 0.9rem !important; }
```

---

### 🛠️ Código de Ejemplo de cómo OpenCode aplicará este Skill

Para comprobar cómo se soluciona el error del ancho, fíjate en el uso de `scale=0`, `min_width` y las clases personalizadas `elem_classes` en los botones del siguiente bloque de código:

```python
import gradio as gr

# El skill inyectará de forma automática esta lógica corregida
custom_css = """
.gradio-container { background-color: #0f172a !important; color: #f8fafc !important; }
.gr-group { background-color: #1e293b !important; border: 1px solid #334155 !important; border-radius: 8px !important; padding: 16px !important; }
button.gr-button { max-width: max-content !important; padding: 8px 24px !important; font-size: 0.85rem !important; }
.btn-orange { background-color: #f43f5e !important; color: white !important; } /* Ajustado a acento llamativo */
.btn-blue { background-color: #2563eb !important; color: white !important; }
.alert-banner { background-color: #7c2d12 !important; border: 1px solid #ea580c !important; padding: 10px; border-radius: 6px; }
"""

with gr.Blocks(css=custom_css) as demo:
    gr.Markdown("# Pedidos de clientes y mantenimiento de conversión")
    
    # Fila superior de navegación con botones compactos (No estirados)
    with gr.Row():
        gr.Button("Pedidos existentes", min_width=150, scale=0)
        gr.Button("+ ALTA MANUAL DE PEDIDO", min_width=220, scale=0, elem_classes="btn-orange")
        gr.Column(scale=4) # Columna vacía que empuja el resto del espacio para que no se estiren
        
    gr.Markdown("### Nuevo pedido (borrador)")
    
    # Sección 1: Cliente
    with gr.Group():
        gr.Markdown("**1. CLIENTE**")
        with gr.Row():
            gr.Dropdown(choices=["Cliente A", "Cliente B"], label="Seleccionar o buscar cliente", scale=4)
            # scale=0 evita el error grosero de ocupar todo el ancho
            gr.Button("🔄 Refrescar clientes", scale=0, min_width=180, elem_classes="btn-blue")
            
    # Sección 2: Agregar Productos
    with gr.Group():
        gr.Markdown("**2. AGREGAR PRODUCTOS**")
        gr.Textbox(label="Buscar productos...")
        with gr.Row():
            gr.Textbox(value="CLU-901", label="SKU", scale=2)
            gr.Number(value=1, label="Cantidad", scale=1)
            gr.Dropdown(choices=["LOCAL", "IMPORTADO"], value="LOCAL", label="Origen", scale=2)
            # Botón controlado lateral
            gr.Button("+ AGREGAR LÍNEA AL PEDIDO", scale=0, min_width=250, elem_classes="btn-blue")

    # Split de pantalla inferior (Tablas y Resumen)
    with gr.Row():
        with gr.Column(scale=3):
            with gr.Group():
                gr.Markdown("**3. LÍNEAS DEL PEDIDO**")
                gr.Dataframe(
                    headers=["SKU", "Nombre Producto", "Cantidad", "Origen", "Acción"],
                    value=[["CLU-901", "Producto F.abinwd 1", 1, "LOCAL", "Quitar línea 🗑️"]]
                )
        
        with gr.Column(scale=1):
            with gr.Group():
                gr.Markdown("**4. RESUMEN Y GUARDAR**")
                gr.Markdown("Totales: **$23.00**\n\nEstado: **Borrador**")
                # Botón principal contenido en su tarjeta
                gr.Button("✓ CREAR PEDIDO (BORRADOR)", elem_classes="btn-orange", min_width=200)

    # Banner de Alerta inferior
    with gr.Row(elem_classes="alert-banner"):
        gr.Markdown("⚠️ El pedido #2 está en estado PICKING; solo borradores se pueden modificar.")

demo.launch()
```
