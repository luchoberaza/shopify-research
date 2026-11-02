"""Conector para la API de MercadoLibre, orientado a RESEARCH de productos.

Da señales de DEMANDA REAL (sold_quantity), precios y competencia por país.
Complementa a CJ Dropshipping: CJ dice "qué puedo vender y a qué costo";
MercadoLibre dice "qué se vende de verdad y a qué precio en el mercado".

LIMITACIONES IMPORTANTES (verificadas contra la API, 2026):
- OAuth solo soporta authorization_code + refresh_token (NO client_credentials).
  Hace falta un login humano UNA vez (bootstrap). Ver scripts/bootstrap_ml.py.
  Después el cliente refresca el token solo. El refresh_token es de UN SOLO USO
  (rota en cada refresh) y dura 6 meses; por eso se persiste siempre el nuevo.
- Todo exige token: búsqueda, detalle y categorías dan 403 sin Bearer.
- sold_quantity y available_quantity vienen ENMASCARADOS en baldes para ítems
  ajenos (1-5 exacto; arriba de 5 es el piso del rango). Tratar como cota
  inferior / orden de magnitud, no como cifra exacta.

Referencia (verificada):
  Auth:  https://developers.mercadolibre.com.ar/es_ar/autenticacion-y-autorizacion
  Items: https://developers.mercadolibre.com.ar/es_ar/items-y-busquedas
  Base API: https://api.mercadolibre.com
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://api.mercadolibre.com"

# Dominio de login (authorization) por país. El endpoint de token siempre es api.mercadolibre.com.
AUTH_DOMAINS = {
    "MLA": "https://auth.mercadolibre.com.ar",   # Argentina
    "MLB": "https://auth.mercadolivre.com.br",   # Brasil (mercadoLIVRE)
    "MLM": "https://auth.mercadolibre.com.mx",   # México
    "MLC": "https://auth.mercadolibre.cl",       # Chile
    "MCO": "https://auth.mercadolibre.com.co",   # Colombia
    "MLU": "https://auth.mercadolibre.com.uy",   # Uruguay
    "MPE": "https://auth.mercadolibre.com.pe",   # Perú
    "MLV": "https://auth.mercadolibre.com.ve",   # Venezuela
    "MBO": "https://auth.mercadolibre.com.bo",   # Bolivia
    "MEC": "https://auth.mercadolibre.com.ec",   # Ecuador
    "MPY": "https://auth.mercadolibre.com.py",   # Paraguay
    "MCR": "https://auth.mercadolibre.com.cr",   # Costa Rica
    "MPA": "https://auth.mercadolibre.com.pa",   # Panamá
    "MRD": "https://auth.mercadolibre.com.do",   # Rep. Dominicana
    "MGT": "https://auth.mercadolibre.com.gt",   # Guatemala
    "MSV": "https://auth.mercadolibre.com.sv",   # El Salvador
    "MHN": "https://auth.mercadolibre.com.hn",   # Honduras
    "MNI": "https://auth.mercadolibre.com.ni",   # Nicaragua
}


# ============================================================================
#  Excepciones
# ============================================================================
class MLError(Exception):
    def __init__(self, message: str, status: Any = None):
        super().__init__(message)
        self.status = status


class MLAuthError(MLError):
    """Token/credenciales inválidos, o falta el bootstrap OAuth."""


class MLRateLimitError(MLError):
    """429: se agotaron los reintentos por exceso de llamadas."""


# ============================================================================
#  Modelos
# ============================================================================
@dataclass
class _Token:
    access_token: str
    access_expiry: datetime
    refresh_token: str
    user_id: Optional[int] = None
    scope: str = ""


@dataclass
class NormalizedListing:
    """Publicación de MercadoLibre en el formato interno del sistema.

    OJO: sold_quantity y available_quantity son referenciales (en baldes) para
    ítems ajenos. Interpretar sold_quantity como 'al menos esta cantidad'.
    """
    source: str                      # "mercadolibre"
    source_id: str                   # item id (ej. "MLA123...")
    title: str
    price: Optional[float]
    currency: Optional[str]
    sold_quantity: Optional[int]     # demanda real (en baldes) -> cota inferior
    available_quantity: Optional[int]
    condition: Optional[str]
    permalink: Optional[str]
    seller_id: Optional[int]
    category_id: Optional[str]
    thumbnail: Optional[str]
    raw: dict = field(default_factory=dict)


# ============================================================================
#  Helpers
# ============================================================================
def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ============================================================================
#  Cliente
# ============================================================================
class MercadoLibreClient:
    """Cliente de research de MercadoLibre (OAuth authorization_code + refresh)."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str = "",
        site_id: str = "MLA",
        qps: float = 5.0,
        token_cache_path: Optional[Path] = None,
        timeout: int = 30,
        max_retries: int = 3,
    ):
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.redirect_uri = (redirect_uri or "").strip()
        self.site_id = (site_id or "MLA").strip().upper()
        self.timeout = timeout
        self.max_retries = max_retries
        self.token_cache_path = Path(token_cache_path) if token_cache_path else None

        self._min_interval = (1.0 / qps) if qps and qps > 0 else 0.0
        self._last_request = 0.0
        self._session = requests.Session()
        self._token: Optional[_Token] = None

        if not (self.client_id and self.client_secret):
            raise MLAuthError("Faltan ML_CLIENT_ID / ML_CLIENT_SECRET.")

        self._load_token_cache()

    # ---- Frecuencia --------------------------------------------------------
    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request = time.monotonic()

    # ---- HTTP con reintentos de red ---------------------------------------
    def _http(self, method, url, headers, params=None, json_body=None, data=None):
        last_exc: Optional[Exception] = None
        for i in range(self.max_retries):
            try:
                return self._session.request(
                    method, url, headers=headers, params=params, json=json_body, data=data, timeout=self.timeout
                )
            except requests.RequestException as e:
                last_exc = e
                espera = 2 ** i
                logger.warning("Error de red (%s). Reintento %d/%d en %ss...", e, i + 1, self.max_retries, espera)
                time.sleep(espera)
        raise MLError(f"Fallo de red tras {self.max_retries} intentos: {last_exc}")

    # ======================================================================
    #  OAuth
    # ======================================================================
    def build_authorization_url(self, state: Optional[str] = None,
                                code_challenge: Optional[str] = None,
                                code_challenge_method: Optional[str] = None) -> str:
        """URL para el login inicial (paso 1 del bootstrap)."""
        if not self.redirect_uri:
            raise MLAuthError("Falta ML_REDIRECT_URI para construir la URL de autorización.")
        domain = AUTH_DOMAINS.get(self.site_id, AUTH_DOMAINS["MLA"])
        params = {"response_type": "code", "client_id": self.client_id, "redirect_uri": self.redirect_uri}
        if state:
            params["state"] = state
        if code_challenge:  # solo si tu app tiene PKCE habilitado
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = code_challenge_method or "S256"
        return f"{domain}/authorization?{urlencode(params)}"

    def exchange_code(self, code: str, code_verifier: Optional[str] = None) -> _Token:
        """Intercambia el code del redirect por tokens (paso 2 del bootstrap)."""
        data = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": self.redirect_uri,
        }
        if code_verifier:
            data["code_verifier"] = code_verifier
        self._store_token(self._post_token(data))
        assert self._token is not None
        return self._token

    def _refresh(self) -> None:
        if not (self._token and self._token.refresh_token):
            raise MLAuthError("No hay refresh_token. Ejecutá el bootstrap: python scripts/bootstrap_ml.py")
        logger.info("Refrescando access token de MercadoLibre...")
        data = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self._token.refresh_token,
        }
        self._store_token(self._post_token(data))

    def _post_token(self, data: dict) -> dict:
        self._throttle()
        url = f"{API_BASE}/oauth/token"
        headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
        resp = self._http("POST", url, headers, data=data)
        try:
            payload = resp.json()
        except ValueError:
            raise MLAuthError(f"Respuesta no-JSON en /oauth/token (HTTP {resp.status_code})")
        if resp.status_code == 200 and "access_token" in payload:
            return payload
        msg = payload.get("error_description") or payload.get("message") or payload.get("error") or "Error de token"
        raise MLAuthError(msg, payload.get("status") or resp.status_code)

    def _ensure_token(self) -> None:
        now = datetime.now(timezone.utc)
        if self._token and (self._token.access_expiry - timedelta(minutes=5)) > now:
            return
        if self._token and self._token.refresh_token:
            self._refresh()
            return
        raise MLAuthError("Sin token. Ejecutá primero el bootstrap OAuth: python scripts/bootstrap_ml.py")

    def _store_token(self, payload: dict) -> None:
        expires_in = int(payload.get("expires_in", 10800))  # usar el de runtime (la doc es ambigua)
        prev_refresh = self._token.refresh_token if self._token else ""
        self._token = _Token(
            access_token=payload["access_token"],
            access_expiry=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            refresh_token=payload.get("refresh_token") or prev_refresh,
            user_id=payload.get("user_id"),
            scope=payload.get("scope", ""),
        )
        self._save_token_cache()
        logger.info("Token ML actualizado (user_id=%s, vence %s).", self._token.user_id, self._token.access_expiry.isoformat())

    # ---- Caché de token ----------------------------------------------------
    def _save_token_cache(self) -> None:
        if not (self.token_cache_path and self._token):
            return
        try:
            self.token_cache_path.write_text(
                json.dumps(
                    {
                        "access_token": self._token.access_token,
                        "access_expiry": self._token.access_expiry.isoformat(),
                        "refresh_token": self._token.refresh_token,
                        "user_id": self._token.user_id,
                        "scope": self._token.scope,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("No se pudo guardar la caché de token ML: %s", e)

    def _load_token_cache(self) -> None:
        if not (self.token_cache_path and self.token_cache_path.exists()):
            return
        try:
            d = json.loads(self.token_cache_path.read_text(encoding="utf-8"))
            self._token = _Token(
                access_token=d["access_token"],
                access_expiry=_parse_dt(d["access_expiry"]),
                refresh_token=d["refresh_token"],
                user_id=d.get("user_id"),
                scope=d.get("scope", ""),
            )
            logger.info("Token ML cargado de caché (user_id=%s).", self._token.user_id)
        except (OSError, KeyError, ValueError) as e:
            logger.warning("Caché de token ML ilegible (%s). Se ignorará.", e)
            self._token = None

    # ---- Petición autenticada genérica ------------------------------------
    def _request(self, method: str, path: str, params: Optional[dict] = None, json_body: Optional[dict] = None) -> Any:
        self._ensure_token()
        refreshed = False
        rate_attempts = 0

        while True:
            self._throttle()
            url = f"{API_BASE}/{path.lstrip('/')}"
            assert self._token is not None
            headers = {"Authorization": f"Bearer {self._token.access_token}", "Accept": "application/json"}
            resp = self._http(method, url, headers, params=params, json_body=json_body)

            if resp.status_code == 200:
                return resp.json()

            try:
                err = resp.json()
            except ValueError:
                err = {}

            if resp.status_code == 401 and not refreshed:
                logger.warning("401: token inválido. Refrescando...")
                refreshed = True
                self._refresh()
                continue

            if resp.status_code == 429 and rate_attempts < self.max_retries:
                rate_attempts += 1
                espera = 2 ** rate_attempts
                logger.warning("429 rate limit. Esperando %ss (intento %d)...", espera, rate_attempts)
                time.sleep(espera)
                continue

            msg = err.get("message") or err.get("error") or err.get("error_description") or f"HTTP {resp.status_code}"
            if resp.status_code in (401, 403):
                raise MLAuthError(msg, resp.status_code)
            if resp.status_code == 429:
                raise MLRateLimitError(msg, resp.status_code)
            raise MLError(msg, resp.status_code)

    # ======================================================================
    #  Métodos de research
    # ======================================================================
    def search(self, query: Optional[str] = None, category: Optional[str] = None,
               offset: int = 0, limit: int = 50, sort: Optional[str] = None,
               extra: Optional[dict] = None) -> dict:
        """Búsqueda de catálogo. Devuelve {paging, results[]}.
        sort: 'price_asc' | 'price_desc' | 'relevance'. limit máx 100."""
        params = {
            "q": query, "category": category,
            "offset": offset, "limit": min(limit, 100), "sort": sort,
        }
        params = {k: v for k, v in params.items() if v is not None}
        if extra:
            params.update(extra)
        return self._request("GET", f"sites/{self.site_id}/search", params=params)

    def get_item(self, item_id: str, attributes: Optional[str] = None) -> dict:
        """Detalle de un ítem (GET /items/{id})."""
        params = {"attributes": attributes} if attributes else None
        return self._request("GET", f"items/{item_id}", params=params)

    def get_items(self, ids: list[str], attributes: Optional[str] = None) -> list[dict]:
        """Multiget de ítems (máx 20 por llamada). Devuelve formato verbose [{code, body}]."""
        salida: list[dict] = []
        for i in range(0, len(ids), 20):
            chunk = ids[i:i + 20]
            params = {"ids": ",".join(chunk)}
            if attributes:
                params["attributes"] = attributes
            salida.extend(self._request("GET", "items", params=params))
        return salida

    def get_categories(self) -> list[dict]:
        """Categorías raíz del site (GET /sites/{site}/categories)."""
        return self._request("GET", f"sites/{self.site_id}/categories")

    def get_category(self, category_id: str) -> dict:
        """Detalle de categoría, incluye total_items_in_this_category (señal de saturación)."""
        return self._request("GET", f"categories/{category_id}")

    def predict_category(self, title: str, limit: int = 1) -> list[dict]:
        """Predice categoría/dominio a partir de un título (domain_discovery)."""
        return self._request(
            "GET", f"sites/{self.site_id}/domain_discovery/search", params={"q": title, "limit": limit}
        )

    # ======================================================================
    #  Normalización
    # ======================================================================
    @staticmethod
    def normalizar_listing(result: dict) -> NormalizedListing:
        seller = result.get("seller") or {}
        return NormalizedListing(
            source="mercadolibre",
            source_id=result.get("id"),
            title=result.get("title") or "",
            price=_to_float(result.get("price")),
            currency=result.get("currency_id"),
            sold_quantity=result.get("sold_quantity"),
            available_quantity=result.get("available_quantity"),
            condition=result.get("condition"),
            permalink=result.get("permalink"),
            seller_id=seller.get("id") if isinstance(seller, dict) else None,
            category_id=result.get("category_id"),
            thumbnail=result.get("thumbnail"),
            raw=result,
        )
