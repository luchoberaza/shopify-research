"""Sincroniza las ventas reales desde Shopify y muestra el ranking por ingresos.

Lee los pedidos de los últimos N días (por defecto 60, el máximo de read_orders)
y guarda métricas por producto: unidades vendidas, ingresos y nº de pedidos.

Uso:
    python scripts/sync_ventas.py [dias]

Requisitos: SHOPIFY_SHOP_DOMAIN + SHOPIFY_ACCESS_TOKEN en .env, con scope read_orders.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from src.connectors.shopify import ShopifyClient, ShopifyError
from src.database import Database
from src import analytics


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")

    if not (config.SHOPIFY_SHOP_DOMAIN and config.SHOPIFY_ACCESS_TOKEN):
        print("\n[!] Faltan SHOPIFY_SHOP_DOMAIN / SHOPIFY_ACCESS_TOKEN en el .env.")
        return 1

    dias = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    desde = (datetime.now(timezone.utc) - timedelta(days=dias)).strftime("%Y-%m-%d")
    query_filter = f"processed_at:>={desde} test:false"

    client = ShopifyClient(config.SHOPIFY_SHOP_DOMAIN, config.SHOPIFY_ACCESS_TOKEN, config.SHOPIFY_API_VERSION)

    with Database() as db:
        print(f"Leyendo ventas desde {desde} ...")
        try:
            stats = analytics.sync_sales(db, client, query_filter=query_filter)
        except ShopifyError as e:
            print(f"\n[!] Error leyendo pedidos: {e}")
            print("    Verificá que la app tenga el scope read_orders.")
            return 1

        print("\n=== RESUMEN ===")
        for k, v in stats.items():
            print(f"  {k:24s}: {v}")

        top = db.top_by_revenue(limit=20)
        if top:
            print("\n=== TOP POR INGRESOS ===")
            for t in top:
                print(f"  {t['revenue']:.2f} {t['currency'] or ''}  | {t['units_sold']} u. en "
                      f"{t['orders_count']} pedido(s)  | {t['name']}")
        else:
            print("\n(todavía no hay ventas registradas para productos de la base)")

    print("\n[OK] Sincronización de ventas terminada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
