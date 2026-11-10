"""
GET /api/planes/publicos (routers/planes_publicos.py): lo que ve la landing.

Requiere la migración 74 aplicada (columna planes.destacado) y Postgres
alcanzable, como el resto de tests con el fixture `db`.
"""

from uuid import uuid4

import pytest

from routers.planes_publicos import LIMITE_PETICIONES
from services import limite_publico
from session import execute, fetch_one


@pytest.fixture(autouse=True)
def _sin_limite_previo():
    limite_publico.reiniciar()
    yield
    limite_publico.reiniciar()


@pytest.fixture
async def nombres(db):
    """Tres nombres únicos por test; el teardown borra los planes pase lo que pase."""
    sufijo = uuid4().hex[:12]
    lista = [f"pub-{i}-{sufijo}" for i in range(3)]
    yield lista
    for n in lista:
        await execute("DELETE FROM planes WHERE nombre = $1", n)


async def _plan(nombre, *, activo=True, price="price_x", orden=9000, destacado=False, precio=100):
    await execute(
        """
        INSERT INTO planes (nombre, descripcion, precio_monthly, activo, orden,
                            stripe_price_id, destacado)
        VALUES ($1, 'desc', $2, $3, $4, $5, $6)
        """,
        nombre, precio, activo, orden, price, destacado,
    )


async def _publicos(http_client):
    r = await http_client.get("/api/planes/publicos")
    assert r.status_code == 200
    return r, {p["nombre"]: p for p in r.json()["planes"]}


@pytest.mark.asyncio
async def test_no_pide_autenticacion_y_cachea_60s(http_client, nombres):
    r, _ = await _publicos(http_client)
    assert r.headers["cache-control"] == "public, max-age=60"


@pytest.mark.asyncio
async def test_pasado_el_limite_responde_429_con_retry_after(http_client, nombres):
    for _ in range(LIMITE_PETICIONES):
        assert (await http_client.get("/api/planes/publicos")).status_code == 200

    r = await http_client.get("/api/planes/publicos")

    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1


@pytest.mark.asyncio
async def test_el_limite_es_por_ip(http_client, nombres):
    for _ in range(LIMITE_PETICIONES):
        await http_client.get("/api/planes/publicos", headers={"X-Forwarded-For": "203.0.113.1"})

    bloqueada = await http_client.get("/api/planes/publicos", headers={"X-Forwarded-For": "203.0.113.1"})
    otra = await http_client.get("/api/planes/publicos", headers={"X-Forwarded-For": "203.0.113.2"})

    assert bloqueada.status_code == 429
    assert otra.status_code == 200


@pytest.mark.asyncio
async def test_solo_activos_con_price_de_stripe(http_client, nombres):
    ok, apagado, sin_price = nombres
    await _plan(ok)
    await _plan(apagado, activo=False)
    await _plan(sin_price, price=None)

    _, por_nombre = await _publicos(http_client)

    assert ok in por_nombre
    assert apagado not in por_nombre
    assert sin_price not in por_nombre


@pytest.mark.asyncio
async def test_no_expone_campos_internos(http_client, nombres):
    await _plan(nombres[0])

    _, por_nombre = await _publicos(http_client)

    campos = set(por_nombre[nombres[0]])
    for prohibido in ("stripe_price_id", "modelos_permitidos", "historial_maximo",
                      "max_numeros_whatsapp", "coexistencia_activa", "activo", "orden"):
        assert prohibido not in campos


@pytest.mark.asyncio
async def test_orden_por_orden_y_precio(http_client, nombres):
    a, b, c = nombres
    await _plan(a, orden=9002, precio=10)
    await _plan(b, orden=9001, precio=500)
    await _plan(c, orden=9001, precio=50)

    r, _ = await _publicos(http_client)

    propios = [p["nombre"] for p in r.json()["planes"] if p["nombre"] in nombres]
    assert propios == [c, b, a]


@pytest.mark.asyncio
async def test_pausar_un_plan_lo_saca_de_inmediato(http_client, nombres):
    await _plan(nombres[0])
    _, antes = await _publicos(http_client)
    await execute("UPDATE planes SET activo = false WHERE nombre = $1", nombres[0])
    _, despues = await _publicos(http_client)

    assert nombres[0] in antes
    assert nombres[0] not in despues


@pytest.mark.asyncio
async def test_el_destacado_viaja_en_la_respuesta(http_client, nombres):
    await _plan(nombres[0], destacado=True)

    _, por_nombre = await _publicos(http_client)

    assert por_nombre[nombres[0]]["destacado"] is True
    fila = await fetch_one("SELECT destacado FROM planes WHERE nombre = $1", nombres[0])
    assert fila["destacado"] is True
