"""
Almacén de archivos en S3 (bucket privado).

Es la única pieza que habla con AWS: el resto del código llama a `subir`,
`leer` y `borrar`, así que cambiar de proveedor es cambiar este módulo.

  - Sin `ARCHIVOS_S3_BUCKET` la función queda apagada (`disponible()` es False
    y los endpoints contestan 503).
  - Credenciales: la cadena estándar de boto3. En AWS, el rol IAM de la
    instancia; no se guardan llaves en el .env.
  - boto3 es síncrono: cada llamada va a un hilo con `run_in_threadpool`.
  - boto3 se importa al usar, no al cargar el módulo, para que los tests y los
    entornos sin la función no lo necesiten.
  - Nunca se loguea el contenido de un archivo.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any
from uuid import UUID

from fastapi.concurrency import run_in_threadpool

from config import settings

log = logging.getLogger("operativai.almacen")

_cliente_s3: Any = None


class ErrorAlmacen(Exception):
    """Falló S3 (red, permisos, objeto inexistente). Sin detalles del proveedor."""


def disponible() -> bool:
    return bool(settings.ARCHIVOS_S3_BUCKET)


def nueva_clave(tenant_id: UUID) -> str:
    """Clave del objeto: `tenants/<tenant>/<uuid>`. No lleva el nombre del archivo."""
    return f"tenants/{tenant_id}/{uuid.uuid4()}"


def _cliente():
    global _cliente_s3
    if _cliente_s3 is None:
        import boto3  # noqa: PLC0415

        _cliente_s3 = boto3.client("s3", region_name=settings.ARCHIVOS_S3_REGION)
    return _cliente_s3


def _exigir() -> None:
    if not disponible():
        raise ErrorAlmacen("ARCHIVOS_S3_BUCKET sin configurar")


def _subir(clave: str, contenido: bytes, mime: str) -> None:
    _cliente().put_object(
        Bucket=settings.ARCHIVOS_S3_BUCKET,
        Key=clave,
        Body=contenido,
        ContentType=mime,
        ServerSideEncryption="AES256",
    )


def _leer(clave: str) -> bytes:
    return _cliente().get_object(Bucket=settings.ARCHIVOS_S3_BUCKET, Key=clave)["Body"].read()


def _borrar(clave: str) -> None:
    _cliente().delete_object(Bucket=settings.ARCHIVOS_S3_BUCKET, Key=clave)


async def subir(clave: str, contenido: bytes, mime: str) -> None:
    _exigir()
    try:
        await run_in_threadpool(_subir, clave, contenido, mime)
    except Exception as e:  # noqa: BLE001 — botocore tiene muchas excepciones
        log.error("No se pudo subir un archivo a S3: %s", type(e).__name__)
        raise ErrorAlmacen("No se pudo guardar el archivo") from e


async def leer(clave: str) -> bytes:
    _exigir()
    try:
        return await run_in_threadpool(_leer, clave)
    except Exception as e:  # noqa: BLE001
        log.error("No se pudo leer un archivo de S3: %s", type(e).__name__)
        raise ErrorAlmacen("No se pudo leer el archivo") from e


async def borrar(clave: str) -> None:
    """Borrar un objeto que ya no existe no es un error."""
    _exigir()
    try:
        await run_in_threadpool(_borrar, clave)
    except Exception as e:  # noqa: BLE001
        log.error("No se pudo borrar un archivo de S3: %s", type(e).__name__)
        raise ErrorAlmacen("No se pudo borrar el archivo") from e
