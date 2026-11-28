"""
Plantillas de WhatsApp de cada negocio: reglas puras, cliente de NeuroAPI,
API (roles, aislamiento entre negocios, confirmación del borrado) y estado por
evento de Meta. Sin BD ni red: la caché y `desde_tenant` se sustituyen por
dobles en memoria y el HTTP de NeuroAPI por `httpx.MockTransport`.
"""

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from deps import UsuarioActual, usuario_actual
from main import app
from services import neuroapi_plantillas as np
from services import plantillas_reglas as reglas
from services import plantillas_tenant as cache

RUTA = "/api/plantillas-whatsapp"
TENANT = uuid4()
OTRO = uuid4()


# ------------------------------------------------------------
# Reglas puras
# ------------------------------------------------------------
@pytest.mark.parametrize("nombre", ["promo_verano", "a1", "x" * 512])
def test_nombre_valido(nombre):
    assert reglas.validar_nombre(nombre) == nombre


@pytest.mark.parametrize("nombre", ["", "Promo", "con espacio", "tilde_á", "x" * 513, "a-b"])
def test_nombre_invalido(nombre):
    with pytest.raises(reglas.PlantillaInvalida):
        reglas.validar_nombre(nombre)


def test_variables_en_orden_y_sin_repetidos():
    assert reglas.variables_de("{{2}} y {{1}} y {{2}}") == [2, 1]
    assert reglas.variables_de("sin variables") == []


def test_cuerpo_con_variables_arma_ejemplos():
    comps = reglas.construir_componentes(
        cuerpo="Hola {{1}}, tu cita es el {{2}}.", ejemplos_cuerpo=["Ana", "lunes"], pie="Gracias"
    )
    assert comps[0] == {
        "type": "BODY",
        "text": "Hola {{1}}, tu cita es el {{2}}.",
        "example": {"body_text": [["Ana", "lunes"]]},
    }
    assert comps[1] == {"type": "FOOTER", "text": "Gracias"}


@pytest.mark.parametrize(
    "cuerpo, ejemplos",
    [
        ("Hola {{1}} y {{3}}.", ["a", "b"]),  # salto
        ("{{1}} te saluda", ["a"]),  # empieza con variable
        ("Te saluda {{1}}", ["a"]),  # termina con variable
        ("Hola {{1}}, ok", []),  # sin ejemplo
        ("Hola {{1}}, ok", ["  "]),  # ejemplo vacío
        ("", []),  # cuerpo vacío
        ("x" * 1025, []),  # largo
    ],
)
def test_cuerpo_invalido(cuerpo, ejemplos):
    with pytest.raises(reglas.PlantillaInvalida):
        reglas.construir_componentes(cuerpo=cuerpo, ejemplos_cuerpo=ejemplos)


def test_pie_sin_variables_y_boton_url_https():
    with pytest.raises(reglas.PlantillaInvalida):
        reglas.construir_componentes(cuerpo="Hola", ejemplos_cuerpo=[], pie="Pie {{1}}")
    with pytest.raises(reglas.PlantillaInvalida):
        reglas.construir_componentes(
            cuerpo="Hola", ejemplos_cuerpo=[], botones=[{"tipo": "URL", "texto": "Ver", "url": "http://x.com"}]
        )


def test_parametros_de_envio_exige_todos_los_valores():
    comps = reglas.construir_componentes(cuerpo="Hola {{1}}, ok", ejemplos_cuerpo=["Ana"])
    assert reglas.parametros_de_envio(comps, ["Luis"]) == [
        {"type": "body", "parameters": [{"type": "text", "text": "Luis"}]}
    ]
    with pytest.raises(reglas.PlantillaInvalida):
        reglas.parametros_de_envio(comps, [])


# ------------------------------------------------------------
# Cliente de NeuroAPI
# ------------------------------------------------------------
def _servicio(monkeypatch, handler) -> np.NeuroApiTemplateService:
    real = httpx.AsyncClient
    monkeypatch.setattr(
        np.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    return np.NeuroApiTemplateService(api_key="clave-secreta", base_url="https://api.test/api/v1")


async def test_listar_manda_filtros_y_clave(monkeypatch):
    visto: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        visto.update(url=str(req.url), clave=req.headers["x-api-key"])
        return httpx.Response(200, json={"success": True, "data": [{"name": "a"}, {"name": "b"}]})

    s = _servicio(monkeypatch, handler)
    r = await s.listar(categoria="UTILITY", estado="APPROVED", limite=50)
    assert [x["name"] for x in r] == ["a", "b"]
    assert visto["clave"] == "clave-secreta"
    assert visto["url"].startswith("https://api.test/api/v1/neuroapi/messaging/templates?")
    assert "category=UTILITY" in visto["url"] and "status=APPROVED" in visto["url"]


async def test_crear_envia_el_cuerpo_de_meta(monkeypatch):
    cuerpo: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        import json

        cuerpo.update(json.loads(req.content))
        return httpx.Response(200, json={"data": {"id": "123", "status": "PENDING"}})

    s = _servicio(monkeypatch, handler)
    comps = [{"type": "BODY", "text": "Hola"}]
    r = await s.crear(nombre="hola", idioma="es_MX", categoria="UTILITY", componentes=comps)
    assert r["id"] == "123"
    assert cuerpo == {"name": "hola", "language": "es_MX", "category": "UTILITY", "components": comps}


@pytest.mark.parametrize(
    "codigo, esperado",
    [(400, 422), (403, 502), (404, 404), (429, 429), (500, 502)],
)
async def test_errores_http_se_traducen(monkeypatch, codigo, esperado):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(codigo, json={"error": {"message": "variables mal formadas"}})

    s = _servicio(monkeypatch, handler)
    with pytest.raises(np.PlantillaApiError) as e:
        await s.eliminar("x")
    http = np.a_http(e.value)
    assert http.status_code == esperado
    if codigo == 400:
        assert http.detail == "variables mal formadas"
    if codigo == 403:
        assert http.detail["codigo"] == "scope_whatsapp_cloud_api_faltante"


async def test_fallo_de_red_es_502(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("caído")

    s = _servicio(monkeypatch, handler)
    with pytest.raises(np.PlantillaApiError) as e:
        await s.obtener("x")
    assert np.a_http(e.value).status_code == 502


# ------------------------------------------------------------
# Factory de proveedor (NeuroAPI vs Meta directo, según bsp_provider)
# ------------------------------------------------------------
async def test_servicio_elige_segun_el_proveedor_de_la_cuenta(monkeypatch):
    import routers.plantillas_whatsapp_tenant as router_mod
    from services import plantillas_cuentas as pc

    class _ServicioNeuroApi:
        @classmethod
        async def desde_tenant(cls, tenant_id):
            return "neuroapi-instancia"

    monkeypatch.setattr(router_mod, "NeuroApiTemplateService", _ServicioNeuroApi)
    unica = pc.Cuenta(clave="unica", waba_cache=None)
    neuro = pc.Cuentas(proveedor="neuroapi", cuentas=[unica])
    assert await router_mod._servicio(TENANT, neuro, unica) == "neuroapi-instancia"

    # Meta directo: Graph contra la WABA de ESA cuenta, no la del primer número.
    w2 = pc.Cuenta(clave="W2", waba_cache="W2")
    meta = pc.Cuentas(proveedor="meta", cuentas=[pc.Cuenta(clave="W1", waba_cache="W1"), w2], token="tok")
    servicio = await router_mod._servicio(TENANT, meta, w2)
    assert servicio.waba_id == "W2"


# ------------------------------------------------------------
# Eventos de Meta
# ------------------------------------------------------------
def _evento(nombre="promo", event="APPROVED", **v):
    return {
        "entry": [
            {
                "id": "WABA1",
                "changes": [
                    {
                        "field": "message_template_status_update",
                        "value": {
                            "event": event,
                            "message_template_name": nombre,
                            "message_template_language": "es_MX",
                            "message_template_id": "555",
                            **v,
                        },
                    }
                ],
            }
        ]
    }


def test_cambios_de_estado_traduce_y_descarta_lo_desconocido():
    c = cache.cambios_de_estado(_evento(event="REJECTED", reason="INVALID_FORMAT"))
    assert c == [
        {"nombre": "promo", "idioma": "es_MX", "meta_id": "555", "waba_id": "WABA1",
         "estado": "REJECTED", "motivo": "INVALID_FORMAT"}
    ]
    assert cache.cambios_de_estado(_evento(event="FLAGGED")) == []
    assert cache.cambios_de_estado({"entry": "x"}) == []


def test_elegir_candidata_no_adivina_entre_homonimos():
    a = {"tenant_id": 1, "meta_template_id": None, "waba_id": None}
    b = {"tenant_id": 2, "meta_template_id": None, "waba_id": None}
    assert cache.elegir_candidata([a], None, None) is a
    assert cache.elegir_candidata([a, b], None, None) is None
    assert cache.elegir_candidata([a, {**b, "meta_template_id": "555"}], "555", None)["tenant_id"] == 2
    assert cache.elegir_candidata([a, {**b, "waba_id": "W"}], None, "W")["tenant_id"] == 2


async def test_aplicar_evento_actualiza_solo_a_su_dueno(monkeypatch):
    filas = [{"tenant_id": TENANT, "nombre": "promo", "idioma": "es_MX", "estado": "PENDING",
              "meta_template_id": None, "waba_id": None}]
    updates: list[tuple] = []

    async def fetch_all(sql, *a):
        return filas

    async def execute(sql, *a):
        updates.append(a)

    monkeypatch.setattr(cache, "fetch_all", fetch_all)
    monkeypatch.setattr(cache, "execute", execute)

    r = await cache.aplicar_evento_meta(_evento(event="APPROVED"))
    assert r == [(TENANT, "promo", "APPROVED")]
    assert updates[0][:5] == (TENANT, "promo", "es_MX", "APPROVED", None)

    # Mismo estado: idempotente, no escribe.
    filas[0]["estado"] = "APPROVED"
    updates.clear()
    assert await cache.aplicar_evento_meta(_evento(event="APPROVED")) == []
    assert updates == []

    # Plantilla que ningún negocio tiene (p. ej. las globales): se ignora.
    filas.clear()
    assert await cache.aplicar_evento_meta(_evento(nombre="recordatorio_cita")) == []


# ------------------------------------------------------------
# API
# ------------------------------------------------------------
class _Cache:
    def __init__(self) -> None:
        self.filas: dict[tuple[UUID, str], dict[str, Any]] = {}

    def instalar(self, mp: pytest.MonkeyPatch) -> None:
        c = self

        # Clave (tenant, nombre) para la cuenta única —como antes de sql/81— y
        # (tenant, nombre, waba) para una WABA concreta de Meta directo.
        def clave(tenant_id, nombre, waba):
            return (tenant_id, nombre) if waba is None else (tenant_id, nombre, waba)

        def de_cuenta(k, waba):
            return waba is cache.TODAS or (len(k) == 2 and waba is None) or (len(k) == 3 and k[2] == waba)

        async def obtener(tenant_id, nombre, *, waba_id=cache.TODAS):
            for k, f in c.filas.items():
                if k[0] == tenant_id and k[1] == nombre and de_cuenta(k, waba_id):
                    return f
            return None

        async def listar(tenant_id, *, categoria=None, estado=None, waba_id=cache.TODAS):
            return [
                f for k, f in c.filas.items()
                if k[0] == tenant_id and de_cuenta(k, waba_id)
                and (categoria is None or f["categoria"] == categoria)
                and (estado is None or f["estado"] == estado)
            ]

        async def guardar(tenant_id, datos, *, creada_por=None, sincronizada):
            from datetime import datetime, timezone

            c.filas[clave(tenant_id, datos["nombre"], datos.get("waba_id"))] = {
                **datos, "motivo_rechazo": datos.get("motivo_rechazo"),
                "actualizada_en": datetime.now(timezone.utc), "sincronizada_en": None,
            }

        async def borrar_local(tenant_id, nombre, *, waba_id=cache.TODAS):
            for k in [k for k in c.filas if k[0] == tenant_id and k[1] == nombre and de_cuenta(k, waba_id)]:
                c.filas.pop(k)

        async def sin_otra(tenant_id, nombre):
            return not any(k[0] == tenant_id and k[1] == nombre for k in c.filas)

        mp.setattr(cache, "_sin_otra_con_ese_nombre", sin_otra)

        async def apagar_funcion(tenant_id, nombre):
            return None  # T6: DELETE llama a reaccion_estado; aquí ninguna función de aviso está activa

        c.usos: dict[str, list[dict[str, Any]]] = {}

        async def usos_en_avisos(tenant_id):
            return c.usos if tenant_id == TENANT else {}

        mp.setattr(cache, "usos_en_avisos", usos_en_avisos)
        mp.setattr(cache, "_apagar_funcion_bd", apagar_funcion)
        mp.setattr(cache, "obtener", obtener)
        mp.setattr(cache, "listar", listar)
        mp.setattr(cache, "guardar", guardar)
        mp.setattr(cache, "borrar_local", borrar_local)


class _Servicio:
    llamadas: list[tuple] = []
    fallo: np.PlantillaApiError | None = None

    @classmethod
    async def desde_tenant(cls, tenant_id):
        return cls()

    async def crear(self, **kw):
        if self.fallo:
            raise self.fallo
        self.llamadas.append(("crear", kw["nombre"]))
        return {"id": "9", "status": "PENDING", "category": kw["categoria"]}

    async def eliminar(self, nombre):
        self.llamadas.append(("eliminar", nombre))


def _usuario(role: str, tenant: UUID = TENANT) -> UsuarioActual:
    return UsuarioActual(id=uuid4(), tenant_id=tenant, email=f"{role}@n.com", role=role)


@pytest.fixture
def api(monkeypatch):
    c = _Cache()
    c.instalar(monkeypatch)
    _Servicio.llamadas = []
    _Servicio.fallo = None
    monkeypatch.setattr("routers.plantillas_whatsapp_tenant.NeuroApiTemplateService", _Servicio)

    # Sin BD: el negocio tiene UNA cuenta de NeuroAPI (el caso de casi todos).
    # Las pruebas de varias cuentas cambian `c.cuentas`.
    from services import plantillas_cuentas as pc

    c.cuentas = pc.Cuentas(proveedor="neuroapi", cuentas=[pc.Cuenta(clave="unica", waba_cache=None)], clave_avisos="unica")

    async def resolver(tenant_id):
        return c.cuentas

    monkeypatch.setattr("routers.plantillas_whatsapp_tenant.cuentas_svc.resolver", resolver)
    actual = {"u": _usuario("owner")}

    async def _u() -> UsuarioActual:
        return actual["u"]

    app.dependency_overrides[usuario_actual] = _u
    c.actual = actual
    yield c
    app.dependency_overrides.pop(usuario_actual, None)


async def _llamar(metodo: str, ruta: str, **kw: Any):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.request(metodo, ruta, **kw)


NUEVA = {"nombre": "promo", "categoria": "UTILITY", "cuerpo": "Hola {{1}}, ok", "ejemplos_cuerpo": ["Ana"]}


@pytest.mark.parametrize("rol", ["vendedor", "proveedor"])
async def test_vendedor_y_proveedor_no_entran(api, rol):
    api.actual["u"] = _usuario(rol)
    assert (await _llamar("GET", RUTA)).status_code == 403
    assert (await _llamar("POST", RUTA, json=NUEVA)).status_code == 403


async def test_member_lee_pero_no_crea_ni_borra(api):
    api.actual["u"] = _usuario("member")
    assert (await _llamar("GET", RUTA)).status_code == 200
    assert (await _llamar("POST", RUTA, json=NUEVA)).status_code == 403
    assert (await _llamar("DELETE", f"{RUTA}/promo?confirmar=true")).status_code == 403


async def test_crear_guarda_pendiente_y_lista(api):
    r = await _llamar("POST", RUTA, json=NUEVA)
    assert r.status_code == 201, r.text
    assert r.json()["estado"] == "PENDING" and r.json()["variables_cuerpo"] == 1
    assert _Servicio.llamadas == [("crear", "promo")]
    lista = (await _llamar("GET", f"{RUTA}?estado=PENDING")).json()
    assert [p["nombre"] for p in lista] == ["promo"]
    assert (await _llamar("GET", f"{RUTA}?estado=APPROVED")).json() == []


async def test_crear_valida_antes_de_llamar_a_neuroapi(api):
    r = await _llamar("POST", RUTA, json={**NUEVA, "nombre": "Mal Nombre"})
    assert r.status_code == 422 and _Servicio.llamadas == []
    r = await _llamar("POST", RUTA, json={**NUEVA, "cuerpo": "Hola {{2}}, ok"})
    assert r.status_code == 422 and _Servicio.llamadas == []


async def test_crear_duplicada_es_409(api):
    assert (await _llamar("POST", RUTA, json=NUEVA)).status_code == 201
    assert (await _llamar("POST", RUTA, json=NUEVA)).status_code == 409


async def test_rechazo_de_meta_llega_como_422(api):
    _Servicio.fallo = np.PlantillaApiError("x", status_code=400, detalle_meta="variable mal formada")
    r = await _llamar("POST", RUTA, json=NUEVA)
    assert r.status_code == 422 and r.json()["detail"] == "variable mal formada"
    assert api.filas == {}


async def test_plantilla_de_otro_negocio_es_404(api):
    await _llamar("POST", RUTA, json=NUEVA)
    api.actual["u"] = _usuario("owner", OTRO)
    assert (await _llamar("GET", f"{RUTA}/promo")).status_code == 404
    assert (await _llamar("DELETE", f"{RUTA}/promo?confirmar=true")).status_code == 404
    assert (await _llamar("GET", RUTA)).json() == []


async def test_borrar_exige_confirmacion(api):
    await _llamar("POST", RUTA, json=NUEVA)
    r = await _llamar("DELETE", f"{RUTA}/promo")
    assert r.status_code == 400 and r.json()["detail"]["codigo"] == "confirmacion_requerida"
    assert ("eliminar", "promo") not in _Servicio.llamadas and (TENANT, "promo") in api.filas
    assert (await _llamar("DELETE", f"{RUTA}/promo?confirmar=true")).status_code == 204
    assert ("eliminar", "promo") in _Servicio.llamadas and (TENANT, "promo") not in api.filas


async def test_prueba_solo_de_plantillas_aprobadas(api):
    await _llamar("POST", RUTA, json=NUEVA)
    r = await _llamar("POST", f"{RUTA}/promo/prueba", json={"telefono": "593999999999", "valores_cuerpo": ["Ana"]})
    assert r.status_code == 409


# ------------------------------------------------------------
# Uso en avisos de cita (en_uso_por) y borrado protegido
# ------------------------------------------------------------
def test_usos_de_config_cuenta_activos_y_apagados():
    fila = {
        "nombre_plantilla_recordatorio": "recordatorio_cita", "recordatorio_activo": True,
        "nombre_plantilla_cancelacion": "recordatorio_cita", "cancelacion_activo": False,
    }
    assert cache.usos_de_config(fila) == {
        "recordatorio_cita": [{"tipo": "recordatorio", "activo": True}, {"tipo": "cancelacion", "activo": False}]
    }
    assert cache.usos_de_config(None) == {}


async def test_lista_y_detalle_dicen_que_aviso_la_usa(api):
    await _llamar("POST", RUTA, json=NUEVA)
    api.usos = {"promo": [{"tipo": "recordatorio", "activo": False}]}
    lista = (await _llamar("GET", RUTA)).json()
    assert lista[0]["en_uso_por"] == [{"tipo": "recordatorio", "activo": False}]
    assert (await _llamar("GET", f"{RUTA}/promo")).json()["en_uso_por"] == [{"tipo": "recordatorio", "activo": False}]


async def test_borrar_la_de_un_aviso_encendido_pide_forzar(api):
    await _llamar("POST", RUTA, json=NUEVA)
    api.usos = {"promo": [{"tipo": "cancelacion", "activo": True}]}
    r = await _llamar("DELETE", f"{RUTA}/promo?confirmar=true")
    assert r.status_code == 409 and r.json()["detail"]["codigo"] == "plantilla_en_uso"
    assert r.json()["detail"]["en_uso_por"] == [{"tipo": "cancelacion", "activo": True}]
    assert ("eliminar", "promo") not in _Servicio.llamadas and (TENANT, "promo") in api.filas
    assert (await _llamar("DELETE", f"{RUTA}/promo?confirmar=true&forzar=true")).status_code == 204
    assert (TENANT, "promo") not in api.filas


async def test_borrar_la_de_un_aviso_apagado_no_pide_forzar(api):
    await _llamar("POST", RUTA, json=NUEVA)
    api.usos = {"promo": [{"tipo": "recordatorio", "activo": False}]}
    assert (await _llamar("DELETE", f"{RUTA}/promo?confirmar=true")).status_code == 204


# ------------------------------------------------------------
# Texto que recibe el cliente (lo que queda en el hilo de la Bandeja)
# ------------------------------------------------------------
def test_texto_enviado_pone_los_valores():
    comps = reglas.construir_componentes(
        cuerpo="Hola {{1}}, tu cita es el {{2}}.", ejemplos_cuerpo=["Ana", "lunes"],
        encabezado="Aviso para {{1}}", ejemplo_encabezado="Ana", pie="Gracias",
        botones=[{"tipo": "QUICK_REPLY", "texto": "Ok"}],
    )
    assert reglas.texto_enviado(comps, ["Luis", "martes"], "Luis") == (
        "Aviso para Luis\n\nHola Luis, tu cita es el martes.\n\nGracias"
    )


# ------------------------------------------------------------
# Varias cuentas de WhatsApp Business (sql/81)
# ------------------------------------------------------------
from services import plantillas_cuentas as pc  # noqa: E402


def _n(pnid, waba=None, principal=False, tel=None):
    return pc.Numero(id=uuid4(), phone_number_id=pnid, telefono_visible=tel, es_principal=principal, waba_id=waba)


def test_meta_agrupa_numeros_por_waba_y_marca_la_de_avisos():
    cuentas, bloqueo, avisos = pc.agrupar("meta", [_n("1", "W1"), _n("2", "W2", principal=True), _n("3", "W1"), _n("4")])
    assert [(c.clave, [n.phone_number_id for n in c.numeros]) for c in cuentas] == [("W1", ["1", "3"]), ("W2", ["2"])]
    assert bloqueo is None and avisos == "W2"  # el número sin WABA resuelta queda fuera


def test_neuroapi_es_una_cuenta_y_se_bloquea_con_wabas_distintas():
    cuentas, bloqueo, avisos = pc.agrupar("neuroapi", [_n("1", "W1"), _n("2", None)])
    assert len(cuentas) == 1 and cuentas[0].waba_cache is None and bloqueo is None and avisos == "unica"
    _, bloqueo, _ = pc.agrupar("neuroapi", [_n("1", "W1"), _n("2", "W2")])
    assert bloqueo == pc.BLOQUEO_NEUROAPI


class _ServicioMeta:
    llamadas: list[tuple] = []

    def __init__(self, token, waba_id):
        self.waba_id = waba_id

    async def crear(self, **kw):
        self.llamadas.append(("crear", self.waba_id, kw["nombre"]))
        return {"id": "7", "status": "PENDING", "category": kw["categoria"]}

    async def eliminar(self, nombre):
        self.llamadas.append(("eliminar", self.waba_id, nombre))


@pytest.fixture
def dos_cuentas(api, monkeypatch):
    _ServicioMeta.llamadas = []
    monkeypatch.setattr("routers.plantillas_whatsapp_tenant.MetaTemplateService", _ServicioMeta)
    api.cuentas = pc.Cuentas(
        proveedor="meta", token="tok", clave_avisos="W1",
        cuentas=[pc.Cuenta("W1", "W1", [_n("1", "W1", True, "+52 55")]), pc.Cuenta("W2", "W2", [_n("2", "W2", tel="+52 81")])],
    )
    return api


async def test_mis_cuentas_lista_cada_waba_con_sus_numeros(dos_cuentas):
    r = (await _llamar("GET", f"{RUTA}/mis-cuentas")).json()
    assert r["proveedor"] == "meta" and r["bloqueo"] is None
    assert [(c["clave"], c["de_avisos"], [n["telefono_visible"] for n in c["numeros"]]) for c in r["cuentas"]] == [
        ("W1", True, ["+52 55"]), ("W2", False, ["+52 81"]),
    ]


async def test_con_varias_cuentas_escribir_pide_la_cuenta(dos_cuentas):
    r = await _llamar("POST", RUTA, json=NUEVA)
    assert r.status_code == 422 and r.json()["detail"]["codigo"] == "cuenta_requerida"
    assert (await _llamar("POST", f"{RUTA}?cuenta=OTRA", json=NUEVA)).status_code == 404
    assert _ServicioMeta.llamadas == []


async def test_la_misma_plantilla_vive_aparte_en_cada_cuenta(dos_cuentas):
    assert (await _llamar("POST", f"{RUTA}?cuenta=W2", json=NUEVA)).status_code == 201
    assert (await _llamar("POST", f"{RUTA}?cuenta=W1", json=NUEVA)).status_code == 201  # otra WABA: no es duplicada
    assert _ServicioMeta.llamadas == [("crear", "W2", "promo"), ("crear", "W1", "promo")]
    w2 = (await _llamar("GET", f"{RUTA}?cuenta=W2")).json()
    assert [(p["nombre"], p["cuenta"]) for p in w2] == [("promo", "W2")]
    # Borrar en W2 no toca la de W1.
    assert (await _llamar("DELETE", f"{RUTA}/promo?confirmar=true&cuenta=W2")).status_code == 204
    assert _ServicioMeta.llamadas[-1] == ("eliminar", "W2", "promo")
    assert [p["cuenta"] for p in (await _llamar("GET", RUTA)).json()] == ["W1"]


async def test_en_uso_por_solo_en_la_cuenta_de_los_avisos(dos_cuentas):
    await _llamar("POST", f"{RUTA}?cuenta=W1", json=NUEVA)
    await _llamar("POST", f"{RUTA}?cuenta=W2", json=NUEVA)
    dos_cuentas.usos = {"promo": [{"tipo": "recordatorio", "activo": True}]}
    por_cuenta = {p["cuenta"]: p["en_uso_por"] for p in (await _llamar("GET", RUTA)).json()}
    assert por_cuenta == {"W1": [{"tipo": "recordatorio", "activo": True}], "W2": []}
    # En W2 no la usa ningún aviso: se borra sin forzar. En W1 sí lo pide.
    assert (await _llamar("DELETE", f"{RUTA}/promo?confirmar=true&cuenta=W2")).status_code == 204
    assert (await _llamar("DELETE", f"{RUTA}/promo?confirmar=true&cuenta=W1")).status_code == 409


async def test_neuroapi_con_wabas_distintas_se_mira_pero_no_se_escribe(api):
    api.cuentas = pc.Cuentas(proveedor="neuroapi", cuentas=[pc.Cuenta("unica", None)], bloqueo=pc.BLOQUEO_NEUROAPI)
    assert (await _llamar("GET", RUTA)).status_code == 200
    r = await _llamar("POST", RUTA, json=NUEVA)
    assert r.status_code == 409 and r.json()["detail"]["codigo"] == pc.BLOQUEO_NEUROAPI
    assert _Servicio.llamadas == []


async def test_sin_whatsapp_escribir_es_502_como_antes(api):
    api.cuentas = pc.Cuentas(proveedor=None, cuentas=[])
    assert (await _llamar("POST", RUTA, json=NUEVA)).status_code == 502
    assert (await _llamar("GET", RUTA)).status_code == 200


async def test_resolver_pide_la_waba_a_graph_una_vez_y_la_guarda(monkeypatch):
    guardadas, pedidas = [], []
    filas = [
        {"id": uuid4(), "phone_number_id": "PN1", "telefono_visible": "+52 55", "es_principal": True, "waba_id": "W1"},
        {"id": uuid4(), "phone_number_id": "PN2", "telefono_visible": "+52 81", "es_principal": False, "waba_id": None},
    ]

    async def fetch_one(sql, *a):
        return {"bsp_provider": "meta", "access_token": "tok", "phone_number_id": "PN1"}

    async def fetch_all(sql, *a):
        return filas

    async def obtener_waba_id(pnid, token):
        pedidas.append(pnid)
        return "W2"

    async def execute(sql, *a):
        guardadas.append(a[1:])

    monkeypatch.setattr(pc, "fetch_one", fetch_one)
    monkeypatch.setattr(pc, "fetch_all", fetch_all)
    monkeypatch.setattr(pc, "execute", execute)
    monkeypatch.setattr(pc.meta, "obtener_waba_id", obtener_waba_id)
    cuentas = await pc.resolver(TENANT)
    assert pedidas == ["PN2"] and guardadas == [("PN2", "W2")]  # PN1 ya la tenía
    assert [c.clave for c in cuentas.cuentas] == ["W1", "W2"] and cuentas.clave_avisos == "W1"
