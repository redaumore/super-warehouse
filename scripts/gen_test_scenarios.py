#!/usr/bin/env python3
"""Generate ``docs/escenarios-testeados.md`` from test docstrings.

Convention (living documentation):

    Cada función ``test_*`` lleva como PRIMERA línea de su docstring una
    descripción humana en una frase. Esa línea es la fuente de verdad del
    documento. El generador recorre ``tests/test_*.py`` con el AST, agrupa los
    escenarios por dominio y renderiza el Markdown.

Usage:

    python scripts/gen_test_scenarios.py            # regenera el doc
    python scripts/gen_test_scenarios.py --check    # falla si el doc está desactualizado

Se integra con ``make test-docs`` y ``make check-test-docs``.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "tests"
OUT_DOC = ROOT / "docs" / "escenarios-testeados.md"

# Mapa ordenado: módulo de test -> título de dominio legible para humanos.
DOMAINS: dict[str, str] = {
    "test_pricing": "Motor de precios",
    "test_sales": "Cotización y ventas",
    "test_inventory": "Stock e inventario",
    "test_dispatch": "Despacho y aprobación del dueño",
    "test_approval": "Registro de aprobaciones",
    "test_orchestrator": "Orquestador y enrutamiento",
    "test_pipeline": "Pipeline de orquestación (walking skeleton)",
    "test_customer": "Agente Customer (respondedor conversacional)",
    "test_order_lifecycle": "Ciclo de vida del pedido",
    "test_rag": "Integración con RAG de catálogo de proveedores",
    "test_product_search": "Búsqueda de producto (precedencia local → RAG)",
    "test_perception": "Percepción (voz e imagen)",
    "test_openai": "Integración con OpenAI",
    "test_search": "Búsqueda en catálogo",
    "test_sweeper": "Vencimiento de reservas (scheduler)",
    "test_channels": "Canales de entrada (Telegram/WhatsApp)",
    "test_whatsapp": "Canal WhatsApp Cloud API",
    "test_webhook": "Webhook de entrada",
    "test_intake": "Intake y trabajo en background",
    "test_db_models": "Modelo de datos y migraciones",
    "test_phone": "Teléfonos y clientes",
    "test_sheets": "Registro en Google Sheets",
    "test_barcode": "Códigos de barras",
    "test_ocr": "OCR de documentos de proveedor",
    "test_backoffice": "Backoffice (catálogo, clientes, monitor, ingesta)",
    "test_features": "Feature flags por fase",
    "test_e2e_order": "E2E: pedido completo",
    "test_e2e_ingestion": "E2E: ingesta de documentos",
}


def _parametrize_case_labels(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Extrae etiquetas legibles de los casos de un test parametrizado.

    Prioriza el argumento ``ids`` explícito; si no existe, deriva las etiquetas
    de los valores que sean cadenas (los casos puramente numéricos se omiten).
    """

    def labels_from_values(node: ast.AST) -> list[str]:
        if not isinstance(node, (ast.List, ast.Tuple)):
            return []
        labels: list[str] = []
        for case in node.elts:
            items = case.elts if isinstance(case, (ast.Tuple, ast.List)) else [case]
            parts = [
                it.value if it.value.strip() else "(vacío)"
                for it in items
                if isinstance(it, ast.Constant) and isinstance(it.value, str)
            ]
            if parts:
                labels.append(" / ".join(parts))
        return labels

    for deco in func.decorator_list:
        if not (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute)):
            continue
        if deco.func.attr != "parametrize":
            continue
        if len(deco.args) >= 3:
            ids_node = deco.args[2]
            if isinstance(ids_node, (ast.List, ast.Tuple)):
                ids = [
                    elt.value
                    for elt in ids_node.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                ]
                if ids:
                    return ids
        if len(deco.args) >= 2:
            derived = labels_from_values(deco.args[1])
            if derived:
                return derived
    return []


def _humanize(name: str) -> str:
    """Fallback mínimo: convierte ``snake_case`` en una frase legible."""
    stem = name.removeprefix("test_")
    text = " ".join(part for part in stem.split("_") if part)
    return text[0].upper() + text[1:] if text else name


def _collect() -> tuple[list[dict], list[str]]:
    domains: list[dict] = []
    gaps: list[str] = []
    for module, title in DOMAINS.items():
        path = TESTS_DIR / f"{module}.py"
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scenarios: list[dict] = []
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            doc = ast.get_docstring(node, clean=True)
            desc = doc.splitlines()[0].strip() if doc else None
            scenarios.append(
                {
                    "name": node.name,
                    "desc": desc,
                    "cases": _parametrize_case_labels(node),
                }
            )
            if not desc:
                gaps.append(f"{module}.py::{node.name}")
        domains.append({"module": module, "title": title, "scenarios": scenarios})
    return domains, gaps


def _render(domains: list[dict], gaps: list[str]) -> str:
    total = sum(len(d["scenarios"]) for d in domains)
    lines: list[str] = []
    lines.append("# Escenarios testeados")
    lines.append("")
    lines.append(
        "Documento generado automáticamente desde los docstrings de los tests. "
        "No lo edites a mano: si un escenario cambia, actualizá la primera línea "
        "del docstring del test y volvé a correr `make test-docs`."
    )
    lines.append("")
    lines.append(f"**Total de escenarios:** {total}, agrupados en {len(domains)} dominios.")
    lines.append("")
    lines.append(
        "> Cada ítem lista el comportamiento que se valida en lenguaje "
        "natural, seguido (entre paréntesis) del nombre técnico del test."
    )
    lines.append("")
    lines.append("## Índice")
    lines.append("")
    for d in domains:
        count = len(d["scenarios"])
        lines.append(f"- [{d['title']}](#{_anchor(d['title'])}) — {count}")
    lines.append("")

    for d in domains:
        lines.append(f"## {d['title']}")
        lines.append("")
        for s in d["scenarios"]:
            desc = s["desc"] if s["desc"] else f"⚠️ Sin descripción — {_humanize(s['name'])}"
            lines.append(f"- {desc} _(`{s['name']}`)_")
            for case in s["cases"]:
                lines.append(f"  - {case}")
        lines.append("")

    if gaps:
        lines.append("## ⚠️ Escenarios sin descripción humana")
        lines.append("")
        lines.append(
            "Los siguientes tests aún no tienen la primera línea del docstring "
            "en lenguaje natural. Agregala y volvé a generar el documento:"
        )
        lines.append("")
        for gap in gaps:
            lines.append(f"- `{gap}`")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _anchor(title: str) -> str:
    """Convierte un título en el ancla Markdown de GitHub."""
    slug = "".join(c.lower() if c.isalnum() else "-" for c in title)
    return "-".join(part for part in slug.split("-") if part)


def main() -> int:
    check = "--check" in sys.argv[1:]
    domains, gaps = _collect()
    rendered = _render(domains, gaps)

    if check:
        current = OUT_DOC.read_text(encoding="utf-8") if OUT_DOC.exists() else ""
        if current != rendered:
            print(
                "docs/escenarios-testeados.md está desactualizado. Corré `make test-docs`.",
                file=sys.stderr,
            )
            return 1
        print("docs/escenarios-testeados.md está al día.")
        return 0

    OUT_DOC.parent.mkdir(parents=True, exist_ok=True)
    OUT_DOC.write_text(rendered, encoding="utf-8")
    total = sum(len(d["scenarios"]) for d in domains)
    print(
        f"Generado {OUT_DOC.relative_to(ROOT)} ({total} escenarios"
        + (f", {len(gaps)} sin descripción)" if gaps else "")
        + "."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
