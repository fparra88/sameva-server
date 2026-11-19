"""
Validación de adjuntos de WhatsApp (JPG, PNG, PDF, DOCX) sin dependencias.

Igual que services/imagen.py, el tipo se decide por la FIRMA de los bytes y
no por el nombre ni por el Content-Type, que los pone el cliente. La
extensión del nombre se exige además y tiene que coincidir con lo detectado:
un PDF renombrado a .png, o un ZIP cualquiera renombrado a .docx, no pasan.
"""

import io
import json
import os
import re
import zipfile
from urllib.parse import quote, urlparse

import httpx
from dataclasses import dataclass

from config import settings
from services.imagen import FIRMA_JPEG, FIRMA_PNG

# mime -> (tipo de mensaje de WhatsApp, extensiones válidas)
TIPOS: dict[str, tuple[str, tuple[str, ...]]] = {
    "image/jpeg": ("image", (".jpg", ".jpeg")),
    "image/png": ("image", (".png",)),
    "application/pdf": ("document", (".pdf",)),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
        "document",
        (".docx",),
    ),
}

MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Texto que se guarda en messages.content cuando no hay leyenda.
ETIQUETA_IMAGEN = "[Imagen]"


class AdjuntoInvalido(Exception):
    """El adjunto no se acepta. `status_code` es el HTTP que corresponde."""

    def __init__(self, status_code: int, mensaje: str):
        super().__init__(mensaje)
        self.status_code = status_code
        self.mensaje = mensaje


@dataclass(frozen=True)
class InfoAdjunto:
    mime: str
    tipo_wa: str  # "image" | "document"
    nombre: str


def tope_absoluto() -> int:
    """Lo máximo que vale la pena leer de una subida antes de validar."""
    return max(settings.WA_IMAGEN_MAX_BYTES, settings.WA_DOCUMENTO_MAX_BYTES)


def tope_entrante() -> int:
    """Lo máximo que se baja de un canal: incluye las notas de voz."""
    return max(tope_absoluto(), settings.WA_AUDIO_MAX_BYTES)


def limite_para(mime: str) -> int:
    tipo_wa = TIPOS[mime][0]
    return settings.WA_IMAGEN_MAX_BYTES if tipo_wa == "image" else settings.WA_DOCUMENTO_MAX_BYTES


def nombre_seguro(nombre: str | None) -> str:
    """Solo el nombre de archivo (sin ruta ni caracteres de control), acotado."""
    base = os.path.basename((nombre or "").replace("\\", "/"))
    base = re.sub(r"[\x00-\x1f\x7f]", "", base).strip()
    if len(base) > 100:
        raiz, ext = os.path.splitext(base)
        base = raiz[: 100 - len(ext)] + ext
    return base


def _es_docx(contenido: bytes) -> bool:
    if not contenido.startswith(b"PK\x03\x04"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(contenido)) as z:
            nombres = z.namelist()
    except zipfile.BadZipFile:
        return False
    return "[Content_Types].xml" in nombres and any(n.startswith("word/") for n in nombres)


def _mime_por_firma(contenido: bytes) -> str | None:
    if contenido.startswith(FIRMA_PNG):
        return "image/png"
    if contenido.startswith(FIRMA_JPEG):
        return "image/jpeg"
    # La especificación de PDF admite basura antes de la cabecera, dentro
    # del primer KB.
    if b"%PDF-" in contenido[:1024]:
        return "application/pdf"
    if _es_docx(contenido):
        return MIME_DOCX
    return None


def validar_adjunto(contenido: bytes, nombre: str | None) -> InfoAdjunto:
    """
      422 -> vacío o sin nombre
      415 -> no es JPG/PNG/PDF/DOCX, o la extensión no coincide con el contenido
      413 -> pesa más que el tope de su tipo
    """
    if not contenido:
        raise AdjuntoInvalido(422, "El archivo está vacío.")

    limpio = nombre_seguro(nombre)
    if not limpio:
        raise AdjuntoInvalido(422, "El archivo no tiene nombre.")

    mime = _mime_por_firma(contenido)
    if mime is None:
        raise AdjuntoInvalido(
            415, "Tipo de archivo no permitido. Solo JPG, PNG, PDF y DOCX."
        )

    tipo_wa, extensiones = TIPOS[mime]
    if os.path.splitext(limpio)[1].lower() not in extensiones:
        raise AdjuntoInvalido(
            415, "La extensión del archivo no coincide con su contenido."
        )

    tope = limite_para(mime)
    if len(contenido) > tope:
        raise AdjuntoInvalido(
            413, f"El archivo pesa más de {tope // (1024 * 1024)} MB."
        )

    return InfoAdjunto(mime=mime, tipo_wa=tipo_wa, nombre=limpio)


def texto_mensaje(info: InfoAdjunto, leyenda: str | None) -> str:
    """messages.content: la leyenda si hay; si no, una etiqueta legible."""
    if leyenda and leyenda.strip():
        return leyenda.strip()
    if info.tipo_wa == "image":
        return ETIQUETA_IMAGEN
    return f"[Documento: {info.nombre}]"


# ------------------------------------------------------------
# Entrantes (lo que manda el cliente): además, notas de voz
# ------------------------------------------------------------
# El audio SOLO se acepta de entrada y nunca se guarda: se transcribe y se
# descarta (services/agente/medios.py). Por eso no está en `TIPOS`, que es lo
# que valida el composer del portal: ampliarlo dejaría enviar audio desde ahí.
TIPOS_AUDIO: dict[str, tuple[str, ...]] = {
    "audio/ogg": (".ogg", ".opus", ".oga"),
    "audio/mpeg": (".mp3",),
    "audio/mp4": (".m4a",),
    "audio/wav": (".wav",),
}


def _es_mp3(contenido: bytes) -> bool:
    if contenido.startswith(b"ID3"):
        return True
    if len(contenido) < 2 or contenido[0] != 0xFF or (contenido[1] & 0xE0) != 0xE0:
        return False
    # Capa 00 es reservada (y es la que usa el AAC en ADTS, que no es MP3) y
    # versión 01 también.
    return (contenido[1] & 0x06) != 0 and ((contenido[1] >> 3) & 0x03) != 1


def mime_audio_por_firma(contenido: bytes) -> str | None:
    if contenido.startswith(b"OggS"):
        return "audio/ogg"
    if contenido[4:8] == b"ftyp" and contenido[8:12] in (b"M4A ", b"M4B "):
        return "audio/mp4"
    if contenido.startswith(b"RIFF") and contenido[8:12] == b"WAVE":
        return "audio/wav"
    if _es_mp3(contenido):
        return "audio/mpeg"
    return None


def validar_adjunto_entrante(contenido: bytes, base: str, nombre: str | None = None) -> InfoAdjunto:
    """
    Lo que mandó un cliente por el canal: JPG/PNG/PDF/DOCX como siempre, o
    una nota de voz (`tipo_wa='audio'`).

    El nombre que da el canal (el `filename` de un documento) se respeta si
    su extensión coincide con lo detectado; si no, o si no hay, se arma
    `base` + la extensión del tipo real. El tipo lo decide la firma, nunca el
    nombre.
      422 -> vacío
      415 -> ningún tipo admitido
      413 -> pesa más que el tope de su tipo
    """
    if not contenido:
        raise AdjuntoInvalido(422, "El archivo está vacío.")

    audio = mime_audio_por_firma(contenido)
    if audio is not None:
        if len(contenido) > settings.WA_AUDIO_MAX_BYTES:
            raise AdjuntoInvalido(
                413, f"El audio pesa más de {settings.WA_AUDIO_MAX_BYTES // (1024 * 1024)} MB."
            )
        return InfoAdjunto(mime=audio, tipo_wa="audio", nombre=f"{nombre_seguro(base)}{TIPOS_AUDIO[audio][0]}")

    mime = _mime_por_firma(contenido)
    if mime is None:
        raise AdjuntoInvalido(415, "Tipo de archivo no admitido.")
    limpio = nombre_seguro(nombre)
    if not limpio or os.path.splitext(limpio)[1].lower() not in TIPOS[mime][1]:
        limpio = f"{nombre_seguro(base)}{TIPOS[mime][1][0]}"
    return validar_adjunto(contenido, limpio)


def cabeceras_descarga(nombre: str, *, privado: bool) -> dict[str, str]:
    """Cabeceras de respuesta al servir un adjunto (nombre codificado RFC 5987)."""
    return {
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(nombre)}",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=3600" if privado else "no-store",
    }


def host_permitido() -> str:
    """Único host del que se descargan archivos entrantes: el de NeuroAPI."""
    return (urlparse(settings.NEUROAPI_API_BASE_URL).hostname or "").lower()


def nombre_desde_url(url: str) -> str:
    return nombre_seguro(os.path.basename(urlparse(url).path))


async def descargar_media(url: str) -> bytes:
    """
    Baja el archivo que NeuroAPI ya resolvió y dejó en una URL HTTPS directa.

    La URL llega en un webhook, así que se trata como no confiable (SSRF):
    solo https, solo el host de NeuroAPI, sin seguir redirecciones, y se corta
    la lectura al pasar el tope absoluto sin cargar todo en memoria.
    """
    partes = urlparse(url)
    if partes.scheme != "https" or (partes.hostname or "").lower() != host_permitido():
        raise AdjuntoInvalido(422, "La URL del archivo no es de un origen permitido.")
    return await _descargar(url, {})


# Ids de media de Instagram: solo lo que puede ir en un segmento de ruta sin
# escapar. Llega en un webhook, así que no se le deja meter "/" ni "..".
_MEDIA_ID_INSTAGRAM = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")


async def descargar_media_instagram(media_id: str) -> bytes:
    """
    Baja un adjunto de Instagram por el proxy de NeuroAPI
    (`GET /neuroapi/instagram/media/{media_id}`, con NEUROAPI_INSTA_KEY).

    Nunca se descarga la URL de CDN de Meta que pueda traer el webhook: el
    único origen es el host de NeuroAPI, igual que en WhatsApp.
    """
    if not _MEDIA_ID_INSTAGRAM.match(media_id or "") or media_id.strip(".") == "":
        raise AdjuntoInvalido(422, "El identificador del archivo no es válido.")
    if not settings.NEUROAPI_INSTA_KEY:
        raise AdjuntoInvalido(503, "NEUROAPI_INSTA_KEY sin configurar.")
    url = f"{settings.NEUROAPI_API_BASE_URL}/neuroapi/instagram/media/{quote(media_id, safe='')}"
    return await _descargar(url, {"x-api-key": settings.NEUROAPI_INSTA_KEY})


def nombre_por_contenido(contenido: bytes, base: str) -> str | None:
    """`base` con la extensión del tipo detectado, o None si no es un tipo admitido."""
    mime = _mime_por_firma(contenido)
    if mime is None:
        return None
    return f"{base}{TIPOS[mime][1][0]}"


async def _descargar(url: str, cabeceras: dict[str, str]) -> bytes:
    """GET sin seguir redirecciones, cortando al pasar el tope de entrada."""
    contenido, _ = await _descargar_con_tipo(url, cabeceras)
    return contenido


async def _descargar_con_tipo(url: str, cabeceras: dict[str, str]) -> tuple[bytes, str]:
    """Como `_descargar`, devolviendo también el Content-Type de la respuesta."""
    tope = tope_entrante()
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as cliente:
            async with cliente.stream("GET", url, headers=cabeceras) as r:
                if r.status_code != 200:
                    raise AdjuntoInvalido(502, f"No se pudo descargar el archivo ({r.status_code}).")
                trozos: list[bytes] = []
                total = 0
                async for trozo in r.aiter_bytes():
                    total += len(trozo)
                    if total > tope:
                        raise AdjuntoInvalido(413, f"El archivo pesa más de {tope // (1024 * 1024)} MB.")
                    trozos.append(trozo)
                tipo = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    except httpx.HTTPError as e:
        raise AdjuntoInvalido(502, f"No se pudo descargar el archivo: {type(e).__name__}") from e
    return b"".join(trozos), tipo


# ------------------------------------------------------------
# WhatsApp: el webhook trae solo el id del media (formato de Meta)
# ------------------------------------------------------------
# Único host del que se baja un media de Graph (las URL firmadas que devuelve
# `GET /{media_id}`). Llega en una respuesta de Meta, pero se valida igual.
_HOSTS_MEDIA_META = ("lookaside.fbsbx.com",)


def _id_media_valido(media_id: str) -> bool:
    return bool(_MEDIA_ID_INSTAGRAM.match(media_id or "")) and media_id.strip(".") != ""


async def descargar_media_whatsapp(media_id: str, cred: dict) -> bytes:
    """
    Baja un media de WhatsApp con las credenciales del canal del tenant
    (`get_channel_credentials`). El token se usa solo en la cabecera.

      - bsp_provider='neuroapi': `GET {NEUROAPI}{NEUROAPI_WA_MEDIA_PATH}` con la
        x-api-key del tenant. Si NeuroAPI contesta un JSON con la URL ya
        resuelta, esa URL tiene que ser HTTPS del host de NeuroAPI.
      - Meta directo: `GET {graph}/{media_id}` da la URL firmada, que solo se
        baja si es de lookaside.fbsbx.com, con el mismo token.
    """
    if not _id_media_valido(media_id):
        raise AdjuntoInvalido(422, "El identificador del archivo no es válido.")
    token = cred.get("access_token")
    if not token:
        raise AdjuntoInvalido(503, "El canal no tiene credenciales activas.")

    if cred.get("bsp_provider") == "neuroapi":
        plantilla = settings.NEUROAPI_WA_MEDIA_PATH
        if not plantilla or "{media_id}" not in plantilla:
            raise AdjuntoInvalido(503, "NEUROAPI_WA_MEDIA_PATH sin configurar.")
        ruta = plantilla.replace("{media_id}", quote(media_id, safe=""))
        url = f"{settings.NEUROAPI_API_BASE_URL.rstrip('/')}/{ruta.lstrip('/')}"
        contenido, tipo = await _descargar_con_tipo(url, {"x-api-key": token})
        if tipo == "application/json":
            enlace = _url_en_json(contenido)
            if enlace is None:
                raise AdjuntoInvalido(502, "NeuroAPI no devolvió el archivo.")
            return await descargar_media(enlace)
        return contenido

    cabeceras = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as cliente:
            r = await cliente.get(f"{settings.graph_url}/{quote(media_id, safe='')}", headers=cabeceras)
    except httpx.HTTPError as e:
        raise AdjuntoInvalido(502, f"No se pudo consultar el archivo: {type(e).__name__}") from e
    if r.status_code != 200:
        raise AdjuntoInvalido(502, f"Meta no devolvió el archivo ({r.status_code}).")
    enlace = _url_en_json(r.content)
    partes = urlparse(enlace or "")
    if partes.scheme != "https" or (partes.hostname or "").lower() not in _HOSTS_MEDIA_META:
        raise AdjuntoInvalido(502, "La URL del archivo no es de un origen permitido.")
    return await _descargar(enlace, cabeceras)


def _url_en_json(cuerpo: bytes) -> str | None:
    try:
        datos = json.loads(cuerpo)
    except ValueError:
        return None
    if not isinstance(datos, dict):
        return None
    for clave in ("url", "link"):
        valor = datos.get(clave)
        if isinstance(valor, str) and valor:
            return valor
    return None
