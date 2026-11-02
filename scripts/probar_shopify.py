"""Prueba del conector de Shopify SIN tocar la red.

Reemplaza el método _graphql del cliente por un doble que devuelve respuestas
simuladas (en el formato real de Shopify), para validar que publish_listing
arma bien las mutations, setea el precio y maneja userErrors.

Uso:
    python scripts/probar_shopify.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.connectors.shopify import ShopifyClient, ShopifyUserError


FICHA = {
    "title": "Organizador de Cables Magnético",
    "body_html": "<p>Ordená tu escritorio.</p>",
    "vendor": "Mi Tienda",
    "product_type": "Organización",
    "tags": ["oficina", "cables"],
    "handle": "organizador-cables",
    "seo_title": "Organizador de cables magnético",
    "seo_description": "Mantené los cables ordenados.",
    "price": 590,
    "compare_at_price": 790,
}


def _fake_ok(calls):
    def fake(query, variables=None):
        calls.append((query, variables))
        if "productCreateMedia" in query:
            return {"productCreateMedia": {"media": [{"mediaContentType": "IMAGE", "status": "READY"}], "mediaUserErrors": []}}
        if "productVariantsBulkUpdate" in query:
            return {"productVariantsBulkUpdate": {"productVariants": [{"id": "gid://shopify/ProductVariant/11", "price": "590.0", "compareAtPrice": "790.0"}], "userErrors": []}}
        if "productCreate" in query:
            return {"productCreate": {"product": {"id": "gid://shopify/Product/1", "handle": "organizador-cables", "status": "DRAFT", "variants": {"nodes": [{"id": "gid://shopify/ProductVariant/11"}]}}, "userErrors": []}}
        if "publications" in query:
            return {"publications": {"edges": [{"node": {"id": "gid://shopify/Publication/9", "name": "Online Store"}}]}}
        if "publishablePublish" in query:
            return {"publishablePublish": {"userErrors": []}}
        return {}
    return fake


def _fake_user_error(query, variables=None):
    if "productCreate" in query:
        return {"productCreate": {"product": None, "userErrors": [{"field": "handle", "message": "ya está en uso"}]}}
    return {}


def main() -> int:
    cliente = ShopifyClient(shop_domain="mi-tienda", access_token="shpat_dummy")
    assert cliente.endpoint == "https://mi-tienda.myshopify.com/admin/api/2026-04/graphql.json"
    print(f"endpoint: {cliente.endpoint}")

    # --- 1. Publicación OK como BORRADOR ------------------------------------
    print("\n=== 1. Crear como BORRADOR (sin publicar al canal) ===")
    calls: list = []
    cliente._graphql = _fake_ok(calls)
    res = cliente.publish_listing(FICHA, status="DRAFT", images=["https://x/1.jpg"])
    print(f"  resultado: {res}")
    assert res["id"] == "gid://shopify/Product/1"
    assert res["status"] == "DRAFT"

    # El productCreate fue con status DRAFT y los datos de la ficha
    qc = next((v for q, v in calls if "productCreate" in q and "productCreateMedia" not in q), None)
    assert qc is not None
    assert qc["product"]["status"] == "DRAFT"
    assert qc["product"]["title"] == FICHA["title"]
    assert qc["product"]["seo"]["title"] == FICHA["seo_title"]
    assert qc["media"] == [{"originalSource": "https://x/1.jpg", "mediaContentType": "IMAGE"}]
    print("  productCreate: status DRAFT + título + seo + media OK")

    # Se seteó el precio en la variante por defecto
    qp = next((v for q, v in calls if "productVariantsBulkUpdate" in q), None)
    assert qp is not None and qp["variants"][0]["price"] == "590"
    assert qp["variants"][0]["compareAtPrice"] == "790"
    print("  precio seteado en la variante por defecto OK")

    # No se publicó al canal (es borrador)
    assert not any("publishablePublish" in q for q, _ in calls)
    print("  no se publicó a ningún canal (correcto para borrador)")

    # --- 2. Publicar también al Online Store --------------------------------
    print("\n=== 2. Crear y publicar al Online Store ===")
    calls2: list = []
    cliente._graphql = _fake_ok(calls2)
    cliente.publish_listing(FICHA, status="DRAFT", publish_online_store=True)
    assert any("publications" in q for q, _ in calls2)
    assert any("publishablePublish" in q for q, _ in calls2)
    print("  consultó publications y llamó publishablePublish OK")

    # --- 3. userError debe lanzar ShopifyUserError --------------------------
    print("\n=== 3. Manejo de userErrors ===")
    cliente._graphql = _fake_user_error
    try:
        cliente.publish_listing(FICHA)
        print("  [X] ERROR: debería haber fallado")
        return 1
    except ShopifyUserError as e:
        print(f"  [OK] Capturado: {e}")

    # --- 4. throttle wait (cálculo) -----------------------------------------
    espera = ShopifyClient._throttle_wait({"extensions": {"cost": {"requestedQueryCost": 200,
                "throttleStatus": {"currentlyAvailable": 100, "restoreRate": 50}}}})
    assert espera == 2.0, espera
    print(f"\n=== 4. Cálculo de espera por throttle: {espera}s (correcto) ===")

    print("\n[OK] El conector de Shopify funciona (crear borrador, precio, publicar, errores).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
