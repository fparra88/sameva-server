"""Tests de services/uso_ia.py (spec 001: tope de llamadas de IA por plan)."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from services import uso_ia
from services.uso_ia import UsoIA, ciclo_de, evaluar
from session import execute


# ------------------------------------------------------------ puros
def test_ciclo_de_resta_un_mes():
    fin = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)
    ini, fin_dev = ciclo_de(fin)
    assert ini == datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
    assert fin_dev == fin


def test_ciclo_de_cruza_anio():
    ini, _ = ciclo_de(datetime(2026, 1, 20, tzinfo=timezone.utc))
    assert ini == datetime(2025, 12, 20, tzinfo=timezone.utc)


def test_ciclo_de_ajusta_dia_inexistente():
    # 31 de marzo - 1 mes = 28 de febrero (2026 no es bisiesto)
    ini, _ = ciclo_de(datetime(2026, 3, 31, tzinfo=timezone.utc))
    assert ini == datetime(2026, 2, 28, tzinfo=timezone.utc)


@pytest.mark.parametrize("limite", [None, 0])
def test_evaluar_sin_tope_no_bloquea(limite):
    assert evaluar(10_000, limite, "activo") is False


def test_evaluar_prueba_no_bloquea():
    assert evaluar(10_000, 100, "prueba") is False


def test_evaluar_limites():
    assert evaluar(99, 100, "activo") is False
    assert evaluar(100, 100, "activo") is True
    assert evaluar(150, 100, None) is True


def test_usoia_propiedades():
    u = UsoIA(usadas=50, limite=200, ciclo_inicio=None, ciclo_fin=None)
    assert u.agotado is False and u.porcentaje == 25
    assert UsoIA(200, 200, None, None).agotado is True
    sin = UsoIA(5, None, None, None)
    assert sin.agotado is False and sin.porcentaje is None


# ------------------------------------------------------------ con BD
async def _plan(nombre: str, tope, orden=0, price="price_x", activo=True):
    await execute(
        """
        INSERT INTO planes (nombre, precio_monthly, max_leads_mensuales, orden,
                            stripe_price_id, activo)
        VALUES ($1, 10, $2, $3, $4, $5)
        """,
        nombre, tope, orden, price, activo,
    )


async def _suscribir(tenant_id, plan, renovacion):
    await execute(
        """
        INSERT INTO tenant_subscriptions (tenant_id, plan, estado, precio_monthly,
                                          fecha_renovacion)
        VALUES ($1, $2, 'activa', 10, $3)
        """,
        tenant_id, plan, renovacion,
    )


async def _uso(tenant_id, origen, llamadas, cuando):
    await execute(
        """
        INSERT INTO tenant_token_usage (tenant_id, origen, llamadas, created_at)
        VALUES ($1, $2, $3, $4)
        """,
        tenant_id, origen, llamadas, cuando,
    )


@pytest.fixture
async def planes_prueba(db):
    sufijo = uuid4().hex[:8]
    nombres = {
        "a": f"t-uso-a-{sufijo}",
        "b": f"t-uso-b-{sufijo}",
        "c": f"t-uso-c-{sufijo}",
    }
    yield nombres
    await execute(
        "DELETE FROM tenant_subscriptions WHERE plan = ANY($1::text[])", list(nombres.values())
    )
    await execute("DELETE FROM planes WHERE nombre = ANY($1::text[])", list(nombres.values()))


async def test_uso_ciclo_suma_solo_ia_dentro_del_ciclo(tenant_y_usuario, planes_prueba):
    t = tenant_y_usuario["tenant_id"]
    await _plan(planes_prueba["a"], 100)
    ahora = datetime.now(timezone.utc)
    await _suscribir(t, planes_prueba["a"], ahora + timedelta(days=10))
    dentro = ahora - timedelta(days=5)
    await _uso(t, "agente", 3, dentro)
    await _uso(t, "herramienta", 2, dentro)
    await _uso(t, "resumen", 1, dentro)
    await _uso(t, "transcripcion", 50, dentro)           # Groq: no cuenta
    await _uso(t, "agente", 40, ahora - timedelta(days=60))  # ciclo anterior
    uso = await uso_ia.uso_ciclo(t)
    assert uso.limite == 100
    assert uso.usadas == 6
    assert uso.agotado is False
    assert uso.ciclo_fin is not None and uso.ciclo_inicio is not None


async def test_uso_ciclo_agotado(tenant_y_usuario, planes_prueba):
    t = tenant_y_usuario["tenant_id"]
    await _plan(planes_prueba["a"], 5)
    ahora = datetime.now(timezone.utc)
    await _suscribir(t, planes_prueba["a"], ahora + timedelta(days=3))
    await _uso(t, "agente", 5, ahora - timedelta(days=1))
    assert (await uso_ia.uso_ciclo(t)).agotado is True


async def test_uso_ciclo_aislado_por_tenant(tenant_y_usuario, planes_prueba):
    t = tenant_y_usuario["tenant_id"]
    otro = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'otro')", otro)
    try:
        await _plan(planes_prueba["a"], 100)
        ahora = datetime.now(timezone.utc)
        await _suscribir(t, planes_prueba["a"], ahora + timedelta(days=10))
        await _uso(otro, "agente", 99, ahora - timedelta(days=1))
        await _uso(t, "agente", 2, ahora - timedelta(days=1))
        assert (await uso_ia.uso_ciclo(t)).usadas == 2
    finally:
        await execute("DELETE FROM tenants WHERE id = $1", otro)


@pytest.mark.parametrize("tope", [None, 0])
async def test_uso_ciclo_sin_tope(tenant_y_usuario, planes_prueba, tope):
    t = tenant_y_usuario["tenant_id"]
    await _plan(planes_prueba["a"], tope)
    ahora = datetime.now(timezone.utc)
    await _suscribir(t, planes_prueba["a"], ahora + timedelta(days=10))
    await _uso(t, "agente", 500, ahora - timedelta(days=1))
    uso = await uso_ia.uso_ciclo(t)
    assert uso.limite is None and uso.agotado is False and uso.porcentaje is None


async def test_uso_ciclo_sin_suscripcion(tenant_y_usuario):
    uso = await uso_ia.uso_ciclo(tenant_y_usuario["tenant_id"])
    assert uso.limite is None and uso.agotado is False


async def test_uso_ciclo_en_prueba_sin_tope(tenant_y_usuario, planes_prueba):
    t = tenant_y_usuario["tenant_id"]
    await _plan(planes_prueba["a"], 1)
    ahora = datetime.now(timezone.utc)
    await _suscribir(t, planes_prueba["a"], ahora + timedelta(days=10))
    await _uso(t, "agente", 10, ahora - timedelta(days=1))
    await execute(
        """
        INSERT INTO tenant_estado_plataforma (tenant_id, estado)
        VALUES ($1, 'prueba')
        ON CONFLICT (tenant_id) DO UPDATE SET estado = 'prueba'
        """,
        t,
    )
    uso = await uso_ia.uso_ciclo(t)
    assert uso.limite is None and uso.agotado is False


async def test_plan_siguiente(tenant_y_usuario, planes_prueba):
    t = tenant_y_usuario["tenant_id"]
    n = planes_prueba
    # orden alto para quedar por encima de los planes reales
    await _plan(n["a"], 100, orden=1001)
    await _plan(n["b"], 100, orden=1002)               # no mejora el tope
    await _plan(n["c"], None, orden=1003)              # sin tope: sí mejora
    ahora = datetime.now(timezone.utc)
    await _suscribir(t, n["a"], ahora + timedelta(days=10))
    assert await uso_ia.plan_siguiente(t) == n["c"]
    # sin price de Stripe deja de ser candidato
    await execute("UPDATE planes SET stripe_price_id = NULL WHERE nombre = $1", n["c"])
    assert await uso_ia.plan_siguiente(t) is None


async def test_plan_siguiente_sin_suscripcion(tenant_y_usuario):
    assert await uso_ia.plan_siguiente(tenant_y_usuario["tenant_id"]) is None
