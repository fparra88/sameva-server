"""
Aviso al dueño cuando el negocio agota su tope de llamadas de IA (spec 001, D6).

Una alerta `limite_llamadas_ia` por negocio y ciclo de facturación: el dedupe
es por (tenant, `datos.ciclo_fin`). El correo sale por el espejo genérico de
`broadcast_alerta` (el tipo no está en `TIPOS_SIN_ESPEJO`). Sin contenido de
mensajes: solo IDs y contadores.
"""

import logging
from uuid import UUID

from services.uso_ia import UsoIA
from session import transaccion

log = logging.getLogger("operativai.limite_ia")

TIPO_ALERTA = "limite_llamadas_ia"
_TITULO = "Llegaste al límite de llamadas de IA de tu plan"
_MENSAJE = (
    "El agente dejó de responder hasta que se reinicie tu ciclo. "
    "Tus clientes reciben un aviso de que un asesor los contactará. "
    "Sube de plan para reactivarlo."
)


async def avisar_owner_una_vez(tenant_id: UUID, uso: UsoIA) -> bool:
    """
    Avisa a owner/superadmin una sola vez por ciclo. Devuelve True si emitió el
    aviso ahora, False si ya existía para ese ciclo o si no hay tope/ciclo.
    Nunca lanza: avisar no debe romper la respuesta al cliente final.
    """
    if uso.limite is None or uso.ciclo_fin is None:
        return False
    ciclo_fin = uso.ciclo_fin.isoformat()
    try:
        # El lock de transacción serializa avisos concurrentes del mismo
        # negocio: el segundo ve la alerta que dejó el primero.
        async with transaccion() as conn:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))",
                f"{TIPO_ALERTA}:{tenant_id}",
            )
            existe = await conn.fetchval(
                """
                SELECT 1 FROM alertas
                 WHERE tenant_id = $1 AND tipo = $2 AND datos->>'ciclo_fin' = $3
                 LIMIT 1
                """,
                tenant_id,
                TIPO_ALERTA,
                ciclo_fin,
            )
            if existe:
                return False

            from realtime import avisar_gerencia

            await avisar_gerencia(
                tenant_id,
                TIPO_ALERTA,
                _TITULO,
                _MENSAJE,
                {
                    "ciclo_fin": ciclo_fin,
                    "usadas": uso.usadas,
                    "limite": uso.limite,
                },
            )
            return True
    except Exception:  # noqa: BLE001
        log.exception("No se pudo avisar del tope de llamadas IA (tenant %s)", tenant_id)
        return False
