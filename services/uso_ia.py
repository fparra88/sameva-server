"""
Uso de llamadas de IA por ciclo de facturación (spec 001: tope por plan).

Unidad: llamada a la API de Anthropic (`tenant_token_usage.llamadas`).
Cuentan los orígenes `agente`, `herramienta` y `resumen`; `transcripcion`
(Groq) no. Tope = `planes.max_leads_mensuales` (NULL o 0 = sin tope).
Ventana = ciclo de facturación `[fecha_renovacion - 1 mes, fecha_renovacion)`.
Sin suscripción, sin tope o en estado de plataforma `prueba` = sin tope.

Sin HTTP. El tenant siempre llega del servidor, nunca del modelo.
"""

import calendar
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from session import fetch_one

ORIGENES_IA = ("agente", "herramienta", "resumen")


@dataclass(frozen=True)
class UsoIA:
    usadas: int
    limite: int | None
    ciclo_inicio: datetime | None
    ciclo_fin: datetime | None

    @property
    def agotado(self) -> bool:
        return self.limite is not None and self.usadas >= self.limite

    @property
    def porcentaje(self) -> int | None:
        if self.limite is None:
            return None
        return int(self.usadas * 100 / self.limite)


def ciclo_de(fecha_renovacion: datetime) -> tuple[datetime, datetime]:
    """(inicio, fin) del ciclo que termina en `fecha_renovacion`."""
    anio, mes = fecha_renovacion.year, fecha_renovacion.month - 1
    if mes == 0:
        anio, mes = anio - 1, 12
    dia = min(fecha_renovacion.day, calendar.monthrange(anio, mes)[1])
    return fecha_renovacion.replace(year=anio, month=mes, day=dia), fecha_renovacion


def evaluar(usadas: int, limite: int | None, estado_plataforma: str | None) -> bool:
    """¿Hay que bloquear al agente? Sin tope (None/0) o en prueba: nunca."""
    if estado_plataforma == "prueba" or not limite:
        return False
    return usadas >= limite


async def uso_ciclo(tenant_id: UUID) -> UsoIA:
    fila = await fetch_one(
        """
        SELECT s.fecha_renovacion,
               NULLIF(p.max_leads_mensuales, 0) AS limite,
               ep.estado AS estado_plataforma
        FROM tenants t
        LEFT JOIN tenant_subscriptions     s  ON  s.tenant_id = t.id
        LEFT JOIN planes                   p  ON  p.nombre    = s.plan
        LEFT JOIN tenant_estado_plataforma ep ON ep.tenant_id = t.id
        WHERE t.id = $1
        """,
        tenant_id,
    )
    sin_tope = UsoIA(usadas=0, limite=None, ciclo_inicio=None, ciclo_fin=None)
    if (
        fila is None
        or fila["fecha_renovacion"] is None
        or fila["limite"] is None
        or fila["estado_plataforma"] == "prueba"
    ):
        return sin_tope

    inicio, fin = ciclo_de(fila["fecha_renovacion"])
    usadas = await fetch_one(
        """
        SELECT COALESCE(SUM(llamadas), 0)::int AS usadas
        FROM tenant_token_usage
        WHERE tenant_id = $1
          AND created_at >= $2 AND created_at < $3
          AND origen = ANY($4::text[])
        """,
        tenant_id,
        inicio,
        fin,
        list(ORIGENES_IA),
    )
    return UsoIA(
        usadas=usadas["usadas"] if usadas else 0,
        limite=fila["limite"],
        ciclo_inicio=inicio,
        ciclo_fin=fin,
    )


async def plan_siguiente(tenant_id: UUID) -> str | None:
    """Próximo plan activo, con precio en Stripe y mayor tope, según `orden`."""
    fila = await fetch_one(
        """
        SELECT sig.nombre
        FROM tenant_subscriptions s
        JOIN planes act ON act.nombre = s.plan
        JOIN planes sig ON sig.activo
                       AND sig.stripe_price_id IS NOT NULL
                       AND sig.orden > act.orden
                       AND (COALESCE(sig.max_leads_mensuales, 0) = 0
                            OR sig.max_leads_mensuales > act.max_leads_mensuales)
        WHERE s.tenant_id = $1
          AND COALESCE(act.max_leads_mensuales, 0) > 0
        ORDER BY sig.orden, sig.nombre
        LIMIT 1
        """,
        tenant_id,
    )
    return fila["nombre"] if fila else None
