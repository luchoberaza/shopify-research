"""Conector para la API V2.0 de CJ Dropshipping.

Provee acceso a catálogo, detalle de producto (con variantes), precios de
compra, peso/dimensiones y stock en tiempo real. Es la "fuente de hechos"
del sistema: lo que esta API devuelve es dato real del proveedor, no estimación.

Características clave:
- Caché de token en disco: el accessToken dura 15 días, así que NO se pide
  en cada llamada (eso gastaría cuota y chocaría con el QPS=1 del login).
- Rate limiting: respeta las peticiones/segundo de tu nivel de cuenta.
- Reintentos con backoff ante errores de red y de rate limit.
- Re-autenticación automática si el token expira o es rechazado.
- Normalización a un modelo interno (NormalizedProduct) listo para scoring.

Referencia de la API (verificada):
  https://developers.cjdropshipping.com/en/api/api2/
  Base URL: https://developers.cjdropshipping.com/api2.0/v1
  Auth: POST authentication/getAccessToken  (body {"apiKey": ...})
  Header de llamadas autenticadas: CJ-Access-Token
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://developers.cjdropshipping.com/api2.0/v1"

# --- Códigos del envelope de respuesta de CJ (ver ps-code.html) -------------
CODE_OK = 200
CODE_TOKEN_INVALID = 1600001   # API key o access token inválido/expirado
CODE_TOKEN_EMPTY = 1600002     # access token vacío
CODE_REFRESH_INVALID = 1600003 # refresh token inválido
CODE_RATE_LIMIT = 1600200      # demasiadas peticiones
CODE_QUOTA_USED = 1600201      # cuota diaria agotada
CODE_POINTS_EMPTY = 16900500   # puntos diarios insuficientes


# ============================================================================
#  Excepciones
# ============================================================================
class CJError(Exception):
    """Error genérico devuelto por la API de CJ."""

    def __init__(self, message: str, code: Optional[int] = None, request_id: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.request_id = request_id


class CJAuthError(CJError):
    """Fallo de autenticación: credenciales o token inválidos."""


class CJRateLimitError(CJError):
    """Se superó el límite de frecuencia y se agotaron los reintentos."""


class CJQuotaError(CJError):
    """Se agotó la cuota/puntos diarios (no se reintenta: es por día)."""


# ============================================================================
#  Modelo de token y modelo de producto normalizado
# ============================================================================
@dataclass
class _Token:
    access_token: str
    access_expiry: datetime
    refresh_token: str
    refresh_expiry: datetime


@dataclass
class NormalizedVariant:
    """Variante de producto en el formato interno del sistema."""
    source_variant_id: str          # vid de CJ
    sku: Optional[str]
    name: str
    cost_price: Optional[float]     # variantSellPrice = costo de compra (USD)
    weight_g: Optional[float]       # gramos
    length_mm: Optional[float]
    width_mm: Optional[float]
    height_mm: Optional[float]
    image: Optional[str]
    stock_total: Optional[int] = None  # se llena con enriquecer_stock()


@dataclass
class NormalizedProduct:
    """Producto en el formato interno del sistema (lo que consume el scoring)."""
    source: str                     # "cj"
    source_id: str                  # pid de CJ
    name: str
    sku: Optional[str]
    description: Optional[str]
    category: Optional[str]
    main_image: Optional[str]
    images: list[str]
    weight_g: Optional[float]
    cost_price: Optional[float]     # sellPrice (costo de compra "desde")
    suggested_price: Optional[float]  # suggestSellPrice
    listed_num: Optional[int]       # cuántas tiendas lo listan -> señal de saturación
    variants: list[NormalizedVariant] = field(default_factory=list)
    raw: dict = field(default_factory=dict)  # respuesta cruda, por si hace falta


# ============================================================================
#  Helpers de módulo
# ============================================================================
def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: str) -> datetime:
    """Parsea fechas ISO de CJ (p.ej. '2021-08-18T09:16:33+08:00') a aware UTC."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ============================================================================
#  Cliente
# ============================================================================
class CJDropshippingClient:
    """Cliente de la API V2.0 de CJ Dropshipping."""

    def __init__(
        self,
        api_key: str = "",
        email: str = "",
        password: str = "",
        qps: float = 1.0,
        token_cache_path: Optional[Path] = None,
        timeout: int = 30,
        max_retries: int = 3,
    ):
        self.api_key = (api_key or "").strip()
        self.email = (email or "").strip()
        self.password = (password or "").strip()
        self.timeout = timeout
        self.max_retries = max_retries
        self.token_cache_path = Path(token_cache_path) if token_cache_path else None

        self._min_interval = (1.0 / qps) if qps and qps > 0 else 0.0
        self._last_request = 0.0
        self._session = requests.Session()
        self._token: Optional[_Token] = None

        if not (self.api_key or (self.email and self.password)):
            raise CJAuthError(
                "Sin credenciales. Configurá CJ_API_KEY (o CJ_EMAIL + CJ_PASSWORD)."
            )

        self._load_token_cache()

    # ---- Control de frecuencia --------------------------------------------
    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request = time.monotonic()

    # ---- HTTP con reintentos de red ---------------------------------------
    def _http(self, method: str, url: str, headers: dict, params: Optional[dict], json_body: Optional[dict]):
        last_exc: Optional[Exception] = None
        for i in range(self.max_retries):
            try:
                return self._session.request(
                    method, url, headers=headers, params=params, json=json_body, timeout=self.timeout
                )
            except requests.RequestException as e:
                last_exc = e
                espera = 2 ** i
                logger.warning("Error de red (%s). Reintento %d/%d en %ss...", e, i + 1, self.max_retries, espera)
                time.sleep(espera)
        raise CJError(f"Fallo de red tras {self.max_retries} intentos: {last_exc}")

    # ---- Autenticación -----------------------------------------------------
    def _post_auth(self, path: str, body: dict) -> dict:
        """Llama a un endpoint de autenticación (sin header de token)."""
        self._throttle()
        url = f"{BASE_URL}/{path}"
        resp = self._http("POST", url, {"Content-Type": "application/json"}, None, body)
        try:
            payload = resp.json()
        except ValueError:
            raise CJAuthError(f"Respuesta no-JSON en auth (HTTP {resp.status_code}): {resp.text[:200]}")
        if payload.get("code") == CODE_OK and payload.get("result", True):
            return payload["data"]
        raise CJAuthError(
            payload.get("message", "Autenticación fallida"),
            payload.get("code"),
            payload.get("requestId"),
        )

    def _authenticate(self) -> None:
        """Obtiene un accessToken nuevo. Intenta apiKey y cae a email+password."""
        if self.api_key:
            try:
                logger.info("Autenticando en CJ con apiKey...")
                data = self._post_auth("authentication/getAccessToken", {"apiKey": self.api_key})
                self._store_token(data)
                return
            except CJAuthError as e:
                if not (self.email and self.password):
                    raise
                logger.warning("Auth con apiKey falló (%s). Probando email+password...", e)
        if self.email and self.password:
            logger.info("Autenticando en CJ con email+password...")
            data = self._post_auth(
                "authentication/getAccessToken", {"email": self.email, "password": self.password}
            )
            self._store_token(data)
            return
        raise CJAuthError("Sin credenciales válidas para autenticar.")

    def _ensure_token(self) -> None:
        """Garantiza un accessToken válido: usa caché, refresca o re-autentica."""
        now = datetime.now(timezone.utc)
        margen = timedelta(minutes=5)

        if self._token and (self._token.access_expiry - margen) > now:
            return  # accessToken todavía vigente

        if self._token and (self._token.refresh_expiry - margen) > now:
            try:
                logger.info("Access token vencido. Refrescando con refreshToken...")
                data = self._post_auth(
                    "authentication/refreshAccessToken", {"refreshToken": self._token.refresh_token}
                )
                self._store_token(data)
                return
            except CJAuthError as e:
                logger.warning("Refresh falló (%s). Re-autenticando desde cero...", e)

        self._authenticate()

    def _store_token(self, data: dict) -> None:
        self._token = _Token(
            access_token=data["accessToken"],
            access_expiry=_parse_dt(data["accessTokenExpiryDate"]),
            refresh_token=data["refreshToken"],
            refresh_expiry=_parse_dt(data["refreshTokenExpiryDate"]),
        )
        self._save_token_cache()
        logger.info("Token CJ actualizado. Access válido hasta %s", self._token.access_expiry.isoformat())

    # ---- Caché de token en disco ------------------------------------------
    def _save_token_cache(self) -> None:
        if not self.token_cache_path or not self._token:
            return
        try:
            self.token_cache_path.write_text(
                json.dumps(
                    {
                        "access_token": self._token.access_token,
                        "access_expiry": self._token.access_expiry.isoformat(),
                        "refresh_token": self._token.refresh_token,
                        "refresh_expiry": self._token.refresh_expiry.isoformat(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("No se pudo guardar la caché de token: %s", e)

    def _load_token_cache(self) -> None:
        if not (self.token_cache_path and self.token_cache_path.exists()):
            return
        try:
            d = json.loads(self.token_cache_path.read_text(encoding="utf-8"))
            self._token = _Token(
                access_token=d["access_token"],
                access_expiry=_parse_dt(d["access_expiry"]),
                refresh_token=d["refresh_token"],
                refresh_expiry=_parse_dt(d["refresh_expiry"]),
            )
            logger.info("Token CJ cargado de caché (access vence %s).", self._token.access_expiry.isoformat())
        except (OSError, KeyError, ValueError) as e:
            logger.warning("Caché de token ilegible (%s). Se ignorará.", e)
            self._token = None

    # ---- Petición autenticada genérica ------------------------------------
    def _request(self, method: str, path: str, params: Optional[dict] = None, json_body: Optional[dict] = None) -> Any:
        self._ensure_token()
        reauthed = False
        rate_attempts = 0

        while True:
            self._throttle()
            url = f"{BASE_URL}/{path.lstrip('/')}"
            assert self._token is not None  # garantizado por _ensure_token
            headers = {"Content-Type": "application/json", "CJ-Access-Token": self._token.access_token}
            resp = self._http(method, url, headers, params, json_body)

            try:
                payload = resp.json()
            except ValueError:
                raise CJError(f"Respuesta no-JSON (HTTP {resp.status_code}): {resp.text[:200]}")

            code = payload.get("code")
            if code == CODE_OK and payload.get("result", True):
                return payload.get("data")

            # Token rechazado -> re-autenticar una sola vez y reintentar
            if code in (CODE_TOKEN_INVALID, CODE_TOKEN_EMPTY) and not reauthed:
                logger.warning("Token rechazado (code=%s). Re-autenticando...", code)
                reauthed = True
                self._token = None
                self._authenticate()
                continue

            # Rate limit -> esperar y reintentar
            if code == CODE_RATE_LIMIT and rate_attempts < self.max_retries:
                rate_attempts += 1
                espera = 2 ** rate_attempts
                logger.warning("Rate limit (code=%s). Esperando %ss (intento %d)...", code, espera, rate_attempts)
                time.sleep(espera)
                continue

            if code == CODE_RATE_LIMIT:
                raise CJRateLimitError(payload.get("message", "Rate limit"), code, payload.get("requestId"))
            if code in (CODE_QUOTA_USED, CODE_POINTS_EMPTY):
                raise CJQuotaError(payload.get("message", "Cuota diaria agotada"), code, payload.get("requestId"))

            raise CJError(payload.get("message", f"Error CJ (code={code})"), code, payload.get("requestId"))

    # ======================================================================
    #  Métodos públicos de la API
    # ======================================================================
    def get_categories(self) -> list[dict]:
        """Devuelve el árbol de categorías (3 niveles). El categoryId del 3er
        nivel es el que se usa para filtrar productos."""
        return self._request("GET", "product/getCategory") or []

    def list_products(
        self,
        page_num: int = 1,
        page_size: int = 20,
        category_id: Optional[str] = None,
        product_name: Optional[str] = None,
        country_code: Optional[str] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        sort: Optional[str] = None,       # "desc" | "asc"
        order_by: Optional[str] = None,   # "createAt" | "listedNum"
    ) -> dict:
        """Lista productos paginados (endpoint product/list).

        Devuelve el objeto data con {list, total, pageNum, pageSize}.
        `product_name` filtra por nombre en inglés (productNameEn).
        """
        params = {
            "pageNum": page_num,
            "pageSize": page_size,
            "categoryId": category_id,
            "productNameEn": product_name,
            "countryCode": country_code,
            "minPrice": min_price,
            "maxPrice": max_price,
            "sort": sort,
            "orderBy": order_by,
        }
        params = {k: v for k, v in params.items() if v is not None}
        return self._request("GET", "product/list", params=params) or {}

    def iter_products(self, max_items: Optional[int] = None, page_size: int = 50, **filtros) -> Iterator[dict]:
        """Itera productos paginando automáticamente. Acepta los mismos filtros
        que list_products (category_id, product_name, etc.)."""
        obtenidos = 0
        page = 1
        while True:
            data = self.list_products(page_num=page, page_size=page_size, **filtros)
            lista = data.get("list") or []
            if not lista:
                break
            for prod in lista:
                yield prod
                obtenidos += 1
                if max_items and obtenidos >= max_items:
                    return
            total = data.get("total", 0) or 0
            if page * page_size >= total:
                break
            page += 1

    def get_product_detail(
        self, pid: Optional[str] = None, product_sku: Optional[str] = None, country_code: Optional[str] = None
    ) -> dict:
        """Detalle completo de un producto, incluyendo variantes (endpoint product/query)."""
        if not (pid or product_sku):
            raise ValueError("Indicá pid o product_sku.")
        params = {"pid": pid, "productSku": product_sku, "countryCode": country_code}
        params = {k: v for k, v in params.items() if v is not None}
        return self._request("GET", "product/query", params=params) or {}

    def get_stock_by_vid(self, vid: str) -> list[dict]:
        """Stock en tiempo real de una variante, por almacén (product/stock/queryByVid)."""
        return self._request("GET", "product/stock/queryByVid", params={"vid": vid}) or []

    # ======================================================================
    #  Normalización al modelo interno
    # ======================================================================
    @staticmethod
    def normalizar_producto(detalle: dict) -> NormalizedProduct:
        """Convierte la respuesta de product/query al modelo interno."""
        variantes: list[NormalizedVariant] = []
        for v in detalle.get("variants") or []:
            variantes.append(
                NormalizedVariant(
                    source_variant_id=v.get("vid"),
                    sku=v.get("variantSku"),
                    name=v.get("variantNameEn") or v.get("variantName") or "",
                    cost_price=_to_float(v.get("variantSellPrice")),
                    weight_g=_to_float(v.get("variantWeight")),
                    length_mm=_to_float(v.get("variantLength")),
                    width_mm=_to_float(v.get("variantWidth")),
                    height_mm=_to_float(v.get("variantHeight")),
                    image=v.get("variantImage"),
                )
            )

        imagenes = detalle.get("productImageSet") or []
        if not imagenes and detalle.get("bigImage"):
            imagenes = [detalle["bigImage"]]

        return NormalizedProduct(
            source="cj",
            source_id=detalle.get("pid"),
            name=detalle.get("productNameEn") or detalle.get("productName") or "",
            sku=detalle.get("productSku"),
            description=detalle.get("description"),
            category=detalle.get("categoryName"),
            main_image=detalle.get("bigImage") or detalle.get("productImage"),
            images=imagenes,
            weight_g=_to_float(detalle.get("productWeight")),
            cost_price=_to_float(detalle.get("sellPrice")),
            suggested_price=_to_float(detalle.get("suggestSellPrice")),
            listed_num=detalle.get("listedNum"),
            variants=variantes,
            raw=detalle,
        )

    def enriquecer_stock(self, producto: NormalizedProduct) -> NormalizedProduct:
        """Consulta el stock real de cada variante y lo agrega al producto.

        OJO: hace una llamada por variante. Con QPS=1 puede ser lento para
        productos con muchas variantes; usalo solo sobre candidatos ya filtrados.
        """
        for var in producto.variants:
            if not var.source_variant_id:
                continue
            try:
                almacenes = self.get_stock_by_vid(var.source_variant_id)
                var.stock_total = sum(int(a.get("totalInventoryNum") or 0) for a in almacenes)
            except CJError as e:
                logger.warning("No se pudo obtener stock de vid=%s: %s", var.source_variant_id, e)
        return producto
