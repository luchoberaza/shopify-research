"""Panel de aprobación (human-in-the-loop), sin dependencias externas.

Lógica para revisar los productos que ya tienen ficha (estado 'drafted' o
'pending_review') y decidir si se aprueban (para publicar) o se rechazan.
La interfaz por consola está en scripts/aprobar.py; acá va la lógica para que
sea reutilizable y testeable.
"""
from __future__ import annotations

from typing import Optional

from src.database import Database, APPROVED, REJECTED, PENDING_REVIEW, DRAFTED

# Productos "pendientes de decisión humana"
ESTADOS_PENDIENTES = (DRAFTED, PENDING_REVIEW)


def pendientes(db: Database, limit: int = 100) -> list[dict]:
    """Productos esperando aprobación, ordenados por score descendente."""
    items: list[dict] = []
    for estado in ESTADOS_PENDIENTES:
        items.extend(db.products_by_status(estado, limit=limit))
    items.sort(key=lambda p: (p.get("score") is not None, p.get("score") or 0.0), reverse=True)
    return items


def resumen(db: Database, product_id: int) -> Optional[dict]:
    """Todo lo necesario para decidir: producto + ficha + research de mercado."""
    p = db.get_product(product_id)
    if not p:
        return None
    mr = db.get_market_research(product_id)
    return {
        "product": p,
        "listing": db.get_listing(product_id),
        "research": mr[0] if mr else None,
    }


def aprobar(db: Database, product_id: int, note: Optional[str] = None) -> None:
    db.update_status(product_id, APPROVED, note=note or "aprobado en panel")


def rechazar(db: Database, product_id: int, note: Optional[str] = None) -> None:
    db.update_status(product_id, REJECTED, note=note or "rechazado en panel")


def enviar_a_revision(db: Database, product_id: int, note: Optional[str] = None) -> None:
    """Mueve un 'drafted' a 'pending_review' (útil para encolar sin aprobar aún)."""
    db.update_status(product_id, PENDING_REVIEW, note=note or "enviado a revisión")
