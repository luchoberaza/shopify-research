"""Pipeline de ingesta: orquesta el flujo de extremo a extremo.

    Fase 1 (ingesta)     CJ -> normaliza -> DB                [discovered]
    Fase 2 (enriquecer)  busca en MercadoLibre -> DB          [enriched]
                         (descarta antes con gates baratos)   [rejected]
    Fase 3 (scoring)     calcula score -> DB                  [scored | rejected]

Las funciones dependen solo de los MÉTODOS PÚBLICOS de los clientes (duck
typing), así que se pueden probar con dobles de prueba (ver probar_pipeline.py)
sin tocar la red.

Limitación honesta: el research en ML usa el nombre del producto como query.
Los nombres de CJ vienen en inglés y MercadoLibre es en español, así que la
búsqueda puede no ser óptima. Se puede mejorar pasando un `query_fn` que
traduzca el nombre (p.ej. con Claude) antes de buscar.
"""
from __future__ import annotations

import logging
import statistics
from collections import Counter
from typing import Any, Callable, Optional

from src.connectors.cj_dropshipping import CJDropshippingClient, CJError
from src.connectors.mercadolibre import MLError
from src.database import Database, DISCOVERED, ENRICHED, SCORED, REJECTED
from src.scoring import ScoringConfig, precheck_gates, score_product

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Research de mercado (resumen de una búsqueda en MercadoLibre)
# ---------------------------------------------------------------------------
def summarize_ml_market(ml, query: str, top_n: int = 20) -> dict:
    """Busca `query` en MercadoLibre y resume el mercado: cuántas publicaciones
    hay (saturación), rango/mediana de precios, demanda (sold del top) y la
    categoría dominante con su total de items."""
    data = ml.search(query=query, limit=top_n)
    paging = data.get("paging") or {}
    results = data.get("results") or []

    base = {
        "query": query,
        "site_id": getattr(ml, "site_id", None),
        "currency": None,
        "total_results": paging.get("total"),
        "price_min": None, "price_median": None, "price_max": None,
        "demand_sold_top": None,
        "category_id": None, "category_total_items": None,
    }
    if not results:
        return base

    precios = [r.get("price") for r in results if isinstance(r.get("price"), (int, float))]
    sold = sum(int(r.get("sold_quantity") or 0) for r in results)
    base["currency"] = next((r.get("currency_id") for r in results if r.get("currency_id")), None)
    if precios:
        base["price_min"] = min(precios)
        base["price_median"] = statistics.median(precios)
        base["price_max"] = max(precios)
    base["demand_sold_top"] = sold or None

    cats = [r.get("category_id") for r in results if r.get("category_id")]
    if cats:
        category_id = Counter(cats).most_common(1)[0][0]
        base["category_id"] = category_id
        try:
            cat = ml.get_category(category_id)
            base["category_total_items"] = cat.get("total_items_in_this_category")
        except Exception as e:  # research best-effort: no romper por una categoría
            logger.warning("No se pudo leer la categoría %s: %s", category_id, e)
    return base


# ---------------------------------------------------------------------------
#  Fase 1: ingesta desde CJ
# ---------------------------------------------------------------------------
def ingest_from_cj(db: Database, cj, max_items: int = 20, fetch_stock: bool = False, **filtros) -> list[int]:
    """Trae productos de CJ, los normaliza y los guarda (status=discovered).
    `filtros` se pasan a cj.iter_products (product_name, category_id, etc.)."""
    ids: list[int] = []
    for resumen in cj.iter_products(max_items=max_items, **filtros):
        pid_cj = resumen.get("pid")
        if not pid_cj:
            continue
        try:
            detalle = cj.get_product_detail(pid=pid_cj)
            normalized = CJDropshippingClient.normalizar_producto(detalle)
            if fetch_stock:
                cj.enriquecer_stock(normalized)
            pid = db.upsert_product(normalized)
            ids.append(pid)
            logger.info("Ingestado [%s] %s", pid, normalized.name)
        except CJError as e:
            logger.warning("CJ falló para pid=%s: %s", pid_cj, e)
    return ids


# ---------------------------------------------------------------------------
#  Fase 2: enriquecer con MercadoLibre
# ---------------------------------------------------------------------------
def enrich_with_ml(db: Database, ml, cfg: Optional[ScoringConfig] = None, limit: int = 200,
                   query_fn: Optional[Callable[[dict], str]] = None, top_n: int = 20) -> dict:
    """Para cada producto 'discovered': descarta con gates baratos (sin gastar
    cuota), o busca en ML, guarda el research y pasa a 'enriched'."""
    cfg = cfg or ScoringConfig()
    enriched = pre_rejected = errores = 0

    for p in db.products_by_status(DISCOVERED, limit=limit):
        motivo = precheck_gates(p, cfg)
        if motivo:
            db.update_status(p["id"], REJECTED, note=motivo)
            pre_rejected += 1
            continue

        query = query_fn(p) if query_fn else (p.get("name") or "")
        if not query.strip():
            continue
        try:
            resumen = summarize_ml_market(ml, query, top_n=top_n)
            db.save_market_research(product_id=p["id"], source="mercadolibre", **resumen)
            db.update_status(p["id"], ENRICHED, note="research de MercadoLibre")
            enriched += 1
        except MLError as e:
            logger.warning("ML falló para '%s': %s", query, e)
            errores += 1

    return {"enriched": enriched, "pre_rejected": pre_rejected, "errores": errores}


# ---------------------------------------------------------------------------
#  Fase 3: scoring
# ---------------------------------------------------------------------------
def score_enriched(db: Database, cfg: Optional[ScoringConfig] = None, limit: int = 500) -> dict:
    """Puntúa cada producto 'enriched' y lo pasa a 'scored' o 'rejected'."""
    cfg = cfg or ScoringConfig()
    passed = rejected = 0
    for p in db.products_by_status(ENRICHED, limit=limit):
        mr = db.get_market_research(p["id"])
        research = mr[0] if mr else None
        res = score_product(p, db.get_variants(p["id"]), research, cfg)
        if res.passed:
            db.set_score(p["id"], res.score)
            db.update_status(p["id"], SCORED, note=f"score={res.score:.3f}")
            passed += 1
        else:
            db.update_status(p["id"], REJECTED, note=res.rejection_reason)
            rejected += 1
    return {"passed": passed, "rejected": rejected}


# ---------------------------------------------------------------------------
#  Orquestador completo
# ---------------------------------------------------------------------------
def run(db: Database, cj, ml, cfg: Optional[ScoringConfig] = None, max_items: int = 20,
        fetch_stock: bool = False, query_fn: Optional[Callable[[dict], str]] = None,
        **filtros) -> dict:
    cfg = cfg or ScoringConfig()

    logger.info("== Fase 1/3: ingesta desde CJ ==")
    ids = ingest_from_cj(db, cj, max_items=max_items, fetch_stock=fetch_stock, **filtros)

    logger.info("== Fase 2/3: enriquecer con MercadoLibre ==")
    enr = enrich_with_ml(db, ml, cfg=cfg, query_fn=query_fn)

    logger.info("== Fase 3/3: scoring ==")
    sco = score_enriched(db, cfg=cfg)

    return {
        "ingested": len(ids),
        "enriched": enr["enriched"],
        "pre_rejected": enr["pre_rejected"],
        "ml_errores": enr["errores"],
        "scored": sco["passed"],
        "rejected_scoring": sco["rejected"],
    }
