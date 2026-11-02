"""Prueba del scoring integrado con la base de datos (datos simulados, sin APIs).

Inserta varios productos con perfiles distintos + sus señales de mercado,
los puntúa, descarta los que fallan gates y rankea el resto. Muestra el
ranking, los rechazados con su motivo, y el desglose del mejor.

Uso:
    python scripts/probar_scoring.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from src.connectors.cj_dropshipping import NormalizedProduct, NormalizedVariant
from src.database import Database, ENRICHED, SCORED, REJECTED
from src.scoring import ScoringConfig, score_product


# (producto, research) — perfiles variados para ver gates y ranking
MOCKS = [
    (
        NormalizedProduct("cj", "P1", "Organizador de cables magnetico", "ORG-1", "", "Home & Office",
                          None, [], 80.0, 2.5, 7.9,  120,
                          [NormalizedVariant("V1", "ORG-1-A", "set", 2.5, 80.0, None, None, None, None, stock_total=1000)]),
        dict(price_median=450, total_results=800, demand_sold_top=250, category_id="MLU1", category_total_items=3000, currency="UYU"),
    ),
    (
        NormalizedProduct("cj", "P2", "Cargador inalambrico rapido", "CHG-1", "", "Electronics",
                          None, [], 150.0, 6.0, 15.0, 410,
                          [NormalizedVariant("V2", "CHG-1-A", "negro", 6.0, 150.0, None, None, None, None, stock_total=500)]),
        dict(price_median=700, total_results=12000, demand_sold_top=500, category_id="MLU2", category_total_items=40000, currency="UYU"),
    ),
    (
        NormalizedProduct("cj", "P3", "Smartwatch estilo Apple Watch", "WCH-1", "", "Electronics",
                          None, [], 60.0, 9.0, 25.0, 900,
                          [NormalizedVariant("V3", "WCH-1-A", "negro", 9.0, 60.0, None, None, None, None, stock_total=300)]),
        dict(price_median=1500, total_results=20000, demand_sold_top=500, category_id="MLU3", category_total_items=50000, currency="UYU"),
    ),
    (
        NormalizedProduct("cj", "P4", "Caminadora plegable electrica", "TRD-1", "", "Fitness",
                          None, [], 12000.0, 120.0, 300.0, 50,
                          [NormalizedVariant("V4", "TRD-1-A", "gris", 120.0, 12000.0, None, None, None, None, stock_total=40)]),
        dict(price_median=18000, total_results=900, demand_sold_top=50, category_id="MLU4", category_total_items=1200, currency="UYU"),
    ),
    (
        NormalizedProduct("cj", "P5", "Llavero de plastico generico", "KEY-1", "", "Gifts",
                          None, [], 20.0, 1.5, 4.0, 30,
                          [NormalizedVariant("V5", "KEY-1-A", "rojo", 1.5, 20.0, None, None, None, None, stock_total=2000)]),
        dict(price_median=90, total_results=500, demand_sold_top=100, category_id="MLU5", category_total_items=2000, currency="UYU"),
    ),
]


def main() -> int:
    test_db = config.DATA_DIR / "shopify_test.db"
    if test_db.exists():
        test_db.unlink()

    # Config calibrada para Uruguay (UYU ~40 por USD). Son valores de arranque.
    cfg = ScoringConfig(usd_to_local_rate=40.0, margin_abs_target=300.0, min_margin_pct=0.25)

    with Database(path=test_db) as db:
        # 1) Ingesta simulada: producto + research, y pasar a 'enriched'
        for prod, research in MOCKS:
            pid = db.upsert_product(prod)
            db.save_market_research(
                product_id=pid, source="mercadolibre", site_id="MLU",
                query=prod.name, currency=research["currency"],
                total_results=research["total_results"],
                price_median=research["price_median"],
                demand_sold_top=research["demand_sold_top"],
                category_id=research["category_id"],
                category_total_items=research["category_total_items"],
            )
            db.update_status(pid, ENRICHED, note="research de ML cargado")

        # 2) Scoring de todos los 'enriched'
        for p in db.products_by_status(ENRICHED, limit=100):
            mr = db.get_market_research(p["id"])
            research = mr[0] if mr else None
            res = score_product(p, db.get_variants(p["id"]), research, cfg)
            if res.passed:
                db.set_score(p["id"], res.score)
                db.update_status(p["id"], SCORED, note=f"score={res.score:.3f}")
            else:
                db.update_status(p["id"], REJECTED, note=res.rejection_reason)

        # 3) Resultados
        print("=== RANKING (productos que pasaron los gates) ===")
        for p in db.products_by_status(SCORED, limit=100):
            mr = db.get_market_research(p["id"])
            res = score_product(p, db.get_variants(p["id"]), mr[0] if mr else None, cfg)
            print(f"  {p['score']:.3f}  {p['name']}")
            print(f"          margen {res.margin_pct:.0%} ({res.margin_abs:.0f} {mr[0]['currency']})  | "
                  f"venta {res.sell_price:.0f}  costo {res.cost_local:.0f}")

        print("\n=== RECHAZADOS (gates) ===")
        for p in db.products_by_status(REJECTED, limit=100):
            motivo = db.get_status_history(p["id"])[-1]["note"]
            print(f"  [X] {p['name']}  ->  {motivo}")

        # 4) Desglose del mejor
        top = db.products_by_status(SCORED, limit=1)
        if top:
            p = top[0]
            mr = db.get_market_research(p["id"])
            res = score_product(p, db.get_variants(p["id"]), mr[0] if mr else None, cfg)
            print(f"\n=== DESGLOSE DEL MEJOR: {p['name']} (score {res.score:.3f}) ===")
            for c in res.components:
                signo = "+" if c.sign == "+" else "-"
                print(f"  {signo} {c.name:14s} crudo={str(c.raw):>8}  norm={c.normalized:.2f}  "
                      f"x peso {c.weight:.2f}  =>  {c.contribution:+.3f}")

    print("\n[OK] El scoring funciona de punta a punta.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
