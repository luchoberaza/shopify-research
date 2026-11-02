"""Seguimiento de ventas: lee pedidos de Shopify y agrega métricas por producto.

Cierra el loop del sistema: vincula las ventas reales con los productos que
publicamos (matcheando por shopify_product_id) y guarda unidades vendidas,
ingresos y cantidad de pedidos. Con esto después se puede medir conversión y,
más adelante, recalibrar el scoring con datos reales.

Usa montos netos: currentQuantity (excluye devoluciones) y discountedTotalSet
(monto con descuentos) por línea. Ignora pedidos de prueba y cancelados.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Optional

from src.database import Database

logger = logging.getLogger(__name__)


def _monto(money_set: Optional[dict]) -> float:
    try:
        return float(((money_set or {}).get("shopMoney") or {}).get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def _moneda(money_set: Optional[dict]) -> Optional[str]:
    return ((money_set or {}).get("shopMoney") or {}).get("currencyCode")


def sync_sales(db: Database, shopify, query_filter: Optional[str] = None,
               incluir_test: bool = False) -> dict:
    """Lee pedidos de Shopify y guarda métricas agregadas por producto.

    Devuelve un resumen: pedidos leídos, productos actualizados y line items
    cuyo producto no está en nuestra base (sin_match).
    """
    units: dict[str, int] = defaultdict(int)
    revenue: dict[str, float] = defaultdict(float)
    pedidos: dict[str, set] = defaultdict(set)
    moneda: dict[str, str] = {}
    ultimo: dict[str, str] = {}
    pedidos_leidos = 0

    for order in shopify.iter_orders(query_filter=query_filter):
        if order.get("test") and not incluir_test:
            continue
        if order.get("cancelledAt"):
            continue
        pedidos_leidos += 1
        oid = order.get("id")
        procesado = order.get("processedAt")

        for li in ((order.get("lineItems") or {}).get("nodes") or []):
            prod = li.get("product") or (li.get("variant") or {}).get("product") or {}
            gid = prod.get("id")
            if not gid:
                continue  # producto borrado u oferta sin producto
            qty = li.get("currentQuantity")
            if qty is None:
                qty = li.get("quantity") or 0
            units[gid] += qty
            revenue[gid] += _monto(li.get("discountedTotalSet"))
            pedidos[gid].add(oid)
            cur = _moneda(li.get("discountedTotalSet"))
            if cur:
                moneda[gid] = cur
            if procesado and (gid not in ultimo or procesado > ultimo[gid]):
                ultimo[gid] = procesado

    actualizados = sin_match = 0
    for gid, u in units.items():
        pid = db.find_by_shopify_id(gid)
        if pid is None:
            sin_match += 1
            continue
        db.upsert_metrics(
            pid, units_sold=u, revenue=round(revenue[gid], 2),
            orders_count=len(pedidos[gid]), currency=moneda.get(gid),
            last_order_at=ultimo.get(gid),
        )
        actualizados += 1

    return {
        "pedidos_leidos": pedidos_leidos,
        "productos_actualizados": actualizados,
        "productos_sin_match": sin_match,
    }
