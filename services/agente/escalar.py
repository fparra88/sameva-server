"""
Herramienta `escalar_humano`: pasar la conversación a una persona.

Port del workflow `escalar-humano`. Marca la conversación como `transferred`
y avisa al portal para que alguien la tome; a partir de ahí el agente deja de
contestar (lo corta `ingreso._conversacion_transferida`).

Dos cosas que se conservan del original:

  - El aviso al portal va DESPUÉS del UPDATE y su fallo no revierte nada. Si
    la conversación quedó transferida pero el equipo no se enteró, al agente
    se le devuelve un texto distinto para que no le prometa al cliente una
    atención inmediata que nadie va a ver.
  - `escalar_humano` no consume crédito. Sin saldo, quien pide una persona
    tiene que poder llegar a ella (ver services/creditos.py).

El aviso ya no sale por HTTP contra este mismo proceso: se llama directo a la
función del router.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from session import execute, fetch_value

log = logging.getLogger("operativai.agente.escalar")

OK = "Conversación transferida a un humano. El equipo ha sido notificado."

# El UPDATE se hizo pero el portal no se enteró. El agente necesita saberlo
# para no prometer una atención que nadie tiene en pantalla todavía.
SIN_AVISO = (
    "La conversación quedó marcada como transferida a un humano, pero NO se pudo "
    "avisar al equipo en el portal. Dile al cliente que una persona lo atenderá en "
    "cuanto revise las conversaciones."
)

# Lo que recibe el cliente en cuanto el agente delega. Lo manda el servidor y
# no el modelo: así sale siempre, con el mismo texto, y el cliente no se queda
# sin respuesta (antes el bot simplemente callaba hasta su siguiente mensaje).
MENSAJE_DELEGACION = (
    "He delegado tu conversación a un colaborador de {negocio}, en un momento te atendemos."
)
MENSAJE_DELEGACION_SIN_NOMBRE = (
    "He delegado tu conversación a un colaborador del equipo, en un momento te atendemos."
)


def texto_delegacion(nombre_negocio: str | None) -> str:
    nombre = (nombre_negocio or "").strip()
    return MENSAJE_DELEGACION.format(negocio=nombre) if nombre else MENSAJE_DELEGACION_SIN_NOMBRE


async def nombre_negocio(tenant_id: UUID) -> str | None:
    return await fetch_value("SELECT name FROM tenants WHERE id = $1", tenant_id)


async def notificar_delegacion(*, tenant_id: UUID, conversation_id: UUID) -> str:
    """
    Guarda el aviso de delegación en el historial y devuelve el texto a
    entregar. Entregarlo por el canal es de quien llama.

    Apaga `ack_pendiente`: el cliente ya sabe que lo van a atender, y el acuse
    de `ingreso._conversacion_transferida` le repetiría lo mismo con su
    siguiente mensaje.
    """
    texto = texto_delegacion(await nombre_negocio(tenant_id))
    await execute(
        """
        INSERT INTO messages (conversation_id, tenant_id, role, content)
        VALUES ($1, $2, 'assistant', $3)
        """,
        conversation_id,
        tenant_id,
        texto,
    )
    await execute(
        """
        UPDATE conversations
           SET last_message_at = NOW(),
               metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), '{ack_pendiente}', 'false'::jsonb)
         WHERE id = $1
        """,
        conversation_id,
    )
    from realtime import avisar_mensaje_nuevo

    await avisar_mensaje_nuevo(tenant_id, conversation_id)
    return texto


async def escalar(
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    canal: str,
    motivo: str | None,
    area: str | None,
    cliente_nombre: str | None,
    cliente_telefono: str | None,
) -> str:
    """
    Transfiere y devuelve el texto que lee el agente.

    `ack_pendiente` queda en true por si el aviso de delegación no llega a
    salir (lo apaga `notificar_delegacion`): así el siguiente mensaje del
    cliente recibe al menos el acuse de ingreso._conversacion_transferida.

    `escalado_por_agente` es lo que enciende «Necesita atención de humano» en
    la bandeja (conversaciones.NECESITA_HUMANO_SQL) hasta que alguien del
    equipo escriba. Solo lo pone esta herramienta: tomar o asignar desde el
    portal no la marcan, porque ahí ya hay una persona al frente.
    """
    await execute(
        """
        UPDATE conversations
           SET status = 'transferred',
               metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                   'motivo_escalamiento', $2::text,
                   'escalado_en', NOW()::text,
                   'ack_pendiente', true,
                   'escalado_por_agente', true
               )
         WHERE id = $1
        """,
        conversation_id,
        motivo,
    )

    # Import diferido por la misma razón que en ingreso.py: esto es lógica de
    # negocio que hoy vive en un router, y los servicios no importan routers.
    from routers.eventos import conversacion_transferida
    from schemas import ConversacionTransferidaIn

    try:
        await conversacion_transferida(
            ConversacionTransferidaIn(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                canal=canal or "desconocido",
                motivo=motivo,
                cliente_nombre=cliente_nombre,
                cliente_telefono=cliente_telefono,
                area=area,
            )
        )
    except Exception:
        # La transferencia ya está hecha y es lo que importa: el bot deja de
        # contestar igual. Lo que falló es el aviso.
        log.exception(
            "Conversación %s transferida pero sin aviso al portal", conversation_id
        )
        return SIN_AVISO

    return OK


def definicion() -> dict[str, Any]:
    """
    El esquema que ve el modelo.

    Solo pide `motivo` y `area`: el tenant, la conversación y los datos del
    cliente los pone el servidor desde el contexto. Si el modelo pudiera
    elegir `conversation_id`, podría transferir la conversación de otro
    cliente.
    """
    return {
        "name": "escalar_humano",
        "description": (
            "Transfiere la conversación a una persona del equipo. Úsala en este mismo "
            "turno cuando el cliente pida una persona, cuando haya una queja seria o se "
            "note molesto, cuando no logres resolver su pedido tras dos intentos, cuando "
            "lo que pide esté fuera de tus herramientas, o cuando una herramienta falle "
            "o responda SIN_CREDITOS. Al usarla el sistema le avisa al cliente que lo "
            "atenderá una persona: no escribas despedida ni otro mensaje. Tras usarla "
            "dejas de atender esta conversación."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo": {
                    "type": "string",
                    "description": "Por qué se transfiere, en una frase.",
                },
                "area": {
                    "type": "string",
                    "enum": ["ventas", "agenda", "otro"],
                    "description": (
                        "A quién le corresponde: 'ventas' (cotizaciones, precios, "
                        "compras), 'agenda' (citas, reservas, horarios) u 'otro'. "
                        "Si dudas, usa 'otro'."
                    ),
                },
            },
            "required": ["motivo", "area"],
        },
    }
