"""Gestión de fichas de producto SIN IA por API (a mano o por copy-paste).

Subcomandos:
    listar                       muestra candidatos (scored) y los que ya tienen ficha (drafted)
    plantilla <product_id>       genera una plantilla JSON para llenar a mano
    prompt    <product_id>       genera un prompt para pegar en cualquier chat de IA
    importar  <product_id> <archivo.json>   valida e importa la ficha -> drafted

Flujo típico:
    python scripts/ficha.py listar
    python scripts/ficha.py prompt 12          # -> pegás el prompt en una IA web
    # (guardás la respuesta JSON de la IA en data/fichas/12_ficha.json)
    python scripts/ficha.py importar 12 data/fichas/12_ficha.json

O totalmente a mano:
    python scripts/ficha.py plantilla 12       # editás el JSON generado
    python scripts/ficha.py importar 12 data/fichas/12_ficha.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from src.database import Database, TransitionError, DRAFTED, SCORED
from src import listings

FICHAS_DIR = config.DATA_DIR / "fichas"


def _contexto(db: Database, product_id: int):
    p = db.get_product(product_id)
    if not p:
        print(f"[!] No existe el producto id={product_id}")
        return None
    variants = db.get_variants(product_id)
    mr = db.get_market_research(product_id)
    return p, variants, (mr[0] if mr else None)


def cmd_listar(db: Database, _args) -> int:
    scored = db.products_by_status(SCORED, limit=50)
    drafted = db.products_by_status(DRAFTED, limit=50)
    print(f"\n=== Candidatos sin ficha (scored): {len(scored)} ===")
    for p in scored:
        print(f"  [{p['id']}] score {p['score']:.3f}  {p['name']}")
    print(f"\n=== Con ficha (drafted): {len(drafted)} ===")
    for p in drafted:
        f = db.get_listing(p["id"])
        print(f"  [{p['id']}] {f['title'] if f else '(?)'}")
    return 0


def cmd_plantilla(db: Database, args) -> int:
    ctx = _contexto(db, args.product_id)
    if not ctx:
        return 1
    p, variants, research = ctx
    ficha = listings.plantilla_vacia(p, variants, research)
    FICHAS_DIR.mkdir(exist_ok=True)
    ruta = FICHAS_DIR / f"{args.product_id}_ficha.json"
    ruta.write_text(json.dumps(ficha, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] Plantilla creada en: {ruta}")
    print("     Editá los campos y luego: python scripts/ficha.py importar "
          f"{args.product_id} {ruta}")
    return 0


def cmd_prompt(db: Database, args) -> int:
    ctx = _contexto(db, args.product_id)
    if not ctx:
        return 1
    p, variants, research = ctx
    texto = listings.prompt_para_ia(p, variants, research, site_id=config.ML_SITE_ID)
    FICHAS_DIR.mkdir(exist_ok=True)
    ruta = FICHAS_DIR / f"{args.product_id}_prompt.txt"
    ruta.write_text(texto, encoding="utf-8")
    print("=" * 70)
    print(texto)
    print("=" * 70)
    print(f"[OK] Prompt guardado en: {ruta}")
    print("     Pegalo en una IA (ChatGPT/Claude/Gemini), guardá el JSON de la "
          f"respuesta en data/fichas/{args.product_id}_ficha.json y luego:")
    print(f"     python scripts/ficha.py importar {args.product_id} data/fichas/{args.product_id}_ficha.json")
    return 0


def cmd_importar(db: Database, args) -> int:
    if not db.get_product(args.product_id):
        print(f"[!] No existe el producto id={args.product_id}")
        return 1
    ruta = Path(args.archivo)
    if not ruta.exists():
        print(f"[!] No existe el archivo: {ruta}")
        return 1
    try:
        data = json.loads(ruta.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[!] El archivo no es JSON válido: {e}")
        return 1

    ficha = listings.normalizar_ficha(data)
    errores = listings.validar_ficha(ficha)
    if errores:
        print("[!] La ficha tiene problemas:")
        for e in errores:
            print(f"    - {e}")
        return 1

    db.upsert_listing(args.product_id, ficha)
    try:
        db.update_status(args.product_id, DRAFTED, note="ficha importada")
    except TransitionError:
        db.update_status(args.product_id, DRAFTED, note="ficha importada (forzado)", force=True)

    print(f"[OK] Ficha importada para el producto {args.product_id}. Estado -> drafted.")
    print(f"     Título: {ficha['title']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Fichas de producto sin IA por API")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("listar", help="ver candidatos y fichas")
    sp = sub.add_parser("plantilla", help="generar plantilla para llenar a mano")
    sp.add_argument("product_id", type=int)
    sp = sub.add_parser("prompt", help="generar prompt para una IA web")
    sp.add_argument("product_id", type=int)
    sp = sub.add_parser("importar", help="importar una ficha desde JSON")
    sp.add_argument("product_id", type=int)
    sp.add_argument("archivo")

    args = parser.parse_args()
    handlers = {
        "listar": cmd_listar, "plantilla": cmd_plantilla,
        "prompt": cmd_prompt, "importar": cmd_importar,
    }
    with Database() as db:
        return handlers[args.cmd](db, args)


if __name__ == "__main__":
    raise SystemExit(main())
