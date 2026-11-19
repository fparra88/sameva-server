"""
Adjuntos que manda el cliente (services/adjuntos.py): notas de voz por firma,
nombre del canal respetado solo si coincide con el tipo real, y descarga de
media de WhatsApp por id (Graph directo o NeuroAPI) sin salir de los hosts
permitidos. Sin BD ni red.
"""

import json

import pytest

from config import settings
from services import adjuntos
from services.adjuntos import AdjuntoInvalido, validar_adjunto, validar_adjunto_entrante
from tests.test_adjuntos import DOCX, JPG, PDF, PNG

OGG = b"OggS\x00\x02" + b"\x00" * 40
MP3_ID3 = b"ID3\x04\x00" + b"\x00" * 40
MP3_FRAME = b"\xff\xfb\x90\x64" + b"\x00" * 40
M4A = b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00" + b"\x00" * 30
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 30
AAC_ADTS = b"\xff\xf1\x50\x80" + b"\x00" * 40
MP4_VIDEO = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00" + b"\x00" * 30


@pytest.mark.parametrize(
    "contenido,mime,ext",
    [
        (OGG, "audio/ogg", ".ogg"),
        (MP3_ID3, "audio/mpeg", ".mp3"),
        (MP3_FRAME, "audio/mpeg", ".mp3"),
        (M4A, "audio/mp4", ".m4a"),
        (WAV, "audio/wav", ".wav"),
    ],
)
def test_reconoce_las_notas_de_voz_por_firma(contenido, mime, ext):
    info = validar_adjunto_entrante(contenido, base="whatsapp-abc")
    assert (info.mime, info.tipo_wa, info.nombre) == (mime, "audio", f"whatsapp-abc{ext}")


@pytest.mark.parametrize("contenido", [AAC_ADTS, MP4_VIDEO, b"GIF89a" + b"\x00" * 20])
def test_lo_que_no_es_un_tipo_admitido_da_415(contenido):
    with pytest.raises(AdjuntoInvalido) as e:
        validar_adjunto_entrante(contenido, base="x")
    assert e.value.status_code == 415


def test_el_composer_del_portal_sigue_rechazando_audio():
    # El audio solo se acepta de entrada: validar_adjunto es lo que usa el
    # envío desde la Bandeja y no debe dejar pasar una nota de voz.
    with pytest.raises(AdjuntoInvalido) as e:
        validar_adjunto(OGG, "nota.ogg")
    assert e.value.status_code == 415


def test_audio_mas_grande_que_el_tope_da_413(monkeypatch):
    monkeypatch.setattr(settings, "WA_AUDIO_MAX_BYTES", 10)
    with pytest.raises(AdjuntoInvalido) as e:
        validar_adjunto_entrante(OGG, base="x")
    assert e.value.status_code == 413


@pytest.mark.parametrize(
    "contenido,nombre,esperado",
    [
        (PDF, "Factura Octubre.pdf", "Factura Octubre.pdf"),
        # El nombre del canal miente sobre el tipo: manda la firma.
        (PDF, "foto.jpg", "wa-1.pdf"),
        (JPG, None, "wa-1.jpg"),
        (PNG, "", "wa-1.png"),
        (DOCX, "../../oferta.docx", "oferta.docx"),
    ],
)
def test_el_nombre_del_canal_se_respeta_solo_si_coincide(contenido, nombre, esperado):
    assert validar_adjunto_entrante(contenido, base="wa-1", nombre=nombre).nombre == esperado


# ============================================================
# Descarga de WhatsApp por id
# ============================================================
@pytest.mark.parametrize("media_id", ["../x", "a/b", "", "..", "y" * 300])
async def test_un_media_id_inseguro_se_rechaza(media_id):
    with pytest.raises(AdjuntoInvalido) as e:
        await adjuntos.descargar_media_whatsapp(media_id, {"access_token": "T"})
    assert e.value.status_code == 422


async def test_sin_credenciales_no_se_descarga():
    with pytest.raises(AdjuntoInvalido) as e:
        await adjuntos.descargar_media_whatsapp("MID", {"access_token": None})
    assert e.value.status_code == 503


async def test_neuroapi_sin_ruta_configurada_no_descarga(monkeypatch):
    monkeypatch.setattr(settings, "NEUROAPI_WA_MEDIA_PATH", "")
    with pytest.raises(AdjuntoInvalido) as e:
        await adjuntos.descargar_media_whatsapp("MID", {"access_token": "K", "bsp_provider": "neuroapi"})
    assert e.value.status_code == 503


async def test_neuroapi_baja_por_su_ruta_con_la_key_del_tenant(monkeypatch):
    monkeypatch.setattr(settings, "NEUROAPI_WA_MEDIA_PATH", "/neuroapi/whatsapp/media/{media_id}")
    pedidos = []

    async def _descargar_con_tipo(url, cabeceras):
        pedidos.append((url, cabeceras))
        return OGG, "audio/ogg"

    monkeypatch.setattr(adjuntos, "_descargar_con_tipo", _descargar_con_tipo)
    contenido = await adjuntos.descargar_media_whatsapp(
        "MID:1", {"access_token": "KEY-TENANT", "bsp_provider": "neuroapi"}
    )
    assert contenido == OGG
    base = settings.NEUROAPI_API_BASE_URL.rstrip("/")
    assert pedidos == [(f"{base}/neuroapi/whatsapp/media/MID%3A1", {"x-api-key": "KEY-TENANT"})]


async def test_neuroapi_que_devuelve_una_url_ajena_no_se_sigue(monkeypatch):
    monkeypatch.setattr(settings, "NEUROAPI_WA_MEDIA_PATH", "/m/{media_id}")

    async def _descargar_con_tipo(url, cabeceras):
        return json.dumps({"url": "https://169.254.169.254/latest"}).encode(), "application/json"

    monkeypatch.setattr(adjuntos, "_descargar_con_tipo", _descargar_con_tipo)
    with pytest.raises(AdjuntoInvalido) as e:
        await adjuntos.descargar_media_whatsapp("MID", {"access_token": "K", "bsp_provider": "neuroapi"})
    assert e.value.status_code == 422


class _Respuesta:
    def __init__(self, status_code, cuerpo):
        self.status_code = status_code
        self.content = json.dumps(cuerpo).encode()


def _graph_falso(monkeypatch, cuerpo, status_code=200):
    pedidos = []

    class Cliente:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers):
            pedidos.append((url, headers))
            return _Respuesta(status_code, cuerpo)

    monkeypatch.setattr(adjuntos.httpx, "AsyncClient", Cliente)
    return pedidos


async def test_meta_directo_resuelve_la_url_y_baja_con_el_mismo_token(monkeypatch):
    pedidos = _graph_falso(monkeypatch, {"url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1"})
    bajados = []

    async def _descargar(url, cabeceras):
        bajados.append((url, cabeceras))
        return JPG

    monkeypatch.setattr(adjuntos, "_descargar", _descargar)
    assert await adjuntos.descargar_media_whatsapp("MID", {"access_token": "TOK"}) == JPG
    assert pedidos == [(f"{settings.graph_url}/MID", {"Authorization": "Bearer TOK"})]
    assert bajados[0][0].startswith("https://lookaside.fbsbx.com/")
    assert bajados[0][1] == {"Authorization": "Bearer TOK"}


@pytest.mark.parametrize(
    "url",
    ["http://lookaside.fbsbx.com/x", "https://evil.example.com/x", "https://lookaside.fbsbx.com.evil.io/x", None],
)
async def test_meta_directo_no_sigue_una_url_fuera_de_lookaside(monkeypatch, url):
    _graph_falso(monkeypatch, {"url": url} if url else {})

    async def _descargar(url, cabeceras):  # pragma: no cover - no debe llamarse
        raise AssertionError("no debía descargar")

    monkeypatch.setattr(adjuntos, "_descargar", _descargar)
    with pytest.raises(AdjuntoInvalido) as e:
        await adjuntos.descargar_media_whatsapp("MID", {"access_token": "TOK"})
    assert e.value.status_code == 502
