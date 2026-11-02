"""Prueba del pipeline completo con DOBLES DE PRUEBA (sin red, sin credenciales).

FakeCJ y FakeML imitan a los clientes reales (mismos métodos y formato de
respuesta), así se valida la orquestación de las 3 fases de punta a punta:
ingesta -> enriquecer (con pre-descarte) -> scoring.

Uso:
    python scripts/probar_pipeline.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from src.database import Database, SCORED, REJECTED
from src.scoring import ScoringConfig
from src import pipeline


# --- Datos simulados en formato REAL de CJ (product/query) ------------------
CJ_DETAILS = {
    "P1": {
        "pid": "P1", "productNameEn": "Wireless magnetic cable organizer", "productSku": "ORG-1",
        "description": "Cable organizer", "categoryName": "Home & Office",
        "bigImage": "http://x/1.jpg", "productImageSet": ["http://x/1.jpg"],
        "productWeight": 80, "sellPrice": 2.5, "suggestSellPrice": 7.9, "listedNum": 120,
        "variants": [{"vid": "V1", "variantSku": "ORG-1-A", "variantNameEn": "set",
                      "variantSellPrice": 2.5, "variantWeight": 80}],
    },
    "P2": {  # marca -> debe caer en pre-descarte (sin gastar ML)
        "pid": "P2", "productNameEn": "Apple Watch silicone strap", "productSku": "WCH-1",
        "categoryName": "Electronics", "bigImage": "http://x/2.jpg",
        "productWeight": 30, "sellPrice": 3.0, "suggestSellPrice": 9.0, "listedNum": 900,
        "variants": [{"vid": "V2", "variantSku": "WCH-1-A", "variantSellPrice": 3.0, "variantWeight": 30}],
    },
    "P3": {  # margen bajo -> rechazado en scoring
        "pid": "P3", "productNameEn": "Generic plastic keychain", "productSku": "KEY-1",
        "categoryName": "Gifts", "bigImage": "http://x/3.jpg",
        "productWeight": 20, "sellPrice": 1.5, "suggestSellPrice": 4.0, "listedNum": 30,
        "variants": [{"vid": "V3", "variantSku": "KEY-1-A", "variantSellPrice": 1.5, "variantWeight": 20}],
    },
}


class FakeCJ:
    def iter_products(self, max_items=None, **filtros):
        for pid in CJ_DETAILS:
            yield {"pid": pid}

    def get_product_detail(self, pid=None, **kw):
        return CJ_DETAILS[pid]


class FakeML:
    site_id = "MLU"

    def search(self, query=None, limit=50, **kw):
        q = (query or "").lower()
        if "organizer" in q:
            return {"paging": {"total": 800}, "results": [
                {"price": 450, "sold_quantity": 50, "category_id": "MLU111", "currency_id": "UYU"},
                {"price": 399, "sold_quantity": 25, "category_id": "MLU111", "currency_id": "UYU"},
                {"price": 520, "sold_quantity": 5, "category_id": "MLU111", "currency_id": "UYU"},
            ]}
        if "keychain" in q:
            return {"paging": {"total": 500}, "results": [
                {"price": 90, "sold_quantity": 50, "category_id": "MLU222", "currency_id": "UYU"},
                {"price": 80, "sold_quantity": 25, "category_id": "MLU222", "currency_id": "UYU"},
                {"price": 120, "sold_quantity": 5, "category_id": "MLU222", "currency_id": "UYU"},
            ]}
        return {"paging": {"total": 0}, "results": []}

    def get_category(self, category_id):
        return {"name": category_id, "total_items_in_this_category": {"MLU111": 3000, "MLU222": 2000}.get(category_id)}


def main() -> int:
    test_db = config.DATA_DIR / "shopify_test.db"
    if test_db.exists():
        test_db.unlink()

    cfg = ScoringConfig(usd_to_local_rate=40.0, margin_abs_target=300.0, min_margin_pct=0.25)

    with Database(path=test_db) as db:
        stats = pipeline.run(db, FakeCJ(), FakeML(), cfg=cfg, max_items=10)

        print("\n=== RESUMEN DEL PIPELINE ===")
        for k, v in stats.items():
            print(f"  {k:18s}: {v}")

        print("\n=== RANKING (scored) ===")
        for p in db.products_by_status(SCORED, limit=10):
            print(f"  {p['score']:.3f}  {p['name']}")

        print("\n=== RECHAZADOS ===")
        for p in db.products_by_status(REJECTED, limit=10):
            motivo = db.get_status_history(p["id"])[-1]["note"]
            print(f"  [X] {p['name']}  ->  {motivo}")

    # Verificación de la lógica esperada
    assert stats["ingested"] == 3, stats
    assert stats["pre_rejected"] == 1, stats     # P2 (Apple) sin gastar ML
    assert stats["enriched"] == 2, stats          # P1 y P3
    assert stats["scored"] == 1, stats            # P1
    assert stats["rejected_scoring"] == 1, stats  # P3 (margen bajo)
    print("\n[OK] El pipeline orquesta las 3 fases correctamente.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
