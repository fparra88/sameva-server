"""
Aviso al owner por tope de llamadas IA (spec 001, T5): una alerta por ciclo.

Con BD real (fixture `tenant_y_usuario`); el WebSocket y el correo espejo se
simulan para no tocar la red.
"""

from datetime import datetime, timedelta, timezone

import pytest

import realtime
from schemas import TipoAlerta
from services import correo_espejo, limite_ia
from services.uso_ia import UsoIA
from session import execute, fetch_all


@pytest.fixture(autouse=True)
def _sin_red(monkeypatch):
    emitidos: list[dict] = []

    async def emit(evento, datos, room=None, **kw):
        emitidos.append(datos)

    async def espejar_nulo(*args, **kwargs):
        return 0

    monkeypatch.setattr(realtime.sio, "emit", emit)
    monkeypatch.setattr(correo_espejo, "espejar", espejar_nulo)
    return emitidos


def _uso(ciclo_fin: datetime) -> UsoIA:
    return UsoIA(
        usadas=100,
        limite=100,
        ciclo_inicio=ciclo_fin - timedelta(days=30),
        ciclo_fin=ciclo_fin,
    )


async def _alertas(tenant_id):
    return await fetch_all(
        "SELECT datos, portal_user_id FROM alertas WHERE tenant_id = $1 AND tipo = 'limite_llamadas_ia'",
        tenant_id,
    )


def test_tipo_alerta_incluye_limite_llamadas_ia():
    assert TipoAlerta("limite_llamadas_ia") is TipoAlerta.limite_llamadas_ia


def test_espejo_por_correo_activo_para_el_tipo():
    # Sin exclusión: el correo espejo cubre el aviso (como todo tipo no listado).
    assert correo_espejo.debe_espejarse("limite_llamadas_ia")


async def test_dos_llamadas_mismo_ciclo_una_alerta(tenant_y_usuario):
    t = tenant_y_usuario["tenant_id"]
    fin = datetime(2030, 2, 1, tzinfo=timezone.utc)

    primera = await limite_ia.avisar_owner_una_vez(t, _uso(fin))
    segunda = await limite_ia.avisar_owner_una_vez(t, _uso(fin))

    assert primera is True
    assert segunda is False
    filas = await _alertas(t)
    assert len(filas) == 1
    assert filas[0]["portal_user_id"] == tenant_y_usuario["usuario_id"]
    assert filas[0]["datos"]["ciclo_fin"] == fin.isoformat()


async def test_ciclo_nuevo_otra_alerta(tenant_y_usuario):
    t = tenant_y_usuario["tenant_id"]
    fin = datetime(2030, 2, 1, tzinfo=timezone.utc)

    assert await limite_ia.avisar_owner_una_vez(t, _uso(fin)) is True
    assert await limite_ia.avisar_owner_una_vez(t, _uso(fin + timedelta(days=30))) is True

    assert len(await _alertas(t)) == 2


async def test_llamadas_concurrentes_una_alerta(tenant_y_usuario):
    import asyncio

    t = tenant_y_usuario["tenant_id"]
    fin = datetime(2030, 3, 1, tzinfo=timezone.utc)

    resultados = await asyncio.gather(
        *(limite_ia.avisar_owner_una_vez(t, _uso(fin)) for _ in range(3))
    )

    assert sum(resultados) == 1
    assert len(await _alertas(t)) == 1


async def test_sin_tope_o_sin_ciclo_no_avisa(tenant_y_usuario):
    t = tenant_y_usuario["tenant_id"]
    sin_tope = UsoIA(usadas=0, limite=None, ciclo_inicio=None, ciclo_fin=None)

    assert await limite_ia.avisar_owner_una_vez(t, sin_tope) is False
    assert await _alertas(t) == []


async def test_aislado_por_tenant(tenant_y_usuario):
    from uuid import uuid4

    otro = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Otro')", otro)
    try:
        fin = datetime(2030, 4, 1, tzinfo=timezone.utc)
        assert await limite_ia.avisar_owner_una_vez(tenant_y_usuario["tenant_id"], _uso(fin)) is True
        assert await limite_ia.avisar_owner_una_vez(otro, _uso(fin)) is True  # sin cuentas: no crea
        assert len(await _alertas(otro)) == 0
    finally:
        await execute("DELETE FROM tenants WHERE id = $1", otro)
