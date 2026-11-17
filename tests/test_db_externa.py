"""
Herramienta «Base de datos externa» (tool_type db_externa).

Primera parte: lógica pura (cifrado, anti-SSRF, constructor de consultas, permisos,
formato del resultado) con dobles de conexión: no abre ninguna base ajena.
Segunda parte: la API y el plan, con la base de la plataforma y `db_externa.probar`
sustituido por un doble.
"""

import json
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from config import settings
from services import db_externa, herramientas_plan, secretos
from session import execute, fetch_all, fetch_one, fetch_value


# ============================================================
# Fixtures
# ============================================================
@pytest.fixture
def clave(monkeypatch):
    k = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "DB_SECRETOS_KEY", k)
    monkeypatch.setattr(settings, "DB_SECRETOS_KEY_ANTERIOR", "")
    return k


PEDIDOS = {
    "objeto": "pedidos",
    "esquema": "public",
    "nombre": "pedidos",
    "tipo": "vista",
    "descripcion": "Pedidos del cliente",
    "columnas": [
        {"nombre": "id", "tipo": "integer"},
        {"nombre": "cliente", "tipo": "text"},
        {"nombre": "total", "tipo": "numeric"},
        {"nombre": "creado", "tipo": "timestamp without time zone"},
        {"nombre": "activo", "tipo": "boolean"},
        {"nombre": "estado", "tipo": "USER-DEFINED"},
    ],
}


# ============================================================
# Cifrado
# ============================================================
def test_secreto_ida_y_vuelta_y_no_queda_en_claro(clave):
    t = uuid4()
    token = secretos.cifrar(t, "mi_db", "s3creto-Ñ")
    assert b"s3creto" not in token
    assert secretos.descifrar(t, "mi_db", token) == "s3creto-Ñ"


def test_secreto_de_otro_negocio_o_herramienta_no_sirve(clave):
    t = uuid4()
    token = secretos.cifrar(t, "mi_db", "pw")
    assert secretos.descifrar(uuid4(), "mi_db", token) is None
    assert secretos.descifrar(t, "otra", token) is None
    assert secretos.descifrar(t, "mi_db", b"basura") is None


def test_rotacion_de_clave(monkeypatch, clave):
    t = uuid4()
    viejo = secretos.cifrar(t, "k", "pw")
    nueva = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "DB_SECRETOS_KEY", nueva)
    assert secretos.descifrar(t, "k", viejo) is None  # sin la anterior no se lee
    monkeypatch.setattr(settings, "DB_SECRETOS_KEY_ANTERIOR", clave)
    assert secretos.descifrar(t, "k", viejo) == "pw"


def test_sin_clave_no_esta_disponible(monkeypatch):
    monkeypatch.setattr(settings, "DB_SECRETOS_KEY", "")
    assert secretos.disponible() is False
    monkeypatch.setattr(settings, "DB_SECRETOS_KEY", "no-es-una-clave")
    assert secretos.disponible() is False


# ============================================================
# Anti-SSRF
# ============================================================
@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1", "localhost", "10.0.0.5", "192.168.1.10", "172.16.0.1", "169.254.169.254",
        "0.0.0.0", "100.64.0.1", "::1", "fe80::1", "::ffff:127.0.0.1", "[::1]",
        "", "mi host", "host/ruta", "user@host", "http://x.com",
    ],
)
async def test_hosts_internos_o_invalidos_se_rechazan(host):
    with pytest.raises(db_externa.ErrorDb) as e:
        await db_externa.resolver_host(host)
    assert e.value.codigo == "host_no_permitido"


async def test_ip_publica_pasa():
    assert await db_externa.resolver_host("8.8.8.8") == ["8.8.8.8"]


async def test_nombre_que_resuelve_a_ip_privada_se_rechaza(monkeypatch):
    import socket

    def falso(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)), (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", falso)
    # Basta UNA dirección interna entre las resueltas para rechazarlo.
    with pytest.raises(db_externa.ErrorDb) as e:
        await db_externa.resolver_host("trampa.ejemplo.com")
    assert e.value.codigo == "host_no_permitido"


# ============================================================
# Constructor de consultas
# ============================================================
def _armar(motor, params):
    return db_externa.construir_consulta(motor, PEDIDOS, params)


def test_consulta_basica_postgres():
    sql, args, cols, limite = _armar("postgresql", {"objeto": "pedidos", "filtros": {"cliente": "Ana", "id": "7"}, "orden": "total desc", "limite": 5})
    assert sql == (
        'SELECT "id", "cliente", "total", "creado", "activo", "estado" FROM "public"."pedidos" '
        'WHERE "cliente" = $1 AND "id" = $2 ORDER BY "total" DESC LIMIT 6'
    )
    assert args == ["Ana", 7]  # el entero se convierte: asyncpg no acepta '7' para integer
    assert limite == 5


def test_consulta_basica_mysql_usa_marcadores_y_backticks():
    sql, args, _, _ = _armar("mysql", {"objeto": "pedidos", "columnas": ["id"], "filtros": {"total": {"op": ">=", "valor": 10}}})
    assert sql == "SELECT `id` FROM `pedidos` WHERE `total` >= %s LIMIT 21"
    assert args == [10]


@pytest.mark.parametrize("motor", ["postgresql", "mysql"])
def test_el_modelo_no_puede_meter_sql(motor):
    # Columna con inyección: no existe en la lista blanca.
    for malo in ['id"; DROP TABLE pedidos;--', "id` FROM x;--", "1=1", "password", "id, password"]:
        with pytest.raises(db_externa.ConsultaInvalida):
            _armar(motor, {"objeto": "pedidos", "columnas": [malo]})
        with pytest.raises(db_externa.ConsultaInvalida):
            _armar(motor, {"objeto": "pedidos", "filtros": {malo: 1}})
        with pytest.raises(db_externa.ConsultaInvalida):
            _armar(motor, {"objeto": "pedidos", "orden": malo})
    # Operador inventado.
    with pytest.raises(db_externa.ConsultaInvalida):
        _armar(motor, {"objeto": "pedidos", "filtros": {"id": {"op": "= 1 OR 1=1 --", "valor": 1}}})


@pytest.mark.parametrize("motor", ["postgresql", "mysql"])
def test_valores_nunca_van_dentro_del_sql(motor):
    veneno = "'; DROP TABLE pedidos; --"
    sql, args, _, _ = _armar(motor, {"objeto": "pedidos", "filtros": {"cliente": veneno}})
    assert "DROP" not in sql
    assert veneno in args


def test_contiene_escapa_comodines_y_castea_no_texto():
    sql, args, _, _ = _armar("postgresql", {"objeto": "pedidos", "filtros": {"cliente": {"op": "contiene", "valor": "50%_x"}, "id": {"op": "empieza_con", "valor": "12"}}})
    assert 'ILIKE $1' in sql and 'CAST("id" AS text) ILIKE $2' in sql
    assert args == ["%50\\%\\_x%", "12%"]


def test_nulos_y_tipos_invalidos():
    sql, args, _, _ = _armar("postgresql", {"objeto": "pedidos", "filtros": {"estado": None}})
    assert '"estado" IS NULL' in sql and args == []
    with pytest.raises(db_externa.ConsultaInvalida):
        _armar("postgresql", {"objeto": "pedidos", "filtros": {"id": "abc"}})
    with pytest.raises(db_externa.ConsultaInvalida):
        _armar("postgresql", {"objeto": "pedidos", "filtros": {"creado": "ayer"}})
    with pytest.raises(db_externa.ConsultaInvalida):
        _armar("postgresql", {"objeto": "pedidos", "filtros": {"id": {"op": ">", "valor": None}}})
    # enum (USER-DEFINED): se compara como texto.
    sql, _, _, _ = _armar("postgresql", {"objeto": "pedidos", "filtros": {"estado": "pagado"}})
    assert 'CAST("estado" AS text) = $1' in sql


def test_limite_se_acota(monkeypatch):
    monkeypatch.setattr(settings, "DB_EXTERNA_MAX_FILAS", 50)
    assert _armar("mysql", {"objeto": "pedidos", "limite": 10_000})[3] == 50
    assert _armar("mysql", {"objeto": "pedidos", "limite": -4})[3] == 1
    assert _armar("mysql", {"objeto": "pedidos"})[3] == db_externa.LIMITE_POR_DEFECTO
    with pytest.raises(db_externa.ConsultaInvalida):
        _armar("mysql", {"objeto": "pedidos", "limite": "muchos"})


def test_demasiados_filtros():
    filtros = {f"c{i}": 1 for i in range(db_externa.MAX_FILTROS + 1)}
    with pytest.raises(db_externa.ConsultaInvalida):
        _armar("mysql", {"objeto": "pedidos", "filtros": filtros})


# ============================================================
# Selección de objetos (lo que guarda el negocio)
# ============================================================
REAL = [
    {
        "objeto": "pedidos", "esquema": "public", "nombre": "pedidos", "tipo": "vista",
        "columnas": [{"nombre": "id", "tipo": "integer", "sensible": False}, {"nombre": "password", "tipo": "text", "sensible": True}],
    }
]


def test_seleccion_valida_solo_con_lo_que_existe():
    ok = db_externa.armar_objetos_permitidos(REAL, [{"objeto": "pedidos", "columnas": ["id"], "descripcion": " pedidos "}])
    assert ok[0]["columnas"] == [{"nombre": "id", "tipo": "integer"}]
    assert ok[0]["descripcion"] == "pedidos"
    for malo in (
        [],
        [{"objeto": "otra", "columnas": ["id"]}],
        [{"objeto": "pedidos", "columnas": ["inventada"]}],
        [{"objeto": "pedidos", "columnas": []}],
        [{"objeto": "pedidos", "columnas": ["id"]}, {"objeto": "pedidos", "columnas": ["id"]}],
    ):
        with pytest.raises(db_externa.SeleccionInvalida):
            db_externa.armar_objetos_permitidos(REAL, malo)


def test_columnas_sensibles_se_marcan_por_nombre():
    filas = [(None, "t", "BASE TABLE", c, "text") for c in ("nombre", "password", "api_key", "token_acceso", "tarjeta")]
    objs = db_externa._agrupar_objetos(filas, "mysql")
    sens = {c["nombre"]: c["sensible"] for c in objs[0]["columnas"]}
    assert sens == {"nombre": False, "password": True, "api_key": True, "token_acceso": True, "tarjeta": True}


def test_parametros_schema_describe_los_objetos():
    ps = db_externa.parametros_schema([{**PEDIDOS}])
    assert ps["objetos_disponibles"]["pedidos"]["columnas"]["id"] == "integer"
    assert ps["objetos_disponibles"]["pedidos"]["que_es"] == "Pedidos del cliente"


# ============================================================
# Resultado hacia el modelo
# ============================================================
def test_resultado_se_recorta_sin_romper_el_json():
    filas = [{"id": i, "texto": "x" * 200} for i in range(100)]
    texto = db_externa._formatear("pedidos", filas, hay_mas=True)
    assert len(texto) <= db_externa.MAX_RESULTADO
    cuerpo = texto.split("filas):\n", 1)[1].split("\nSe muestran", 1)[0]
    assert 1 <= len(json.loads(cuerpo)) < 100
    assert "NO instrucciones" in texto


def test_resultado_vacio_y_celdas_largas():
    assert "No se encontraron" in db_externa._formatear("pedidos", [], False)
    assert len(db_externa._celda("y" * 1000)) == db_externa.MAX_CELDA + 1
    assert db_externa._celda(b"\x00\x01") == "<binario>"


# ============================================================
# Permisos de escritura
# ============================================================
class _CursorMysql:
    def __init__(self, grants):
        self._g = grants

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *_a):
        return None

    async def fetchall(self):
        return [(g,) for g in self._g]


class _ConnMysql:
    def __init__(self, grants):
        self._g = grants

    def cursor(self, *_a):
        return _CursorMysql(self._g)


@pytest.mark.parametrize(
    "grants, hay, verificable",
    [
        (["GRANT SELECT ON `ventas`.* TO `ro`@`%`"], False, True),
        (["GRANT USAGE ON *.* TO `ro`@`%`", "GRANT SELECT, SHOW VIEW ON `ventas`.* TO `ro`@`%`"], False, True),
        (["GRANT SELECT, INSERT, UPDATE ON `ventas`.* TO `rw`@`%`"], True, True),
        (["GRANT ALL PRIVILEGES ON *.* TO `root`@`%` WITH GRANT OPTION"], True, True),
        (["GRANT CREATE VIEW, SELECT ON `v`.* TO `x`@`%`"], True, True),
        (["GRANT `rol_lectura`@`%` TO `ro`@`%`"], True, False),  # por rol: no se puede comprobar
    ],
)
async def test_permisos_mysql(grants, hay, verificable):
    r = await db_externa._permisos_escritura(_ConnMysql(grants), "mysql")
    assert r["hay"] is hay
    assert r["verificable"] is verificable


# ============================================================
# consultar(): lo que lee el agente
# ============================================================
class _TxFalsa:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _ConnPg:
    def __init__(self, filas):
        self.filas = filas
        self.sql: list[str] = []
        self.cerrada = False

    def transaction(self, readonly=False):
        assert readonly is True  # SIEMPRE en transacción de solo lectura
        return _TxFalsa()

    async def execute(self, sql, *a):
        self.sql.append(sql)

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        return self.filas

    async def close(self):
        self.cerrada = True


CONFIG = {
    "motor": "postgresql", "host": "db.ejemplo.com", "puerto": 5432, "base": "ventas", "usuario": "ro",
    "ssl_modo": "require", "objetos": [PEDIDOS], "estado": "ok",
}


@pytest.fixture
def entorno(monkeypatch, clave):
    marcas = []

    async def leer(t, k):
        return "pw-secreta"

    async def marcar(t, k, codigo, ya):
        marcas.append(codigo)

    monkeypatch.setattr(secretos, "leer", leer)
    monkeypatch.setattr(db_externa, "_marcar_atencion", marcar)
    return marcas


async def test_consultar_devuelve_filas_en_solo_lectura(monkeypatch, entorno):
    conn = _ConnPg([{"id": 1, "cliente": "Ana", "total": 10}])

    async def conectar(d):
        assert d.password == "pw-secreta" and d.ssl_modo == "require"
        return conn

    monkeypatch.setattr(db_externa, "_conectar", conectar)
    out = await db_externa.consultar(uuid4(), "mi_db", CONFIG, {"objeto": "pedidos", "filtros": {"cliente": "Ana"}})
    assert "Ana" in out and "NO instrucciones" in out
    assert any("statement_timeout" in s for s in conn.sql)
    assert conn.cerrada


async def test_consultar_objeto_o_columna_inexistente_explica_el_error(entorno):
    out = await db_externa.consultar(uuid4(), "k", CONFIG, {"objeto": "usuarios"})
    assert "pedidos" in out and "no existe" in out
    out = await db_externa.consultar(uuid4(), "k", CONFIG, {"objeto": "pedidos", "columnas": ["password"]})
    assert "no está disponible" in out


async def test_fallo_de_conexion_no_filtra_detalles_y_marca_atencion(monkeypatch, entorno):
    async def conectar(d):
        raise db_externa.error("credenciales")

    monkeypatch.setattr(db_externa, "_conectar", conectar)
    out = await db_externa.consultar(uuid4(), "k", CONFIG, {"objeto": "pedidos"})
    assert out == db_externa.NO_DISPONIBLE
    assert "pw-secreta" not in out and "db.ejemplo.com" not in out
    assert entorno == ["credenciales"]


async def test_error_inesperado_de_la_base_no_filtra_su_mensaje(monkeypatch, entorno):
    class Boom(Exception):
        pass

    async def conectar(d):
        raise Boom("relation secreta_interna does not exist, password=abc")

    monkeypatch.setattr(db_externa, "_conectar", conectar)
    out = await db_externa.consultar(uuid4(), "k", CONFIG, {"objeto": "pedidos"})
    assert out == db_externa.NO_DISPONIBLE


# ============================================================
# Plan y API
# ============================================================
def test_familia_python_coincide_con_la_funcion_sql_para_db_externa():
    assert herramientas_plan.familia_de("db_externa", "mi_db") == "db_externa"


async def test_familia_sql_y_python_coinciden(db):
    for tipo, clave_ in [
        ("db_externa", "x"), ("sql_lectura", "x"), ("http_generico", "x"), ("google_sheets", "x"),
        ("google_docs", "x"), ("otro", "x"), ("sql_lectura", "crear_reserva"),
    ]:
        sql = await fetch_value("SELECT familia_de_herramienta($1, $2)", tipo, clave_)
        assert sql == herramientas_plan.familia_de(tipo, clave_)


async def test_db_externa_no_entra_en_el_catalogo_completo_sin_plan():
    from services.numeros_reglas import FAMILIAS_HERRAMIENTAS, FAMILIAS_CATALOGO

    assert "db_externa" not in FAMILIAS_HERRAMIENTAS
    assert "db_externa" in FAMILIAS_CATALOGO


FAKE_OBJETOS = [
    {
        "objeto": "pedidos", "esquema": "public", "nombre": "pedidos", "tipo": "vista",
        "columnas": [
            {"nombre": "id", "tipo": "integer", "sensible": False},
            {"nombre": "cliente", "tipo": "text", "sensible": False},
        ],
    }
]


@pytest.fixture
async def api_db(http_client, tenant_y_usuario, plan_enterprise, clave, monkeypatch):
    """Negocio con plan enterprise que SÍ incluye db_externa, y `probar` sustituido."""
    from services import limite_publico

    limite_publico.reiniciar()
    await execute("INSERT INTO plan_herramientas (plan_nombre, familia) VALUES ('enterprise', 'db_externa') ON CONFLICT DO NOTHING")
    estado = {"permisos": {"hay": False, "verificable": True, "detalle": []}, "llamadas": 0}

    async def probar(d):
        estado["llamadas"] += 1
        estado["destino"] = d
        return {"objetos": FAKE_OBJETOS, "permisos_escritura": estado["permisos"]}

    monkeypatch.setattr(db_externa, "probar", probar)
    yield {"c": http_client, "h": {"Authorization": f"Bearer {tenant_y_usuario['token']}"}, "t": tenant_y_usuario, "estado": estado}
    await execute("DELETE FROM plan_herramientas WHERE plan_nombre = 'enterprise' AND familia = 'db_externa'")
    limite_publico.reiniciar()


def _cuerpo(**extra):
    base = {
        "motor": "postgresql", "host": "db.ejemplo.com", "base": "ventas", "usuario": "solo_lectura",
        "password": "contraseña-super-secreta", "display_name": "Mis pedidos",
        "description": "Consulta pedidos del cliente",
        "objetos": [{"objeto": "pedidos", "columnas": ["id", "cliente"], "descripcion": "Pedidos"}],
        "acepta_responsabilidad": True,
    }
    base.update(extra)
    return base


async def test_sin_la_familia_en_el_plan_es_402(http_client, tenant_y_usuario, plan_enterprise, clave):
    h = {"Authorization": f"Bearer {tenant_y_usuario['token']}"}
    r = await http_client.post("/api/herramientas/db-externa/probar", headers=h, json={k: v for k, v in _cuerpo().items() if k in ("motor", "host", "base", "usuario", "password")})
    assert r.status_code == 402
    assert r.json()["detail"]["codigo"] == "herramienta_no_incluida_en_plan"
    info = (await http_client.get("/api/herramientas/db-externa/info", headers=h)).json()
    assert info["incluida_en_plan"] is False


async def test_sin_clave_de_cifrado_es_503(api_db, monkeypatch):
    monkeypatch.setattr(settings, "DB_SECRETOS_KEY", "")
    r = await api_db["c"].post("/api/herramientas/db-externa", headers=api_db["h"], json=_cuerpo())
    assert r.status_code == 503


async def test_conectar_guarda_cifrado_y_nunca_devuelve_la_contrasena(api_db):
    c, h, t = api_db["c"], api_db["h"], api_db["t"]
    r = await c.post("/api/herramientas/db-externa", headers=h, json=_cuerpo())
    assert r.status_code == 201, r.text
    assert "contraseña-super-secreta" not in r.text
    out = r.json()
    assert (out["tool_type"], out["motor"], out["estado"], out["n_objetos"]) == ("db_externa", "postgresql", "ok", 1)

    # La contraseña solo existe cifrada y fuera de tenant_tools.
    fila = await fetch_one("SELECT config, parametros_schema FROM tenant_tools WHERE tenant_id=$1 AND tool_key=$2", t["tenant_id"], out["tool_key"])
    assert "contraseña" not in json.dumps(fila["config"]) and "contraseña" not in json.dumps(fila["parametros_schema"])
    sec = await fetch_one("SELECT secreto FROM tool_secretos WHERE tenant_id=$1 AND tool_key=$2", t["tenant_id"], out["tool_key"])
    assert b"contrase" not in bytes(sec["secreto"])
    assert await secretos.leer(t["tenant_id"], out["tool_key"]) == "contraseña-super-secreta"

    # La lista tampoco la trae.
    lista = await c.get("/api/herramientas", headers=h)
    assert "contraseña-super-secreta" not in lista.text
    assert lista.json()[0]["host"] == "db.ejemplo.com"

    # Quedó constancia de la aceptación (sin permisos de escritura: solo la de responsabilidad).
    acept = await fetch_all("SELECT tipo, email, texto_version FROM db_externa_aceptaciones WHERE tenant_id=$1", t["tenant_id"])
    assert [a["tipo"] for a in acept] == ["responsabilidad"]
    assert acept[0]["email"] == t["email"]

    # Borrar la herramienta borra el secreto, pero la constancia se conserva.
    assert (await c.delete(f"/api/herramientas/{out['tool_key']}", headers=h)).status_code == 204
    assert await fetch_value("SELECT count(*) FROM tool_secretos WHERE tenant_id=$1", t["tenant_id"]) == 0
    assert await fetch_value("SELECT count(*) FROM db_externa_aceptaciones WHERE tenant_id=$1", t["tenant_id"]) == 1


async def test_exige_aceptar_la_responsabilidad(api_db):
    r = await api_db["c"].post("/api/herramientas/db-externa", headers=api_db["h"], json=_cuerpo(acepta_responsabilidad=False))
    assert r.status_code == 400
    assert r.json()["detail"]["codigo"] == "falta_aceptar_responsabilidad"
    assert await fetch_value("SELECT count(*) FROM tenant_tools WHERE tenant_id=$1 AND tool_type='db_externa'", api_db["t"]["tenant_id"]) == 0


async def test_usuario_con_permisos_de_escritura_requiere_aceptar_y_se_registra(api_db):
    api_db["estado"]["permisos"] = {"hay": True, "verificable": True, "detalle": ["Puede insertar, modificar o borrar datos en: public.pedidos."]}
    c, h, t = api_db["c"], api_db["h"], api_db["t"]
    r = await c.post("/api/herramientas/db-externa", headers=h, json=_cuerpo())
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["codigo"] == "permisos_escritura" and d["detalle"]
    assert await fetch_value("SELECT count(*) FROM tool_secretos WHERE tenant_id=$1", t["tenant_id"]) == 0

    r = await c.post("/api/herramientas/db-externa", headers=h, json=_cuerpo(acepta_riesgo_escritura=True))
    assert r.status_code == 201, r.text
    tipos = sorted(f["tipo"] for f in await fetch_all("SELECT tipo FROM db_externa_aceptaciones WHERE tenant_id=$1", t["tenant_id"]))
    assert tipos == ["permisos_escritura", "responsabilidad"]
    det = await fetch_value("SELECT detalle FROM db_externa_aceptaciones WHERE tenant_id=$1 AND tipo='permisos_escritura'", t["tenant_id"])
    assert "public.pedidos" in json.dumps(det)


async def test_seleccion_que_no_existe_en_la_base_se_rechaza(api_db):
    cuerpo = _cuerpo(objetos=[{"objeto": "pedidos", "columnas": ["id", "no_existe"]}])
    r = await api_db["c"].post("/api/herramientas/db-externa", headers=api_db["h"], json=cuerpo)
    assert r.status_code == 400 and r.json()["detail"]["codigo"] == "seleccion_invalida"


async def test_limite_de_tres_conexiones(api_db):
    c, h = api_db["c"], api_db["h"]
    for i in range(3):
        r = await c.post("/api/herramientas/db-externa", headers=h, json=_cuerpo(display_name=f"Base {i}"))
        assert r.status_code == 201, r.text
    r = await c.post("/api/herramientas/db-externa", headers=h, json=_cuerpo(display_name="Cuarta"))
    assert r.status_code == 409 and r.json()["detail"]["codigo"] == "limite_conexiones"


async def test_error_de_conexion_se_traduce_a_un_mensaje_claro(api_db, monkeypatch):
    async def probar(d):
        raise db_externa.error("credenciales")

    monkeypatch.setattr(db_externa, "probar", probar)
    r = await api_db["c"].post("/api/herramientas/db-externa", headers=api_db["h"], json=_cuerpo())
    assert r.status_code == 400
    assert r.json()["detail"]["codigo"] == "credenciales"
    assert "contraseña-super-secreta" not in r.text


async def test_probar_tiene_limite_por_negocio(api_db):
    cuerpo = {k: v for k, v in _cuerpo().items() if k in ("motor", "host", "base", "usuario", "password")}
    codigos = [(await api_db["c"].post("/api/herramientas/db-externa/probar", headers=api_db["h"], json=cuerpo)).status_code for _ in range(12)]
    assert codigos[:10] == [200] * 10
    assert 429 in codigos[10:]


async def test_editar_sin_password_conserva_la_guardada(api_db):
    c, h, t = api_db["c"], api_db["h"], api_db["t"]
    key = (await c.post("/api/herramientas/db-externa", headers=h, json=_cuerpo())).json()["tool_key"]
    cuerpo = {k: v for k, v in _cuerpo().items() if k not in ("password", "display_name", "description")}
    cuerpo["host"] = "otra.ejemplo.com"
    r = await c.put(f"/api/herramientas/db-externa/{key}", headers=h, json=cuerpo)
    assert r.status_code == 200, r.text
    assert api_db["estado"]["destino"].password == "contraseña-super-secreta"
    assert r.json()["host"] == "otra.ejemplo.com"
    assert await secretos.leer(t["tenant_id"], key) == "contraseña-super-secreta"
    # Con contraseña nueva, se reemplaza.
    r = await c.put(f"/api/herramientas/db-externa/{key}", headers=h, json={**cuerpo, "password": "nueva"})
    assert r.status_code == 200
    assert await secretos.leer(t["tenant_id"], key) == "nueva"


async def test_editar_una_herramienta_ajena_o_de_otro_tipo_es_404(api_db):
    cuerpo = {k: v for k, v in _cuerpo().items() if k not in ("display_name", "description")}
    r = await api_db["c"].put("/api/herramientas/db-externa/no_existe", headers=api_db["h"], json=cuerpo)
    assert r.status_code == 404


async def test_bajar_de_plan_retira_la_herramienta_de_los_numeros(api_db):
    """La familia sale del plan: el retiro automático existente la desactiva sin borrar la config."""
    from session import conexion

    t = api_db["t"]["tenant_id"]
    key = (await api_db["c"].post("/api/herramientas/db-externa", headers=api_db["h"], json=_cuerpo())).json()["tool_key"]
    async with conexion() as conn:
        assert await herramientas_plan.herramienta_activa_para(conn, t, None, key) is False  # nace desactivada en los canales
        await herramientas_plan.activar_herramientas_social(conn, t, [key], herramientas_plan.SISTEMA)
        assert await herramientas_plan.herramienta_activa_para(conn, t, None, key) is True
        await herramientas_plan.retirar_herramientas_por_plan(conn, t, ["http"])
        assert await herramientas_plan.herramienta_activa_para(conn, t, None, key) is False
    assert await fetch_value("SELECT count(*) FROM tenant_tools WHERE tenant_id=$1 AND tool_key=$2", t, key) == 1


async def test_probar_al_editar_usa_la_contrasena_guardada(api_db):
    c, h = api_db["c"], api_db["h"]
    key = (await c.post("/api/herramientas/db-externa", headers=h, json=_cuerpo())).json()["tool_key"]
    sin_password = {"motor": "postgresql", "host": "db.ejemplo.com", "base": "ventas", "usuario": "solo_lectura"}
    # Sin contraseña ni herramienta: 400 claro.
    r = await c.post("/api/herramientas/db-externa/probar", headers=h, json=sin_password)
    assert r.status_code == 400 and r.json()["detail"]["codigo"] == "falta_password"
    # Con la herramienta, el servidor usa la guardada (nunca viaja al navegador).
    r = await c.post("/api/herramientas/db-externa/probar", headers=h, json={**sin_password, "tool_key": key})
    assert r.status_code == 200, r.text
    assert api_db["estado"]["destino"].password == "contraseña-super-secreta"
    assert "contraseña-super-secreta" not in r.text


async def test_detalle_para_editar_no_trae_secretos(api_db):
    c, h = api_db["c"], api_db["h"]
    key = (await c.post("/api/herramientas/db-externa", headers=h, json=_cuerpo())).json()["tool_key"]
    r = await c.get(f"/api/herramientas/db-externa/{key}", headers=h)
    assert r.status_code == 200
    d = r.json()
    assert d["host"] == "db.ejemplo.com" and d["usuario"] == "solo_lectura" and d["estado"] == "ok"
    assert d["objetos"] == [{"objeto": "pedidos", "descripcion": "Pedidos", "columnas": ["id", "cliente"]}]
    assert "contraseña-super-secreta" not in r.text and "password" not in d
    assert (await c.get("/api/herramientas/db-externa/no_existe", headers=h)).status_code == 404


async def test_un_miembro_no_puede_conectar_bases(api_db, tenant_y_usuario):
    """Solo dueño/administrador (gerencia_actual): un 'member' puede ver la lista pero no conectar."""
    from security import crear_access_token

    await execute("UPDATE portal_users SET role = 'member' WHERE id = $1", tenant_y_usuario["usuario_id"])
    token = crear_access_token(tenant_y_usuario["usuario_id"], tenant_y_usuario["tenant_id"], "member")
    h = {"Authorization": f"Bearer {token}"}
    r = await api_db["c"].post("/api/herramientas/db-externa", headers=h, json=_cuerpo())
    assert r.status_code == 403
    r = await api_db["c"].post("/api/herramientas/db-externa/probar", headers=h, json={"motor": "mysql", "host": "a.b.c", "base": "x", "usuario": "u", "password": "p"})
    assert r.status_code == 403


# ============================================================
# Contra un PostgreSQL real (la base de la plataforma, saltando la validación de host
# porque es local): comprueba que el SQL de introspección, permisos y lectura corre de verdad.
# ============================================================
@pytest.fixture
async def pg_real(db):
    import asyncpg

    conn = await asyncpg.connect(settings.DATABASE_URL)
    yield conn
    await conn.close()


async def test_pg_introspeccion_y_permisos_con_sql_real(pg_real):
    objetos = await db_externa._introspeccionar(pg_real, "postgresql")
    por_nombre = {o["objeto"]: o for o in objetos}
    assert "tenants" in por_nombre and por_nombre["tenants"]["tipo"] == "tabla"
    assert {"id", "name"} <= {c["nombre"] for c in por_nombre["tenants"]["columnas"]}
    # El usuario local es de la plataforma (escribe en todo): debe detectarse.
    permisos = await db_externa._permisos_escritura(pg_real, "postgresql")
    assert permisos["hay"] is True and permisos["detalle"]


async def test_pg_consulta_generada_corre_en_solo_lectura_y_la_escritura_falla(pg_real):
    objetos = await db_externa._introspeccionar(pg_real, "postgresql")
    real = next(o for o in objetos if o["objeto"] == "tenants")
    elegido = db_externa.armar_objetos_permitidos(objetos, [{"objeto": "tenants", "columnas": ["id", "name"]}])[0]
    assert real["objeto"] == elegido["objeto"]

    sql, args, cols, limite = db_externa.construir_consulta(
        "postgresql", elegido, {"objeto": "tenants", "filtros": {"name": {"op": "contiene", "valor": "no-existe-%_zzz"}}, "orden": "name", "limite": 3}
    )
    assert await db_externa._ejecutar_sql(pg_real, "postgresql", sql, args) == []

    # Aunque el SQL fuera de escritura, la transacción READ ONLY lo rechaza.
    import asyncpg

    with pytest.raises(asyncpg.exceptions.ReadOnlySQLTransactionError):
        await db_externa._ejecutar_sql(pg_real, "postgresql", "INSERT INTO tenants (id, name) VALUES (gen_random_uuid(), 'x')", [])
    assert await pg_real.fetchval("SELECT count(*) FROM tenants WHERE name = 'x'") == 0


async def test_pg_filtros_tipados_con_sql_real(pg_real):
    """uuid, timestamp y texto: asyncpg es estricto con los tipos y el constructor debe convertirlos."""
    info = await pg_real.fetch(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_name='tenants' AND table_schema='public'"
    )
    cols = [{"nombre": f["column_name"], "tipo": f["data_type"]} for f in info]
    obj = {"objeto": "tenants", "esquema": "public", "nombre": "tenants", "tipo": "tabla", "descripcion": "", "columnas": cols}
    filtros = {"id": str(uuid4())}
    for c in cols:
        if c["tipo"].startswith("timestamp"):
            filtros[c["nombre"]] = {"op": ">=", "valor": "2020-01-01T00:00:00Z"}
            break
    sql, args, _, _ = db_externa.construir_consulta("postgresql", obj, {"objeto": "tenants", "filtros": filtros})
    assert await db_externa._ejecutar_sql(pg_real, "postgresql", sql, args) == []

