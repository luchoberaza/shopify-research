"""Fichas de producto (listings) para Shopify, SIN depender de una IA por API.

Dos formas de llenar una ficha, ambas gratis y sin API key:

  1) A MANO: se genera una plantilla JSON con los datos reales del producto
     como referencia; la editás y la importás.

  2) CON IA por copy-paste: se genera un PROMPT listo para pegar en cualquier
     chat de IA (ChatGPT, Claude.ai, Gemini, etc.). La IA devuelve un JSON con
     el mismo formato, que después importás.

El formato de la ficha es idéntico en los dos casos, así que el resto del
sistema no necesita saber cómo la llenaste. Los campos están alineados con
Shopify para que publicar después sea directo.
"""
from __future__ import annotations

import json
from typing import Optional

# Campos de la ficha (alineados con Shopify)
CAMPOS = [
    "title", "body_html", "vendor", "product_type", "tags",
    "handle", "seo_title", "seo_description", "price", "compare_at_price",
]

SEO_TITLE_MAX = 60
SEO_DESC_MAX = 160


def _specs_producto(product: dict, variants: Optional[list[dict]] = None,
                    research: Optional[dict] = None) -> dict:
    """Datos REALES del producto, para usar como referencia (no inventar)."""
    variants = variants or []
    specs = {
        "nombre_original": product.get("name"),
        "categoria": product.get("category"),
        "peso_g": product.get("weight_g"),
        "costo_usd": product.get("cost_price"),
        "precio_sugerido_usd": product.get("suggested_price"),
        "variantes": [{"sku": v.get("sku"), "nombre": v.get("name")} for v in variants],
    }
    if research:
        specs["mercado"] = {
            "moneda": research.get("currency"),
            "precio_mediana": research.get("price_median"),
            "precio_min": research.get("price_min"),
            "precio_max": research.get("price_max"),
            "publicaciones_compitiendo": research.get("total_results"),
        }
    return specs


def plantilla_vacia(product: dict, variants: Optional[list[dict]] = None,
                    research: Optional[dict] = None) -> dict:
    """Ficha en blanco para llenar a mano. Incluye los datos del producto como
    referencia bajo '_referencia_no_editar' (el importador la ignora)."""
    return {
        "title": "",
        "body_html": "",
        "vendor": "",
        "product_type": product.get("category") or "",
        "tags": [],
        "handle": "",
        "seo_title": "",
        "seo_description": "",
        "price": (research or {}).get("price_median"),
        "compare_at_price": None,
        "source": "manual",
        "_referencia_no_editar": _specs_producto(product, variants, research),
    }


def prompt_para_ia(product: dict, variants: Optional[list[dict]] = None,
                   research: Optional[dict] = None, site_id: str = "MLU") -> str:
    """Prompt listo para pegar en cualquier chat de IA. Pide devolver SOLO JSON."""
    datos = _specs_producto(product, variants, research)
    esquema = {
        "title": "string (<=70 chars, en español, claro y atractivo)",
        "body_html": "string (HTML simple: <p>, <ul><li>; 2 a 4 párrafos)",
        "vendor": "string (marca o nombre de tienda; opcional)",
        "product_type": "string (tipo de producto)",
        "tags": ["string", "..."],
        "handle": "string (slug de URL en minúsculas con guiones)",
        "seo_title": f"string (<= {SEO_TITLE_MAX} caracteres)",
        "seo_description": f"string (<= {SEO_DESC_MAX} caracteres)",
        "price": "number (precio de venta sugerido, en moneda local)",
        "compare_at_price": "number o null (precio tachado, opcional)",
    }
    return (
        "Sos un copywriter experto en ecommerce. Generá una ficha de producto "
        f"para una tienda Shopify, en español neutro para el mercado de {site_id}.\n\n"
        "REGLAS IMPORTANTES:\n"
        "- NO inventes especificaciones (materiales, medidas, capacidad de batería, "
        "certificaciones, etc.) que NO estén en los datos. Si un dato no está, no lo afirmes.\n"
        "- Traducí y mejorá el nombre al español.\n"
        f"- 'seo_title' máximo {SEO_TITLE_MAX} caracteres; 'seo_description' máximo {SEO_DESC_MAX}.\n"
        "- Tono honesto, sin promesas exageradas.\n"
        "- Devolvé EXCLUSIVAMENTE un JSON válido con esta estructura exacta (sin texto adicional):\n\n"
        f"{json.dumps(esquema, ensure_ascii=False, indent=2)}\n\n"
        "DATOS REALES DEL PRODUCTO (usá solo esto, no inventes):\n"
        f"{json.dumps(datos, ensure_ascii=False, indent=2)}\n"
    )


def normalizar_ficha(ficha: dict) -> dict:
    """Deja la ficha solo con los campos válidos y tipos consistentes."""
    f = {k: ficha.get(k) for k in CAMPOS}

    tags = ficha.get("tags")
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    f["tags"] = tags or []

    for k in ("price", "compare_at_price"):
        v = f.get(k)
        if isinstance(v, str):
            try:
                f[k] = float(v.replace(",", "."))
            except ValueError:
                f[k] = None

    f["source"] = ficha.get("source", "manual")
    return f


def validar_ficha(ficha: dict) -> list[str]:
    """Devuelve la lista de errores (vacía si la ficha está bien)."""
    errores = []
    if not (ficha.get("title") or "").strip():
        errores.append("Falta 'title' (título).")
    if not (ficha.get("body_html") or "").strip():
        errores.append("Falta 'body_html' (descripción).")

    st = ficha.get("seo_title") or ""
    if len(st) > SEO_TITLE_MAX:
        errores.append(f"'seo_title' tiene {len(st)} caracteres (máx {SEO_TITLE_MAX}).")
    sd = ficha.get("seo_description") or ""
    if len(sd) > SEO_DESC_MAX:
        errores.append(f"'seo_description' tiene {len(sd)} caracteres (máx {SEO_DESC_MAX}).")

    price = ficha.get("price")
    if price is not None and (not isinstance(price, (int, float)) or price <= 0):
        errores.append("'price' debe ser un número mayor a 0 (o null).")
    return errores
