"""
Persona principal del equipo de plataforma (gerencia_users).

Sale del .env (GERENCIA_PRINCIPAL_*): se crea en `gerencia_users` al arrancar
si todavía no está, y es la única que agrega o quita al resto del equipo desde
el panel (deps.gerencia_principal_actual). Reemplaza el alta manual por SQL del
primer usuario.

Reglas:
  - Idempotente: si el correo ya existe (sin importar mayúsculas) no se toca,
    ni su nombre ni su cargo; lo que haya editado alguien en la base manda.
  - Cambiar el correo del .env NO borra al principal anterior: queda como
    miembro común. Nadie se quita solo, así que no hay forma de quedarse sin
    acceso por un cambio de configuración.
  - Para entrar al panel, el principal necesita además una cuenta de portal con
    ese correo (se registra o entra con Google como cualquiera): este alta solo
    le da el nivel.
"""

import logging

import asyncpg

from config import settings
from session import transaccion

log = logging.getLogger("operativai.gerencia_equipo")


def es_principal(email: str) -> bool:
    principal = settings.GERENCIA_PRINCIPAL_EMAIL
    return bool(principal) and email.strip().lower() == principal


def puede_administrar_equipo(email: str) -> bool:
    """Con principal configurado, solo él; sin principal, cualquier gerencia (regla previa)."""
    return es_principal(email) if settings.GERENCIA_PRINCIPAL_EMAIL else True


async def sembrar_principal() -> bool:
    """
    Crea al principal si falta. True si lo creó. Sin variable configurada no hace nada.

    Nunca tumba el arranque: si la base va atrasada de migraciones (p. ej. falta
    la tabla `gerencia_users`) o no responde, queda en el log y la API sube igual.
    Que no haya principal es un problema acotado; que no suba la API, no.
    """
    email = settings.GERENCIA_PRINCIPAL_EMAIL
    if not email:
        return False
    try:
        async with transaccion() as conn:
            existe = await conn.fetchval(
                "SELECT 1 FROM gerencia_users WHERE LOWER(email) = $1", email
            )
            if existe:
                return False
            # ON CONFLICT: con varios workers arrancando a la vez, solo uno inserta.
            await conn.execute(
                "INSERT INTO gerencia_users (email, full_name, cargo) VALUES ($1, $2, $3) ON CONFLICT (email) DO NOTHING",
                email,
                settings.GERENCIA_PRINCIPAL_NOMBRE,
                settings.GERENCIA_PRINCIPAL_CARGO,
            )
    except (asyncpg.PostgresError, OSError):
        log.exception(
            "No se pudo crear al principal de gerencia (¿faltan migraciones? "
            "corre `python aplicar_sql.py --revisar`). La API arranca sin él."
        )
        return False
    log.info("Principal de gerencia creado desde GERENCIA_PRINCIPAL_EMAIL")
    return True
