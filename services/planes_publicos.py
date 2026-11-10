"""
Planes que muestra la landing pública.

Misma tabla `planes` que administra gerencia (routers/gerencia_planes.py) y
que lee el portal (/api/pagos/catalogo). Se ofrece un plan en la landing solo
si está activo Y tiene `stripe_price_id`: sin Price no se puede contratar en
línea (crear-pago respondería 409), así que no se promete.
"""

from schemas import PlanPublicoOut
from session import fetch_all


async def planes_publicos() -> list[PlanPublicoOut]:
    filas = await fetch_all(
        """
        SELECT nombre, descripcion, precio_monthly,
               max_vendedores, max_proveedores, max_leads_mensuales,
               creditos_incluidos_mensual,
               agente_ia_activo, gestion_vendedores_activo,
               herramientas_activo, crm_campo_activo, calendario_activo,
               destacado
        FROM planes
        WHERE activo AND stripe_price_id IS NOT NULL
        ORDER BY orden, precio_monthly
        """
    )
    return [PlanPublicoOut(**dict(f)) for f in filas]
