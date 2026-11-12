"""
Equipo de plataforma con persona principal (GERENCIA_PRINCIPAL_EMAIL):
  - se siembra al arrancar, sin duplicar ni pisar lo que ya hay;
  - solo el principal agrega o quita miembros (sin variable, cualquier gerencia);
  - cambiar el principal en el .env no borra al anterior.
"""

from uuid import uuid4

import pytest

from config import settings
from security import crear_access_token, hash_password
from services import gerencia_equipo
from session import execute, fetch_one


async def _gerente(nombre: str) -> dict:
    tid, uid = uuid4(), uuid4()
    email = f"{nombre}-{uid}@operativai.test"
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Interno')", tid)
    await execute(
        "INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active) "
        "VALUES ($1, $2, $3, $4, 'owner', true)",
        uid, tid, email, hash_password("x" * 12),
    )
    await execute(
        "INSERT INTO gerencia_users (email, full_name, cargo) VALUES ($1, 'Test', 'QA')", email
    )
    return {
        "tid": tid,
        "email": email,
        "headers": {"Authorization": f"Bearer {crear_access_token(uid, tid, 'owner')}"},
    }


async def _borrar(g: dict) -> None:
    await execute("DELETE FROM gerencia_users WHERE LOWER(email) = LOWER($1)", g["email"])
    await execute("DELETE FROM gerencia_auditoria WHERE actor_email = $1", g["email"])
    await execute("DELETE FROM tenants WHERE id = $1", g["tid"])


@pytest.fixture
async def equipo(db):
    principal, otro = await _gerente("principal"), await _gerente("otro")
    nuevos: list[str] = []
    yield {"principal": principal, "otro": otro, "nuevos": nuevos}
    for e in nuevos:
        await execute("DELETE FROM gerencia_users WHERE LOWER(email) = LOWER($1)", e)
    await _borrar(principal)
    await _borrar(otro)


def _alta(email: str) -> dict:
    return {"email": email, "full_name": "Nueva Persona", "cargo": "Soporte"}


@pytest.mark.asyncio
async def test_solo_el_principal_agrega(http_client, equipo, monkeypatch):
    monkeypatch.setattr(settings, "GERENCIA_PRINCIPAL_EMAIL", equipo["principal"]["email"].lower())
    nuevo = f"nuevo-{uuid4()}@ejemplo.com"
    equipo["nuevos"].append(nuevo)

    rechazado = await http_client.post(
        "/api/gerencia/usuarios", json=_alta(nuevo), headers=equipo["otro"]["headers"]
    )
    aceptado = await http_client.post(
        "/api/gerencia/usuarios", json=_alta(nuevo), headers=equipo["principal"]["headers"]
    )

    assert rechazado.status_code == 403
    assert aceptado.status_code == 201


@pytest.mark.asyncio
async def test_solo_el_principal_quita(http_client, equipo, monkeypatch):
    monkeypatch.setattr(settings, "GERENCIA_PRINCIPAL_EMAIL", equipo["principal"]["email"].lower())
    fila = await fetch_one("SELECT id FROM gerencia_users WHERE email = $1", equipo["otro"]["email"])

    rechazado = await http_client.delete(
        f"/api/gerencia/usuarios/{fila['id']}", headers=equipo["otro"]["headers"]
    )
    aceptado = await http_client.delete(
        f"/api/gerencia/usuarios/{fila['id']}", headers=equipo["principal"]["headers"]
    )

    assert rechazado.status_code == 403
    assert aceptado.status_code == 204


@pytest.mark.asyncio
async def test_cualquiera_puede_ver_el_equipo_y_la_respuesta_dice_que_puede(http_client, equipo, monkeypatch):
    monkeypatch.setattr(settings, "GERENCIA_PRINCIPAL_EMAIL", equipo["principal"]["email"].lower())

    como_otro = await http_client.get("/api/gerencia/usuarios", headers=equipo["otro"]["headers"])
    como_principal = await http_client.get("/api/gerencia/usuarios", headers=equipo["principal"]["headers"])

    assert como_otro.status_code == 200
    assert all(u["puede_administrar"] is False for u in como_otro.json())
    assert all(u["puede_administrar"] is True for u in como_principal.json())
    marcados = [u["email"] for u in como_principal.json() if u["es_principal"]]
    assert marcados == [equipo["principal"]["email"]]


@pytest.mark.asyncio
async def test_sin_principal_configurado_cualquier_gerencia_administra(http_client, equipo, monkeypatch):
    monkeypatch.setattr(settings, "GERENCIA_PRINCIPAL_EMAIL", "")
    nuevo = f"nuevo-{uuid4()}@ejemplo.com"
    equipo["nuevos"].append(nuevo)

    r = await http_client.post(
        "/api/gerencia/usuarios", json=_alta(nuevo), headers=equipo["otro"]["headers"]
    )

    assert r.status_code == 201


@pytest.mark.asyncio
async def test_se_siembra_una_vez_y_no_pisa_lo_existente(db, monkeypatch):
    email = f"semilla-{uuid4()}@operativai.test"
    monkeypatch.setattr(settings, "GERENCIA_PRINCIPAL_EMAIL", email)
    monkeypatch.setattr(settings, "GERENCIA_PRINCIPAL_NOMBRE", "Nombre del env")
    monkeypatch.setattr(settings, "GERENCIA_PRINCIPAL_CARGO", "Director")
    try:
        assert await gerencia_equipo.sembrar_principal() is True
        await execute("UPDATE gerencia_users SET full_name = 'Editado a mano' WHERE email = $1", email)
        assert await gerencia_equipo.sembrar_principal() is False

        fila = await fetch_one("SELECT full_name, cargo FROM gerencia_users WHERE email = $1", email)
        assert fila["full_name"] == "Editado a mano"
        assert fila["cargo"] == "Director"
    finally:
        await execute("DELETE FROM gerencia_users WHERE email = $1", email)


@pytest.mark.asyncio
async def test_sembrar_ignora_mayusculas_del_existente(db, monkeypatch):
    email = f"mayus-{uuid4()}@operativai.test"
    monkeypatch.setattr(settings, "GERENCIA_PRINCIPAL_EMAIL", email)
    await execute("INSERT INTO gerencia_users (email, full_name, cargo) VALUES ($1, 'X', 'Y')", email.upper())
    try:
        assert await gerencia_equipo.sembrar_principal() is False
    finally:
        await execute("DELETE FROM gerencia_users WHERE LOWER(email) = $1", email)


@pytest.mark.asyncio
async def test_sin_variable_no_siembra_nada(db, monkeypatch):
    monkeypatch.setattr(settings, "GERENCIA_PRINCIPAL_EMAIL", "")
    assert await gerencia_equipo.sembrar_principal() is False


@pytest.mark.asyncio
async def test_cambiar_el_principal_no_borra_al_anterior(db, monkeypatch):
    uno, dos = f"uno-{uuid4()}@operativai.test", f"dos-{uuid4()}@operativai.test"
    try:
        monkeypatch.setattr(settings, "GERENCIA_PRINCIPAL_EMAIL", uno)
        await gerencia_equipo.sembrar_principal()
        monkeypatch.setattr(settings, "GERENCIA_PRINCIPAL_EMAIL", dos)
        await gerencia_equipo.sembrar_principal()

        filas = await fetch_one(
            "SELECT COUNT(*) AS n FROM gerencia_users WHERE email = ANY($1::text[])", [uno, dos]
        )
        assert filas["n"] == 2
        assert gerencia_equipo.es_principal(dos) and not gerencia_equipo.es_principal(uno)
    finally:
        await execute("DELETE FROM gerencia_users WHERE email = ANY($1::text[])", [uno, dos])


@pytest.mark.asyncio
async def test_si_la_base_va_atrasada_el_arranque_no_se_cae(monkeypatch):
    import asyncpg

    def sin_tabla():
        raise asyncpg.UndefinedTableError('relation "gerencia_users" does not exist')

    monkeypatch.setattr(settings, "GERENCIA_PRINCIPAL_EMAIL", "alguien@ejemplo.com")
    monkeypatch.setattr(gerencia_equipo, "transaccion", sin_tabla)

    assert await gerencia_equipo.sembrar_principal() is False

