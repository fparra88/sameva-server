"""
services/agente/medios.py y services/agente/transcripcion.py.

  1. solo se lanza tarea para lo que se puede bajar (WhatsApp; Instagram por
     NeuroAPI); Facebook, video y stickers no
  2. un adjunto que no se puede bajar no rompe nada (devuelve False)
  3. imagen/documento -> message_attachments
  4. nota de voz -> se transcribe, se cobra 1 crédito y se guarda SOLO el
     texto; sin familia, sin proveedor o sin saldo ni se descarga
  5. un fallo del proveedor no cobra
  6. Groq: petición multipart, duración y costo con el mínimo de 10 s

Sin BD ni red.
"""

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from config import settings
from services import adjuntos
from services.agente import medios, transcripcion
from services.agente.modelos import MensajeNormalizado
from tests.test_adjuntos import JPG
from tests.test_adjuntos_entrantes import OGG

TENANT = uuid4()
CONV = uuid4()
MSG = uuid4()


def _mensaje(canal="whatsapp", tipo="image", media_id="MEDIA-1", contenido="[Imagen]"):
    return MensajeNormalizado(procesar=True, canal=canal, tipo=tipo, media_id=media_id, contenido=contenido)


# ============================================================
# Qué se lanza
# ============================================================
@pytest.mark.parametrize(
    "mensaje,proveedor,lanza",
    [
        (_mensaje(), None, True),
        (_mensaje(tipo="audio"), None, True),
        (_mensaje(tipo="document"), None, True),
        (_mensaje(tipo="video"), None, False),
        (_mensaje(tipo="sticker"), None, False),
        (_mensaje(media_id=None), None, False),
        (_mensaje(canal="instagram"), "neuroapi", True),
        (_mensaje(canal="instagram"), None, False),
        (_mensaje(canal="facebook"), None, False),
    ],
)
async def test_lanza_solo_lo_que_se_puede_bajar(monkeypatch, mensaje, proveedor, lanza):
    async def procesar(*a):
        return True

    monkeypatch.setattr(medios, "procesar", procesar)
    tarea = medios.lanzar(TENANT, CONV, MSG, mensaje, proveedor)
    assert (tarea is not None) is lanza
    if tarea:
        await tarea


# ============================================================
# Espera de la ráfaga: los adjuntos de mensajes anteriores
# ============================================================
async def test_esperar_conversacion_espera_las_descargas_de_otros_mensajes(monkeypatch):
    """Foto + "¿tienen esto?": la pasada del texto espera la descarga de la foto."""
    import asyncio

    liberar = asyncio.Event()
    terminadas: list[str] = []

    async def procesar(tenant_id, conversation_id, message_id, mensaje):
        await liberar.wait()
        terminadas.append(mensaje.media_id)
        return True

    monkeypatch.setattr(medios, "procesar", procesar)
    conv = uuid4()
    tarea = medios.lanzar(TENANT, conv, uuid4(), _mensaje(media_id="FOTO"), None)

    espera = asyncio.create_task(medios.esperar_conversacion(conv))
    await asyncio.sleep(0)
    assert not espera.done()  # sigue esperando la foto

    liberar.set()
    await espera
    assert terminadas == ["FOTO"]
    await tarea
    await asyncio.sleep(0)
    assert conv not in medios._EN_CURSO  # se limpia sola al terminar


async def test_esperar_conversacion_tiene_tope_y_no_cancela(monkeypatch):
    import asyncio

    liberar = asyncio.Event()

    async def procesar(*a):
        await liberar.wait()
        return True

    monkeypatch.setattr(medios, "procesar", procesar)
    conv = uuid4()
    tarea = medios.lanzar(TENANT, conv, uuid4(), _mensaje(), None)

    await medios.esperar_conversacion(conv, timeout=0.01)
    assert not tarea.done() and not tarea.cancelled()
    liberar.set()
    assert await tarea is True


async def test_esperar_conversacion_sin_descargas_no_espera():
    await medios.esperar_conversacion(uuid4(), timeout=0)


async def test_esperar_conversacion_no_mezcla_conversaciones(monkeypatch):
    import asyncio

    liberar = asyncio.Event()

    async def procesar(*a):
        await liberar.wait()
        return True

    monkeypatch.setattr(medios, "procesar", procesar)
    tarea = medios.lanzar(TENANT, uuid4(), uuid4(), _mensaje(), None)
    # Otra conversación no espera la descarga ajena.
    await asyncio.wait_for(medios.esperar_conversacion(uuid4()), timeout=0.5)
    liberar.set()
    await tarea


# ============================================================
# Procesar
# ============================================================
@pytest.fixture
def entorno(monkeypatch):
    estado = SimpleNamespace(
        insertados=[], ejecutados=[], cobros=[], avisos=[], descargas=[],
        contenido=JPG, familia=True, disponible=True, saldo=Decimal(5), permitido=True,
        transcripcion=transcripcion.Transcripcion("hola, quiero una cita", Decimal("4.2"), "groq", "whisper"),
        falla_transcripcion=None, usos=[],
    )

    async def credenciales(tenant_id):
        return {"access_token": "T"}

    async def descargar(media_id, cred):
        estado.descargas.append(media_id)
        if isinstance(estado.contenido, Exception):
            raise estado.contenido
        return estado.contenido

    async def fetch_one(sql, *args):
        estado.insertados.append((sql, args))
        return {"id": uuid4()}

    async def execute(sql, *args):
        estado.ejecutados.append((sql, args))
        return "INSERT 0 1"

    async def plan_incluye(tenant_id):
        return estado.familia

    async def saldo_total(tenant_id):
        return estado.saldo

    async def consumir_credito(tenant_id, herramienta, referencia, conversation_id=None):
        estado.cobros.append((herramienta, referencia))
        return SimpleNamespace(permitido=estado.permitido)

    async def transcribir(audio, nombre, mime):
        if estado.falla_transcripcion:
            raise transcripcion.TranscripcionError(estado.falla_transcripcion)
        return estado.transcripcion

    async def avisar(tenant_id, conversation_id):
        estado.avisos.append(conversation_id)

    async def registrar_uso_tokens(tenant_id, **k):
        estado.usos.append(k)
        return True

    import realtime
    from services import gerencia

    monkeypatch.setattr(medios, "_credenciales_whatsapp", credenciales)
    monkeypatch.setattr(medios.adjuntos, "descargar_media_whatsapp", descargar)
    monkeypatch.setattr(medios, "fetch_one", fetch_one)
    monkeypatch.setattr(medios, "execute", execute)
    monkeypatch.setattr(medios.multimedia, "plan_incluye", plan_incluye)
    monkeypatch.setattr(medios.transcripcion, "disponible", lambda: estado.disponible)
    monkeypatch.setattr(medios.transcripcion, "transcribir", transcribir)
    monkeypatch.setattr(medios.creditos, "saldo_total", saldo_total)
    monkeypatch.setattr(medios.creditos, "consumir_credito", consumir_credito)
    monkeypatch.setattr(realtime, "avisar_mensaje_nuevo", avisar)
    monkeypatch.setattr(gerencia, "registrar_uso_tokens", registrar_uso_tokens)
    return estado


async def test_una_imagen_se_guarda_en_message_attachments(entorno):
    assert await medios.procesar(TENANT, CONV, MSG, _mensaje()) is True
    sql, args = entorno.insertados[0]
    assert "INSERT INTO message_attachments" in sql
    assert args[2] == "image/jpeg" and args[5] == JPG
    assert entorno.cobros == []  # guardar para el portal no cobra; analizar sí
    assert entorno.avisos == [CONV]


async def test_un_adjunto_que_no_se_puede_bajar_no_rompe_el_mensaje(entorno):
    entorno.contenido = adjuntos.AdjuntoInvalido(502, "caído")
    assert await medios.procesar(TENANT, CONV, MSG, _mensaje()) is False
    assert entorno.insertados == [] and entorno.avisos == []


async def test_un_error_inesperado_tampoco_rompe(entorno):
    entorno.contenido = RuntimeError("boom")
    assert await medios.procesar(TENANT, CONV, MSG, _mensaje()) is False


async def test_nota_de_voz_se_transcribe_cobra_y_guarda_solo_el_texto(entorno):
    entorno.contenido = OGG
    assert await medios.procesar(TENANT, CONV, MSG, _mensaje(tipo="audio", contenido="[Audio]")) is True

    assert entorno.cobros == [("transcripcion", f"transcripcion:{MSG}")]
    assert entorno.insertados == []  # el audio NO va a message_attachments
    sql, args = entorno.ejecutados[0]
    assert "INSERT INTO message_transcripciones" in sql
    assert args[2] == "hola, quiero una cita"
    assert OGG not in args
    assert entorno.usos[0]["origen"] == "transcripcion"
    assert entorno.usos[0]["idempotency_key"] == f"msg:{MSG}:transcripcion"


@pytest.mark.parametrize("campo,valor", [("familia", False), ("disponible", False), ("saldo", Decimal(0))])
async def test_sin_familia_proveedor_o_saldo_el_audio_ni_se_descarga(entorno, campo, valor):
    setattr(entorno, campo, valor)
    entorno.contenido = OGG
    assert await medios.procesar(TENANT, CONV, MSG, _mensaje(tipo="audio")) is False
    assert entorno.descargas == [] and entorno.cobros == []


async def test_si_el_proveedor_falla_no_se_cobra(entorno):
    entorno.contenido = OGG
    entorno.falla_transcripcion = "timeout"
    assert await medios.procesar(TENANT, CONV, MSG, _mensaje(tipo="audio")) is False
    assert entorno.cobros == [] and entorno.ejecutados == []


async def test_si_se_acabo_el_saldo_al_cobrar_el_texto_se_tira(entorno):
    entorno.contenido = OGG
    entorno.permitido = False
    assert await medios.procesar(TENANT, CONV, MSG, _mensaje(tipo="audio")) is False
    assert entorno.ejecutados == []


# ============================================================
# Groq
# ============================================================
class _Resp:
    def __init__(self, status_code, cuerpo):
        self.status_code = status_code
        self._cuerpo = cuerpo

    def json(self):
        return self._cuerpo


def _groq(monkeypatch, status_code=200, cuerpo=None):
    pedidos = []

    class Cliente:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers, data, files):
            pedidos.append((url, headers, data, files))
            return _Resp(status_code, cuerpo if cuerpo is not None else {"text": " Hola ", "duration": 3.5})

    monkeypatch.setattr(transcripcion.httpx, "AsyncClient", Cliente)
    monkeypatch.setattr(settings, "GROQ_API_KEY", "gsk-test")
    monkeypatch.setattr(settings, "TRANSCRIPCION_PROVIDER", "groq")
    return pedidos


async def test_groq_recibe_el_audio_como_multipart(monkeypatch):
    pedidos = _groq(monkeypatch)
    r = await transcripcion.transcribir(OGG, "nota.ogg", "audio/ogg")
    assert r.texto == "Hola" and r.segundos == Decimal("3.5")
    url, headers, data, files = pedidos[0]
    assert url.endswith("/audio/transcriptions")
    assert headers == {"Authorization": "Bearer gsk-test"}
    assert data["model"] == settings.TRANSCRIPCION_MODELO
    assert files == {"file": ("nota.ogg", OGG, "audio/ogg")}


@pytest.mark.parametrize(
    "status_code,cuerpo,codigo",
    [(401, {}, "credenciales"), (429, {}, "limite"), (500, {}, "http_500"), (200, {"text": "  "}, "sin_texto")],
)
async def test_groq_errores_se_resumen_en_un_codigo(monkeypatch, status_code, cuerpo, codigo):
    _groq(monkeypatch, status_code, cuerpo)
    with pytest.raises(transcripcion.TranscripcionError) as e:
        await transcripcion.transcribir(OGG, "n.ogg", "audio/ogg")
    assert e.value.codigo == codigo


async def test_sin_clave_no_hay_proveedor(monkeypatch):
    monkeypatch.setattr(settings, "GROQ_API_KEY", "")
    assert transcripcion.disponible() is False
    with pytest.raises(transcripcion.TranscripcionError):
        await transcripcion.transcribir(OGG, "n.ogg", "audio/ogg")


def test_costo_con_minimo_de_10_segundos(monkeypatch):
    monkeypatch.setattr(settings, "TRANSCRIPCION_USD_POR_HORA", 0.04)
    corta = transcripcion.Transcripcion("x", Decimal(2), "groq", "m")
    larga = transcripcion.Transcripcion("x", Decimal(360), "groq", "m")
    assert corta.costo_usd == Decimal("0.000111")
    assert larga.costo_usd == Decimal("0.004000")
