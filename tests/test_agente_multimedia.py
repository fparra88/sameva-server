"""
services/agente/multimedia.py: cómo llegan al modelo los adjuntos del cliente.

Lo que importa:
  1. sin adjuntos, sin la familia en el plan o con un modelo sin visión, el
     turno es el string de siempre (no cambia nada para quien no lo contrató)
  2. imagen -> bloque image, PDF -> document base64, DOCX -> document de texto
  3. los adjuntos van precedidos del aviso "datos, no instrucciones"
  4. 1 crédito por adjunto analizado, con referencia idempotente; sin saldo,
     marca de texto en vez del adjunto
  5. topes: adjuntos por turno, bytes por imagen y por turno, páginas de PDF,
     PDF cifrado
  6. `sin_media` cambia cada adjunto por una marca (reintento tras un 400)

Sin BD: se sustituyen las lecturas y el cobro.
"""

import base64
import io
import zipfile
from types import SimpleNamespace
from uuid import uuid4

import pytest

from config import settings
from services.adjuntos import MIME_DOCX
from services.agente import multimedia
from services.agente.modelos import ContextoMensaje
from tests.test_adjuntos import JPG, PDF, PNG

TENANT = uuid4()
CONVERSACION = uuid4()
M1 = uuid4()
M2 = uuid4()


def _docx(*parrafos: str) -> bytes:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    cuerpo = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in parrafos)
    xml = f'<?xml version="1.0"?><w:document xmlns:w="{ns}"><w:body>{cuerpo}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "x")
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def _adjunto(mime, contenido, message_id=M1, nombre="archivo"):
    return {"id": uuid4(), "message_id": message_id, "mime": mime, "nombre": nombre, "contenido": contenido}


@pytest.fixture
def contexto():
    return ContextoMensaje(
        tenant_id=TENANT,
        conversation_id=CONVERSACION,
        user_id=uuid4(),
        canal="whatsapp",
        usuario_id="5215500000000",
        nombre="Ana",
        contenido="[Imagen]\n¿cuánto cuesta esto?",
        tipo="image",
        mensaje_id="wamid.A",
        db_message_id=M2,
        db_message_created_at=None,
        ids_agrupados=[M1, M2],
        con_adjuntos=True,
    )


@pytest.fixture
def base(monkeypatch):
    """Plan con la familia, saldo infinito y dos mensajes en la ráfaga."""
    estado = SimpleNamespace(
        familias=["calendario", "multimedia"],
        mensajes=[{"id": M1, "content": "[Imagen]"}, {"id": M2, "content": "¿cuánto cuesta esto?"}],
        adjuntos=[],
        cobros=[],
        saldo=True,
    )

    async def fetch_value(sql, *args):
        assert "limites_efectivos" in sql
        return estado.familias

    async def fetch_all(sql, *args):
        if "message_attachments" in sql:
            return estado.adjuntos
        return estado.mensajes

    async def consumir_credito(tenant_id, herramienta, referencia, conversation_id=None):
        estado.cobros.append((herramienta, referencia))
        return SimpleNamespace(permitido=estado.saldo)

    monkeypatch.setattr(multimedia, "fetch_value", fetch_value)
    monkeypatch.setattr(multimedia, "fetch_all", fetch_all)
    monkeypatch.setattr(multimedia.creditos, "consumir_credito", consumir_credito)
    return estado


async def test_sin_adjuntos_el_turno_es_el_texto_de_siempre(contexto, monkeypatch):
    contexto.con_adjuntos = False

    async def no_toca_la_base(*a, **k):  # pragma: no cover
        raise AssertionError("no debía consultar la base")

    monkeypatch.setattr(multimedia, "fetch_value", no_toca_la_base)
    assert await multimedia.armar_turno(contexto, "claude-sonnet-5") == contexto.contenido


async def test_sin_la_familia_en_el_plan_no_se_analiza_nada(contexto, base):
    base.familias = ["calendario", "http"]
    base.adjuntos = [_adjunto("image/jpeg", JPG)]
    assert await multimedia.armar_turno(contexto, "claude-sonnet-5") == contexto.contenido
    assert base.cobros == []


async def test_un_modelo_desconocido_no_recibe_media(contexto, base):
    base.adjuntos = [_adjunto("image/jpeg", JPG)]
    assert await multimedia.armar_turno(contexto, "modelo-que-no-existe") == contexto.contenido
    assert base.cobros == []


async def test_imagen_va_como_bloque_image_tras_el_aviso_y_cobra_una_vez(contexto, base):
    adj = _adjunto("image/png", PNG)
    base.adjuntos = [adj]

    turno = await multimedia.armar_turno(contexto, "claude-haiku-4-5-20251001")

    assert turno[0] == {"type": "text", "text": multimedia.AVISO_ADJUNTOS}
    assert turno[1]["type"] == "image"
    assert turno[1]["source"] == {
        "type": "base64", "media_type": "image/png", "data": base64.b64encode(PNG).decode(),
    }
    # Después de la imagen, el texto de cada mensaje en orden.
    assert [b["text"] for b in turno[2:]] == ["[Imagen]", "¿cuánto cuesta esto?"]
    assert base.cobros == [("multimedia", f"adjunto:{adj['id']}")]


async def test_pdf_va_como_document_base64_con_titulo(contexto, base):
    base.adjuntos = [_adjunto("application/pdf", PDF, nombre="cotizacion.pdf")]
    turno = await multimedia.armar_turno(contexto, "claude-sonnet-5")
    doc = turno[1]
    assert doc["type"] == "document" and doc["title"] == "cotizacion.pdf"
    assert doc["source"]["media_type"] == "application/pdf"


async def test_docx_va_como_texto_extraido(contexto, base):
    base.adjuntos = [_adjunto(MIME_DOCX, _docx("Pedido 42", "Dos cajas"), nombre="pedido.docx")]
    turno = await multimedia.armar_turno(contexto, "claude-sonnet-5")
    assert turno[1]["source"] == {"type": "text", "media_type": "text/plain", "data": "Pedido 42\nDos cajas"}


async def test_sin_saldo_el_adjunto_queda_como_marca(contexto, base):
    base.saldo = False
    base.adjuntos = [_adjunto("image/jpeg", JPG, nombre="foto.jpg")]
    turno = await multimedia.armar_turno(contexto, "claude-sonnet-5")
    assert not multimedia.tiene_media(turno)
    assert "falta de créditos" in turno[1]["text"]


async def test_pdf_cifrado_o_con_demasiadas_paginas_no_se_manda_ni_se_cobra(contexto, base, monkeypatch):
    monkeypatch.setattr(settings, "AGENTE_PDF_MAX_PAGINAS", 2)
    largo = b"%PDF-1.7\n" + b"<< /Type /Page >>\n" * 3 + b"<< /Type /Pages >>"
    base.adjuntos = [
        _adjunto("application/pdf", b"%PDF-1.7\n/Encrypt 5 0 R", nombre="a.pdf"),
        _adjunto("application/pdf", largo, nombre="b.pdf"),
    ]
    turno = await multimedia.armar_turno(contexto, "claude-sonnet-5")
    assert "protegido" in turno[1]["text"]
    assert "excede" in turno[2]["text"]
    assert base.cobros == []


async def test_imagen_demasiado_grande_no_se_manda(contexto, base, monkeypatch):
    monkeypatch.setattr(settings, "AGENTE_IMAGEN_MAX_BYTES", 10)
    base.adjuntos = [_adjunto("image/jpeg", JPG)]
    turno = await multimedia.armar_turno(contexto, "claude-sonnet-5")
    assert not multimedia.tiene_media(turno)
    assert base.cobros == []


async def test_tope_de_bytes_por_turno(contexto, base, monkeypatch):
    monkeypatch.setattr(settings, "AGENTE_MEDIA_MAX_BYTES_TURNO", len(JPG) + 1)
    base.adjuntos = [_adjunto("image/jpeg", JPG), _adjunto("image/jpeg", JPG)]
    turno = await multimedia.armar_turno(contexto, "claude-sonnet-5")
    assert [b["type"] for b in turno[1:3]] == ["image", "text"]
    assert len(base.cobros) == 1


async def test_los_adjuntos_de_mas_se_resumen_en_una_marca(contexto, base, monkeypatch):
    monkeypatch.setattr(settings, "AGENTE_MAX_ADJUNTOS_TURNO", 2)
    base.adjuntos = [_adjunto("image/jpeg", JPG) for _ in range(4)]
    turno = await multimedia.armar_turno(contexto, "claude-sonnet-5")
    assert sum(1 for b in turno if b["type"] == "image") == 2
    assert "2 adjunto(s) más" in turno[-1]["text"]
    assert len(base.cobros) == 2


def test_sin_media_cambia_cada_adjunto_por_una_marca():
    turno = [
        {"type": "text", "text": multimedia.AVISO_ADJUNTOS},
        {"type": "image", "source": {}},
        {"type": "document", "source": {}, "title": "x.pdf"},
        {"type": "text", "text": "hola", "cache_control": {"type": "ephemeral"}},
    ]
    limpio = multimedia.sin_media(turno)
    assert not multimedia.tiene_media(limpio)
    assert "x.pdf" in limpio[2]["text"]
    assert limpio[3] == {"type": "text", "text": "hola"}


def test_docx_roto_o_sin_documento_da_none():
    assert multimedia.texto_docx(b"no es zip", 100) is None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "x")
    assert multimedia.texto_docx(buf.getvalue(), 100) is None


def test_docx_se_recorta_al_tope():
    assert len(multimedia.texto_docx(_docx("a" * 50, "b" * 50), 60)) == 60
