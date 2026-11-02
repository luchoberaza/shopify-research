"""Script de prueba del conector de CJ Dropshipping.

Recorre el flujo completo de extremo a extremo:
  1. Autentica (o usa el token cacheado).
  2. Lista categorías.
  3. Busca productos por una palabra clave.
  4. Pide el detalle del primero y lo normaliza.
  5. Consulta el stock real de su primera variante.

Uso:
    1. Copiá .env.example a .env y completá CJ_API_KEY.
    2. pip install -r requirements.txt
    3. python scripts/probar_cj.py [palabra_clave]
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Permitir importar config y src cuando se ejecuta desde la raíz del proyecto
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from src.connectors.cj_dropshipping import (
    CJAuthError,
    CJDropshippingClient,
    CJError,
)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if not (config.CJ_API_KEY or (config.CJ_EMAIL and config.CJ_PASSWORD)):
        print("\n[!] Falta configurar las credenciales de CJ.")
        print("    1) Copiá .env.example a .env")
        print("    2) Completá CJ_API_KEY (cjdropshipping.com -> My CJ -> Authorization -> API)")
        return 1

    palabra = sys.argv[1] if len(sys.argv) > 1 else "wireless"

    cliente = CJDropshippingClient(
        api_key=config.CJ_API_KEY,
        email=config.CJ_EMAIL,
        password=config.CJ_PASSWORD,
        qps=config.CJ_QPS,
        token_cache_path=config.CJ_TOKEN_CACHE,
    )

    # --- 1 y 2: autenticación + categorías ---------------------------------
    print("\n=== Categorías (primeras 5 de 3er nivel) ===")
    try:
        categorias = cliente.get_categories()
        impresas = 0
        for c1 in categorias:
            for c2 in c1.get("categoryFirstList", []):
                for c3 in c2.get("categorySecondList", []):
                    ruta = f"{c1.get('categoryFirstName')} > {c2.get('categorySecondName')} > {c3.get('categoryName')}"
                    print(f"  [{c3.get('categoryId')}] {ruta}")
                    impresas += 1
                    if impresas >= 5:
                        break
                if impresas >= 5:
                    break
            if impresas >= 5:
                break
    except CJAuthError as e:
        print(f"\n[!] Error de autenticación: {e}")
        print("    Revisá que CJ_API_KEY sea correcta y esté activa.")
        return 1
    except CJError as e:
        print(f"\n[!] Error consultando categorías: {e} (code={e.code})")
        return 1

    # --- 3: búsqueda de productos ------------------------------------------
    print(f"\n=== Búsqueda: '{palabra}' (primeros 5) ===")
    try:
        data = cliente.list_products(product_name=palabra, page_size=5)
        productos = data.get("list") or []
        if not productos:
            print("  (sin resultados para esa palabra)")
            return 0
        for p in productos:
            print(f"  [{p.get('pid')}] {p.get('productNameEn')}  ->  US$ {p.get('sellPrice')}  (listado en {p.get('listedNum')} tiendas)")
    except CJError as e:
        print(f"\n[!] Error en la búsqueda: {e} (code={e.code})")
        return 1

    # --- 4: detalle + normalización ----------------------------------------
    primer_pid = productos[0].get("pid")
    print(f"\n=== Detalle normalizado de {primer_pid} ===")
    try:
        detalle = cliente.get_product_detail(pid=primer_pid)
        prod = cliente.normalizar_producto(detalle)
        print(f"  Nombre:        {prod.name}")
        print(f"  Categoría:     {prod.category}")
        print(f"  Costo (desde): US$ {prod.cost_price}")
        print(f"  Sugerido:      US$ {prod.suggested_price}")
        print(f"  Peso:          {prod.weight_g} g")
        print(f"  Imágenes:      {len(prod.images)}")
        print(f"  Variantes:     {len(prod.variants)}")
        for var in prod.variants[:3]:
            print(f"    - {var.sku}  US$ {var.cost_price}  {var.weight_g} g  (vid={var.source_variant_id})")
    except CJError as e:
        print(f"\n[!] Error obteniendo el detalle: {e} (code={e.code})")
        return 1

    # --- 5: stock real de la primera variante ------------------------------
    if prod.variants:
        vid = prod.variants[0].source_variant_id
        print(f"\n=== Stock en tiempo real (vid={vid}) ===")
        try:
            almacenes = cliente.get_stock_by_vid(vid)
            if not almacenes:
                print("  (sin datos de stock)")
            for a in almacenes:
                print(f"    {a.get('countryCode')} / {a.get('areaEn')}: {a.get('totalInventoryNum')} u.")
        except CJError as e:
            print(f"\n[!] Error consultando stock: {e} (code={e.code})")

    print("\n[OK] Prueba completada. El conector de CJ funciona.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
