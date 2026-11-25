"""
Corte por tope de llamadas de IA en `ingreso` y `loop_agente` (spec 001, T6).

Sin base ni red: tenant, usuario, conversación y escrituras se sustituyen por
dobles. Lo que se afirma es la decisión: agotado -> no corre el agente, el
mensaje se guarda, el aviso sale una vez por conversación y ciclo.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services import limite_ia, numeros_whatsapp, uso_ia
from services.agente import ingreso, loop_agente
from services.agente import prompt as prompt_mod
from services.agente.modelos import ContextoMensaje, MensajeNormalizado, PeticionVerificada
from services.uso_ia import UsoIA

TENANT = uuid4()
USUARIO = uuid4()
CONVERSACION = uuid4()
MENSAJE = uuid4()
FIN = datetime(2030, 2, 1, tzinfo=timezone.utc)


def _uso(usadas: int, limite: int | None, fin: datetime = FIN) -> UsoIA:
    if limite is None:
        return UsoIA(usadas=0, limite=None, ciclo_inicio=None, ciclo_fin=None)
    return UsoIA(usadas=usadas, limite=limite, ciclo_inicio=fin - timedelta(days=30), ciclo_fin=fin)


class Estado:
    def __init__(self):
        self.uso = _uso(100, 100)
        self.metadata: dict = {}
        self.status = "active"
        self.escrituras: list[tuple[str, tuple]] = []
        self.bloqueos: list[str] = []
        self.avisos_owner = 0
        self.duplicado = False
        self.uso_falla = False


@pytest.fixture
def e(monkeypatch):
    est = Estado()

    async def _resolver_tenant(m, p):
        return TENANT

    async def _upsert(t, m):
        return USUARIO

    async def _conversacion(t, u, c, a=None):
        return {"id": CONVERSACION, "status": est.status, "metadata": dict(est.metadata)}

    async def _guardar(t, c, m):
        if est.duplicado:
            return None
        return {"id": MENSAJE, "created_at": datetime(2026, 10, 16, 12, 0)}

    async def execute(query, *args):
        est.escrituras.append((query, args))
        if "limite_aviso_ciclo" in query:
            est.metadata["limite_aviso_ciclo"] = args[1]
        return "OK"

    async def _resolver_entrada(pnid, *, ahora=None):
        return numeros_whatsapp.ResultadoEntrada(numero={"id": uuid4()}, motivo=None)

    async def cfg(_):
        return {"agent_name": "A", "system_prompt": "P", "model": "claude-haiku-4-5-20251001",
                "temperature": None, "history_window": 30}

    async def _rafaga(ctx):
        return True

    async def _bloqueo(tenant_id, mensaje, motivo="plan"):
        est.bloqueos.append(motivo)

    async def uso_ciclo(tenant_id):
        assert tenant_id == TENANT
        if est.uso_falla:
            raise RuntimeError("BD caída")
        return est.uso

    async def avisar(tenant_id, uso):
        est.avisos_owner += 1
        return True

    async def sin_tramite(*a, **k):
        return None

    async def activo(*a, **k):
        return SimpleNamespace(agente_ia_activo=True)

    async def sin_nombre(*a, **k):
        return None

    async def en_vivo(*a, **k):
        return None

    import routers.eventos as eventos

    monkeypatch.setattr(eventos, "on_mensaje_entrante", activo)
    monkeypatch.setattr(ingreso, "_resolver_tenant", _resolver_tenant)
    monkeypatch.setattr(ingreso, "_upsert_usuario", _upsert)
    monkeypatch.setattr(ingreso, "_conversacion", _conversacion)
    monkeypatch.setattr(ingreso, "_guardar_mensaje", _guardar)
    monkeypatch.setattr(ingreso, "_completar_nombre", sin_nombre)
    monkeypatch.setattr(ingreso, "_en_vivo", en_vivo)
    monkeypatch.setattr(ingreso, "_tramitar_baja_o_alta", sin_tramite)
    monkeypatch.setattr(ingreso, "_registrar_bloqueo", _bloqueo)
    monkeypatch.setattr(ingreso, "execute", execute)
    monkeypatch.setattr(ingreso, "_esperar_rafaga", _rafaga)

    async def _sigue_ia(conversation_id):
        return True

    monkeypatch.setattr(ingreso, "sigue_en_manos_de_la_ia", _sigue_ia)
    monkeypatch.setattr(ingreso.medios, "lanzar", lambda *a, **k: None)
    monkeypatch.setattr(numeros_whatsapp, "resolver_entrada", _resolver_entrada)
    monkeypatch.setattr(prompt_mod, "config_numero", cfg)
    monkeypatch.setattr(prompt_mod, "config_social", cfg)
    monkeypatch.setattr(uso_ia, "uso_ciclo", uso_ciclo)
    monkeypatch.setattr(limite_ia, "avisar_owner_una_vez", avisar)
    monkeypatch.setattr(
        ingreso.normalizar_mod, "normalizar",
        lambda cuerpo, tenant_id=None: MensajeNormalizado(
            procesar=True, canal="whatsapp", usuario_id="5215599999999", nombre="Ana",
            mensaje_id="wamid.X", contenido="hola", phone_number_id="PNID",
        ),
    )
    return est


def _peticion() -> PeticionVerificada:
    return PeticionVerificada(cuerpo={}, origen="meta")


def _insert_aviso(est: Estado) -> list:
    return [q for q, a in est.escrituras if "INSERT INTO messages" in q]


# ---------------- ingreso ----------------
async def test_agotado_no_pasa_al_agente_guarda_y_avisa(e):
    r = await ingreso.procesar(_peticion())

    assert r.contexto is None and not r.sigue_al_agente
    assert r.respuesta_inmediata == ingreso.AVISO_LIMITE_IA
    assert r.destino is not None and r.destino.conversation_id == CONVERSACION
    assert e.bloqueos == ["limite_llamadas_ia"]
    assert e.avisos_owner == 1
    assert len(_insert_aviso(e)) == 1
    assert e.metadata["limite_aviso_ciclo"] == FIN.isoformat()


async def test_segundo_mensaje_mismo_ciclo_es_silencioso(e):
    await ingreso.procesar(_peticion())
    r2 = await ingreso.procesar(_peticion())

    assert r2.respuesta_inmediata is None and r2.contexto is None
    assert "silencio" in (r2.motivo or "")
    assert len(_insert_aviso(e)) == 1  # solo el aviso de la primera vez
    assert e.bloqueos == ["limite_llamadas_ia", "limite_llamadas_ia"]  # ambos quedan registrados


async def test_conversacion_ya_abierta_tambien_se_corta(e):
    e.metadata = {"otra_clave": 1}  # conversación viva con historial previo
    r = await ingreso.procesar(_peticion())
    assert r.contexto is None
    assert r.respuesta_inmediata == ingreso.AVISO_LIMITE_IA


async def test_ciclo_nuevo_agotado_vuelve_a_avisar(e):
    await ingreso.procesar(_peticion())
    e.uso = _uso(100, 100, FIN + timedelta(days=30))
    r = await ingreso.procesar(_peticion())
    assert r.respuesta_inmediata == ingreso.AVISO_LIMITE_IA
    assert len(_insert_aviso(e)) == 2


async def test_ciclo_nuevo_con_cupo_el_agente_retoma(e):
    await ingreso.procesar(_peticion())
    e.uso = _uso(0, 100, FIN + timedelta(days=30))  # el ciclo reinició
    r = await ingreso.procesar(_peticion())
    assert r.sigue_al_agente
    assert r.respuesta_inmediata is None


async def test_bajo_el_tope_flujo_intacto(e):
    e.uso = _uso(99, 100)
    r = await ingreso.procesar(_peticion())
    assert r.sigue_al_agente
    assert e.bloqueos == [] and e.avisos_owner == 0 and _insert_aviso(e) == []


async def test_sin_tope_flujo_intacto(e):
    e.uso = _uso(0, None)
    r = await ingreso.procesar(_peticion())
    assert r.sigue_al_agente
    assert e.bloqueos == [] and e.avisos_owner == 0


async def test_mensaje_duplicado_no_registra_ni_avisa(e):
    e.duplicado = True
    r = await ingreso.procesar(_peticion())
    assert r.motivo == "Mensaje duplicado"
    assert e.bloqueos == [] and e.avisos_owner == 0


async def test_conversacion_transferida_sigue_su_camino(e):
    e.status = "transferred"
    r = await ingreso.procesar(_peticion())
    assert e.bloqueos == [] and e.avisos_owner == 0
    assert r.respuesta_inmediata != ingreso.AVISO_LIMITE_IA


async def test_fallo_al_consultar_uso_deja_pasar_al_agente(e, caplog):
    e.uso_falla = True
    r = await ingreso.procesar(_peticion())
    assert r.sigue_al_agente
    assert str(TENANT) in caplog.text  # se registra el ID del negocio


# ---------------- loop_agente ----------------
@pytest.fixture
def ctx():
    return ContextoMensaje(
        tenant_id=TENANT, conversation_id=CONVERSACION, user_id=USUARIO, canal="whatsapp",
        usuario_id="5215599999999", nombre="Ana", contenido="hola", tipo="text",
        mensaje_id="wamid.A", db_message_id=MENSAJE, db_message_created_at=None,
        ids_agrupados=[MENSAJE],
    )


@pytest.fixture
def loop(monkeypatch, e):
    llamadas = {"cliente": 0}

    def cliente():
        llamadas["cliente"] += 1
        raise AssertionError("no debe invocarse al modelo")

    async def cfg(_):
        return {"agent_name": "A", "system_prompt": "P", "model": "claude-sonnet-5",
                "temperature": None, "history_window": 30}

    async def catalogo(*a, **k):
        return "", []

    async def historial(*a, **k):
        return []

    async def eventos(*a, **k):
        return []

    monkeypatch.setattr(loop_agente, "_cliente", cliente)
    monkeypatch.setattr(prompt_mod, "config_numero", cfg)
    monkeypatch.setattr(prompt_mod, "config_social", cfg)
    monkeypatch.setattr(prompt_mod, "catalogo_herramientas", catalogo)
    monkeypatch.setattr(prompt_mod, "historial", historial)
    monkeypatch.setattr(prompt_mod, "eventos_de_contexto", eventos)
    return llamadas


async def test_loop_agotado_no_invoca_al_modelo(e, loop, ctx):
    r = await loop_agente.responder(ctx)
    assert r.bloqueada_por_limite is True
    assert r.texto == "" and r.consumo.llamadas == 0
    assert loop["cliente"] == 0


async def test_loop_tester_no_se_bloquea(e, loop, ctx, monkeypatch):
    # simulacion=True: llega hasta pedir el cliente (que aquí explota a propósito).
    with pytest.raises(AssertionError, match="no debe invocarse"):
        await loop_agente.responder(ctx, simulacion=True, historial_previo=[])
    assert loop["cliente"] == 1


async def test_loop_bajo_el_tope_llega_al_modelo(e, loop, ctx):
    e.uso = _uso(5, 100)
    with pytest.raises(AssertionError, match="no debe invocarse"):
        await loop_agente.responder(ctx)
    assert loop["cliente"] == 1


async def test_loop_fallo_al_consultar_uso_deja_pasar(e, loop, ctx):
    e.uso_falla = True
    with pytest.raises(AssertionError, match="no debe invocarse"):
        await loop_agente.responder(ctx)
    assert loop["cliente"] == 1
