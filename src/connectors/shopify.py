"""Conector para la Shopify Admin GraphQL API (publicar productos).

Publica un producto a partir de una ficha. Por defecto lo crea como BORRADOR
(status=DRAFT, sin publicar a ningún canal): el producto queda en tu admin de
Shopify para que lo revises antes de ponerlo a la venta.

Puntos clave de la API (verificados, versión 2026-04):
- Auth: header X-Shopify-Access-Token con el token de la custom app (shpat_...).
- Endpoint: https://{shop}.myshopify.com/admin/api/{version}/graphql.json
- productCreate YA NO acepta precio/variantes en el input: crea el producto y
  su variante por defecto; el precio se setea aparte con productVariantsBulkUpdate.
- status DRAFT/ACTIVE/ARCHIVED. Crear NO publica en canales: para borrador,
  dejamos DRAFT y no publicamos.
- Rate limit por costo: se lee throttleStatus de extensions.cost; ante THROTTLED
  se espera y se reintenta.
- Dos niveles de error: errors[] (transporte/throttle/permiso) y userErrors[]
  (validación de negocio).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


# ============================================================================
#  Excepciones
# ============================================================================
class ShopifyError(Exception):
    def __init__(self, message: str, errors: Any = None):
        super().__init__(message)
        self.errors = errors


class ShopifyThrottledError(ShopifyError):
    """Se agotó la capacidad de rate limit tras los reintentos."""


class ShopifyUserError(ShopifyError):
    """Error de validación de negocio (userErrors) en una mutation."""
    def __init__(self, message: str, user_errors: list):
        super().__init__(message)
        self.user_errors = user_errors


# ============================================================================
#  Cliente
# ============================================================================
class ShopifyClient:
    def __init__(self, shop_domain: str, access_token: str, api_version: str = "2026-04",
                 timeout: int = 30, max_retries: int = 3):
        domain = (shop_domain or "").strip().replace("https://", "").replace("http://", "").rstrip("/")
        if domain and "." not in domain:
            domain = f"{domain}.myshopify.com"
        if not domain or not access_token:
            raise ShopifyError("Faltan SHOPIFY_SHOP_DOMAIN o SHOPIFY_ACCESS_TOKEN.")

        self.domain = domain
        self.api_version = api_version
        self.endpoint = f"https://{domain}/admin/api/{api_version}/graphql.json"
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": access_token,
        })

    # ---- HTTP con reintentos de red ---------------------------------------
    def _post(self, payload: dict):
        last_exc: Optional[Exception] = None
        for i in range(self.max_retries):
            try:
                return self._session.post(self.endpoint, json=payload, timeout=self.timeout)
            except requests.RequestException as e:
                last_exc = e
                espera = 2 ** i
                logger.warning("Error de red (%s). Reintento %d/%d en %ss...", e, i + 1, self.max_retries, espera)
                time.sleep(espera)
        raise ShopifyError(f"Fallo de red tras {self.max_retries} intentos: {last_exc}")

    @staticmethod
    def _throttle_wait(payload: dict) -> float:
        cost = (payload.get("extensions") or {}).get("cost") or {}
        ts = cost.get("throttleStatus") or {}
        requested = cost.get("requestedQueryCost") or 0
        disponible = ts.get("currentlyAvailable") or 0
        restore = ts.get("restoreRate") or 50
        falta = max(0, requested - disponible)
        return max(1.0, falta / restore) if restore else 2.0

    def _graphql(self, query: str, variables: Optional[dict] = None) -> dict:
        """Ejecuta una operación GraphQL. Maneja throttle y errores top-level."""
        for intento in range(self.max_retries + 1):
            resp = self._post({"query": query, "variables": variables or {}})
            try:
                payload = resp.json()
            except ValueError:
                raise ShopifyError(f"Respuesta no-JSON (HTTP {resp.status_code}): {resp.text[:200]}")

            errors = payload.get("errors")
            if errors:
                throttled = any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors)
                if throttled and intento < self.max_retries:
                    espera = self._throttle_wait(payload)
                    logger.warning("THROTTLED. Esperando %.1fs (intento %d)...", espera, intento + 1)
                    time.sleep(espera)
                    continue
                if throttled:
                    raise ShopifyThrottledError("Rate limit excedido tras reintentos", errors)
                raise ShopifyError(errors[0].get("message", "Error GraphQL"), errors)

            return payload.get("data") or {}
        raise ShopifyThrottledError("Rate limit: sin capacidad")

    @staticmethod
    def _check_user_errors(obj: dict, contexto: str) -> None:
        ue = (obj or {}).get("userErrors") or (obj or {}).get("mediaUserErrors") or []
        if ue:
            detalle = "; ".join(f"{e.get('field')}: {e.get('message')}" for e in ue)
            raise ShopifyUserError(f"{contexto}: {detalle}", ue)

    # ======================================================================
    #  Operaciones
    # ======================================================================
    def product_create(self, product_input: dict, media: Optional[list[dict]] = None) -> dict:
        """Crea el producto (con su variante por defecto). Devuelve el product."""
        query = """
        mutation CrearProducto($product: ProductCreateInput!, $media: [CreateMediaInput!]) {
          productCreate(product: $product, media: $media) {
            product { id handle status variants(first: 1) { nodes { id } } }
            userErrors { field message }
          }
        }"""
        data = self._graphql(query, {"product": product_input, "media": media})
        res = data.get("productCreate") or {}
        self._check_user_errors(res, "productCreate")
        return res["product"]

    def set_variant_price(self, product_id: str, variant_id: str, price: Any,
                          compare_at_price: Any = None) -> None:
        """Setea el precio de una variante (la API maneja precio aparte del producto)."""
        query = """
        mutation FijarPrecio($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
          productVariantsBulkUpdate(productId: $productId, variants: $variants) {
            productVariants { id price compareAtPrice }
            userErrors { field message }
          }
        }"""
        variante = {"id": variant_id, "price": str(price)}
        if compare_at_price is not None:
            variante["compareAtPrice"] = str(compare_at_price)
        data = self._graphql(query, {"productId": product_id, "variants": [variante]})
        self._check_user_errors(data.get("productVariantsBulkUpdate") or {}, "precio")

    def add_media(self, product_id: str, image_urls: list[str]) -> None:
        """Agrega imágenes por URL al producto."""
        if not image_urls:
            return
        query = """
        mutation AgregarImagenes($productId: ID!, $media: [CreateMediaInput!]!) {
          productCreateMedia(productId: $productId, media: $media) {
            media { mediaContentType status }
            mediaUserErrors { field message }
          }
        }"""
        media = [{"originalSource": u, "mediaContentType": "IMAGE"} for u in image_urls]
        data = self._graphql(query, {"productId": product_id, "media": media})
        self._check_user_errors(data.get("productCreateMedia") or {}, "imágenes")

    def get_publication_id(self, name: str = "Online Store") -> Optional[str]:
        """Devuelve el ID de un canal de venta por nombre (para publicar)."""
        query = "{ publications(first: 25) { edges { node { id name } } } }"
        data = self._graphql(query)
        for edge in (data.get("publications") or {}).get("edges") or []:
            node = edge.get("node") or {}
            if node.get("name") == name:
                return node.get("id")
        return None

    def publish(self, resource_id: str, publication_id: str) -> None:
        """Publica un producto (resource_id) en un canal (publication_id)."""
        query = """
        mutation Publicar($id: ID!, $input: [PublicationInput!]!) {
          publishablePublish(id: $id, input: $input) {
            userErrors { field message }
          }
        }"""
        data = self._graphql(query, {"id": resource_id, "input": [{"publicationId": publication_id}]})
        self._check_user_errors(data.get("publishablePublish") or {}, "publicación")

    # ======================================================================
    #  Lectura: pedidos (para métricas de ventas)
    # ======================================================================
    def iter_orders(self, query_filter: Optional[str] = None, page_size: int = 50,
                    line_items_per_order: int = 50):
        """Itera pedidos paginando con cursores. `query_filter` usa la sintaxis de
        búsqueda de Shopify (ej. 'processed_at:>=2026-01-01 test:false').
        Requiere el scope read_orders (últimos 60 días; histórico mayor necesita
        read_all_orders). Pide solo datos de producto/cantidad/monto (no PII)."""
        gql = """
        query Pedidos($cursor: String, $q: String) {
          orders(first: %d, after: $cursor, sortKey: PROCESSED_AT, query: $q) {
            edges { node {
              id name processedAt displayFinancialStatus test cancelledAt
              currentTotalPriceSet { shopMoney { amount currencyCode } }
              lineItems(first: %d) { nodes {
                quantity currentQuantity
                product { id }
                variant { id product { id } }
                discountedTotalSet { shopMoney { amount currencyCode } }
              } }
            } }
            pageInfo { hasNextPage endCursor }
          }
        }""" % (page_size, line_items_per_order)

        cursor = None
        while True:
            data = self._graphql(gql, {"cursor": cursor, "q": query_filter})
            conn = data.get("orders") or {}
            for edge in conn.get("edges") or []:
                yield edge["node"]
            page = conn.get("pageInfo") or {}
            if page.get("hasNextPage"):
                cursor = page.get("endCursor")
            else:
                break

    # ======================================================================
    #  Alto nivel: publicar una ficha
    # ======================================================================
    def publish_listing(self, ficha: dict, status: str = "DRAFT",
                        images: Optional[list[str]] = None,
                        publish_online_store: bool = False) -> dict:
        """Crea un producto desde una ficha. Por defecto BORRADOR sin publicar.

        ficha: dict con title, body_html, vendor, product_type, tags, handle,
               seo_title, seo_description, price, compare_at_price.
        """
        product_input: dict[str, Any] = {
            "title": ficha.get("title"),
            "descriptionHtml": ficha.get("body_html"),
            "vendor": ficha.get("vendor") or None,
            "productType": ficha.get("product_type") or None,
            "tags": ficha.get("tags") or [],
            "status": status,
        }
        if ficha.get("handle"):
            product_input["handle"] = ficha["handle"]
        seo = {}
        if ficha.get("seo_title"):
            seo["title"] = ficha["seo_title"]
        if ficha.get("seo_description"):
            seo["description"] = ficha["seo_description"]
        if seo:
            product_input["seo"] = seo
        product_input = {k: v for k, v in product_input.items() if v is not None}

        media = [{"originalSource": u, "mediaContentType": "IMAGE"} for u in (images or [])]
        product = self.product_create(product_input, media=media or None)
        gid = product["id"]

        price = ficha.get("price")
        variant_nodes = (product.get("variants") or {}).get("nodes") or []
        if price is not None and variant_nodes:
            self.set_variant_price(gid, variant_nodes[0]["id"], price, ficha.get("compare_at_price"))

        if publish_online_store:
            pub_id = self.get_publication_id("Online Store")
            if pub_id:
                self.publish(gid, pub_id)
            else:
                logger.warning("No se encontró el canal 'Online Store'; el producto quedó sin publicar.")

        return {"id": gid, "handle": product.get("handle"), "status": product.get("status")}
