"""
Delegación a humano: el aviso al cliente y «Necesita atención de humano».

Lo que importa si falla:

  1. cuando el agente delega, el cliente recibe en el acto el aviso fijo con
     el nombre del negocio (antes el bot callaba) y NO el texto del modelo
  2. ese aviso apaga `ack_pendiente`: si no, el siguiente mensaje del cliente
     recibiría un segundo acuse diciendo lo mismo
  3. `necesita_humano` se enciende solo si delegó el AGENTE y se apaga solo
     en cuanto alguien del equipo escribe o la conversación vuelve a la IA
"""

from uuid import uuid4

import pytest

from routers import agente_webhook
from services import conversaciones as svc
from services.agente import escalar, loop_agente, prompt
from services.agente.modelos import ContextoMensaje, ResultadoIngreso
from session import execute, fetch_one


# ============================================================
# 1. Texto y prompt (puro)
# ============================================================
def test_el_aviso_lleva_el_nombre_del_negocio():
    assert escalar.texto_delegacion("Barbería Don Pepe") == (
        "He delegado tu conversación a un colaborador de Barbería Don Pepe, "
        "en un momento te atendemos."
    )


@pytest.mark.parametrize("nombre", [None, "", "   "])
def test_sin_nombre_no_queda_un_hueco(nombre):
    texto = escalar.texto_delegacion(nombre)
    assert texto == escalar.MENSAJE_DELEGACION_SIN_NOMBRE
    assert "de ," not in texto


def test_las_reglas_de_plataforma_piden_delegar_con_la_herramienta():
    reglas = prompt.REGLAS_PLATAFORMA
    assert "escalar_humano" in reglas
    assert "SIN_CREDITOS" in reglas
    # El modelo no debe despedirse: el aviso lo manda el sistema.
    assert "no escribas despedida" in reglas


# ============================================================
# 2. El webhook entrega el aviso al delegar
# ============================================================
def _contexto(tenant_id=None, conversation_id=None, canal="whatsapp"):
    return ContextoMensaje(
        tenant_id=tenant_id or uuid4(),
        conversation_id=conversation_id or uuid4(),
        user_id=uuid4(),
        canal=canal,
        usuario_id="5215599999999",
        nombre="Ana",
        contenido="quiero hablar con alguien",
        tipo="text",
        mensaje_id="wamid.A",
        db_message_id=uuid4(),
        db_message_created_at=None,
        ids_agrupados=[],
    )


async def test_al_delegar_se_entrega_el_aviso_y_no_el_texto_del_modelo(monkeypatch):
    contexto = _contexto()
    entregados: list[str] = []
    notificadas: list[dict] = []

    async def responder(_ctx):
        return loop_agente.Respuesta(texto="Claro, te paso con alguien.", transferida=True)

    async def notificar(**kwargs):
        notificadas.append(kwargs)
        return "AVISO"

    async def entregar(_destino, texto):
        entregados.append(texto)

    monkeypatch.setattr(agente_webhook.loop_agente, "responder", responder)
    monkeypatch.setattr(agente_webhook.escalar, "notificar_delegacion", notificar)
    monkeypatch.setattr(agente_webhook, "_entregar", entregar)

    texto = await agente_webhook._resolver_respuesta(ResultadoIngreso(contexto=contexto))

    assert texto == "AVISO"
    assert entregados == ["AVISO"]
    assert notificadas == [
        {"tenant_id": contexto.tenant_id, "conversation_id": contexto.conversation_id}
    ]


# ============================================================
# 3. Con base de datos
# ============================================================
@pytest.fixture
async def conversacion(tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    await execute("UPDATE tenants SET name = 'Barbería Don Pepe' WHERE id = $1", tenant_id)
    usuario = await fetch_one(
        "INSERT INTO users (tenant_id, whatsapp_id, display_name) VALUES ($1, $2, 'Ana') RETURNING id",
        tenant_id,
        f"52155{uuid4().int % 10**8:08d}",
    )
    conv = await fetch_one(
        """
        INSERT INTO conversations (tenant_id, user_id, channel_type, status)
        VALUES ($1, $2, 'whatsapp', 'active')
        RETURNING id
        """,
        tenant_id,
        usuario["id"],
    )
    return {"tenant_id": tenant_id, "id": conv["id"]}


async def _escalar(monkeypatch, conv):
    """`escalar.escalar` real, sin el aviso al portal (alertas y asignación)."""
    import routers.eventos as eventos

    async def sin_aviso(_datos):
        return None

    monkeypatch.setattr(eventos, "conversacion_transferida", sin_aviso)
    return await escalar.escalar(
        tenant_id=conv["tenant_id"],
        conversation_id=conv["id"],
        canal="whatsapp",
        motivo="pide una persona",
        area="otro",
        cliente_nombre="Ana",
        cliente_telefono=None,
    )


async def _flag(conv) -> bool:
    filas = await svc.listar(conv["tenant_id"], None, None, None, 50, 0)
    (fila,) = [f for f in filas if f["id"] == conv["id"]]
    return fila["necesita_humano"]


async def test_notificar_guarda_el_aviso_y_apaga_el_acuse(monkeypatch, conversacion):
    monkeypatch.setattr("realtime.avisar_mensaje_nuevo", _nada)
    await _escalar(monkeypatch, conversacion)

    texto = await escalar.notificar_delegacion(
        tenant_id=conversacion["tenant_id"], conversation_id=conversacion["id"]
    )

    assert "Barbería Don Pepe" in texto
    guardado = await fetch_one(
        "SELECT role, content FROM messages WHERE conversation_id = $1", conversacion["id"]
    )
    assert dict(guardado) == {"role": "assistant", "content": texto}
    meta = await fetch_one("SELECT metadata FROM conversations WHERE id = $1", conversacion["id"])
    assert _json(meta["metadata"])["ack_pendiente"] is False


async def test_necesita_humano_hasta_que_alguien_del_equipo_escribe(monkeypatch, conversacion):
    assert await _flag(conversacion) is False

    await _escalar(monkeypatch, conversacion)
    assert await _flag(conversacion) is True

    # El cliente sigue escribiendo: sigue necesitando a alguien.
    await _mensaje(conversacion, "user", "¿hola?")
    assert await _flag(conversacion) is True

    await _mensaje(conversacion, "human", "Hola Ana, soy Luis.")
    assert await _flag(conversacion) is False


async def test_el_filtro_devuelve_solo_las_que_necesitan_humano(monkeypatch, conversacion):
    filas = await svc.listar(conversacion["tenant_id"], None, None, None, 50, 0, necesita_humano=True)
    assert conversacion["id"] not in {f["id"] for f in filas}

    await _escalar(monkeypatch, conversacion)

    filas = await svc.listar(conversacion["tenant_id"], None, None, None, 50, 0, necesita_humano=True)
    assert conversacion["id"] in {f["id"] for f in filas}


async def test_tomada_desde_el_portal_no_se_marca(conversacion):
    """Ahí ya hay una persona al frente: no hay nada que llamar la atención."""
    await svc.tomar_conversacion(conversacion["tenant_id"], conversacion["id"])
    assert await _flag(conversacion) is False


async def test_devuelta_a_la_ia_se_apaga(monkeypatch, conversacion):
    await _escalar(monkeypatch, conversacion)
    await execute("UPDATE conversations SET status = 'active' WHERE id = $1", conversacion["id"])
    assert await _flag(conversacion) is False


async def test_el_detalle_tambien_lo_trae(monkeypatch, conversacion):
    cab, _ = await svc.detalle(conversacion["tenant_id"], conversacion["id"])
    assert cab["necesita_humano"] is False

    await _escalar(monkeypatch, conversacion)

    cab, _ = await svc.detalle(conversacion["tenant_id"], conversacion["id"])
    assert cab["necesita_humano"] is True


# ------------------------------------------------------------
async def _nada(*_a, **_k):
    return None


async def _mensaje(conv, role, texto):
    await execute(
        "INSERT INTO messages (conversation_id, tenant_id, role, content) VALUES ($1, $2, $3, $4)",
        conv["id"],
        conv["tenant_id"],
        role,
        texto,
    )


def _json(valor):
    import json

    return json.loads(valor) if isinstance(valor, str) else valor
