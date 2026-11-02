"""Configuración central del proyecto.

Carga las variables del archivo .env y expone constantes y rutas
que usan los distintos módulos. Mantener acá un único punto de verdad
evita repartir os.getenv() por todo el código.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# --- Rutas base -------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# Base de datos local (SQLite). Está en .gitignore.
DB_PATH = DATA_DIR / "shopify.db"

# Cargar variables de entorno desde .env (si existe)
load_dotenv(BASE_DIR / ".env")


def _get(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


# --- CJ Dropshipping --------------------------------------------------------
CJ_API_KEY = _get("CJ_API_KEY")
CJ_EMAIL = _get("CJ_EMAIL")
CJ_PASSWORD = _get("CJ_PASSWORD")

try:
    CJ_QPS = float(_get("CJ_QPS", "1") or "1")
except ValueError:
    CJ_QPS = 1.0

# Dónde se cachea el token de CJ (válido 15 días). Está en .gitignore.
CJ_TOKEN_CACHE = DATA_DIR / "cj_token.json"


# --- MercadoLibre -----------------------------------------------------------
ML_CLIENT_ID = _get("ML_CLIENT_ID")
ML_CLIENT_SECRET = _get("ML_CLIENT_SECRET")
ML_REDIRECT_URI = _get("ML_REDIRECT_URI")
ML_SITE_ID = _get("ML_SITE_ID", "MLU") or "MLU"  # país: MLU=UY, MLA=AR, MLM=MX, MLC=CL, MCO=CO, MLB=BR...

# Tokens de MercadoLibre (access 3-6h, refresh 6 meses rotativo). En .gitignore.
ML_TOKEN_CACHE = DATA_DIR / "ml_token.json"


# --- Scoring ----------------------------------------------------------------
# Tasa de cambio USD -> moneda local, para calcular margen (costo CJ está en USD,
# precios de MercadoLibre en moneda local). Uruguay: ~40 UYU por USD. ¡Ajustar!
try:
    USD_TO_LOCAL_RATE = float(_get("USD_TO_LOCAL_RATE", "40") or "40")
except ValueError:
    USD_TO_LOCAL_RATE = 40.0


# --- Shopify ----------------------------------------------------------------
# Dominio de tu tienda (ej. mi-tienda.myshopify.com) y Admin API access token (shpat_...)
SHOPIFY_SHOP_DOMAIN = _get("SHOPIFY_SHOP_DOMAIN")
SHOPIFY_ACCESS_TOKEN = _get("SHOPIFY_ACCESS_TOKEN")
SHOPIFY_API_VERSION = _get("SHOPIFY_API_VERSION", "2026-04") or "2026-04"
