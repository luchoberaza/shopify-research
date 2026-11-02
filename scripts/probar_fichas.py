"""Prueba del flujo de fichas (sin APIs ni IA).

Verifica: plantilla, prompt para IA, normalización, validación (ok y con
errores), guardado de la ficha y transición a 'drafted'.

Uso:
    python scripts/probar_fichas.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from src.connectors.cj_dropshipping import NormalizedProduct, NormalizedVariant
from src.database import Database, ENRICHED, SCORED, DRAFTED
from src import listings


def main() -> int:
    test_db = config.DATA_DIR / "shopify_test.db"
    if test_db.exists():
        test_db.unlink()

    with Database(path=test_db) as db:
        # Producto candidato en estado 'scored'
        prod = NormalizedProduct(
            "cj", "F1", "Wireless magnetic cable organizer", "ORG-1", "", "Home & Office",
            None, [], 80.0, 2.5, 7.9, 120,
            [NormalizedVariant("V1", "ORG-1-A", "set x5", 2.5, 80.0, None, None, None, None)],
        )
        pid = db.upsert_product(prod)
        db.save_market_research(product_id=pid, source="mercadolibre", site_id="MLU",
                                currency="UYU", price_median=450, price_min=399, price_max=520,
                                total_results=800, demand_sold_top=80, category_id="MLU111")
        db.update_status(pid, ENRICHED)
        db.update_status(pid, SCORED)
        p = db.get_product(pid)
        variants = db.get_variants(pid)
        research = db.get_market_research(pid)[0]

        print("=== 1. Plantilla (para llenar a mano) ===")
        plantilla = listings.plantilla_vacia(p, variants, research)
        print(f"  campos: {[k for k in plantilla if not k.startswith('_')]}")
        print(f"  precio sugerido precargado: {plantilla['price']} (mediana de mercado)")
        print(f"  referencia incluida: {list(plantilla['_referencia_no_editar'].keys())}")

        print("\n=== 2. Prompt para IA web (primeras líneas) ===")
        prompt = listings.prompt_para_ia(p, variants, research, site_id="MLU")
        for linea in prompt.splitlines()[:6]:
            print(f"  {linea}")
        assert "JSON" in prompt and "Wireless magnetic cable organizer" in prompt
        assert "NO inventes" in prompt
        print("  ... (prompt completo incluye esquema JSON + datos reales)")

        print("\n=== 3. Ficha llena (simulada) -> normalizar + validar ===")
        ficha_cruda = {
            "title": "Organizador de Cables Magnetico (set x5)",
            "body_html": "<p>Mantené tu escritorio ordenado.</p><ul><li>Set de 5</li><li>Adhesivo fuerte</li></ul>",
            "vendor": "Mi Tienda",
            "product_type": "Organización",
            "tags": "oficina, escritorio, cables",   # string -> debe volverse lista
            "handle": "organizador-cables-magnetico",
            "seo_title": "Organizador de cables magnético x5",
            "seo_description": "Ordená los cables de tu escritorio con este set magnético de 5 piezas. Envío a todo el país.",
            "price": "590",                            # string -> debe volverse número
            "compare_at_price": None,
        }
        ficha = listings.normalizar_ficha(ficha_cruda)
        errores = listings.validar_ficha(ficha)
        print(f"  tags normalizados: {ficha['tags']}")
        print(f"  price normalizado: {ficha['price']} ({type(ficha['price']).__name__})")
        print(f"  errores de validación: {errores}")
        assert errores == [], errores
        assert ficha["tags"] == ["oficina", "escritorio", "cables"]
        assert ficha["price"] == 590.0

        print("\n=== 4. Guardar ficha -> producto pasa a 'drafted' ===")
        db.upsert_listing(pid, ficha)
        db.update_status(pid, DRAFTED, note="ficha importada")
        guardada = db.get_listing(pid)
        print(f"  estado: {db.get_product(pid)['status']}")
        print(f"  título guardado: {guardada['title']}")
        assert db.get_product(pid)["status"] == DRAFTED
        assert guardada["title"] == ficha["title"]

        print("\n=== 5. Validación con ficha inválida (debe listar errores) ===")
        mala = listings.normalizar_ficha({"title": "", "seo_description": "x" * 200, "price": -5})
        errs = listings.validar_ficha(mala)
        for e in errs:
            print(f"  - {e}")
        assert any("title" in e for e in errs)
        assert any("seo_description" in e for e in errs)
        assert any("price" in e for e in errs)

    print("\n[OK] El flujo de fichas funciona (a mano o por copy-paste con IA).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
