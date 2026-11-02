"""Ejecuta el pipeline REAL contra CJ y MercadoLibre (usa tus credenciales).

Requisitos:
  - CJ_API_KEY en .env
  - Credenciales de MercadoLibre + bootstrap hecho (python scripts/bootstrap_ml.py)
  - USD_TO_LOCAL_RATE ajustado en .env

Uso:
    python scripts/run_pipeline.py "palabra clave" [cantidad]

Ejemplo:
    python scripts/run_pipeline.py "kitchen gadget" 10
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from src.connectors.cj_dropshipping import CJDropshippingClient
from src.connectors.mercadolibre import MercadoLibreClient
from src.database import Database, SCORED, REJECTED
from src.scoring import ScoringConfig
from src import pipeline, translation


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")

    palabra = sys.argv[1] if len(sys.argv) > 1 else None
    cantidad = int(sys.argv[2]) if len(sys.argv) > 2 else 10

    # --- Validaciones de credenciales --------------------------------------
    if not (config.CJ_API_KEY or (config.CJ_EMAIL and config.CJ_PASSWORD)):
        print("\n[!] Falta CJ_API_KEY en el .env. (probar: python scripts/probar_cj.py)")
        return 1
    if not (config.ML_CLIENT_ID and config.ML_CLIENT_SECRET):
        print("\n[!] Faltan ML_CLIENT_ID / ML_CLIENT_SECRET en el .env.")
        return 1
    if not config.ML_TOKEN_CACHE.exists():
        print("\n[!] MercadoLibre sin autorizar. Corré primero: python scripts/bootstrap_ml.py")
        return 1

    cj = CJDropshippingClient(
        api_key=config.CJ_API_KEY, email=config.CJ_EMAIL, password=config.CJ_PASSWORD,
        qps=config.CJ_QPS, token_cache_path=config.CJ_TOKEN_CACHE,
    )
    ml = MercadoLibreClient(
        client_id=config.ML_CLIENT_ID, client_secret=config.ML_CLIENT_SECRET,
        redirect_uri=config.ML_REDIRECT_URI, site_id=config.ML_SITE_ID,
        token_cache_path=config.ML_TOKEN_CACHE,
    )
    cfg = ScoringConfig(usd_to_local_rate=config.USD_TO_LOCAL_RATE, margin_abs_target=300.0)

    filtros = {"product_name": palabra} if palabra else {}

    # Traduce el nombre del producto (inglés de CJ) a español para buscar en ML
    query_fn = translation.query_fn_glosario()

    with Database() as db:
        print(f"\nBuscando hasta {cantidad} productos" + (f" de '{palabra}'" if palabra else "") + " ...\n")
        stats = pipeline.run(db, cj, ml, cfg=cfg, max_items=cantidad, query_fn=query_fn, **filtros)

        print("\n=== RESUMEN ===")
        for k, v in stats.items():
            print(f"  {k:18s}: {v}")

        print("\n=== TOP CANDIDATOS (scored) ===")
        for p in db.products_by_status(SCORED, limit=20):
            print(f"  {p['score']:.3f}  {p['name']}")

        rej = db.products_by_status(REJECTED, limit=20)
        if rej:
            print(f"\n=== Rechazados ({len(rej)}) ===")
            for p in rej[:10]:
                motivo = db.get_status_history(p["id"])[-1]["note"]
                print(f"  [X] {p['name']}  ->  {motivo}")

    print("\n[OK] Pipeline terminado. Los candidatos quedaron guardados en la base.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
