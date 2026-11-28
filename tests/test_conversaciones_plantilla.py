"""
Plantillas aprobadas desde la Bandeja (services/conversaciones.py:
enviar_plantilla_humano / plantillas_para_conversacion).

  1. se manda con el número de la conversación y queda en el hilo con el
     texto que recibió el cliente (valores puestos)
  2. solo APPROVED, solo WhatsApp, con todos los valores, nunca a un cliente
     de baja; nada de eso llama al proveedor
  3. si Meta dice que la plantilla está pausada, se marca en la caché
  4. Meta directo usa el adaptador de Graph
  5. el selector solo lista aprobadas y solo en WhatsApp

Sin BD ni red: `_preparar_envio`, la caché, las bajas, el proveedor y el
guardado se sustituyen por dobles.
"""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException

from services import conversaciones as svc
from services import plantillas_cuentas
from services import plantillas_reglas as reglas
from services import plantillas_tenant
from services import whatsapp
from services.agente import bajas

TENANT = uuid4()
CONV = uuid4()
USUARIO = uuid4()

COMPONENTES = reglas.construir_componentes(
    cuerpo="Hola {{1}}, tu pedido {{2}} está listo.", ejemplos_cuerpo=["Ana", "123"], pie="Gracias"
)


def _plantilla(estado: str = "APPROVED") -> dict[str, Any]:
    return {"nombre": "pedido_listo", "idioma": "es_MX", "estado": estado, "componentes": COMPONENTES,
            "categoria": "UTILITY", "motivo_rechazo": None, "actualizada_en": None, "sincronizada_en": None}


@pytest.fixture
def entorno(monkeypatch):
    e = SimpleNamespace(
        canal="whatsapp", bsp="neuroapi", plantilla=_plantilla(), de_baja=False,
        envios=[], guardados=[], marcadas=[], fallo=None, waba_del_numero=None, wabas_pedidas=[],
    )

    async def preparar(tenant_id, conversacion_id):
        return e.canal, "593999999999", {"bsp_provider": e.bsp, "access_token": "clave", "phone_number_id": "PNID-CONV"}

    async def obtener(tenant_id, nombre, *, waba_id=plantillas_tenant.TODAS):
        e.wabas_pedidas.append(waba_id)
        return e.plantilla if nombre == "pedido_listo" and tenant_id == TENANT else None

    async def agente_canal(sql, *a):
        return "NUMERO-1"

    async def waba_de_numero(tenant_id, agente_canal_id):
        assert agente_canal_id == "NUMERO-1"
        return e.waba_del_numero

    async def esta_de_baja(tenant_id, canal, usuario_id):
        return e.de_baja

    class _Proveedor:
        def __init__(self, **kw):
            self.pnid = kw.get("phone_number_id")

        async def enviar_plantilla_con_componentes(self, destinatario, nombre, idioma, componentes):
            if e.fallo:
                raise e.fallo
            e.envios.append({"pnid": self.pnid, "a": destinatario, "nombre": nombre, "idioma": idioma, "comps": componentes})
            return SimpleNamespace(id_mensaje="wamid.1")

    async def guardar(tenant_id, conversacion_id, texto, usuario, wamid):
        e.guardados.append({"texto": texto, "wamid": wamid})
        return {"id": uuid4(), "role": "human", "content": texto, "created_at": None}

    async def marcar(tenant_id, nombre, estado, **kw):
        e.marcadas.append((nombre, estado))

    monkeypatch.setattr(svc, "_preparar_envio", preparar)
    monkeypatch.setattr(svc, "_guardar_humano", guardar)
    monkeypatch.setattr(svc, "fetch_value", agente_canal)
    monkeypatch.setattr(plantillas_cuentas, "waba_cache_de_numero", waba_de_numero)
    monkeypatch.setattr(plantillas_tenant, "obtener", obtener)
    monkeypatch.setattr(plantillas_tenant, "marcar_estado_por_error", marcar)
    monkeypatch.setattr(bajas, "esta_de_baja", esta_de_baja)
    monkeypatch.setattr(whatsapp, "NeuroApiProvider", _Proveedor)
    monkeypatch.setattr("services.meta_plantillas._EnvioPlantillaMeta", _Proveedor)
    return e


async def _enviar(valores=("Luis", "456"), nombre="pedido_listo"):
    return await svc.enviar_plantilla_humano(TENANT, CONV, nombre, list(valores), None, USUARIO)


async def test_se_manda_con_el_numero_de_la_conversacion_y_queda_el_texto(entorno):
    fila = await _enviar()
    assert entorno.envios == [{
        "pnid": "PNID-CONV", "a": "593999999999", "nombre": "pedido_listo", "idioma": "es_MX",
        "comps": [{"type": "body", "parameters": [{"type": "text", "text": "Luis"}, {"type": "text", "text": "456"}]}],
    }]
    assert entorno.guardados == [{"texto": "Hola Luis, tu pedido 456 está listo.\n\nGracias", "wamid": "wamid.1"}]
    assert fila["content"].startswith("Hola Luis")


async def test_busca_la_plantilla_en_la_cuenta_del_numero_de_la_conversacion(entorno):
    """Varias WABAs (sql/81): la misma plantilla puede estar en dos cuentas; vale la del número."""
    entorno.bsp, entorno.waba_del_numero = "meta", "WABA-B"
    await _enviar()
    assert entorno.wabas_pedidas == ["WABA-B"]


async def test_meta_directo_usa_el_adaptador_de_graph(entorno):
    entorno.bsp = "meta"
    await _enviar()
    assert entorno.envios[0]["pnid"] == "PNID-CONV"


@pytest.mark.parametrize(
    "cambio, valores, codigo",
    [
        ({"plantilla": _plantilla("PENDING")}, ("Luis", "456"), 409),
        ({"plantilla": None}, ("Luis", "456"), 404),
        ({}, ("Luis",), 422),
        ({}, ("Luis", "  "), 422),
        ({"de_baja": True}, ("Luis", "456"), 409),
        ({"canal": "instagram"}, ("Luis", "456"), 400),
    ],
)
async def test_lo_que_no_se_puede_mandar_no_llega_al_proveedor(entorno, cambio, valores, codigo):
    for k, v in cambio.items():
        setattr(entorno, k, v)
    with pytest.raises(HTTPException) as e:
        await _enviar(valores)
    assert e.value.status_code == codigo
    assert entorno.envios == [] and entorno.guardados == []


async def test_plantilla_pausada_en_meta_se_marca_y_no_queda_en_el_hilo(entorno):
    entorno.fallo = whatsapp.ProveedorWhatsAppError(
        "pausada", status_code=400, cuerpo='{"error": {"code": 132015, "message": "paused"}}'
    )
    with pytest.raises(HTTPException) as e:
        await _enviar()
    assert e.value.status_code == 502
    assert entorno.marcadas == [("pedido_listo", "PAUSED")]
    assert entorno.guardados == []


async def test_selector_solo_aprobadas_y_solo_whatsapp(monkeypatch):
    canal = {"channel_type": "whatsapp"}
    pedidos: list[Any] = []

    async def fetch_one(sql, *a):
        return {**canal, "agente_canal_id": "NUMERO-1"}

    async def listar(tenant_id, *, categoria=None, estado=None, waba_id=None):
        pedidos.append((estado, waba_id))
        return [_plantilla()]

    async def waba_de_numero(tenant_id, agente_canal_id):
        return "WABA-A" if agente_canal_id == "NUMERO-1" else None

    monkeypatch.setattr(svc, "fetch_one", fetch_one)
    monkeypatch.setattr(plantillas_tenant, "listar", listar)
    monkeypatch.setattr(plantillas_cuentas, "waba_cache_de_numero", waba_de_numero)
    salida = await svc.plantillas_para_conversacion(TENANT, CONV)
    assert pedidos == [("APPROVED", "WABA-A")] and salida[0]["variables_cuerpo"] == 2

    canal["channel_type"] = "instagram"
    assert await svc.plantillas_para_conversacion(TENANT, CONV) == []
