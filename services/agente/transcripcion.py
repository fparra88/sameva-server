"""
Notas de voz a texto.

La API de Claude no acepta audio, así que la nota de voz se transcribe con un
proveedor aparte y al modelo le llega solo el texto. Hoy el único proveedor
es Groq (Whisper, API compatible con la de OpenAI); `TRANSCRIPCION_PROVIDER`
deja la puerta abierta a otro sin tocar quien llama.

Sin clave configurada `disponible()` es False y nadie llama a esto: el
agente sigue viendo "[Audio]", como antes de esta función.

Ni el audio ni el texto se escriben en el log: solo ids, bytes y segundos.
Los errores del proveedor se resumen en un código, sin el cuerpo de la
respuesta (podría traer fragmentos del audio transcrito).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

import httpx

from config import settings

log = logging.getLogger("operativai.agente.transcripcion")

# Groq cobra un mínimo de 10 s por petición.
SEGUNDOS_MINIMOS_COBRO = Decimal(10)


class TranscripcionError(Exception):
    """No se pudo transcribir. `codigo` es corto y sin datos del cliente."""

    def __init__(self, codigo: str):
        super().__init__(codigo)
        self.codigo = codigo


@dataclass(frozen=True)
class Transcripcion:
    texto: str
    segundos: Decimal | None
    proveedor: str
    modelo: str

    @property
    def costo_usd(self) -> Decimal:
        segundos = max(self.segundos or SEGUNDOS_MINIMOS_COBRO, SEGUNDOS_MINIMOS_COBRO)
        por_hora = Decimal(str(settings.TRANSCRIPCION_USD_POR_HORA))
        return (segundos * por_hora / Decimal(3600)).quantize(Decimal("0.000001"))


def disponible() -> bool:
    return settings.TRANSCRIPCION_PROVIDER == "groq" and bool(settings.GROQ_API_KEY)


async def transcribir(audio: bytes, nombre: str, mime: str) -> Transcripcion:
    """Lanza TranscripcionError si el proveedor no está o no contesta bien."""
    if not disponible():
        raise TranscripcionError("sin_proveedor")

    datos = {
        "model": settings.TRANSCRIPCION_MODELO,
        "response_format": "verbose_json",
        "temperature": "0",
    }
    if settings.TRANSCRIPCION_IDIOMA:
        datos["language"] = settings.TRANSCRIPCION_IDIOMA

    try:
        async with httpx.AsyncClient(timeout=settings.TRANSCRIPCION_TIMEOUT_S) as cliente:
            r = await cliente.post(
                f"{settings.GROQ_API_BASE_URL}/audio/transcriptions",
                headers={"Authorization": f"Bearer {settings.GROQ_API_KEY}"},
                data=datos,
                files={"file": (nombre, audio, mime)},
            )
    except httpx.TimeoutException as e:
        raise TranscripcionError("timeout") from e
    except httpx.HTTPError as e:
        raise TranscripcionError("sin_conexion") from e

    if r.status_code in (401, 403):
        log.error("Groq rechazó GROQ_API_KEY (%s)", r.status_code)
        raise TranscripcionError("credenciales")
    if r.status_code == 429:
        raise TranscripcionError("limite")
    if r.status_code != 200:
        raise TranscripcionError(f"http_{r.status_code}")

    try:
        cuerpo = r.json()
    except ValueError as e:
        raise TranscripcionError("respuesta_invalida") from e

    texto = str(cuerpo.get("text") or "").strip() if isinstance(cuerpo, dict) else ""
    if not texto:
        raise TranscripcionError("sin_texto")

    segundos: Decimal | None = None
    duracion = cuerpo.get("duration")
    if isinstance(duracion, (int, float)) and duracion >= 0:
        segundos = Decimal(str(round(float(duracion), 2)))

    return Transcripcion(
        texto=texto,
        segundos=segundos,
        proveedor="groq",
        modelo=settings.TRANSCRIPCION_MODELO,
    )
