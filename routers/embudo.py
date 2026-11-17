"""
Tablero del embudo para el dueño (pantalla «Embudo» del portal).

La ficha CRM vive dentro de cada conversación; esto es la vista de conjunto:
una sola lectura que junta, por lead, lo que se llenó a mano en el CRM
(etapa, monto, vendedor, seguimiento, motivo de pérdida) con lo que se sabe
por su chat (canal, mensajes, último mensaje y quién lo escribió, quién lo
atiende). Solo lectura: mover un lead sigue siendo `PATCH /pipeline/...`.

Las métricas por período (`/metricas`, `/metricas/conversion`,
`/metricas/tendencia`) siguen en routers/vendedores.py; acá va la foto de hoy.
"""

from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from deps import UsuarioActual, negocio_actual
from routers.vendedores import modulo_en_ruta
from schemas import (
    EmbudoCanalOut,
    EmbudoEtapaResumenOut,
    EmbudoLeadOut,
    EmbudoMotivoOut,
    EmbudoOut,
)
from services import pipeline_estados
from services.conversaciones import _ASIGNADO_NOMBRE, _HANDLE, _NOMBRE, _USERNAME
from services.pipeline_estados import ESTADOS_CERRADOS
from session import fetch_all

router = APIRouter(prefix="/tenants", tags=["vendedores"])

# La conversación más reciente del contacto. Las de números desvinculados no
# cuentan: están ocultas en la bandeja y el enlace «abrir chat» daría 404.
_ULTIMA_CONVERSACION = """
    SELECT c.id, c.channel_type, c.status, c.last_message_at, c.asignado_a
    FROM conversations c
    WHERE c.tenant_id = p.tenant_id AND c.user_id = p.user_id
      AND NOT EXISTS (SELECT 1 FROM agentes_canal ac
                      WHERE ac.id = c.agente_canal_id AND ac.estado = 'desvinculado')
    ORDER BY c.last_message_at DESC
    LIMIT 1
"""


@router.get("/{tenant_id}/embudo", response_model=EmbudoOut)
async def tablero_embudo(
    tenant_id: UUID = Depends(modulo_en_ruta),
    _: UsuarioActual = Depends(negocio_actual),
    limite_abiertos: int = Query(500, ge=1, le=500),
    limite_cerrados: int = Query(30, ge=0, le=200, description="Por cada etapa cerrada"),
):
    """
    `leads` trae los abiertos (los que hay que trabajar) y solo los últimos
    cerrados de cada etapa: ganados y perdidos crecen sin límite y el tablero
    no necesita la historia completa. Los totales de `etapas` sí son reales.
    """
    cerrados = list(ESTADOS_CERRADOS)

    filas = await fetch_all(
        f"""
        WITH base AS (
            SELECT p.*, ROW_NUMBER() OVER (
                PARTITION BY p.estado ORDER BY p.actualizado_en DESC
            ) AS rn
            FROM client_pipeline p
            WHERE p.tenant_id = $1
        )
        SELECT
            p.id, p.user_id,
            {_NOMBRE} AS cliente_nombre,
            {_HANDLE} AS cliente_handle,
            {_USERNAME} AS cliente_username,
            u.created_at AS cliente_desde,
            p.vendedor_id, v.nombre AS vendedor_nombre,
            p.estado, p.monto_estimado, p.motivo_perdida, p.actualizado_en,
            p.proximo_seguimiento, p.seguimiento_nota,
            h.alta_en, h.etapa_desde,
            cv.id AS conversation_id,
            cv.channel_type AS canal,
            cv.status AS chat_estado,
            cv.last_message_at AS ultimo_mensaje_en,
            cv.asignado_a AS chat_asignado_a,
            {_ASIGNADO_NOMBRE} AS chat_asignado_nombre,
            COALESCE(mc.total, 0) AS total_mensajes,
            COALESCE(mc.del_cliente, 0) AS mensajes_cliente,
            um.contenido AS ultimo_mensaje,
            um.role AS ultimo_mensaje_rol
        FROM base p
        JOIN users u ON u.id = p.user_id
        LEFT JOIN vendedores v ON v.id = p.vendedor_id
        LEFT JOIN LATERAL (
            SELECT
                MIN(creado_en) FILTER (WHERE estado_anterior IS NULL) AS alta_en,
                MAX(creado_en) FILTER (WHERE estado_anterior IS DISTINCT FROM estado_nuevo)
                    AS etapa_desde
            FROM pipeline_historial
            WHERE client_pipeline_id = p.id
        ) h ON true
        LEFT JOIN LATERAL ({_ULTIMA_CONVERSACION}) cv ON true
        LEFT JOIN portal_users pa ON pa.id = cv.asignado_a
        LEFT JOIN LATERAL (
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE m.role = 'user') AS del_cliente
            FROM messages m WHERE m.conversation_id = cv.id
        ) mc ON true
        LEFT JOIN LATERAL (
            SELECT LEFT(m.content, 160) AS contenido, m.role
            FROM messages m WHERE m.conversation_id = cv.id
            ORDER BY m.created_at DESC LIMIT 1
        ) um ON true
        WHERE p.rn <= CASE WHEN p.estado = ANY($2::text[]) THEN $3::int ELSE $4::int END
        ORDER BY p.actualizado_en DESC
        """,
        tenant_id,
        cerrados,
        limite_cerrados,
        limite_abiertos,
    )

    por_etapa = await fetch_all(
        """
        SELECT estado,
               COUNT(*) AS total,
               COALESCE(SUM(monto_estimado), 0) AS monto,
               COUNT(*) FILTER (WHERE vendedor_id IS NULL) AS sin_vendedor,
               COUNT(*) FILTER (WHERE proximo_seguimiento < NOW()) AS seguimientos_vencidos
        FROM client_pipeline
        WHERE tenant_id = $1
        GROUP BY estado
        """,
        tenant_id,
    )
    etapas = {f["estado"]: f for f in por_etapa}

    por_canal = await fetch_all(
        f"""
        SELECT COALESCE(cv.channel_type, 'sin_chat') AS canal,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE p.estado = 'ganado') AS ganados,
               COALESCE(SUM(p.monto_estimado) FILTER (WHERE p.estado = 'ganado'), 0)
                   AS monto_ganado
        FROM client_pipeline p
        LEFT JOIN LATERAL ({_ULTIMA_CONVERSACION}) cv ON true
        WHERE p.tenant_id = $1
        GROUP BY 1
        ORDER BY total DESC, canal
        """,
        tenant_id,
    )

    # Texto libre: se agrupa sin distinguir mayúsculas ni espacios de más
    # para que «Precio» y «precio » no salgan como dos motivos.
    motivos = await fetch_all(
        """
        SELECT LOWER(TRIM(motivo_perdida)) AS motivo, COUNT(*) AS total
        FROM client_pipeline
        WHERE tenant_id = $1 AND estado = 'perdido'
          AND NULLIF(TRIM(motivo_perdida), '') IS NOT NULL
        GROUP BY 1
        ORDER BY total DESC, motivo
        LIMIT 8
        """,
        tenant_id,
    )

    return EmbudoOut(
        leads=[
            EmbudoLeadOut(
                **dict(f),
                transiciones_posibles=pipeline_estados.transiciones_desde(f["estado"]),
            )
            for f in filas
        ],
        # Se recorre la máquina de estados para que las etapas vacías salgan
        # en 0 y en el orden del embudo (igual que /metricas).
        etapas=[
            EmbudoEtapaResumenOut(
                estado=estado,
                total=etapas[estado]["total"] if estado in etapas else 0,
                monto=Decimal(etapas[estado]["monto"]) if estado in etapas else Decimal(0),
                sin_vendedor=etapas[estado]["sin_vendedor"] if estado in etapas else 0,
                seguimientos_vencidos=(
                    etapas[estado]["seguimientos_vencidos"] if estado in etapas else 0
                ),
            )
            for estado in pipeline_estados.ESTADOS
        ],
        por_canal=[EmbudoCanalOut(**dict(f)) for f in por_canal],
        motivos_perdida=[EmbudoMotivoOut(**dict(f)) for f in motivos],
    )
