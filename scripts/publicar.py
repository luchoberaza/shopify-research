"""Publica en Shopify los productos APROBADOS, como BORRADOR (status DRAFT).

Los productos quedan en tu admin de Shopify SIN estar a la venta, para que los
revises antes de activarlos. Es la última etapa del flujo human-in-the-loop.

Uso:
    python scripts/publicar.py            # crea borradores (no publica al canal)
    python scripts/publicar.py --publicar # además publica en el canal Online Store

Requisitos: SHOPIFY_SHOP_DOMAIN y SHOPIFY_ACCESS_TOKEN en .env.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from src.connectors.shopify import ShopifyClient, ShopifyError
from src.database import Database, APPROVED, PUBLISHED


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")

    if not (config.SHOPIFY_SHOP_DOMAIN and config.SHOPIFY_ACCESS_TOKEN):
        print("\n[!] Faltan SHOPIFY_SHOP_DOMAIN / SHOPIFY_ACCESS_TOKEN en el .env.")
        print("    En tu tienda: Settings -> Apps and sales channels -> Develop apps.")
        return 1

    publicar_canal = "--publicar" in sys.argv

    client = ShopifyClient(config.SHOPIFY_SHOP_DOMAIN, config.SHOPIFY_ACCESS_TOKEN, config.SHOPIFY_API_VERSION)

    with Database() as db:
        aprobados = db.products_by_status(APPROVED, limit=100)
        if not aprobados:
            print("No hay productos aprobados para publicar. Aprobá con: python scripts/aprobar.py")
            return 0

        modo = "BORRADOR + publicado en Online Store" if publicar_canal else "BORRADOR (sin publicar)"
        print(f"{len(aprobados)} producto(s) aprobado(s). Creando como {modo}...\n")

        for p in aprobados:
            ficha = db.get_listing(p["id"])
            if not ficha:
                print(f"  [!] {p['name']} no tiene ficha; lo salto.")
                continue
            images = (p.get("images") or ([p["main_image"]] if p.get("main_image") else []))[:6]
            try:
                res = client.publish_listing(
                    ficha, status="DRAFT", images=images, publish_online_store=publicar_canal,
                )
            except ShopifyError as e:
                print(f"  [X] {ficha['title']}: {e}")
                continue
            db.set_shopify_ids(p["id"], res["id"], res.get("handle"))
            db.update_status(p["id"], PUBLISHED, note=f"shopify {res['id']} ({res['status']})")
            print(f"  [OK] {ficha['title']}  ->  {res['id']}  (status {res['status']})")

    print("\nListo. Los productos están en tu admin de Shopify como BORRADOR.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
