"""
Política de contraseñas nuevas (services/politica_password.py, tipo
PasswordNueva en schemas.py): 8+ caracteres con letra, número y un símbolo de
@#$%.:, y `password_confirmacion` opcional que, si llega, debe coincidir.

Las pruebas de la regla no usan BD. Las de los endpoints sí (fixtures `db`,
`tenant_y_usuario`): comprueban que una contraseña inválida o una
confirmación distinta no guarda nada, y que una cuenta con contraseña vieja
que no cumple la regla sigue entrando.
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

import routers.auth as auth
from schemas import AceptarInvitacionIn, RegistroIn, RestablecerPasswordIn
from services import recuperacion_password
from services.politica_password import problemas_password, validar_password_nueva
from session import execute, fetch_value

VALIDA = "Clave.2026"


# ============================================================
# La regla
# ============================================================
@pytest.mark.parametrize("password", [
    "Clave.2026", "abc@1234", "a#1aaaaa", "x$9yyyyy", "q%5wwwww", "z.0zzzzz", "m:7mmmmm",
    "ñandú#12",                      # letras con acento cuentan como letra
    "Otra-cosa!_1.",                 # otros símbolos se permiten (no cuentan, pero no estorban)
    "  esp@cio 1 ",                  # espacios: se guarda tal cual, no se recorta
])
def test_cumplen(password):
    assert problemas_password(password) == []
    assert validar_password_nueva(password) == password


@pytest.mark.parametrize("password, falta", [
    ("Cl.1", "al menos 8 caracteres"),
    ("12345678@", "al menos una letra"),
    ("sinnumero@", "al menos un número"),
    ("Clave²³⁴.x", "al menos un número"),          # superíndices no son el número que se pide
    ("Clave1234", "al menos un símbolo (@ # $ % . :)"),
    ("Clave-123!", "al menos un símbolo (@ # $ % . :)"),  # - y ! no son de la lista
])
def test_falta_un_requisito(password, falta):
    assert problemas_password(password) == [falta]


def test_mensaje_junta_todo_lo_que_falta():
    with pytest.raises(ValueError) as e:
        validar_password_nueva("abc")
    assert str(e.value) == (
        "La contraseña necesita al menos 8 caracteres, al menos un número y al menos un símbolo (@ # $ % . :)"
    )


# ============================================================
# Los tres esquemas (sin BD)
# ============================================================
BASE_REGISTRO = {"email": "a@ejemplo.com", "nombre_negocio": "Negocio", "acepta_terminos": True}


@pytest.mark.parametrize("modelo, base", [
    (RegistroIn, BASE_REGISTRO),
    (AceptarInvitacionIn, {"token": "t" * 43, "acepta_terminos": True}),
    (RestablecerPasswordIn, {"email": "a@ejemplo.com", "codigo": "123456"}),
])
def test_esquemas_aplican_regla_y_confirmacion(modelo, base):
    modelo(**base, password=VALIDA)                                   # confirmación opcional
    modelo(**base, password=VALIDA, password_confirmacion=VALIDA)
    with pytest.raises(ValidationError, match="no coinciden"):
        modelo(**base, password=VALIDA, password_confirmacion="Clave.2027")
    with pytest.raises(ValidationError, match="al menos un símbolo"):
        modelo(**base, password="Clave2026", password_confirmacion="Clave2026")
    with pytest.raises(ValidationError):
        modelo(**base, password="Aa.1" * 33)                          # tope de 128


def test_login_no_aplica_la_regla():
    from schemas import LoginIn
    assert LoginIn(email="a@ejemplo.com", password="vieja").password == "vieja"


# ============================================================
# Endpoints (BD)
# ============================================================
@pytest.fixture
def correo_simulado(monkeypatch):
    enviados: list[str] = []

    async def enviar(email: str, codigo: str, negocio: str) -> None:
        enviados.append(codigo)

    monkeypatch.setattr(auth, "enviar_codigo_verificacion", enviar)
    return enviados


@pytest.fixture
async def email_nuevo(db):
    email = f"politica-{uuid4().hex[:12]}@ejemplo.com"
    yield email
    await execute("DELETE FROM email_verifications WHERE email = $1", email)
    tenant_id = await fetch_value("SELECT tenant_id FROM portal_users WHERE LOWER(email) = $1", email)
    if tenant_id:
        await execute("DELETE FROM tenants WHERE id = $1", tenant_id)


def _registro(email: str, **extra) -> dict:
    return {"email": email, "password": VALIDA, "nombre_negocio": "Negocio de prueba", "acepta_terminos": True, **extra}


async def _pendientes(email: str) -> int:
    return await fetch_value("SELECT COUNT(*) FROM email_verifications WHERE email = $1", email)


@pytest.mark.asyncio
@pytest.mark.parametrize("extra, mensaje", [
    ({"password": "corta1."}, "al menos 8 caracteres"),
    ({"password": "sinsimbolo1"}, "al menos un símbolo"),
    ({"password_confirmacion": "Clave.2027"}, "Las contraseñas no coinciden"),
])
async def test_registro_invalido_no_guarda_nada(http_client, correo_simulado, email_nuevo, extra, mensaje):
    r = await http_client.post("/api/auth/registro", json=_registro(email_nuevo, **extra))
    assert r.status_code == 422
    assert mensaje in r.text
    assert correo_simulado == []
    assert await _pendientes(email_nuevo) == 0


@pytest.mark.asyncio
async def test_registro_con_confirmacion_igual_sigue_al_codigo(http_client, correo_simulado, email_nuevo):
    r = await http_client.post("/api/auth/registro", json=_registro(email_nuevo, password_confirmacion=VALIDA))
    assert r.status_code == 202, r.text
    assert len(correo_simulado) == 1
    assert await _pendientes(email_nuevo) == 1


@pytest.fixture
async def cuenta_vieja(tenant_y_usuario):
    """Cuenta de conftest: su contraseña "test_password" NO cumple la regla nueva."""
    await execute(
        "UPDATE portal_users SET terminos_aceptados_en = NOW(), terminos_version = 'test' WHERE id = $1",
        tenant_y_usuario["usuario_id"],
    )
    return tenant_y_usuario


@pytest.mark.asyncio
async def test_contraseña_registrada_antes_sigue_entrando(http_client, cuenta_vieja):
    assert problemas_password("test_password") != []
    r = await http_client.post("/api/auth/login", json={"email": cuenta_vieja["email"], "password": "test_password"})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_restablecer_con_contraseña_debil_no_cambia_nada(http_client, cuenta_vieja, monkeypatch):
    codigos: list[str] = []

    async def falso(destino: str, codigo: str, minutos: int) -> None:
        codigos.append(codigo)

    monkeypatch.setattr(recuperacion_password, "enviar_codigo_recuperacion", falso)
    await http_client.post("/api/auth/recuperar/solicitar", json={"email": cuenta_vieja["email"]})
    assert codigos

    r = await http_client.post(
        "/api/auth/recuperar/restablecer",
        json={"email": cuenta_vieja["email"], "codigo": codigos[0], "password": "debil"},
    )
    assert r.status_code == 422
    # Ni se gastó el código ni cambió la contraseña.
    login = await http_client.post("/api/auth/login", json={"email": cuenta_vieja["email"], "password": "test_password"})
    assert login.status_code == 200
    r = await http_client.post(
        "/api/auth/recuperar/restablecer",
        json={"email": cuenta_vieja["email"], "codigo": codigos[0], "password": VALIDA, "password_confirmacion": VALIDA},
    )
    assert r.status_code == 204, r.text
