"""
`/api/canales/entrada` con un cambio de estado de plantilla de Meta directo
(routers/agente_webhook.py): firmado con el app_secret de la WABA, va a la
caché de plantillas (global y del negocio) y nunca entra al agente. Sin BD ni red.
"""

import asyncio
import hashlib
import hmac
import json

from httpx import ASGITransport, AsyncClient

from config import settings
from main import app
from routers import agente_webhook
from services.agente import firma

SECRETO = "app-secret-del-negocio"
CUERPO = json.dumps({
    "object": "whatsapp_business_account",
    "entry": [{"id": "WABA-1", "time": 1, "changes": [{
        "field": "message_template_status_update",
        "value": {"event": "PAUSED", "message_template_id": 9, "message_template_name": "recordatorio_cita",
                  "message_template_language": "es_MX", "reason": "NONE"},
    }]}],
}).encode()


def _firma(cuerpo: bytes) -> str:
    return "sha256=" + hmac.new(SECRETO.encode(), cuerpo, hashlib.sha256).hexdigest()


async def test_estado_de_plantilla_va_a_la_cache_y_no_al_agente(monkeypatch):
    monkeypatch.setattr(settings, "AGENTE_EN_CODIGO", True)
    vistos: list[str] = []

    async def secreto(sql, *a):
        return SECRETO

    async def global_(evento):
        vistos.append("global")

    async def negocio(evento):
        vistos.append(("negocio", evento["entry"][0]["id"]))
        return []

    async def agente(peticion):
        raise AssertionError("un estado de plantilla no es un mensaje")

    monkeypatch.setattr(firma, "fetch_value", secreto)
    monkeypatch.setattr(agente_webhook.plantillas_whatsapp, "aplicar_evento_meta", global_)
    monkeypatch.setattr(agente_webhook.plantillas_tenant, "aplicar_evento_meta", negocio)
    monkeypatch.setattr(agente_webhook.ingreso, "procesar", agente)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/canales/entrada", content=CUERPO,
                         headers={"x-hub-signature-256": _firma(CUERPO), "content-type": "application/json"})
    assert r.status_code == 200 and r.text == "EVENT_RECEIVED"
    await asyncio.gather(*list(agente_webhook._EN_CURSO))
    assert vistos == ["global", ("negocio", "WABA-1")]


async def test_si_falla_el_global_igual_se_aplica_el_del_negocio(monkeypatch):
    vistos: list[str] = []

    async def global_(evento):
        raise RuntimeError("boom")

    async def negocio(evento):
        vistos.append("negocio")
        return []

    monkeypatch.setattr(agente_webhook.plantillas_whatsapp, "aplicar_evento_meta", global_)
    monkeypatch.setattr(agente_webhook.plantillas_tenant, "aplicar_evento_meta", negocio)
    await agente_webhook._aplicar_estado_de_plantillas(json.loads(CUERPO))
    assert vistos == ["negocio"]


async def test_firma_de_otra_app_se_rechaza(monkeypatch):
    monkeypatch.setattr(settings, "AGENTE_EN_CODIGO", True)

    async def secreto(sql, *a):
        return "otro-secreto"

    monkeypatch.setattr(firma, "fetch_value", secreto)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/canales/entrada", content=CUERPO,
                         headers={"x-hub-signature-256": _firma(CUERPO), "content-type": "application/json"})
    assert r.status_code == 403
