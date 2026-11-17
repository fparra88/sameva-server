"""
Cifrado de los secretos de las herramientas (hoy: la contraseña de la base de
datos externa de un negocio).

Fernet (AES-128-CBC + HMAC) con la clave `DB_SECRETOS_KEY`. Para rotar se pone
la nueva en `DB_SECRETOS_KEY` y la vieja en `DB_SECRETOS_KEY_ANTERIOR`: se
descifra con ambas y se escribe siempre con la nueva (`MultiFernet`).

Lo cifrado lleva dentro el negocio y la herramienta a los que pertenece: si
alguien copiara una fila de `tool_secretos` a otro negocio, al descifrar no
coincidiría y se rechaza (el secreto de un negocio nunca sirve en otro).

Nunca se loguea el secreto, ni cifrado ni en claro.
"""

from __future__ import annotations

import json
import logging
from typing import Optional
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from config import settings
from session import execute, fetch_one

log = logging.getLogger("operativai.secretos")


class SecretosNoDisponibles(RuntimeError):
    """Falta DB_SECRETOS_KEY o es inválida: no se puede guardar ni leer nada."""


def disponible() -> bool:
    try:
        _fernet()
    except SecretosNoDisponibles:
        return False
    return True


def _fernet() -> MultiFernet:
    if not settings.DB_SECRETOS_KEY:
        raise SecretosNoDisponibles("DB_SECRETOS_KEY sin configurar")
    claves = [settings.DB_SECRETOS_KEY]
    if settings.DB_SECRETOS_KEY_ANTERIOR:
        claves.append(settings.DB_SECRETOS_KEY_ANTERIOR)
    try:
        return MultiFernet([Fernet(c.encode()) for c in claves])
    except (ValueError, TypeError) as e:
        raise SecretosNoDisponibles("DB_SECRETOS_KEY no es una clave Fernet válida") from e


def cifrar(tenant_id: UUID, tool_key: str, secreto: str) -> bytes:
    carga = json.dumps({"t": str(tenant_id), "k": tool_key, "s": secreto})
    return _fernet().encrypt(carga.encode())


def descifrar(tenant_id: UUID, tool_key: str, token: bytes) -> Optional[str]:
    """El secreto, o None si el token no es válido o no es de este negocio/herramienta."""
    try:
        carga = json.loads(_fernet().decrypt(bytes(token)))
    except (InvalidToken, ValueError):
        log.error("Secreto ilegible (tenant %s, herramienta %s)", tenant_id, tool_key)
        return None
    if carga.get("t") != str(tenant_id) or carga.get("k") != tool_key:
        log.error("Secreto de otro negocio/herramienta (tenant %s, herramienta %s)", tenant_id, tool_key)
        return None
    return carga.get("s")


async def guardar(conn, tenant_id: UUID, tool_key: str, secreto: str) -> None:
    """Escribe (o reemplaza) el secreto en la transacción de quien llama."""
    await conn.execute(
        """
        INSERT INTO tool_secretos (tenant_id, tool_key, secreto)
        VALUES ($1, $2, $3)
        ON CONFLICT (tenant_id, tool_key)
        DO UPDATE SET secreto = EXCLUDED.secreto, updated_at = NOW()
        """,
        tenant_id,
        tool_key,
        cifrar(tenant_id, tool_key, secreto),
    )


async def leer(tenant_id: UUID, tool_key: str) -> Optional[str]:
    fila = await fetch_one(
        "SELECT secreto FROM tool_secretos WHERE tenant_id = $1 AND tool_key = $2",
        tenant_id,
        tool_key,
    )
    if fila is None:
        return None
    return descifrar(tenant_id, tool_key, fila["secreto"])


async def borrar(tenant_id: UUID, tool_key: str) -> None:
    await execute(
        "DELETE FROM tool_secretos WHERE tenant_id = $1 AND tool_key = $2", tenant_id, tool_key
    )
