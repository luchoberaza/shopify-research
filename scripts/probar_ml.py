"""Script de prueba del conector de MercadoLibre (research).

Requiere haber corrido antes:  python scripts/bootstrap_ml.py

Recorre:
  1. Búsqueda por palabra clave -> precios + sold_quantity (demanda en baldes).
  2. Detalle del primer ítem.
  3. Predicción de categoría por título.
  4. total_items_in_this_category de la categoría (señal de saturación).

Uso:
    python scripts/probar_ml.py [palabra_clave]
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from src.connectors.mercadolibre import MercadoLibreClient, MLAuthError, MLError


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")

    if not (config.ML_CLIENT_ID and config.ML_CLIENT_SECRET):
        print("\n[!] Faltan ML_CLIENT_ID / ML_CLIENT_SECRET en el .env.")
        return 1
    if not config.ML_TOKEN_CACHE.exists():
        print("\n[!] No hay token guardado. Corré primero:  python scripts/bootstrap_ml.py")
        return 1

    palabra = sys.argv[1] if len(sys.argv) > 1 else "lampara led escritorio"

    cliente = MercadoLibreClient(
        client_id=config.ML_CLIENT_ID,
        client_secret=config.ML_CLIENT_SECRET,
        redirect_uri=config.ML_REDIRECT_URI,
        site_id=config.ML_SITE_ID,
        token_cache_path=config.ML_TOKEN_CACHE,
    )

    # --- 1: búsqueda --------------------------------------------------------
    print(f"\n=== Búsqueda en {config.ML_SITE_ID}: '{palabra}' (primeros 5) ===")
    try:
        data = cliente.search(query=palabra, limit=5, sort="relevance")
    except MLAuthError as e:
        print(f"\n[!] Error de autenticación: {e}")
        print("    Volvé a correr el bootstrap: python scripts/bootstrap_ml.py")
        return 1
    except MLError as e:
        print(f"\n[!] Error en la búsqueda: {e} (status={e.status})")
        return 1

    results = data.get("results") or []
    total = (data.get("paging") or {}).get("total")
    print(f"  Total de publicaciones que matchean: {total}")
    if not results:
        print("  (sin resultados)")
        return 0
    for r in results:
        lst = cliente.normalizar_listing(r)
        vendidos = "?" if lst.sold_quantity is None else f">={lst.sold_quantity}"
        print(f"  [{lst.source_id}] {lst.title[:60]}")
        print(f"        {lst.currency} {lst.price}  | vendidos: {vendidos}  | {lst.condition}")

    primero = cliente.normalizar_listing(results[0])

    # --- 2: detalle ---------------------------------------------------------
    print(f"\n=== Detalle de {primero.source_id} ===")
    try:
        item = cliente.get_item(primero.source_id)
        print(f"  Título:     {item.get('title')}")
        print(f"  Precio:     {item.get('currency_id')} {item.get('price')}")
        print(f"  Vendidos:   >= {item.get('sold_quantity')} (referencial / en baldes)")
        print(f"  Disponible: {item.get('available_quantity')}")
        print(f"  Categoría:  {item.get('category_id')}")
    except MLError as e:
        print(f"\n[!] Error obteniendo el detalle: {e} (status={e.status})")
        item = {}

    # --- 3: predicción de categoría ----------------------------------------
    print(f"\n=== Categoría predicha para el título ===")
    try:
        pred = cliente.predict_category(primero.title, limit=1)
        if pred:
            p = pred[0]
            print(f"  {p.get('category_id')} - {p.get('category_name')}  (dominio: {p.get('domain_name')})")
    except MLError as e:
        print(f"\n[!] Error en el predictor: {e}")

    # --- 4: saturación de la categoría -------------------------------------
    cat_id = item.get("category_id") or primero.category_id
    if cat_id:
        print(f"\n=== Saturación de la categoría {cat_id} ===")
        try:
            cat = cliente.get_category(cat_id)
            print(f"  {cat.get('name')}: {cat.get('total_items_in_this_category')} publicaciones en total")
        except MLError as e:
            print(f"\n[!] Error obteniendo la categoría: {e}")

    print("\n[OK] Prueba completada. El conector de MercadoLibre funciona.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
