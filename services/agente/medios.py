"""
El archivo que mandó el cliente: bajarlo, validarlo y guardarlo.

Corre en segundo plano mientras `ingreso` espera la ráfaga, así que no suma
latencia al caso normal. Nunca lanza: un adjunto que no se pudo bajar deja el
mensaje con su texto ("[Imagen]", "[Audio]"), que es lo que el agente veía
antes de esta función.

Por canal (Facebook queda fuera a propósito: solo trae una URL del CDN de Meta,
que no se descarga nunca por SSRF):

    WhatsApp   por el id del media, con las credenciales del canal del tenant
               (Graph directo o NeuroAPI, ver adjuntos.descargar_media_whatsapp)
    Instagram  por el proxy de NeuroAPI (adjuntos.descargar_media_instagram)

Imágenes y documentos se guardan en `message_attachments`, como siempre, para
el portal; el agente los analiza después solo si el plan incluye la familia
`multimedia` (services/agente/multimedia.py).

Las notas de voz NO se guardan: si el plan incluye la familia, hay proveedor
de transcripción y hay saldo, se transcriben, se cobra 1 crédito y queda solo
el texto (`message_transcripciones`, sql/77). Si no, ni se descargan.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, Optional
from uuid import UUID

from services import adjuntos, creditos
from services.agente import multimedia, transcripcion
from services.agente.modelos import MensajeNormalizado
from session import execute, fetch_one

log = logging.getLogger("operativai.agente.medios")

# Tipos del canal que se descargan. Video y stickers no se analizan: se quedan
# con su marca de texto, como antes.
_TIPOS_WHATSAPP = frozenset({"image", "document", "audio"})
_TIPOS_INSTAGRAM = frozenset({"image", "file", "audio"})
_TIPOS_AUDIO = frozenset({"audio"})


def lanzar(
    tenant_id: UUID,
    conversation_id: UUID,
    message_id: UUID,
    mensaje: MensajeNormalizado,
    proveedor: Optional[str],
) -> Optional[asyncio.Task]:
    """La tarea de este adjunto, o None si el mensaje no trae nada que bajar."""
    se_baja = mensaje.media_id and (
        (mensaje.canal == "whatsapp" and mensaje.tipo in _TIPOS_WHATSAPP)
        or (mensaje.canal == "instagram" and proveedor == "neuroapi" and mensaje.tipo in _TIPOS_INSTAGRAM)
    )
    if not se_baja:
        return None
    return asyncio.create_task(
        procesar(tenant_id, conversation_id, message_id, mensaje),
        name=f"medios:{message_id}",
    )


async def procesar(
    tenant_id: UUID,
    conversation_id: UUID,
    message_id: UUID,
    mensaje: MensajeNormalizado,
) -> bool:
    """True si quedó algo guardado (adjunto o transcripción)."""
    try:
        return await _procesar(tenant_id, conversation_id, message_id, mensaje)
    except Exception:
        log.exception("Falló el adjunto del mensaje %s (tenant %s)", message_id, tenant_id)
        return False


async def _procesar(
    tenant_id: UUID,
    conversation_id: UUID,
    message_id: UUID,
    mensaje: MensajeNormalizado,
) -> bool:
    es_audio = mensaje.tipo in _TIPOS_AUDIO
    if es_audio and not await _puede_transcribir(tenant_id):
        # Sin familia, sin proveedor o sin saldo: no se baja algo que no se
        # va a guardar.
        return False

    media_id = str(mensaje.media_id)
    try:
        contenido = await _descargar(tenant_id, mensaje.canal or "", media_id)
        info = adjuntos.validar_adjunto_entrante(
            contenido,
            base=f"{mensaje.canal}-{media_id[-12:]}",
            nombre=mensaje.contenido if mensaje.tipo == "document" else None,
        )
    except adjuntos.AdjuntoInvalido as e:
        log.warning(
            "Adjunto del mensaje %s no guardado (tenant %s): %s",
            message_id, tenant_id, e.status_code,
        )
        return False

    if info.tipo_wa == "audio":
        guardado = await _transcribir(tenant_id, conversation_id, message_id, contenido, info)
    else:
        guardado = await _guardar_adjunto(tenant_id, message_id, contenido, info)

    if guardado:
        from realtime import avisar_mensaje_nuevo

        await avisar_mensaje_nuevo(tenant_id, conversation_id)
    return guardado


async def _descargar(tenant_id: UUID, canal: str, media_id: str) -> bytes:
    if canal == "instagram":
        return await adjuntos.descargar_media_instagram(media_id)
    cred = await _credenciales_whatsapp(tenant_id)
    if cred is None:
        raise adjuntos.AdjuntoInvalido(503, "El canal no tiene credenciales activas.")
    return await adjuntos.descargar_media_whatsapp(media_id, cred)


async def _credenciales_whatsapp(tenant_id: UUID) -> dict[str, Any] | None:
    fila = await fetch_one("SELECT * FROM get_channel_credentials($1, $2)", tenant_id, "whatsapp")
    return dict(fila) if fila is not None else None


async def _puede_transcribir(tenant_id: UUID) -> bool:
    if not transcripcion.disponible():
        return False
    if not await multimedia.plan_incluye(tenant_id):
        return False
    return await creditos.saldo_total(tenant_id) >= 1


async def _guardar_adjunto(
    tenant_id: UUID, message_id: UUID, contenido: bytes, info: adjuntos.InfoAdjunto
) -> bool:
    try:
        await fetch_one(
            """
            INSERT INTO message_attachments
                (tenant_id, message_id, direccion, mime, nombre, bytes, contenido, token_publico)
            VALUES ($1, $2, 'in', $3, $4, $5, $6, $7)
            RETURNING id
            """,
            tenant_id,
            message_id,
            info.mime,
            info.nombre,
            len(contenido),
            contenido,
            secrets.token_urlsafe(32),
        )
    except Exception:
        log.exception("No se pudo guardar el adjunto del mensaje %s (tenant %s)", message_id, tenant_id)
        return False
    return True


async def _transcribir(
    tenant_id: UUID,
    conversation_id: UUID,
    message_id: UUID,
    audio: bytes,
    info: adjuntos.InfoAdjunto,
) -> bool:
    """
    Transcribe, cobra y guarda solo el texto. El audio se descarta al salir.

    Se transcribe antes de cobrar: si el proveedor falla, el cliente no paga
    un crédito por nada. Si entre medio se acabó el saldo, el texto se tira.
    """
    try:
        resultado = await transcripcion.transcribir(audio, info.nombre, info.mime)
    except transcripcion.TranscripcionError as e:
        log.warning(
            "No se pudo transcribir el audio del mensaje %s (tenant %s): %s",
            message_id, tenant_id, e.codigo,
        )
        return False

    cobro = await creditos.consumir_credito(
        tenant_id, "transcripcion", f"transcripcion:{message_id}", conversation_id
    )
    if not cobro.permitido:
        log.warning("Audio del mensaje %s sin saldo (tenant %s): no se guarda", message_id, tenant_id)
        return False

    await execute(
        """
        INSERT INTO message_transcripciones
            (message_id, tenant_id, texto, segundos, proveedor, modelo)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (message_id) DO NOTHING
        """,
        message_id,
        tenant_id,
        resultado.texto,
        resultado.segundos,
        resultado.proveedor,
        resultado.modelo[:100],
    )
    await _registrar_costo(tenant_id, conversation_id, message_id, resultado)
    log.info(
        "Audio del mensaje %s transcrito (tenant %s): %s bytes, %s s",
        message_id, tenant_id, len(audio), resultado.segundos,
    )
    return True


async def _registrar_costo(
    tenant_id: UUID,
    conversation_id: UUID,
    message_id: UUID,
    resultado: transcripcion.Transcripcion,
) -> None:
    from services.gerencia import registrar_uso_tokens

    try:
        await registrar_uso_tokens(
            tenant_id,
            conversation_id=conversation_id,
            origen="transcripcion",
            modelo=f"{resultado.proveedor}:{resultado.modelo}"[:100],
            tokens_entrada=0,
            tokens_salida=0,
            costo_usd=resultado.costo_usd,
            idempotency_key=f"msg:{message_id}:transcripcion",
        )
    except Exception:
        # Perder la medición no puede costarle la transcripción al cliente.
        log.exception("No se pudo registrar el costo de la transcripción del mensaje %s", message_id)
