"""Prueba end-to-end de la base de datos (con datos simulados, sin APIs).

Verifica: creación del esquema, upsert de producto+variantes, idempotencia,
guardado de research de mercado, máquina de estados (incluida una transición
inválida) y consultas.

Uso:
    python scripts/probar_db.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from src.connectors.cj_dropshipping import NormalizedProduct, NormalizedVariant
from src.database import Database, TransitionError, ENRICHED, SCORED, LIVE


def producto_mock() -> NormalizedProduct:
    return NormalizedProduct(
        source="cj",
        source_id="DEMO-PID-001",
        name="Lampara LED de escritorio plegable",
        sku="CJ-LAMP-001",
        description="Lampara LED regulable con carga USB.",
        category="Home & Garden",
        main_image="https://example.com/lamp.jpg",
        images=["https://example.com/lamp.jpg", "https://example.com/lamp2.jpg"],
        weight_g=420.0,
        cost_price=7.85,
        suggested_price=19.9,
        listed_num=392,
        variants=[
            NormalizedVariant("VID-1", "CJ-LAMP-001-BK", "Negro", 7.85, 420.0, 300, 120, 60, None),
            NormalizedVariant("VID-2", "CJ-LAMP-001-WH", "Blanco", 8.10, 430.0, 300, 120, 60, None),
        ],
    )


def main() -> int:
    # Usar una DB de prueba aparte y limpia, para que el script sea repetible.
    test_db = config.DATA_DIR / "shopify_test.db"
    if test_db.exists():
        test_db.unlink()

    with Database(path=test_db) as db:
        print("=== 1. Upsert de producto + variantes ===")
        pid = db.upsert_product(producto_mock())
        prod = db.get_product(pid)
        print(f"  Producto id={pid}, status={prod['status']}, costo={prod['cost_price']}")
        print(f"  Variantes: {len(db.get_variants(pid))}")

        print("\n=== 2. Idempotencia (re-upsert no duplica) ===")
        pid2 = db.upsert_product(producto_mock())
        print(f"  Mismo id devuelto: {pid == pid2}  (id={pid2})")
        print(f"  Total productos: {sum(db.count_by_status().values())}")

        print("\n=== 3. Research de mercado (datos simulados de ML) ===")
        db.save_market_research(
            product_id=pid, source="mercadolibre", query="lampara led escritorio",
            site_id="MLA", currency="ARS",
            total_results=4820, price_min=3500, price_median=8900, price_max=21000,
            demand_sold_top=500, category_id="MLA1000", category_total_items=18743,
            raw={"nota": "snapshot demo"},
        )
        mr = db.get_market_research(pid)[0]
        print(f"  Demanda (sold top, >=): {mr['demand_sold_top']}")
        print(f"  Precio mercado (min/med/max): {mr['price_min']}/{mr['price_median']}/{mr['price_max']} {mr['currency']}")
        print(f"  Saturacion (resultados / categoria): {mr['total_results']} / {mr['category_total_items']}")

        print("\n=== 4. Maquina de estados ===")
        db.update_status(pid, ENRICHED, note="señales de ML cargadas")
        db.set_score(pid, 0.47)
        db.update_status(pid, SCORED, note="score calculado")
        prod = db.get_product(pid)
        print(f"  Estado actual: {prod['status']}, score: {prod['score']}")

        print("\n=== 5. Transicion invalida (debe fallar) ===")
        try:
            db.update_status(pid, LIVE)  # scored -> live no esta permitido
            print("  [X] ERROR: deberia haber fallado")
        except TransitionError as e:
            print(f"  [OK] Bloqueada correctamente: {e}")

        print("\n=== 6. Historial de estados ===")
        for h in db.get_status_history(pid):
            print(f"  {h['from_status']} -> {h['to_status']}  ({h['note']})")

        print("\n=== 7. Conteo por estado ===")
        print(f"  {db.count_by_status()}")

    print("\n[OK] La base de datos funciona de punta a punta.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
