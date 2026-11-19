"""
Archivos que el agente envía al cliente por WhatsApp (familia `archivos`).

Dos mitades:

  1. Catálogo del negocio (portal): hasta `ARCHIVOS_MAX_POR_NEGOCIO` archivos
     (5), JPG/PNG/PDF/DOCX validados por firma (services/adjuntos.py). Los
     bytes van a S3 (services/almacen_archivos.py); la BD guarda metadatos.
  2. Envío del agente: la tool `enviar_archivo` manda el archivo NATIVO por
     WhatsApp. El `link` que lleva la petición lo descargan Meta/NeuroAPI de
     /api/media/{token} (vence en minutos) y nunca se escribe al cliente.

Reglas que no se relajan: el modelo solo elige `archivo_id`; tenant y
conversación los pone el servidor, y el archivo se busca SIEMPRE con el
tenant. Nunca se loguea contenido ni nombres de archivo.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status

from config import settings
from services import adjuntos, almacen_archivos, creditos, meta, whatsapp
from session import execute, fetch_all, fetch_one, fetch_value, transaccion

log = logging.getLogger("operativai.archivos")

FAMILIA = "archivos"
MAX_NOMBRE = 100
MAX_DESCRIPCION = 500


def _err(estado: int, codigo: str, mensaje: str) -> HTTPException:
    return HTTPException(status_code=estado, detail={"codigo": codigo, "mensaje": mensaje})


# ============================================================
# Plan y disponibilidad
# ============================================================
async def plan_incluye(tenant_id: UUID) -> bool:
    """La familia está en la lista del plan que rige hoy al negocio."""
    familias = await fetch_value("SELECT familias FROM limites_efectivos($1)", tenant_id)
    return FAMILIA in (familias or ())


async def exigir_disponible(tenant_id: UUID) -> None:
    """503 sin bucket; 402 si el plan no incluye la familia."""
    if not almacen_archivos.disponible():
        raise _err(503, "archivos_no_disponible", "Esta función no está disponible por ahora.")
    if not await plan_incluye(tenant_id):
        raise _err(
            402,
            "herramienta_no_incluida_en_plan",
            "Enviar archivos no está incluido en tu plan. Mejora tu plan para usarlo.",
        )


# ============================================================
# Catálogo (portal)
# ============================================================
_COLUMNAS = "id, nombre, descripcion, mime, tipo_wa, bytes, created_at"


async def listar(tenant_id: UUID) -> list[dict[str, Any]]:
    filas = await fetch_all(
        f"SELECT {_COLUMNAS} FROM tenant_archivos WHERE tenant_id = $1 ORDER BY created_at",
        tenant_id,
    )
    return [dict(f) for f in filas]


def _limpiar_descripcion(descripcion: str | None) -> str:
    texto = " ".join((descripcion or "").split())
    if len(texto) < 3:
        raise _err(422, "descripcion_invalida", "Explica en pocas palabras cuándo debe enviarse este archivo.")
    if len(texto) > MAX_DESCRIPCION:
        raise _err(422, "descripcion_invalida", f"La descripción admite hasta {MAX_DESCRIPCION} caracteres.")
    return texto


async def crear(
    tenant_id: UUID, contenido: bytes, nombre_archivo: str | None, descripcion: str | None
) -> dict[str, Any]:
    try:
        info = adjuntos.validar_adjunto(contenido, nombre_archivo)
    except adjuntos.AdjuntoInvalido as e:
        raise HTTPException(status_code=e.status_code, detail=e.mensaje)
    descripcion = _limpiar_descripcion(descripcion)

    clave = almacen_archivos.nueva_clave(tenant_id)
    # Primero S3: si falla no queda fila. Si la inserción falla (cupo), se borra el objeto.
    try:
        await almacen_archivos.subir(clave, contenido, info.mime)
    except almacen_archivos.ErrorAlmacen:
        raise _err(503, "almacen_no_disponible", "No se pudo guardar el archivo. Intenta de nuevo.")

    try:
        async with transaccion() as conn:
            # Serializa las altas del mismo negocio: sin esto, dos subidas
            # simultáneas pasarían el tope.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"archivos:{tenant_id}")
            usados = await conn.fetchval("SELECT count(*) FROM tenant_archivos WHERE tenant_id = $1", tenant_id)
            if usados >= settings.ARCHIVOS_MAX_POR_NEGOCIO:
                raise _err(
                    409,
                    "limite_archivos",
                    f"Ya tienes {settings.ARCHIVOS_MAX_POR_NEGOCIO} archivos. Borra uno para subir otro.",
                )
            fila = await conn.fetchrow(
                f"""
                INSERT INTO tenant_archivos
                    (tenant_id, nombre, descripcion, mime, tipo_wa, bytes, storage_key)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING {_COLUMNAS}
                """,
                tenant_id, info.nombre, descripcion, info.mime, info.tipo_wa, len(contenido), clave,
            )
    except Exception:
        try:
            await almacen_archivos.borrar(clave)
        except almacen_archivos.ErrorAlmacen:
            log.error("Objeto S3 huérfano tras un alta fallida (tenant %s)", tenant_id)
        raise
    return dict(fila)


async def actualizar(tenant_id: UUID, archivo_id: UUID, descripcion: str) -> dict[str, Any]:
    fila = await fetch_one(
        f"""
        UPDATE tenant_archivos SET descripcion = $3
         WHERE id = $1 AND tenant_id = $2
        RETURNING {_COLUMNAS}
        """,
        archivo_id, tenant_id, _limpiar_descripcion(descripcion),
    )
    if fila is None:
        raise _err(404, "archivo_no_encontrado", "Archivo no encontrado.")
    return dict(fila)


async def eliminar(tenant_id: UUID, archivo_id: UUID) -> None:
    fila = await fetch_one(
        "DELETE FROM tenant_archivos WHERE id = $1 AND tenant_id = $2 RETURNING storage_key",
        archivo_id, tenant_id,
    )
    if fila is None:
        raise _err(404, "archivo_no_encontrado", "Archivo no encontrado.")
    try:
        await almacen_archivos.borrar(fila["storage_key"])
    except almacen_archivos.ErrorAlmacen:
        # La fila ya no existe: el objeto quedó huérfano pero inaccesible.
        log.error("No se pudo borrar el objeto S3 de un archivo eliminado (tenant %s)", tenant_id)


async def descargar(tenant_id: UUID, archivo_id: UUID) -> tuple[str, str, bytes]:
    """(mime, nombre, bytes) para que el portal previsualice un archivo propio."""
    fila = await fetch_one(
        "SELECT mime, nombre, storage_key FROM tenant_archivos WHERE id = $1 AND tenant_id = $2",
        archivo_id, tenant_id,
    )
    if fila is None:
        raise _err(404, "archivo_no_encontrado", "Archivo no encontrado.")
    try:
        contenido = await almacen_archivos.leer(fila["storage_key"])
    except almacen_archivos.ErrorAlmacen:
        raise _err(503, "almacen_no_disponible", "No se pudo leer el archivo. Intenta de nuevo.")
    return fila["mime"], fila["nombre"], contenido


# ============================================================
# Lo que ve el agente
# ============================================================
async def catalogo_para_agente(tenant_id: UUID, canal: str) -> list[dict[str, Any]]:
    """
    Archivos que el agente puede ofrecer en esta conversación; vacío si no
    aplica (canal distinto de WhatsApp, bucket sin configurar, plan sin la
    familia o catálogo vacío). Vacío = la tool ni se le ofrece al modelo.
    """
    if canal != "whatsapp" or not almacen_archivos.disponible():
        return []
    if not await plan_incluye(tenant_id):
        return []
    return await listar(tenant_id)


def texto_catalogo(archivos: list[dict[str, Any]]) -> str:
    return "\n".join(
        f'- archivo_id: "{a["id"]}"\n  Nombre: {a["nombre"]}\n  Cuándo enviarlo: {a["descripcion"]}'
        for a in archivos
    )


def definicion(archivos: list[dict[str, Any]]) -> dict[str, Any]:
    """La tool `enviar_archivo`. Los ids válidos van en un enum: el modelo no inventa otros."""
    return {
        "name": "enviar_archivo",
        "description": (
            "Envía al cliente, por este mismo chat de WhatsApp, un archivo del catálogo del "
            "negocio (el archivo en sí, no un enlace). Úsala cuando el cliente pida ese "
            "material o cuando claramente le sirva. Envía solo lo que haga falta."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "archivo_id": {"type": "string", "enum": [str(a["id"]) for a in archivos]},
                "leyenda": {
                    "type": "string",
                    "description": "Texto corto opcional que acompaña al archivo (máx. 200 caracteres).",
                },
            },
            "required": ["archivo_id"],
        },
    }


SIN_CREDITOS = (
    "SIN_CREDITOS: El negocio no tiene créditos disponibles para enviar archivos en este "
    "momento. No lo vuelvas a intentar en esta respuesta; ofrece al cliente hablar con una persona."
)
NO_ENVIADO = (
    "No se pudo enviar el archivo ahora. No digas que lo enviaste; ofrece intentarlo de nuevo "
    "o hablar con una persona."
)


async def enviar(
    *,
    contexto,
    archivo_id: str,
    leyenda: str | None,
    referencia_cobro: str,
    simulacion: bool = False,
) -> str:
    """
    Manda el archivo al cliente de la conversación y devuelve el texto que ve
    el modelo. Nunca levanta. `contexto` es el ContextoMensaje del turno.
    """
    if simulacion:
        return "[SIMULADO] Modo prueba: el archivo no se envió de verdad."

    try:
        return await _enviar(contexto, archivo_id, leyenda, referencia_cobro)
    except Exception:  # noqa: BLE001 — el bucle del agente no puede romperse por esto
        log.exception("Falló el envío de un archivo (tenant %s)", contexto.tenant_id)
        return NO_ENVIADO


async def _enviar(contexto, archivo_id: str, leyenda: str | None, referencia_cobro: str) -> str:
    if contexto.canal != "whatsapp":
        return "Este canal no admite envío de archivos."
    if getattr(contexto, "archivos_enviados", 0) >= settings.ARCHIVOS_MAX_POR_TURNO:
        return "Ya enviaste el máximo de archivos en esta respuesta."
    if not almacen_archivos.disponible() or not await plan_incluye(contexto.tenant_id):
        return "El envío de archivos no está disponible para este negocio."

    try:
        uid = UUID(str(archivo_id))
    except ValueError:
        return "Ese archivo no existe en el catálogo."
    # SIEMPRE con el tenant del contexto: el modelo no elige de quién es el archivo.
    arch = await fetch_one(
        "SELECT mime, nombre, tipo_wa, bytes, storage_key FROM tenant_archivos WHERE id = $1 AND tenant_id = $2",
        uid, contexto.tenant_id,
    )
    if arch is None:
        return "Ese archivo no existe en el catálogo."

    if not settings.BASE_URL_BACKEND.startswith("https://"):
        log.error("BASE_URL_BACKEND no es HTTPS: no se pueden enviar archivos")
        return NO_ENVIADO

    # Imports locales: estos módulos importan servicios del agente.
    from services.agente import envio

    destino = contexto.destino
    if not await envio._ventana_abierta(destino):
        return (
            "No se puede enviar el archivo: pasaron más de 24 horas desde el último mensaje "
            "del cliente. Ofrécele hablar con una persona."
        )
    try:
        cred = await envio._credenciales(contexto.tenant_id, "whatsapp")
    except envio.SinCredenciales:
        return NO_ENVIADO
    if contexto.phone_number_id:
        cred = {**cred, "phone_number_id": contexto.phone_number_id}

    # Se reserva el cupo del turno antes de cualquier await largo: varias
    # llamadas en paralelo no pueden pasarse del tope.
    contexto.archivos_enviados = getattr(contexto, "archivos_enviados", 0) + 1

    cobro = await creditos.consumir_credito(
        contexto.tenant_id, FAMILIA, referencia_cobro, contexto.conversation_id
    )
    if not cobro.permitido:
        contexto.archivos_enviados -= 1
        return SIN_CREDITOS

    leyenda = " ".join((leyenda or "").split())[:200] or None
    info = adjuntos.InfoAdjunto(mime=arch["mime"], tipo_wa=arch["tipo_wa"], nombre=arch["nombre"])
    token = secrets.token_urlsafe(32)

    async with transaccion() as conn:
        mensaje = await conn.fetchrow(
            """
            INSERT INTO messages (conversation_id, tenant_id, role, content)
            VALUES ($1, $2, 'assistant', $3)
            RETURNING id
            """,
            contexto.conversation_id, contexto.tenant_id, adjuntos.texto_mensaje(info, leyenda),
        )
        # Un envío vencido o fallido no debe dejar su enlace vivo: la fila se
        # borra si el proveedor rechaza (más abajo).
        await conn.execute(
            """
            INSERT INTO message_attachments
                (tenant_id, message_id, direccion, mime, nombre, bytes, storage_key,
                 token_publico, expira_at)
            VALUES ($1, $2, 'out', $3, $4, $5, $6, $7, NOW() + make_interval(mins => $8))
            """,
            contexto.tenant_id, mensaje["id"], info.mime, info.nombre, arch["bytes"],
            arch["storage_key"], token, settings.ARCHIVOS_ENLACE_MINUTOS,
        )

    from services import conversaciones

    try:
        await conversaciones._mandar_media(
            cred, contexto.usuario_id, info, conversaciones.url_publica_adjunto(token), leyenda
        )
    except (whatsapp.ProveedorWhatsAppError, meta.MetaError, HTTPException):
        # ON DELETE CASCADE se lleva también la fila del adjunto.
        await execute("DELETE FROM messages WHERE id = $1", mensaje["id"])
        contexto.archivos_enviados -= 1
        log.warning("El proveedor rechazó un archivo (tenant %s)", contexto.tenant_id)
        return NO_ENVIADO

    await execute(
        "UPDATE conversations SET last_message_at = NOW() WHERE id = $1", contexto.conversation_id
    )
    from realtime import avisar_mensaje_nuevo

    await avisar_mensaje_nuevo(contexto.tenant_id, contexto.conversation_id)
    return "Archivo enviado al cliente por este chat."
