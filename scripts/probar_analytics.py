"""Prueba del seguimiento de ventas (sin red).

Inserta productos con su shopify_product_id, simula pedidos con un FakeShopify
y verifica que las métricas se agreguen y guarden bien (unidades, ingresos,
pedidos), ignorando test/cancelados y line items sin match.

Uso:
    python scripts/probar_analytics.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from src.connectors.cj_dropshipping import NormalizedProduct
from src.database import Database
from src import analytics


def _money(amount, cur="UYU"):
    return {"shopMoney": {"amount": str(amount), "currencyCode": cur}}


class FakeShopify:
    """Imita ShopifyClient.iter_orders con pedidos canned."""
    ORDERS = [
        {  # válido: producto 111 (x2) y 222 (x1)
            "id": "gid://shopify/Order/1", "processedAt": "2026-06-01T10:00:00Z",
            "test": False, "cancelledAt": None,
            "lineItems": {"nodes": [
                {"currentQuantity": 2, "quantity": 2, "product": {"id": "gid://shopify/Product/111"},
                 "discountedTotalSet": _money(900)},
                {"currentQuantity": 1, "quantity": 1, "product": {"id": "gid://shopify/Product/222"},
                 "discountedTotalSet": _money(700)},
            ]},
        },
        {  # válido: producto 111 otra vez (x3) + uno sin match (999)
            "id": "gid://shopify/Order/2", "processedAt": "2026-06-03T12:00:00Z",
            "test": False, "cancelledAt": None,
            "lineItems": {"nodes": [
                {"currentQuantity": 3, "quantity": 3, "product": {"id": "gid://shopify/Product/111"},
                 "discountedTotalSet": _money(1350)},
                {"currentQuantity": 1, "quantity": 1, "product": {"id": "gid://shopify/Product/999"},
                 "discountedTotalSet": _money(500)},
            ]},
        },
        {  # debe ignorarse: pedido de prueba
            "id": "gid://shopify/Order/3", "processedAt": "2026-06-04T12:00:00Z",
            "test": True, "cancelledAt": None,
            "lineItems": {"nodes": [
                {"currentQuantity": 9, "quantity": 9, "product": {"id": "gid://shopify/Product/111"},
                 "discountedTotalSet": _money(9999)},
            ]},
        },
        {  # debe ignorarse: cancelado
            "id": "gid://shopify/Order/4", "processedAt": "2026-06-05T12:00:00Z",
            "test": False, "cancelledAt": "2026-06-06T00:00:00Z",
            "lineItems": {"nodes": [
                {"currentQuantity": 5, "quantity": 5, "product": {"id": "gid://shopify/Product/222"},
                 "discountedTotalSet": _money(3500)},
            ]},
        },
    ]

    def iter_orders(self, query_filter=None, **kw):
        yield from self.ORDERS


def main() -> int:
    test_db = config.DATA_DIR / "shopify_test.db"
    if test_db.exists():
        test_db.unlink()

    with Database(path=test_db) as db:
        pid1 = db.upsert_product(NormalizedProduct("cj", "A", "Producto 111", "A", "", "X", None, [], 100.0, 3.0, 9.0, 1, []))
        pid2 = db.upsert_product(NormalizedProduct("cj", "B", "Producto 222", "B", "", "X", None, [], 100.0, 3.0, 9.0, 1, []))
        db.set_shopify_ids(pid1, "gid://shopify/Product/111", "producto-111")
        db.set_shopify_ids(pid2, "gid://shopify/Product/222", "producto-222")

        print("=== Sincronizando ventas (FakeShopify) ===")
        stats = analytics.sync_sales(db, FakeShopify())
        for k, v in stats.items():
            print(f"  {k}: {v}")

        m1 = db.get_metrics(pid1)
        m2 = db.get_metrics(pid2)
        print("\n=== Métricas guardadas ===")
        print(f"  Producto 111: {m1['units_sold']} u, {m1['revenue']} {m1['currency']}, {m1['orders_count']} pedidos")
        print(f"  Producto 222: {m2['units_sold']} u, {m2['revenue']} {m2['currency']}, {m2['orders_count']} pedidos")

        # Verificaciones (ignora test y cancelado; suma neto)
        assert stats["pedidos_leidos"] == 2, stats          # 2 válidos (test y cancelado fuera)
        assert stats["productos_sin_match"] == 1, stats     # el 999
        assert m1["units_sold"] == 5 and m1["revenue"] == 2250.0, m1   # 2+3 u, 900+1350
        assert m1["orders_count"] == 2, m1
        assert m2["units_sold"] == 1 and m2["revenue"] == 700.0, m2
        assert m1["last_order_at"] == "2026-06-03T12:00:00Z", m1       # el más reciente

        print("\n=== Top por ingresos ===")
        for t in db.top_by_revenue():
            print(f"  {t['revenue']} {t['currency']}  {t['name']}")

    print("\n[OK] El seguimiento de ventas funciona (agrega, filtra test/cancelados, matchea por shopify_id).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
