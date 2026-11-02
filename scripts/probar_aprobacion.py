"""Prueba de la lógica de aprobación (sin la parte interactiva).

Verifica: detección de pendientes, resumen, aprobar y rechazar con sus
transiciones de estado.

Uso:
    python scripts/probar_aprobacion.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from src.connectors.cj_dropshipping import NormalizedProduct
from src.database import Database, APPROVED, REJECTED, DRAFTED, ENRICHED, SCORED
from src import approval


def _producto_drafted(db: Database, source_id: str, nombre: str, score: float) -> int:
    """Inserta un producto y lo lleva hasta 'drafted' con una ficha mínima."""
    pid = db.upsert_product(NormalizedProduct(
        "cj", source_id, nombre, source_id, "", "Home", None, [], 100.0, 3.0, 9.0, 50, [],
    ))
    db.save_market_research(product_id=pid, source="mercadolibre", currency="UYU",
                            price_median=500, total_results=600)
    db.update_status(pid, ENRICHED)
    db.set_score(pid, score)
    db.update_status(pid, SCORED)
    db.upsert_listing(pid, {"title": f"Ficha de {nombre}", "body_html": "<p>desc</p>",
                            "tags": ["a"], "price": 590})
    db.update_status(pid, DRAFTED, note="ficha importada")
    return pid


def main() -> int:
    test_db = config.DATA_DIR / "shopify_test.db"
    if test_db.exists():
        test_db.unlink()

    with Database(path=test_db) as db:
        pid_a = _producto_drafted(db, "A1", "Producto bueno", 0.40)
        pid_b = _producto_drafted(db, "B1", "Producto dudoso", 0.20)

        print("=== Pendientes (ordenados por score) ===")
        items = approval.pendientes(db)
        for p in items:
            print(f"  [{p['id']}] score {p['score']:.2f}  {p['name']}")
        assert len(items) == 2
        assert items[0]["id"] == pid_a  # mayor score primero

        print("\n=== Resumen del primero ===")
        r = approval.resumen(db, pid_a)
        print(f"  título ficha: {r['listing']['title']}")
        print(f"  mercado: {r['research']['price_median']} {r['research']['currency']}")
        assert r["listing"]["title"].startswith("Ficha")

        print("\n=== Aprobar A, rechazar B ===")
        approval.aprobar(db, pid_a)
        approval.rechazar(db, pid_b, "margen ajustado")
        print(f"  A -> {db.get_product(pid_a)['status']}")
        print(f"  B -> {db.get_product(pid_b)['status']}")
        assert db.get_product(pid_a)["status"] == APPROVED
        assert db.get_product(pid_b)["status"] == REJECTED

        print("\n=== Ya no quedan pendientes ===")
        assert approval.pendientes(db) == []
        print("  pendientes: 0")

        # El historial deja la traza de la decisión
        hist = db.get_status_history(pid_b)
        assert hist[-1]["to_status"] == REJECTED and hist[-1]["note"] == "margen ajustado"

    print("\n[OK] El panel de aprobación (lógica) funciona.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
