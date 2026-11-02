"""Traducción EN->ES para mejorar el research en MercadoLibre.

Los nombres de productos de CJ vienen en inglés y MercadoLibre es en español:
buscar 'wireless charger' rinde menos que 'cargador inalámbrico'. Este módulo
traduce con un glosario simple (gratis, sin API, sin dependencias). No es
traducción perfecta: reemplaza términos conocidos palabra por palabra y, si no
reconoce nada, devuelve el nombre original (mejor que no buscar).

Se puede ampliar el glosario editando data/glosario.json (un objeto JSON
{"palabra_en": "palabra_es", ...}); se combina con el glosario por defecto.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Optional

import config

GLOSARIO_DEFAULT = {
    "wireless": "inalámbrico", "charger": "cargador", "cable": "cable",
    "organizer": "organizador", "holder": "soporte", "stand": "soporte",
    "kitchen": "cocina", "bottle": "botella", "water": "agua",
    "led": "led", "lamp": "lámpara", "light": "luz", "desk": "escritorio",
    "office": "oficina", "phone": "teléfono", "case": "funda",
    "headphones": "auriculares", "earphones": "auriculares", "earbuds": "auriculares",
    "speaker": "parlante", "watch": "reloj", "smart": "inteligente",
    "bag": "bolso", "backpack": "mochila", "wallet": "billetera",
    "toy": "juguete", "toys": "juguetes", "pet": "mascota", "dog": "perro",
    "cat": "gato", "car": "auto", "kids": "niños", "baby": "bebé",
    "women": "mujer", "woman": "mujer", "men": "hombre", "man": "hombre",
    "shoes": "zapatos", "fitness": "fitness", "gym": "gimnasio",
    "massage": "masajeador", "massager": "masajeador", "cleaner": "limpiador",
    "brush": "cepillo", "mini": "mini", "portable": "portátil",
    "rechargeable": "recargable", "magnetic": "magnético", "set": "set",
    "kit": "kit", "garden": "jardín", "tool": "herramienta", "tools": "herramientas",
    "knife": "cuchillo", "cup": "taza", "mug": "taza", "storage": "organizador",
    "box": "caja", "camera": "cámara", "keyboard": "teclado", "mouse": "mouse",
    "usb": "usb", "bluetooth": "bluetooth", "smartwatch": "smartwatch",
    "waterproof": "impermeable", "foldable": "plegable", "adjustable": "ajustable",
    "electric": "eléctrico", "digital": "digital", "wood": "madera",
    "leather": "cuero", "silicone": "silicona", "steel": "acero",
}


def cargar_glosario() -> dict:
    """Glosario por defecto + lo que haya en data/glosario.json (si existe)."""
    glos = dict(GLOSARIO_DEFAULT)
    archivo = config.DATA_DIR / "glosario.json"
    if archivo.exists():
        try:
            extra = json.loads(archivo.read_text(encoding="utf-8"))
            glos.update({str(k).lower(): str(v) for k, v in extra.items()})
        except (OSError, ValueError):
            pass
    return glos


def traducir(texto: str, glosario: Optional[dict] = None) -> str:
    """Reemplaza palabra por palabra según el glosario, conservando separadores.
    Si no reconoce ninguna palabra, devuelve el texto original."""
    glosario = glosario if glosario is not None else cargar_glosario()
    tokens = re.findall(r"\w+|\W+", texto or "")
    salida, hubo = [], False
    for tok in tokens:
        clave = tok.lower()
        if clave in glosario:
            salida.append(glosario[clave])
            hubo = True
        else:
            salida.append(tok)
    resultado = "".join(salida).strip()
    return resultado if hubo else (texto or "")


def query_fn_glosario(glosario: Optional[dict] = None) -> Callable[[dict], str]:
    """Devuelve una función product->query para usar en el pipeline (enrich_with_ml)."""
    glosario = glosario if glosario is not None else cargar_glosario()

    def fn(product: dict) -> str:
        return traducir(product.get("name") or "", glosario)

    return fn
