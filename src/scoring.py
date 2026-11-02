"""Módulo de scoring: decide qué productos vale la pena vender.

Dos mecanismos, como diseñamos:

1) GATES (knockout binario): si un producto falla un gate, se DESCARTA y no se
   puntúa. No se "compensan" con buen margen. Ej: marca registrada, margen por
   debajo del mínimo, demasiado pesado para enviar.

2) SCORE (continuo, ponderado): entre los que pasan los gates, se calcula un
   puntaje 0-1 con variables normalizadas y pesos. Sirve para ORDENAR
   candidatos, no para predecir ventas en términos absolutos.

IMPORTANTE / honestidad: las escalas de normalización y los pesos son un punto
de partida razonable, NO una verdad. Se calibran después con ventas reales
(regresión). Hasta entonces, tratá el score como un ranking relativo.

Monedas: CJ da costos en USD; MercadoLibre da precios en moneda local. Para el
margen se convierte el costo a moneda local con `usd_to_local_rate` (CONFIGURAR
según tu país: p.ej. ~40 para UYU). Los fees de marketplace/pasarela se
descuentan con `marketplace_fee_pct`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
#  Configuración (pesos, gates, escalas). Ajustable sin tocar la lógica.
# ---------------------------------------------------------------------------
@dataclass
class ScoringConfig:
    # --- Pesos de variables que SUMAN ---
    w_margin_pct: float = 0.20
    w_margin_abs: float = 0.10
    w_demand: float = 0.15
    w_stock: float = 0.05
    # --- Pesos de variables que RESTAN (penalizaciones) ---
    w_saturation: float = 0.10
    w_weight: float = 0.05
    w_return_risk: float = 0.05

    # --- Gates (knockout) ---
    min_margin_pct: float = 0.25          # margen mínimo aceptable
    max_weight_g: float = 3000.0          # más pesado que esto, se descarta (envío)
    require_market_price: bool = True     # exigir precio de mercado para puntuar
    blocked_keywords: tuple = (
        "nike", "adidas", "apple", "iphone", "samsung", "gucci", "disney",
        "rolex", "louis vuitton", "marvel", "lego", "playstation", "xbox",
    )

    # --- Económicas (para el margen) ---
    usd_to_local_rate: float = 1.0        # USD -> moneda local del research (¡CONFIGURAR!)
    marketplace_fee_pct: float = 0.13     # comisión marketplace/pasarela aprox.
    extra_cost_pct: float = 0.05          # otros costos variables (packaging, etc.)

    # --- Escalas de normalización (placeholder, calibrar con datos reales) ---
    margin_abs_target: float = 20.0       # margen "muy bueno" en moneda local
    demand_scale: float = 5000.0          # demanda (sold) que satura a 1.0
    saturation_scale: float = 50000.0     # nº de publicaciones que satura la penalización
    weight_scale_g: float = 3000.0        # peso que satura la penalización
    stock_target: float = 500.0           # stock que satura a 1.0

    # --- Riesgo de devolución por keyword (0=bajo, 1=alto) ---
    return_risk_default: float = 0.3
    return_risk_keywords: dict = field(default_factory=lambda: {
        "ropa": 0.6, "talle": 0.6, "zapat": 0.6, "calza": 0.6, "remera": 0.6,
        "reloj": 0.5, "electron": 0.5, "bateria": 0.5, "vidrio": 0.5,
        "fragil": 0.6, "celular": 0.5, "perfume": 0.4,
    })


# ---------------------------------------------------------------------------
#  Resultado
# ---------------------------------------------------------------------------
@dataclass
class ScoringComponent:
    name: str
    raw: Any                # valor crudo (antes de normalizar)
    normalized: float       # 0-1
    weight: float
    sign: str               # "+" suma, "-" resta

    @property
    def contribution(self) -> float:
        c = self.weight * self.normalized
        return c if self.sign == "+" else -c


@dataclass
class ScoringResult:
    passed: bool
    score: Optional[float] = None
    rejection_reason: Optional[str] = None
    margin_pct: Optional[float] = None
    margin_abs: Optional[float] = None
    sell_price: Optional[float] = None
    cost_local: Optional[float] = None
    components: list[ScoringComponent] = field(default_factory=list)


# ---------------------------------------------------------------------------
#  Helpers de normalización
# ---------------------------------------------------------------------------
def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _norm_linear(value: Optional[float], max_value: float) -> float:
    """0..1 saturando linealmente en max_value."""
    if value is None or max_value <= 0:
        return 0.0
    return _clamp(value / max_value)


def _norm_log(value: Optional[float], max_value: float) -> float:
    """0..1 en escala logarítmica (útil para demanda/saturación, muy dispersas)."""
    if value is None or value <= 0:
        return 0.0
    return _clamp(math.log10(1 + value) / math.log10(1 + max_value))


def _riesgo_devolucion(texto: str, cfg: ScoringConfig) -> float:
    texto = (texto or "").lower()
    for kw, risk in cfg.return_risk_keywords.items():
        if kw in texto:
            return risk
    return cfg.return_risk_default


# ---------------------------------------------------------------------------
#  Cálculo de margen
# ---------------------------------------------------------------------------
def compute_margin(cost_usd: Optional[float], sell_price_local: Optional[float],
                   cfg: ScoringConfig) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Devuelve (margin_pct, margin_abs, cost_local). None si faltan datos."""
    if cost_usd is None or sell_price_local is None or sell_price_local <= 0:
        return None, None, None
    cost_local = cost_usd * cfg.usd_to_local_rate
    fees = sell_price_local * (cfg.marketplace_fee_pct + cfg.extra_cost_pct)
    margin_abs = sell_price_local - cost_local - fees
    margin_pct = margin_abs / sell_price_local
    return margin_pct, margin_abs, cost_local


# ---------------------------------------------------------------------------
#  Gates baratos (no necesitan datos de mercado)
# ---------------------------------------------------------------------------
def precheck_gates(product: dict, cfg: Optional[ScoringConfig] = None) -> Optional[str]:
    """Gates que se evalúan ANTES de gastar cuota de research (keyword/peso).
    Devuelve el motivo de rechazo, o None si pasa."""
    cfg = cfg or ScoringConfig()
    texto = f"{product.get('category') or ''} {product.get('name') or ''}".lower()
    for kw in cfg.blocked_keywords:
        if kw in texto:
            return f"keyword bloqueada: '{kw}' (posible marca/IP)"
    weight = product.get("weight_g")
    if weight is not None and weight > cfg.max_weight_g:
        return f"peso {weight:.0f}g > máx {cfg.max_weight_g:.0f}g"
    return None


# ---------------------------------------------------------------------------
#  Scoring
# ---------------------------------------------------------------------------
def score_product(product: dict, variants: Optional[list[dict]] = None,
                  research: Optional[dict] = None, cfg: Optional[ScoringConfig] = None) -> ScoringResult:
    """Puntúa un producto. `product`/`variants`/`research` son dicts como los
    que devuelve la base de datos (Database.get_product / get_variants /
    get_market_research[0])."""
    cfg = cfg or ScoringConfig()
    variants = variants or []
    research = research or {}

    name = (product.get("name") or "")
    category = (product.get("category") or "")
    texto = f"{category} {name}".lower()

    # ============ GATES baratos (keyword/peso) ============
    motivo = precheck_gates(product, cfg)
    if motivo:
        return ScoringResult(False, rejection_reason=motivo)

    # ============ GATES que necesitan datos de mercado ============
    cost = product.get("cost_price")
    if cost is None:
        return ScoringResult(False, rejection_reason="sin precio de costo")

    sell = research.get("price_median")
    if sell is None and cfg.require_market_price:
        return ScoringResult(False, rejection_reason="sin precio de mercado (falta research)")
    if sell is None:  # fallback al sugerido de CJ (USD) convertido a local
        sp = product.get("suggested_price")
        sell = sp * cfg.usd_to_local_rate if sp else None

    margin_pct, margin_abs, cost_local = compute_margin(cost, sell, cfg)
    if margin_pct is None:
        return ScoringResult(False, rejection_reason="no se pudo calcular margen")
    if margin_pct < cfg.min_margin_pct:
        return ScoringResult(
            False, rejection_reason=f"margen {margin_pct:.0%} < mínimo {cfg.min_margin_pct:.0%}",
            margin_pct=margin_pct, margin_abs=margin_abs, sell_price=sell, cost_local=cost_local,
        )

    weight = product.get("weight_g")  # ya pasó el gate de peso; se usa para penalizar

    # ============ SCORE ============
    demand = research.get("demand_sold_top")
    saturation = research.get("total_results")
    stock = sum(v.get("stock_total") or 0 for v in variants) if variants else 0
    rr = _riesgo_devolucion(texto, cfg)

    comps = [
        ScoringComponent("margen_%",       round(margin_pct, 3), _clamp(margin_pct),                cfg.w_margin_pct,  "+"),
        ScoringComponent("margen_abs",     round(margin_abs, 2), _norm_linear(margin_abs, cfg.margin_abs_target), cfg.w_margin_abs, "+"),
        ScoringComponent("demanda",        demand,               _norm_log(demand, cfg.demand_scale),  cfg.w_demand,    "+"),
        ScoringComponent("stock",          stock,                _norm_linear(stock, cfg.stock_target), cfg.w_stock,    "+"),
        ScoringComponent("saturacion",     saturation,           _norm_log(saturation, cfg.saturation_scale), cfg.w_saturation, "-"),
        ScoringComponent("peso",           weight,               _norm_linear(weight, cfg.weight_scale_g), cfg.w_weight, "-"),
        ScoringComponent("riesgo_devol",   round(rr, 2),         rr,                                  cfg.w_return_risk, "-"),
    ]

    score = _clamp(sum(c.contribution for c in comps))

    return ScoringResult(
        passed=True, score=score, margin_pct=margin_pct, margin_abs=margin_abs,
        sell_price=sell, cost_local=cost_local, components=comps,
    )
