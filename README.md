# Sistema de research y publicación para Shopify

Sistema propio para vender más inteligentemente en Shopify: ingesta productos de
proveedores + señales de mercado, los puntúa con un scoring, genera fichas con IA
y publica en Shopify, cerrando el loop con ventas reales.

**Enfoque:** human-in-the-loop primero. La máquina sugiere y prepara; vos aprobás.
Todo se publica como borrador hasta validar.

## Estado actual

✅ **Conector de CJ Dropshipping** — catálogo, precio de compra, stock real,
peso/dimensiones y variantes. Es la "fuente de hechos" del sistema.

✅ **Conector de MercadoLibre** — demanda real (`sold_quantity`, en baldes),
precios de mercado, competencia y saturación por categoría. Es la "señal de
qué se vende de verdad". Requiere un login OAuth inicial (una vez).

✅ **Base de datos (SQLite)** — persiste productos, variantes, señales de
mercado y un historial de estados. Modela cada producto como máquina de estados
(`discovered → enriched → scored → ... → live`). Evita re-consultar las APIs.

✅ **Scoring** — gates duros (marca/IP, margen mínimo, peso) + score ponderado
0-1 con variables normalizadas (margen, demanda, stock, saturación, peso, riesgo
de devolución). Desglose transparente y pesos/escalas configurables para calibrar.

✅ **Pipeline de ingesta** — orquesta todo en un comando: CJ → DB → enriquecer
con MercadoLibre → scoring. Pre-descarta marcas/pesados antes de gastar cuota.

✅ **Fichas (sin IA por API)** — generás la ficha a mano (con plantilla) o con
cualquier IA web por copy-paste (el sistema arma el prompt). Mismo formato en
ambos casos, alineado con Shopify. Validación de campos y SEO incluida.

✅ **Panel de aprobación (consola)** — revisás cada producto con ficha y decidís
aprobar / rechazar / saltar. Sin costo ni dependencias. `python scripts/aprobar.py`

✅ **Conector de Shopify** — publica los aprobados como BORRADOR (Admin GraphQL
API): crea el producto, setea precio, sube imágenes por URL. Opcionalmente los
publica al canal Online Store.

✅ **Seguimiento de ventas** — lee los pedidos de Shopify y agrega métricas por
producto (unidades, ingresos, nº de pedidos). Cierra el loop para medir
resultados. `python scripts/sync_ventas.py`

✅ **Traducción EN→ES** — mejora el research en MercadoLibre traduciendo el
nombre del producto de CJ (glosario gratis, sin dependencias, ampliable en
`data/glosario.json`).

**El flujo completo está construido:** `discovered → enriched → scored → drafted
→ approved → published`, más el seguimiento de ventas. Todo probado con datos
simulados; falta validar los conectores con credenciales reales.

## Publicar en Shopify

Configurá `SHOPIFY_SHOP_DOMAIN` y `SHOPIFY_ACCESS_TOKEN` en `.env` (custom app
con scope `write_products`). Luego:

```powershell
python scripts/publicar.py             # crea los aprobados como BORRADOR
python scripts/publicar.py --publicar  # además publica en Online Store
```

## Fichas de producto (sin costo de IA)

Dos formas, las dos gratis:

```powershell
# Ver candidatos rankeados que necesitan ficha
python scripts/ficha.py listar

# Opción A — con IA web (copy-paste): genera un prompt para pegar en ChatGPT/Claude/Gemini
python scripts/ficha.py prompt 12
#   ...pegás el prompt, guardás el JSON de la respuesta en data/fichas/12_ficha.json

# Opción B — a mano: genera una plantilla para editar
python scripts/ficha.py plantilla 12

# Importar la ficha (cualquiera de las dos vías) -> el producto pasa a 'drafted'
python scripts/ficha.py importar 12 data/fichas/12_ficha.json
```

## Ejecutar el pipeline completo

Necesita CJ + MercadoLibre configurados (ver secciones de abajo) y
`USD_TO_LOCAL_RATE` ajustado en `.env`.

```powershell
python scripts/run_pipeline.py "kitchen gadget" 10
```

Trae hasta 10 productos, los enriquece con datos de mercado, los puntúa y deja
los candidatos rankeados en la base. La lógica está probada con dobles en
`scripts/probar_pipeline.py` (corre sin credenciales).

## Puesta en marcha

### 1. Instalar dependencias

```powershell
pip install -r requirements.txt
```

### 2. Conseguir la API Key de CJ

1. Creá una cuenta gratis en [cjdropshipping.com](https://cjdropshipping.com).
2. Andá a **My CJ → Authorization → API** y generá tu **API Key**
   (formato `CJUserNum@api@xxxx`).
3. La API es gratis; solo pagás cuando hacés un pedido real.

### 3. Configurar credenciales

```powershell
Copy-Item .env.example .env
```

Editá `.env` y pegá tu `CJ_API_KEY`.

### 4. Probar

```powershell
python scripts/probar_cj.py
# o con una palabra clave:
python scripts/probar_cj.py "kitchen gadget"
```

Si todo va bien, verás categorías, productos con precio, el detalle normalizado
y el stock real de una variante.

## MercadoLibre (demanda real)

MercadoLibre exige un login OAuth humano **una sola vez** (no permite
server-to-server). Después el conector refresca el token solo.

### 1. Crear la app

1. Entrá a [developers.mercadolibre.com.ar](https://developers.mercadolibre.com.ar) → **Mis aplicaciones** → crear app.
2. Configurá un **Redirect URI** (ej. `https://localhost/callback`).
3. Copiá **App ID** y **Secret Key** al `.env` (`ML_CLIENT_ID`, `ML_CLIENT_SECRET`,
   `ML_REDIRECT_URI`) y elegí tu país en `ML_SITE_ID` (MLA=Argentina, MLM=México, etc.).

### 2. Login inicial (una vez)

```powershell
python scripts/bootstrap_ml.py
```

Abrí la URL que imprime, aprobá la app, y pegá la URL de redirección (la que
tiene `?code=...`). Aunque el navegador muestre error 404, lo único que importa
es esa URL.

### 3. Probar

```powershell
python scripts/probar_ml.py
python scripts/probar_ml.py "auriculares bluetooth"
```

> **Sobre `sold_quantity`:** MercadoLibre lo devuelve en baldes para ítems
> ajenos (1-5 exacto; arriba, el piso del rango). Es señal de demanda por orden
> de magnitud, no cifra exacta. Para tamaño de mercado por nicho, usá
> `total_items_in_this_category`.

## Estructura

```
.
├── config.py                   # configuración central (lee .env)
├── requirements.txt
├── .env.example                # plantilla de credenciales (copiar a .env)
├── data/                       # caché de tokens y, más adelante, la base de datos
├── scripts/
│   ├── probar_cj.py            # prueba end-to-end del conector de CJ
│   ├── bootstrap_ml.py         # login OAuth inicial de MercadoLibre (una vez)
│   ├── probar_ml.py            # prueba del conector de MercadoLibre
│   ├── probar_db.py            # prueba end-to-end de la base de datos
│   ├── probar_scoring.py       # prueba del scoring (gates + ranking)
│   ├── probar_pipeline.py      # prueba del pipeline con dobles (sin red)
│   ├── run_pipeline.py         # ejecuta el pipeline REAL (con credenciales)
│   ├── ficha.py                # fichas: listar/plantilla/prompt/importar
│   ├── probar_fichas.py        # prueba del flujo de fichas
│   ├── aprobar.py              # panel de aprobación por consola
│   ├── probar_aprobacion.py    # prueba de la lógica de aprobación
│   ├── publicar.py             # publica los aprobados en Shopify (borrador)
│   ├── probar_shopify.py       # prueba del conector de Shopify (sin red)
│   ├── sync_ventas.py          # sincroniza ventas reales desde Shopify
│   └── probar_analytics.py     # prueba del seguimiento de ventas
└── src/
    ├── database.py             # SQLite: productos, variantes, señales, estados, métricas
    ├── scoring.py              # gates + score ponderado de productos
    ├── pipeline.py             # orquestador: CJ -> ML -> scoring
    ├── listings.py             # fichas: plantilla, prompt para IA, validación
    ├── approval.py             # lógica del panel de aprobación
    ├── analytics.py            # seguimiento de ventas (pedidos -> métricas)
    ├── translation.py          # traducción EN->ES para el research
    └── connectors/
        ├── cj_dropshipping.py  # cliente de la API V2.0 de CJ
        ├── mercadolibre.py     # cliente de research de MercadoLibre
        └── shopify.py          # cliente Admin GraphQL (publicar + leer ventas)
```

## Notas técnicas

- **Token cacheado:** el `accessToken` de CJ dura 15 días y se guarda en
  `data/cj_token.json` (ignorado por git). No se re-pide en cada llamada.
- **Rate limit:** ajustá `CJ_QPS` en `.env` según tu nivel de cuenta
  (Free = 1 req/s). El cliente respeta ese ritmo automáticamente.
- **Reintentos:** ante errores de red o rate limit, reintenta con backoff.
  Si la cuota diaria se agota (`CJQuotaError`), no reintenta (es por día).
- La API V2.0 de CJ puede autenticar con `apiKey` (recomendado) o, en cuentas
  antiguas, con `email` + `password`. El cliente intenta lo primero y cae a lo
  segundo si configurás `CJ_EMAIL` y `CJ_PASSWORD`.
```
