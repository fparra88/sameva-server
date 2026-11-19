"""
Archivos que el agente envía por WhatsApp (services/archivos_envio.py, sql/78).

Sin BD, sin red y sin AWS: se sustituyen la sesión, S3 y el envío de media.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from config import settings
from services import archivos_envio as svc
from services import almacen_archivos, conversaciones, creditos
from services.agente import envio
from services.agente.modelos import ContextoMensaje
from tests.test_adjuntos import PDF, PNG

TENANT = uuid4()
ARCH_ID = uuid4()
KEY = f"tenants/{TENANT}/obj"


def _ctx(canal="whatsapp") -> ContextoMensaje:
    from datetime import datetime

    return ContextoMensaje(
        tenant_id=TENANT, conversation_id=uuid4(), user_id=uuid4(), canal=canal,
        usuario_id="593999111222", nombre="Ana", contenido="hola", tipo="text",
        mensaje_id="wamid.1", db_message_id=uuid4(), db_message_created_at=datetime.now(),
        phone_number_id="123",
    )


# ------------------------------------------------------------
# Definición de la tool y catálogo
# ------------------------------------------------------------
def test_la_tool_solo_acepta_ids_del_catalogo():
    d = svc.definicion([{"id": ARCH_ID, "nombre": "a.pdf", "descripcion": "x"}])
    assert d["name"] == "enviar_archivo"
    assert d["input_schema"]["properties"]["archivo_id"]["enum"] == [str(ARCH_ID)]
    assert "tenant" not in str(d["input_schema"]).lower()


def test_texto_del_catalogo_lleva_id_nombre_y_cuando():
    t = svc.texto_catalogo([{"id": ARCH_ID, "nombre": "menu.pdf", "descripcion": "Si piden el menú"}])
    assert str(ARCH_ID) in t and "menu.pdf" in t and "Si piden el menú" in t


@pytest.mark.asyncio
async def test_catalogo_vacio_fuera_de_whatsapp(monkeypatch):
    monkeypatch.setattr(settings, "ARCHIVOS_S3_BUCKET", "bucket")
    for canal in ("instagram", "facebook", "web"):
        assert await svc.catalogo_para_agente(TENANT, canal) == []


@pytest.mark.asyncio
async def test_catalogo_vacio_sin_bucket(monkeypatch):
    monkeypatch.setattr(settings, "ARCHIVOS_S3_BUCKET", "")
    assert await svc.catalogo_para_agente(TENANT, "whatsapp") == []


@pytest.mark.asyncio
async def test_catalogo_vacio_si_el_plan_no_incluye_la_familia(monkeypatch):
    monkeypatch.setattr(settings, "ARCHIVOS_S3_BUCKET", "bucket")

    async def sin_plan(_):
        return False

    monkeypatch.setattr(svc, "plan_incluye", sin_plan)
    assert await svc.catalogo_para_agente(TENANT, "whatsapp") == []


# ------------------------------------------------------------
# Alta: tope de 5, validación por firma
# ------------------------------------------------------------
class _Conn:
    def __init__(self, usados):
        self.usados = usados
        self.inserts = []

    async def execute(self, sql, *a):
        return "OK"

    async def fetchval(self, sql, *a):
        return self.usados

    async def fetchrow(self, sql, *a):
        self.inserts.append(a)
        from datetime import datetime

        return {
            "id": ARCH_ID, "nombre": a[1], "descripcion": a[2], "mime": a[3],
            "tipo_wa": a[4], "bytes": a[5], "created_at": datetime.now(),
        }


@pytest.fixture
def alta(monkeypatch):
    reg = SimpleNamespace(subidos=[], borrados=[], conn=_Conn(0))

    async def subir(clave, contenido, mime):
        reg.subidos.append(clave)

    async def borrar(clave):
        reg.borrados.append(clave)

    @asynccontextmanager
    async def transaccion():
        yield reg.conn

    monkeypatch.setattr(almacen_archivos, "subir", subir)
    monkeypatch.setattr(almacen_archivos, "borrar", borrar)
    monkeypatch.setattr(svc, "transaccion", transaccion)
    monkeypatch.setattr(settings, "ARCHIVOS_MAX_POR_NEGOCIO", 5)
    return reg


@pytest.mark.asyncio
async def test_alta_guarda_en_s3_y_en_la_base(alta):
    fila = await svc.crear(TENANT, PDF, "catalogo.pdf", "Cuando pidan el catálogo")
    assert fila["nombre"] == "catalogo.pdf" and fila["tipo_wa"] == "document"
    assert len(alta.subidos) == 1 and alta.subidos[0].startswith(f"tenants/{TENANT}/")
    assert alta.borrados == []


@pytest.mark.asyncio
async def test_el_sexto_archivo_se_rechaza_y_no_deja_objeto_huerfano(alta):
    alta.conn.usados = 5
    with pytest.raises(HTTPException) as e:
        await svc.crear(TENANT, PNG, "foto.png", "Foto del local")
    assert e.value.status_code == 409 and e.value.detail["codigo"] == "limite_archivos"
    assert alta.borrados == alta.subidos  # el objeto subido se limpia


@pytest.mark.asyncio
async def test_alta_rechaza_tipo_falso_antes_de_tocar_s3(alta):
    with pytest.raises(HTTPException) as e:
        await svc.crear(TENANT, b"MZ\x90\x00 no es un pdf", "virus.pdf", "algo útil")
    assert e.value.status_code == 415
    assert alta.subidos == []


@pytest.mark.asyncio
async def test_alta_exige_descripcion(alta):
    with pytest.raises(HTTPException) as e:
        await svc.crear(TENANT, PDF, "a.pdf", "  ")
    assert e.value.status_code == 422 and alta.subidos == []


# ------------------------------------------------------------
# Envío del agente
# ------------------------------------------------------------
@pytest.fixture
def envio_ok(monkeypatch):
    reg = SimpleNamespace(
        fetch_arch={"mime": "application/pdf", "nombre": "catalogo.pdf", "tipo_wa": "document",
                    "bytes": 40, "storage_key": KEY},
        ventana=True, creditos=True, falla=False,
        media=[], inserts=[], execute=[], avisos=[], cobros=[],
    )

    async def plan(_):
        return True

    async def fetch_one(sql, *a):
        return reg.fetch_arch if a[1] == TENANT else None

    async def execute(sql, *a):
        reg.execute.append(sql)
        return "OK"

    class Conn:
        async def fetchrow(self, sql, *a):
            reg.inserts.append(("messages", a))
            return {"id": uuid4()}

        async def execute(self, sql, *a):
            reg.inserts.append(("attachments", a))

    @asynccontextmanager
    async def transaccion():
        yield Conn()

    async def ventana(_destino):
        return reg.ventana

    async def credenciales(_t, _c):
        return {"access_token": "k", "bsp_provider": "neuroapi", "phone_number_id": "999"}

    async def consumir(tenant, herramienta, referencia, conv=None):
        reg.cobros.append((herramienta, referencia))
        return SimpleNamespace(permitido=reg.creditos)

    async def mandar(cred, destinatario, info, url, leyenda):
        if reg.falla:
            raise HTTPException(status_code=502, detail="x")
        reg.media.append((cred["phone_number_id"], destinatario, info.tipo_wa, url, leyenda))

    async def avisar(t, c):
        reg.avisos.append(c)

    import realtime

    monkeypatch.setattr(settings, "ARCHIVOS_S3_BUCKET", "bucket")
    monkeypatch.setattr(settings, "BASE_URL_BACKEND", "https://api.ejemplo.com")
    monkeypatch.setattr(svc, "plan_incluye", plan)
    monkeypatch.setattr(svc, "fetch_one", fetch_one)
    monkeypatch.setattr(svc, "execute", execute)
    monkeypatch.setattr(svc, "transaccion", transaccion)
    monkeypatch.setattr(envio, "_ventana_abierta", ventana)
    monkeypatch.setattr(envio, "_credenciales", credenciales)
    monkeypatch.setattr(creditos, "consumir_credito", consumir)
    monkeypatch.setattr(conversaciones, "_mandar_media", mandar)
    monkeypatch.setattr(realtime, "avisar_mensaje_nuevo", avisar)
    return reg


async def _enviar(ctx, archivo_id=ARCH_ID, **kw):
    return await svc.enviar(
        contexto=ctx, archivo_id=str(archivo_id), leyenda=kw.pop("leyenda", None),
        referencia_cobro="archivo:toolu_1", **kw,
    )


@pytest.mark.asyncio
async def test_envia_el_archivo_nativo_por_el_numero_de_la_conversacion(envio_ok):
    ctx = _ctx()
    r = await _enviar(ctx, leyenda="Aquí va")
    assert "enviado" in r.lower()
    (pnid, destinatario, tipo, url, leyenda), = envio_ok.media
    assert (pnid, destinatario, tipo, leyenda) == ("123", "593999111222", "document", "Aquí va")
    assert url.startswith("https://api.ejemplo.com/api/media/")
    assert envio_ok.cobros == [("archivos", "archivo:toolu_1")]
    assert envio_ok.avisos == [ctx.conversation_id]
    # La respuesta al modelo NO contiene el enlace: no hay nada que pueda repetir al cliente.
    assert "http" not in r


@pytest.mark.asyncio
async def test_archivo_de_otro_negocio_no_existe(envio_ok):
    envio_ok.fetch_arch = None
    r = await _enviar(_ctx())
    assert "no existe" in r and envio_ok.media == [] and envio_ok.cobros == []


@pytest.mark.asyncio
async def test_id_que_no_es_uuid(envio_ok):
    r = await _enviar(_ctx(), archivo_id="'; DROP TABLE x;--")
    assert "no existe" in r and envio_ok.media == []


@pytest.mark.asyncio
async def test_simulacion_no_envia_ni_cobra(envio_ok):
    r = await _enviar(_ctx(), simulacion=True)
    assert "SIMULADO" in r and envio_ok.media == [] and envio_ok.cobros == []


@pytest.mark.asyncio
@pytest.mark.parametrize("canal", ["instagram", "facebook", "web"])
async def test_fuera_de_whatsapp_no_envia(envio_ok, canal):
    r = await _enviar(_ctx(canal))
    assert "no admite" in r and envio_ok.media == []


@pytest.mark.asyncio
async def test_ventana_cerrada_no_envia_ni_cobra(envio_ok):
    envio_ok.ventana = False
    r = await _enviar(_ctx())
    assert "24 horas" in r and envio_ok.media == [] and envio_ok.cobros == []


@pytest.mark.asyncio
async def test_sin_creditos_no_envia(envio_ok):
    envio_ok.creditos = False
    ctx = _ctx()
    r = await _enviar(ctx)
    assert r.startswith("SIN_CREDITOS") and envio_ok.media == [] and ctx.archivos_enviados == 0


@pytest.mark.asyncio
async def test_si_el_proveedor_falla_se_borra_el_mensaje_y_no_se_afirma_el_envio(envio_ok):
    envio_ok.falla = True
    ctx = _ctx()
    r = await _enviar(ctx)
    assert r == svc.NO_ENVIADO
    assert any("DELETE FROM messages" in s for s in envio_ok.execute)
    assert ctx.archivos_enviados == 0 and envio_ok.avisos == []


@pytest.mark.asyncio
async def test_tope_de_archivos_por_turno(envio_ok, monkeypatch):
    monkeypatch.setattr(settings, "ARCHIVOS_MAX_POR_TURNO", 2)
    ctx = _ctx()
    for _ in range(2):
        assert "enviado" in (await _enviar(ctx)).lower()
    r = await _enviar(ctx)
    assert "máximo" in r and len(envio_ok.media) == 2


@pytest.mark.asyncio
async def test_https_obligatorio_para_que_el_proveedor_descargue(envio_ok, monkeypatch):
    monkeypatch.setattr(settings, "BASE_URL_BACKEND", "http://localhost:8000")
    assert await _enviar(_ctx()) == svc.NO_ENVIADO and envio_ok.media == []


@pytest.mark.asyncio
async def test_una_excepcion_inesperada_no_rompe_el_bucle_del_agente(envio_ok, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("s3 caído")

    monkeypatch.setattr(svc, "fetch_one", boom)
    assert await _enviar(_ctx()) == svc.NO_ENVIADO


# ------------------------------------------------------------
# Esquema y registro de la familia
# ------------------------------------------------------------
def test_la_familia_es_opcional_y_no_es_herramienta():
    from services import numeros_reglas as nr

    assert "archivos" in nr.FAMILIAS_OPCIONALES
    assert "archivos" not in nr.FAMILIAS_HERRAMIENTAS


def test_la_migracion_amplia_el_check_con_todas_las_familias():
    sql = (Path(__file__).resolve().parent.parent / "sql" / "78_archivos_envio.sql").read_text(encoding="utf-8")
    for fam in ("calendario", "sql_lectura", "http", "google_sheets", "google_docs",
                "db_externa", "multimedia", "archivos"):
        assert f"'{fam}'" in sql
    assert "tenant_archivos" in sql and "storage_key" in sql
