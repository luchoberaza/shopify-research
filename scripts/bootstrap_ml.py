"""Bootstrap OAuth de MercadoLibre (login inicial, se hace UNA sola vez).

MercadoLibre no permite login server-to-server: hay que aprobar la app en el
navegador una vez. Después el conector refresca el token solo.

Flujo:
  1. El script imprime (y abre) la URL de autorización.
  2. Te logueás en MercadoLibre y aprobás la app.
  3. Te redirige a tu redirect_uri con ?code=... (aunque la página dé error 404,
     lo único que importa es la URL de la barra de direcciones).
  4. Pegás esa URL (o solo el code) acá y se guardan los tokens.

Requisitos previos (una vez):
  - Crear una app en https://developers.mercadolibre.com.ar -> Mis aplicaciones.
  - Configurar un "Redirect URI" (puede ser https://localhost/callback).
  - Copiar App ID y Secret Key al .env (ML_CLIENT_ID, ML_CLIENT_SECRET,
    ML_REDIRECT_URI) y elegir tu país en ML_SITE_ID.
"""
from __future__ import annotations

import logging
import sys
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from src.connectors.mercadolibre import MercadoLibreClient, MLAuthError, MLError


def _extraer_code(texto: str) -> str:
    """Acepta la URL completa de redirect o el code pelado."""
    texto = texto.strip()
    if texto.lower().startswith("http"):
        qs = parse_qs(urlparse(texto).query)
        return (qs.get("code") or [""])[0]
    return texto


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")

    faltan = [n for n in ("ML_CLIENT_ID", "ML_CLIENT_SECRET", "ML_REDIRECT_URI") if not getattr(config, n)]
    if faltan:
        print(f"\n[!] Faltan en el .env: {', '.join(faltan)}")
        print("    1) Creá una app en https://developers.mercadolibre.com.ar -> Mis aplicaciones")
        print("    2) Poné un Redirect URI (ej. https://localhost/callback)")
        print("    3) Copiá App ID, Secret y Redirect URI al .env, y elegí ML_SITE_ID (país)")
        return 1

    cliente = MercadoLibreClient(
        client_id=config.ML_CLIENT_ID,
        client_secret=config.ML_CLIENT_SECRET,
        redirect_uri=config.ML_REDIRECT_URI,
        site_id=config.ML_SITE_ID,
        token_cache_path=config.ML_TOKEN_CACHE,
    )

    url = cliente.build_authorization_url(state="bootstrap")
    print("\n=== Bootstrap OAuth de MercadoLibre ===")
    print(f"País (site_id): {config.ML_SITE_ID}")
    print("\n1) Abrí esta URL, logueate y aprobá la app:\n")
    print(f"   {url}\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    print("2) Vas a terminar en tu redirect_uri con ?code=... (si da error 404, no importa: copiá la URL).")
    pegado = input("3) Pegá acá la URL de redirección completa (o solo el code): ")

    code = _extraer_code(pegado)
    if not code:
        print("\n[!] No encontré un 'code' en lo que pegaste.")
        return 1

    try:
        token = cliente.exchange_code(code)
    except MLAuthError as e:
        print(f"\n[!] Error al canjear el code: {e}")
        print("    El code dura poco y es de un solo uso: repetí el flujo y pegalo rápido.")
        return 1
    except MLError as e:
        print(f"\n[!] Error: {e}")
        return 1

    print(f"\n[OK] Autorizado. user_id={token.user_id}, scope='{token.scope}'.")
    print(f"     Tokens guardados en {config.ML_TOKEN_CACHE}")
    print("     Ya podés correr:  python scripts/probar_ml.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
