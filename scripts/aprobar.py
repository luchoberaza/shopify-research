"""Panel de aprobación por consola (sin costo, sin dependencias).

Recorre los productos que ya tienen ficha y te deja decidir uno por uno:
aprobar (para publicar), rechazar o saltar.

Uso:
    python scripts/aprobar.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from src.database import Database
from src import approval


def _texto_plano(html: str, limite: int = 300) -> str:
    txt = re.sub(r"<[^>]+>", " ", html or "")
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:limite] + ("..." if len(txt) > limite else "")


def _mostrar(r: dict) -> None:
    p = r["product"]
    f = r["listing"] or {}
    mr = r["research"] or {}
    print("\n" + "=" * 70)
    print(f"  [{p['id']}]  score {p['score']:.3f}" if p.get("score") is not None else f"  [{p['id']}]")
    print(f"  TÍTULO:   {f.get('title') or '(sin ficha)'}")
    print(f"  Categoría:{f.get('product_type') or p.get('category')}")
    moneda = mr.get("currency") or ""
    print(f"  Precio venta: {f.get('price')} {moneda}   | costo CJ: US$ {p.get('cost_price')}"
          f"   | mercado (mediana): {mr.get('price_median')} {moneda}")
    print(f"  Competencia: {mr.get('total_results')} publicaciones")
    print(f"  Tags: {', '.join(f.get('tags') or []) or '-'}")
    print(f"  SEO: {f.get('seo_title') or '-'}")
    print(f"  Descripción: {_texto_plano(f.get('body_html'))}")
    print("=" * 70)


def main() -> int:
    with Database() as db:
        items = approval.pendientes(db)
        if not items:
            print("No hay productos pendientes de aprobación (estado drafted/pending_review).")
            print("Generá fichas primero:  python scripts/ficha.py listar")
            return 0

        print(f"{len(items)} producto(s) pendiente(s) de aprobación.\n")
        for p in items:
            r = approval.resumen(db, p["id"])
            if not r:
                continue
            _mostrar(r)

            resp = ""
            while resp not in ("a", "r", "s", "q"):
                resp = input("  [a]probar  [r]echazar  [s]altar  [q]salir: ").strip().lower()

            if resp == "q":
                print("Saliendo.")
                break
            if resp == "s":
                continue
            if resp == "a":
                approval.aprobar(db, p["id"])
                print("  -> APROBADO (listo para publicar).")
            elif resp == "r":
                motivo = input("  motivo (opcional): ").strip()
                approval.rechazar(db, p["id"], motivo or None)
                print("  -> rechazado.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
