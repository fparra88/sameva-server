"""
Tests de GET /api/tenants/{id}/embudo (tablero del embudo del portal).

Vía HTTP, como test_reportes_endpoints.py: interesa el contrato completo
(auth -> módulo activo -> SQL -> forma de la respuesta).
"""

from uuid import uuid4

import pytest

from security import crear_access_token, hash_password
from services.pipeline import get_or_create_pipeline
from session import conexion, execute, fetch_value

pytestmark = pytest.mark.usefixtures("plan_enterprise")


def ruta(tenant_id) -> str:
    return f"/api/tenants/{tenant_id}/embudo"


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def activar_modulo(tenant_id) -> None:
    await execute(
        """
        INSERT INTO tenant_servicios (tenant_id, gestion_vendedores_activo)
        VALUES ($1, true)
        ON CONFLICT (tenant_id) DO UPDATE SET gestion_vendedores_activo = true
        """,
        tenant_id,
    )


async def crear_lead(tenant_id, nombre="Carla", whatsapp="5215500000001"):
    """Contacto + fila de embudo en 'nuevo' (con su renglón de alta en la bitácora)."""
    user_id = await fetch_value(
        "INSERT INTO users (tenant_id, whatsapp_id, display_name) VALUES ($1, $2, $3) RETURNING id",
        tenant_id,
        whatsapp,
        nombre,
    )
    async with conexion() as conn:
        await get_or_create_pipeline(tenant_id, user_id, conn)
    return user_id


async def crear_chat(tenant_id, user_id, mensajes: list[tuple[str, str]]):
    """Conversación de WhatsApp con mensajes (rol, texto) en orden."""
    conversacion_id = await fetch_value(
        """
        INSERT INTO conversations (tenant_id, user_id, channel_type)
        VALUES ($1, $2, 'whatsapp') RETURNING id
        """,
        tenant_id,
        user_id,
    )
    for i, (rol, texto) in enumerate(mensajes):
        await execute(
            """
            INSERT INTO messages (conversation_id, tenant_id, role, content, created_at)
            VALUES ($1, $2, $3, $4, now() - make_interval(mins => $5))
            """,
            conversacion_id,
            tenant_id,
            rol,
            texto,
            len(mensajes) - i,
        )
    return conversacion_id


async def test_sin_token_rechazado(http_client, tenant_y_usuario):
    r = await http_client.get(ruta(tenant_y_usuario["tenant_id"]))
    assert r.status_code == 401


async def test_modulo_apagado_da_409(http_client, tenant_y_usuario):
    r = await http_client.get(
        ruta(tenant_y_usuario["tenant_id"]), headers=auth(tenant_y_usuario["token"])
    )
    assert r.status_code == 409


async def test_vendedor_no_ve_el_tablero(http_client, tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    await activar_modulo(tenant_id)
    usuario_id = uuid4()
    await execute(
        """
        INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active)
        VALUES ($1, $2, $3, $4, 'vendedor', true)
        """,
        usuario_id,
        tenant_id,
        f"vendedor-{usuario_id}@ejemplo.com",
        hash_password("test_password"),
    )
    token = crear_access_token(usuario_id, tenant_id, "vendedor")

    r = await http_client.get(ruta(tenant_id), headers=auth(token))
    assert r.status_code == 403


async def test_junta_crm_y_chat(http_client, tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    await activar_modulo(tenant_id)

    con_chat = await crear_lead(tenant_id, "Carla", "5215500000001")
    conversacion_id = await crear_chat(
        tenant_id,
        con_chat,
        [("user", "Hola"), ("assistant", "¿En qué te ayudo?"), ("user", "Quiero una cotización")],
    )
    await execute(
        """
        UPDATE client_pipeline
        SET monto_estimado = 1500, proximo_seguimiento = now() - interval '1 day',
            seguimiento_nota = 'Mandar cotización'
        WHERE tenant_id = $1 AND user_id = $2
        """,
        tenant_id,
        con_chat,
    )
    sin_chat = await crear_lead(tenant_id, "Beto", "5215500000002")
    await execute(
        """
        UPDATE client_pipeline SET estado = 'perdido', motivo_perdida = ' Precio '
        WHERE tenant_id = $1 AND user_id = $2
        """,
        tenant_id,
        sin_chat,
    )

    r = await http_client.get(ruta(tenant_id), headers=auth(tenant_y_usuario["token"]))
    assert r.status_code == 200
    cuerpo = r.json()

    leads = {lead["user_id"]: lead for lead in cuerpo["leads"]}
    assert set(leads) == {str(con_chat), str(sin_chat)}

    carla = leads[str(con_chat)]
    assert carla["cliente_nombre"] == "Carla"
    assert carla["estado"] == "nuevo"
    assert carla["transiciones_posibles"] == ["contactado", "perdido"]
    assert float(carla["monto_estimado"]) == 1500
    assert carla["seguimiento_nota"] == "Mandar cotización"
    assert carla["alta_en"] is not None and carla["etapa_desde"] is not None
    assert carla["conversation_id"] == str(conversacion_id)
    assert carla["canal"] == "whatsapp"
    assert carla["total_mensajes"] == 3
    assert carla["mensajes_cliente"] == 2
    assert carla["ultimo_mensaje"] == "Quiero una cotización"
    assert carla["ultimo_mensaje_rol"] == "user"

    beto = leads[str(sin_chat)]
    assert beto["conversation_id"] is None and beto["canal"] is None
    assert beto["total_mensajes"] == 0 and beto["ultimo_mensaje"] is None

    etapas = {e["estado"]: e for e in cuerpo["etapas"]}
    assert list(etapas)[0] == "nuevo" and len(etapas) == 7
    assert etapas["nuevo"]["total"] == 1
    assert float(etapas["nuevo"]["monto"]) == 1500
    assert etapas["nuevo"]["sin_vendedor"] == 1
    assert etapas["nuevo"]["seguimientos_vencidos"] == 1
    assert etapas["perdido"]["total"] == 1
    assert etapas["cotizado"]["total"] == 0

    canales = {c["canal"]: c["total"] for c in cuerpo["por_canal"]}
    assert canales == {"whatsapp": 1, "sin_chat": 1}
    assert cuerpo["motivos_perdida"] == [{"motivo": "precio", "total": 1}]


async def test_recorta_cerrados_pero_no_los_totales(http_client, tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    await activar_modulo(tenant_id)
    for i in range(3):
        user_id = await crear_lead(tenant_id, f"Ganado {i}", f"52155000001{i}")
        await execute(
            "UPDATE client_pipeline SET estado = 'ganado' WHERE tenant_id = $1 AND user_id = $2",
            tenant_id,
            user_id,
        )

    r = await http_client.get(
        ruta(tenant_id) + "?limite_cerrados=2", headers=auth(tenant_y_usuario["token"])
    )
    assert r.status_code == 200
    cuerpo = r.json()
    assert len(cuerpo["leads"]) == 2
    assert {e["estado"]: e["total"] for e in cuerpo["etapas"]}["ganado"] == 3


async def test_no_cruza_negocios(http_client, tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    await activar_modulo(tenant_id)
    otro = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Otro negocio')", otro)
    try:
        await crear_lead(otro, "Ajeno", "5215599999999")

        propio = await http_client.get(ruta(tenant_id), headers=auth(tenant_y_usuario["token"]))
        assert propio.status_code == 200 and propio.json()["leads"] == []

        ajeno = await http_client.get(ruta(otro), headers=auth(tenant_y_usuario["token"]))
        assert ajeno.status_code in (403, 404)
    finally:
        await execute("DELETE FROM tenants WHERE id = $1", otro)
