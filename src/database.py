"""Base de datos local del sistema (SQLite).

Persiste lo que traen los conectores y modela cada producto como una MÁQUINA DE
ESTADOS. Es la fuente de verdad interna: una vez que un producto está acá, no se
vuelve a pedir a la API (cuidamos las cuotas).

Tablas:
- products          : el producto candidato (hechos del proveedor + estado + score)
- variants          : variantes de cada producto (SKU, precio, peso, stock)
- market_research   : snapshots de mercado por producto (demanda, precios, saturación)
- status_history    : auditoría de cada transición de estado

Máquina de estados:
    discovered -> enriched -> scored -> drafted -> pending_review
       -> approved -> published -> live
    (cualquiera puede ir a 'rejected' o 'archived'; ver TRANSITIONS)

Diseñado para migrar a PostgreSQL más adelante: SQL estándar, sin features
exclusivas de SQLite salvo el UPSERT (que Postgres también soporta).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

import config
from src.connectors.cj_dropshipping import NormalizedProduct, NormalizedVariant

# ---------------------------------------------------------------------------
#  Máquina de estados
# ---------------------------------------------------------------------------
DISCOVERED = "discovered"
ENRICHED = "enriched"
SCORED = "scored"
DRAFTED = "drafted"
PENDING_REVIEW = "pending_review"
APPROVED = "approved"
PUBLISHED = "published"
LIVE = "live"
REJECTED = "rejected"
ARCHIVED = "archived"

STATUSES = [
    DISCOVERED, ENRICHED, SCORED, DRAFTED, PENDING_REVIEW,
    APPROVED, PUBLISHED, LIVE, REJECTED, ARCHIVED,
]

# Transiciones permitidas. Casi todo puede ir a rejected/archived.
TRANSITIONS: dict[str, set[str]] = {
    DISCOVERED: {ENRICHED, REJECTED, ARCHIVED},
    ENRICHED: {SCORED, REJECTED, ARCHIVED},
    SCORED: {DRAFTED, ENRICHED, REJECTED, ARCHIVED},        # puede re-enriquecerse
    DRAFTED: {PENDING_REVIEW, APPROVED, REJECTED, ARCHIVED},  # el panel puede aprobar directo
    PENDING_REVIEW: {APPROVED, REJECTED, DRAFTED, ARCHIVED},
    APPROVED: {PUBLISHED, REJECTED, ARCHIVED},
    PUBLISHED: {LIVE, ARCHIVED},
    LIVE: {SCORED, ARCHIVED},                               # puede re-evaluarse con ventas
    REJECTED: {DISCOVERED, ARCHIVED},                       # reconsiderar
    ARCHIVED: set(),                                        # terminal (salvo force=True)
}


class TransitionError(Exception):
    """Transición de estado no permitida."""


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT    NOT NULL,
    source_id       TEXT    NOT NULL,
    name            TEXT,
    sku             TEXT,
    description     TEXT,
    category        TEXT,
    main_image      TEXT,
    images          TEXT,                       -- JSON array
    weight_g        REAL,
    cost_price      REAL,                       -- costo de compra (USD)
    suggested_price REAL,
    listed_num      INTEGER,                    -- saturación nativa del proveedor
    status          TEXT    NOT NULL DEFAULT 'discovered',
    score           REAL,
    shopify_product_id TEXT,                    -- gid del producto en Shopify (tras publicar)
    shopify_handle  TEXT,
    raw             TEXT,                       -- JSON
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_products_status ON products(status);
CREATE INDEX IF NOT EXISTS idx_products_score  ON products(score);

CREATE TABLE IF NOT EXISTS variants (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id        INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    source_variant_id TEXT,
    sku               TEXT,
    name              TEXT,
    cost_price        REAL,
    weight_g          REAL,
    length_mm         REAL,
    width_mm          REAL,
    height_mm         REAL,
    image             TEXT,
    stock_total       INTEGER,
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (product_id, source_variant_id)
);
CREATE INDEX IF NOT EXISTS idx_variants_product ON variants(product_id);

CREATE TABLE IF NOT EXISTS market_research (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id           INTEGER REFERENCES products(id) ON DELETE CASCADE,
    source               TEXT    NOT NULL,      -- 'mercadolibre', ...
    query                TEXT,
    site_id              TEXT,
    currency             TEXT,
    total_results        INTEGER,               -- saturación: cuántas publicaciones matchean
    price_min            REAL,
    price_median         REAL,
    price_max            REAL,
    demand_sold_top      INTEGER,               -- suma de sold_quantity del top (cota inferior)
    category_id          TEXT,
    category_total_items INTEGER,               -- saturación de la categoría
    raw                  TEXT,                  -- JSON
    captured_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_market_product ON market_research(product_id);

CREATE TABLE IF NOT EXISTS status_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_history_product ON status_history(product_id);

CREATE TABLE IF NOT EXISTS listings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id       INTEGER NOT NULL UNIQUE REFERENCES products(id) ON DELETE CASCADE,
    title            TEXT,
    body_html        TEXT,
    vendor           TEXT,
    product_type     TEXT,
    tags             TEXT,        -- JSON array
    handle           TEXT,
    seo_title        TEXT,
    seo_description  TEXT,
    price            REAL,
    compare_at_price REAL,
    source           TEXT,        -- 'manual' | 'ia'
    raw              TEXT,        -- JSON completo de la ficha
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS product_metrics (
    product_id    INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    units_sold    INTEGER DEFAULT 0,
    revenue       REAL DEFAULT 0,
    orders_count  INTEGER DEFAULT 0,
    currency      TEXT,
    last_order_at TEXT,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _loads(value: Optional[str]) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


class Database:
    """Acceso a la base de datos. Usar como context manager o llamar close()."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path or config.DB_PATH)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.init_schema()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        # Migraciones idempotentes (para bases creadas antes de agregar columnas)
        for col, decl in (("shopify_product_id", "TEXT"), ("shopify_handle", "TEXT")):
            try:
                self.conn.execute(f"ALTER TABLE products ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass  # la columna ya existe
        self.conn.commit()

    # ----------------------------------------------------------------------
    #  Escritura: productos y variantes
    # ----------------------------------------------------------------------
    def upsert_product(self, p: NormalizedProduct) -> int:
        """Inserta o actualiza un producto por (source, source_id).

        NO pisa status ni score si el producto ya existía (solo refresca los
        hechos del proveedor). Devuelve el id interno.
        """
        images_json = json.dumps(p.images or [])
        raw_json = json.dumps(p.raw or {}, default=str)

        row = self.conn.execute(
            "SELECT id FROM products WHERE source=? AND source_id=?", (p.source, p.source_id)
        ).fetchone()

        if row:
            pid = row["id"]
            self.conn.execute(
                """UPDATE products SET name=?, sku=?, description=?, category=?, main_image=?,
                       images=?, weight_g=?, cost_price=?, suggested_price=?, listed_num=?,
                       raw=?, updated_at=datetime('now')
                   WHERE id=?""",
                (p.name, p.sku, p.description, p.category, p.main_image, images_json,
                 p.weight_g, p.cost_price, p.suggested_price, p.listed_num, raw_json, pid),
            )
        else:
            cur = self.conn.execute(
                """INSERT INTO products (source, source_id, name, sku, description, category,
                       main_image, images, weight_g, cost_price, suggested_price, listed_num, raw)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (p.source, p.source_id, p.name, p.sku, p.description, p.category, p.main_image,
                 images_json, p.weight_g, p.cost_price, p.suggested_price, p.listed_num, raw_json),
            )
            pid = cur.lastrowid
            self.conn.execute(
                "INSERT INTO status_history (product_id, from_status, to_status, note) VALUES (?,?,?,?)",
                (pid, None, DISCOVERED, "alta inicial"),
            )

        for v in p.variants:
            self._upsert_variant(pid, v)

        self.conn.commit()
        return pid

    def _upsert_variant(self, product_id: int, v: NormalizedVariant) -> None:
        self.conn.execute(
            """INSERT INTO variants (product_id, source_variant_id, sku, name, cost_price,
                   weight_g, length_mm, width_mm, height_mm, image, stock_total, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
               ON CONFLICT(product_id, source_variant_id) DO UPDATE SET
                   sku=excluded.sku, name=excluded.name, cost_price=excluded.cost_price,
                   weight_g=excluded.weight_g, length_mm=excluded.length_mm,
                   width_mm=excluded.width_mm, height_mm=excluded.height_mm,
                   image=excluded.image, stock_total=excluded.stock_total,
                   updated_at=datetime('now')""",
            (product_id, v.source_variant_id, v.sku, v.name, v.cost_price, v.weight_g,
             v.length_mm, v.width_mm, v.height_mm, v.image, v.stock_total),
        )

    # ----------------------------------------------------------------------
    #  Escritura: estado y score
    # ----------------------------------------------------------------------
    def update_status(self, product_id: int, to_status: str, note: Optional[str] = None,
                      force: bool = False) -> None:
        if to_status not in STATUSES:
            raise TransitionError(f"Estado desconocido: {to_status!r}")
        row = self.conn.execute("SELECT status FROM products WHERE id=?", (product_id,)).fetchone()
        if not row:
            raise ValueError(f"No existe el producto id={product_id}")
        actual = row["status"]
        if not force and to_status not in TRANSITIONS.get(actual, set()):
            raise TransitionError(f"Transición no permitida: {actual} -> {to_status}")
        self.conn.execute(
            "UPDATE products SET status=?, updated_at=datetime('now') WHERE id=?", (to_status, product_id)
        )
        self.conn.execute(
            "INSERT INTO status_history (product_id, from_status, to_status, note) VALUES (?,?,?,?)",
            (product_id, actual, to_status, note),
        )
        self.conn.commit()

    def set_score(self, product_id: int, score: float) -> None:
        self.conn.execute(
            "UPDATE products SET score=?, updated_at=datetime('now') WHERE id=?", (score, product_id)
        )
        self.conn.commit()

    def set_shopify_ids(self, product_id: int, shopify_product_id: str, handle: Optional[str] = None) -> None:
        self.conn.execute(
            "UPDATE products SET shopify_product_id=?, shopify_handle=?, updated_at=datetime('now') WHERE id=?",
            (shopify_product_id, handle, product_id),
        )
        self.conn.commit()

    # ----------------------------------------------------------------------
    #  Métricas de ventas (seguimiento)
    # ----------------------------------------------------------------------
    def upsert_metrics(self, product_id: int, units_sold: int = 0, revenue: float = 0.0,
                       orders_count: int = 0, currency: Optional[str] = None,
                       last_order_at: Optional[str] = None) -> None:
        self.conn.execute(
            """INSERT INTO product_metrics
                   (product_id, units_sold, revenue, orders_count, currency, last_order_at, updated_at)
               VALUES (?,?,?,?,?,?,datetime('now'))
               ON CONFLICT(product_id) DO UPDATE SET
                   units_sold=excluded.units_sold, revenue=excluded.revenue,
                   orders_count=excluded.orders_count, currency=excluded.currency,
                   last_order_at=excluded.last_order_at, updated_at=datetime('now')""",
            (product_id, units_sold, revenue, orders_count, currency, last_order_at),
        )
        self.conn.commit()

    def get_metrics(self, product_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM product_metrics WHERE product_id=?", (product_id,)).fetchone()
        return dict(row) if row else None

    def find_by_shopify_id(self, shopify_product_id: str) -> Optional[int]:
        row = self.conn.execute(
            "SELECT id FROM products WHERE shopify_product_id=?", (shopify_product_id,)
        ).fetchone()
        return row["id"] if row else None

    def top_by_revenue(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            """SELECT p.id, p.name, m.units_sold, m.revenue, m.orders_count, m.currency
               FROM product_metrics m JOIN products p ON p.id = m.product_id
               ORDER BY m.revenue DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------------
    #  Escritura: research de mercado
    # ----------------------------------------------------------------------
    def save_market_research(
        self,
        product_id: Optional[int],
        source: str,
        query: Optional[str] = None,
        site_id: Optional[str] = None,
        currency: Optional[str] = None,
        total_results: Optional[int] = None,
        price_min: Optional[float] = None,
        price_median: Optional[float] = None,
        price_max: Optional[float] = None,
        demand_sold_top: Optional[int] = None,
        category_id: Optional[str] = None,
        category_total_items: Optional[int] = None,
        raw: Any = None,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO market_research (product_id, source, query, site_id, currency,
                   total_results, price_min, price_median, price_max, demand_sold_top,
                   category_id, category_total_items, raw)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (product_id, source, query, site_id, currency, total_results, price_min,
             price_median, price_max, demand_sold_top, category_id, category_total_items,
             json.dumps(raw, default=str) if raw is not None else None),
        )
        self.conn.commit()
        return cur.lastrowid

    # ----------------------------------------------------------------------
    #  Escritura: fichas (listings)
    # ----------------------------------------------------------------------
    def upsert_listing(self, product_id: int, ficha: dict) -> int:
        """Guarda o actualiza la ficha de un producto (una por producto)."""
        tags_json = json.dumps(ficha.get("tags") or [], ensure_ascii=False)
        raw_json = json.dumps(ficha, ensure_ascii=False, default=str)
        campos = (
            ficha.get("title"), ficha.get("body_html"), ficha.get("vendor"),
            ficha.get("product_type"), tags_json, ficha.get("handle"),
            ficha.get("seo_title"), ficha.get("seo_description"),
            ficha.get("price"), ficha.get("compare_at_price"),
            ficha.get("source", "manual"), raw_json,
        )
        row = self.conn.execute("SELECT id FROM listings WHERE product_id=?", (product_id,)).fetchone()
        if row:
            lid = row["id"]
            self.conn.execute(
                """UPDATE listings SET title=?, body_html=?, vendor=?, product_type=?, tags=?,
                       handle=?, seo_title=?, seo_description=?, price=?, compare_at_price=?,
                       source=?, raw=?, updated_at=datetime('now')
                   WHERE id=?""",
                (*campos, lid),
            )
        else:
            cur = self.conn.execute(
                """INSERT INTO listings (product_id, title, body_html, vendor, product_type, tags,
                       handle, seo_title, seo_description, price, compare_at_price, source, raw)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (product_id, *campos),
            )
            lid = cur.lastrowid
        self.conn.commit()
        return lid

    def get_listing(self, product_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM listings WHERE product_id=?", (product_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["tags"] = _loads(d.get("tags")) or []
        d["raw"] = _loads(d.get("raw"))
        return d

    # ----------------------------------------------------------------------
    #  Lectura
    # ----------------------------------------------------------------------
    def get_product(self, product_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["images"] = _loads(d.get("images"))
        d["raw"] = _loads(d.get("raw"))
        return d

    def find_product(self, source: str, source_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT id FROM products WHERE source=? AND source_id=?", (source, source_id)
        ).fetchone()
        return self.get_product(row["id"]) if row else None

    def get_variants(self, product_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM variants WHERE product_id=? ORDER BY id", (product_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_market_research(self, product_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM market_research WHERE product_id=? ORDER BY captured_at DESC", (product_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_status_history(self, product_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM status_history WHERE product_id=? ORDER BY id", (product_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def products_by_status(self, status: str, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM products WHERE status=? ORDER BY score DESC NULLS LAST, id LIMIT ?",
            (status, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_by_status(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM products GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}
