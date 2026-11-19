"""
Herramientas por tenant.
"""

import logging
import re
import unicodedata
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool

from config import settings
from deps import (
    UsuarioActual,
    gerencia_actual,
    negocio_actual,
    requiere_herramienta,
    tenant_actual,
    usuario_actual,
)
from session import conexion, execute, fetch_all, fetch_one
from schemas import (
    ActualizarHerramientaIn,
    ArchivoEnvioOut,
    ArchivosEnvioOut,
    EditarArchivoEnvioIn,
    ConectarDbExternaIn,
    ConectarGoogleDocIn,
    ConectarGoogleSheetIn,
    DbConexionIn,
    DbExternaInfoOut,
    DbProbarIn,
    EditarDbExternaIn,
    ConsumoPaginaOut,
    ConsumoResumenOut,
    HerramientaInfoOut,
    HerramientaOut,
    HerramientaNumeroOut,
    HerramientasNumeroIn,
)
from services import adjuntos, almacen_archivos, archivos_envio
from services import consumo_creditos as consumo_svc
from services import conversaciones as conversaciones_svc
from services import db_externa, google_tools, herramientas_calendario, limite_publico, secretos
from services import herramientas_plan
from services import numeros_whatsapp as numeros
from services.numeros_bitacora import Autor

log = logging.getLogger("operativai.herramientas")

router = APIRouter(
    prefix="/herramientas",
    tags=["herramientas"],
    # Ver la lista: todo el equipo del negocio. Conectar, editar o borrar:
    # solo gerencia (cada endpoint lo pide).
    dependencies=[Depends(negocio_actual), Depends(requiere_herramienta("herramientas"))],
)


@router.get("/info", response_model=HerramientaInfoOut)
async def info(usuario: UsuarioActual = Depends(usuario_actual)):
    try:
        correo = google_tools.correo_cuenta_servicio()
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    return HerramientaInfoOut(correo_servicio=correo)


# ============================================================
# Archivos que el agente envía por WhatsApp (familia `archivos`, sql/78)
# ============================================================
# Van antes de las rutas con `/{tool_key}`. Ver: todo el equipo; subir, editar
# y borrar: solo gerencia del negocio (owner/superadmin).
@router.get("/archivos", response_model=ArchivosEnvioOut)
async def archivos_listar(tenant_id: UUID = Depends(tenant_actual)):
    return ArchivosEnvioOut(
        disponible=almacen_archivos.disponible(),
        incluida_en_plan=await archivos_envio.plan_incluye(tenant_id),
        maximo=settings.ARCHIVOS_MAX_POR_NEGOCIO,
        archivos=await archivos_envio.listar(tenant_id),
    )


@router.post("/archivos", response_model=ArchivoEnvioOut, status_code=status.HTTP_201_CREATED)
async def archivos_subir(
    archivo: UploadFile = File(...),
    descripcion: str = Form(..., max_length=archivos_envio.MAX_DESCRIPCION * 2),
    _: UsuarioActual = Depends(gerencia_actual),
    tenant_id: UUID = Depends(tenant_actual),
):
    await archivos_envio.exigir_disponible(tenant_id)
    contenido = await conversaciones_svc.leer_subida(archivo)
    return await archivos_envio.crear(tenant_id, contenido, archivo.filename, descripcion)


@router.patch("/archivos/{archivo_id}", response_model=ArchivoEnvioOut)
async def archivos_editar(
    archivo_id: UUID,
    datos: EditarArchivoEnvioIn,
    _: UsuarioActual = Depends(gerencia_actual),
    tenant_id: UUID = Depends(tenant_actual),
):
    return await archivos_envio.actualizar(tenant_id, archivo_id, datos.descripcion)


@router.delete("/archivos/{archivo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archivos_borrar(
    archivo_id: UUID,
    _: UsuarioActual = Depends(gerencia_actual),
    tenant_id: UUID = Depends(tenant_actual),
):
    await archivos_envio.eliminar(tenant_id, archivo_id)


@router.get("/archivos/{archivo_id}/contenido")
async def archivos_ver(archivo_id: UUID, tenant_id: UUID = Depends(tenant_actual)):
    """El archivo tal cual, para previsualizarlo en el portal (autenticado)."""
    mime, nombre, contenido = await archivos_envio.descargar(tenant_id, archivo_id)
    return Response(
        content=contenido,
        media_type=mime,
        headers=adjuntos.cabeceras_descarga(nombre, privado=True),
    )


async def _solo_owner(usuario: UsuarioActual = Depends(usuario_actual)) -> UsuarioActual:
    """La auditoría de consumo (con números de clientes) es solo del dueño del negocio."""
    if usuario.role != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo el propietario del negocio puede ver el consumo de créditos",
        )
    return usuario


@router.get("/consumo", response_model=ConsumoPaginaOut)
async def consumo_creditos(
    desde: date | None = None,
    hasta: date | None = None,
    herramienta: str | None = Query(None, max_length=100),
    limite: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: UsuarioActual = Depends(_solo_owner),
    tenant_id: UUID = Depends(tenant_actual),
):
    """
    Historial de créditos gastados por herramienta, más recientes primero.
    Solo lee el tenant de la sesión; no hay forma de pedir otro.
    """
    if desde and hasta and desde > hasta:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'desde' no puede ser posterior a 'hasta'",
        )
    return await consumo_svc.listar_consumo(tenant_id, desde, hasta, herramienta, limite, offset)


@router.get("/consumo/resumen", response_model=ConsumoResumenOut)
async def consumo_creditos_resumen(
    desde: date | None = None,
    hasta: date | None = None,
    herramienta: str | None = Query(None, max_length=100),
    _: UsuarioActual = Depends(_solo_owner),
    tenant_id: UUID = Depends(tenant_actual),
):
    if desde and hasta and desde > hasta:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'desde' no puede ser posterior a 'hasta'",
        )
    return await consumo_svc.resumen_consumo(tenant_id, desde, hasta, herramienta)


@router.get("", response_model=list[HerramientaOut])
async def listar(tenant_id: UUID = Depends(tenant_actual)):
    filas = await fetch_all(
        """
        SELECT tool_key, tool_type, display_name, description,
               is_enabled, last_verified_at, config
        FROM tenant_tools
        WHERE tenant_id = $1
        ORDER BY created_at DESC
        """,
        tenant_id,
    )
    return [
        HerramientaOut(
            tool_key=f["tool_key"],
            tool_type=f["tool_type"],
            display_name=f["display_name"],
            description=f["description"],
            is_enabled=f["is_enabled"],
            last_verified_at=f["last_verified_at"],
            nombre_documento=(f["config"] or {}).get("nombre_documento"),
            url_original=(f["config"] or {}).get("url_original"),
            gestionada=herramientas_calendario.es_gestionada(f["tool_key"]),
            **_extra_db(f["tool_type"], f["config"]),
        )
        for f in filas
    ]


def _extra_db(tool_type: str, config: dict | None) -> dict:
    """Datos NO secretos de una conexión db_externa para la lista (nunca la contraseña)."""
    if tool_type != "db_externa":
        return {}
    c = config or {}
    return {
        "motor": c.get("motor"),
        "host": c.get("host"),
        "base": c.get("base"),
        "estado": c.get("estado") or "ok",
        "n_objetos": len(c.get("objetos") or []),
    }


def _slug(texto: str) -> str:
    sin_acentos = (
        unicodedata.normalize("NFKD", texto)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    limpio = re.sub(r"[^a-zA-Z0-9]+", "_", sin_acentos).strip("_").lower()
    return limpio[:80] or "herramienta"


async def _tool_key_disponible(tenant_id: UUID, base: str) -> str:
    candidato = base
    intento = 1
    while True:
        existe = await fetch_one(
            "SELECT 1 FROM tenant_tools WHERE tenant_id = $1 AND tool_key = $2",
            tenant_id,
            candidato,
        )
        if not existe:
            return candidato
        intento += 1
        candidato = f"{base}_{intento}"


@router.post("/google-sheets", response_model=HerramientaOut, status_code=201)
async def conectar_sheet(
    datos: ConectarGoogleSheetIn,
    tenant_id: UUID = Depends(tenant_actual),
    _: UsuarioActual = Depends(gerencia_actual),
):
    try:
        spreadsheet_id = google_tools.extraer_id_de_url(datos.url)
    except google_tools.UrlInvalidaError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.mensaje)

    try:
        # googleapiclient es síncrono y hace red: fuera del event loop.
        info = await run_in_threadpool(google_tools.validar_sheet, spreadsheet_id)
    except google_tools.ToolAccessError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=e.mensaje)

    tool_key = await _tool_key_disponible(tenant_id, _slug(datos.display_name))

    config = {
        "spreadsheet_id": spreadsheet_id,
        "range": datos.rango,
        "nombre_documento": info["titulo"],
        "hojas_disponibles": info["hojas"],
        "url_original": datos.url,
    }

    fila = await fetch_one(
        """
        INSERT INTO tenant_tools
            (tenant_id, tool_key, tool_type, display_name, description,
             parametros_schema, config, is_enabled, last_verified_at)
        VALUES ($1, $2, 'google_sheets', $3, $4, '{}'::jsonb, $5::jsonb, true, NOW())
        RETURNING tool_key, tool_type, display_name, description,
                  is_enabled, last_verified_at, config
        """,
        tenant_id,
        tool_key,
        datos.display_name,
        datos.description,
        config,
    )

    return HerramientaOut(
        tool_key=fila["tool_key"],
        tool_type=fila["tool_type"],
        display_name=fila["display_name"],
        description=fila["description"],
        is_enabled=fila["is_enabled"],
        last_verified_at=fila["last_verified_at"],
        nombre_documento=info["titulo"],
        url_original=datos.url,
    )


@router.post("/google-docs", response_model=HerramientaOut, status_code=201)
async def conectar_doc(
    datos: ConectarGoogleDocIn,
    tenant_id: UUID = Depends(tenant_actual),
    _: UsuarioActual = Depends(gerencia_actual),
):
    try:
        document_id = google_tools.extraer_id_de_url(datos.url)
    except google_tools.UrlInvalidaError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.mensaje)

    try:
        info = await run_in_threadpool(google_tools.validar_doc, document_id)
    except google_tools.ToolAccessError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=e.mensaje)

    tool_key = await _tool_key_disponible(tenant_id, _slug(datos.display_name))

    config = {
        "document_id": document_id,
        "nombre_documento": info["titulo"],
        "url_original": datos.url,
    }

    fila = await fetch_one(
        """
        INSERT INTO tenant_tools
            (tenant_id, tool_key, tool_type, display_name, description,
             parametros_schema, config, is_enabled, last_verified_at)
        VALUES ($1, $2, 'google_docs', $3, $4, '{}'::jsonb, $5::jsonb, true, NOW())
        RETURNING tool_key, tool_type, display_name, description,
                  is_enabled, last_verified_at, config
        """,
        tenant_id,
        tool_key,
        datos.display_name,
        datos.description,
        config,
    )

    return HerramientaOut(
        tool_key=fila["tool_key"],
        tool_type=fila["tool_type"],
        display_name=fila["display_name"],
        description=fila["description"],
        is_enabled=fila["is_enabled"],
        last_verified_at=fila["last_verified_at"],
        nombre_documento=info["titulo"],
        url_original=datos.url,
    )


@router.post("/{tool_key}/verificar", response_model=HerramientaOut)
async def verificar(
    tool_key: str,
    tenant_id: UUID = Depends(tenant_actual),
    _: UsuarioActual = Depends(gerencia_actual),
):
    fila = await fetch_one(
        """
        SELECT tool_type, display_name, description, is_enabled, config
        FROM tenant_tools
        WHERE tenant_id = $1 AND tool_key = $2
        """,
        tenant_id,
        tool_key,
    )
    if fila is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Herramienta no encontrada")

    config = fila["config"] or {}
    if fila["tool_type"] == "db_externa":
        return await _verificar_db_externa(tenant_id, tool_key, config)

    try:
        if fila["tool_type"] == "google_sheets":
            info = await run_in_threadpool(
                google_tools.validar_sheet, config["spreadsheet_id"]
            )
            config["hojas_disponibles"] = info["hojas"]
        elif fila["tool_type"] == "google_docs":
            info = await run_in_threadpool(
                google_tools.validar_doc, config["document_id"]
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Este tipo de herramienta no se verifica por esta vía",
            )
    except google_tools.ToolAccessError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=e.mensaje)

    config["nombre_documento"] = info["titulo"]

    actualizada = await fetch_one(
        """
        UPDATE tenant_tools
        SET config = $3::jsonb, last_verified_at = NOW(), updated_at = NOW()
        WHERE tenant_id = $1 AND tool_key = $2
        RETURNING tool_key, tool_type, display_name, description,
                  is_enabled, last_verified_at, config
        """,
        tenant_id,
        tool_key,
        config,
    )

    return HerramientaOut(
        tool_key=actualizada["tool_key"],
        tool_type=actualizada["tool_type"],
        display_name=actualizada["display_name"],
        description=actualizada["description"],
        is_enabled=actualizada["is_enabled"],
        last_verified_at=actualizada["last_verified_at"],
        nombre_documento=config.get("nombre_documento"),
        url_original=config.get("url_original"),
    )


@router.patch("/{tool_key}", response_model=HerramientaOut)
async def actualizar(
    tool_key: str,
    datos: ActualizarHerramientaIn,
    tenant_id: UUID = Depends(tenant_actual),
    _: UsuarioActual = Depends(gerencia_actual),
):
    # Compuerta de despliegue (spec 002, R25): hasta que n8n publique, el
    # historial no se puede activar. Primero debe existir (404 como siempre);
    # el 409 rechaza el PATCH completo, también los textos del mismo cuerpo.
    if (
        datos.is_enabled is True
        and not herramientas_calendario.compuerta_abierta(
            tool_key, settings.HISTORIAL_CITAS_N8N_PUBLICADO
        )
    ):
        existe = await fetch_one(
            "SELECT 1 FROM tenant_tools WHERE tenant_id = $1 AND tool_key = $2",
            tenant_id,
            tool_key,
        )
        if existe is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Herramienta no encontrada")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Esta herramienta aún no está disponible para tu negocio.",
        )

    fila = await fetch_one(
        """
        UPDATE tenant_tools SET
            display_name = COALESCE($3, display_name),
            description  = COALESCE($4, description),
            is_enabled   = COALESCE($5, is_enabled),
            updated_at   = NOW()
        WHERE tenant_id = $1 AND tool_key = $2
        RETURNING tool_key, tool_type, display_name, description,
                  is_enabled, last_verified_at, config
        """,
        tenant_id,
        tool_key,
        datos.display_name,
        datos.description,
        datos.is_enabled,
    )
    if fila is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Herramienta no encontrada")

    config = fila["config"] or {}
    return HerramientaOut(
        tool_key=fila["tool_key"],
        tool_type=fila["tool_type"],
        display_name=fila["display_name"],
        description=fila["description"],
        is_enabled=fila["is_enabled"],
        last_verified_at=fila["last_verified_at"],
        nombre_documento=config.get("nombre_documento"),
        url_original=config.get("url_original"),
        gestionada=herramientas_calendario.es_gestionada(fila["tool_key"]),
        **_extra_db(fila["tool_type"], config),
    )


@router.delete("/{tool_key}", status_code=204)
async def eliminar(
    tool_key: str,
    tenant_id: UUID = Depends(tenant_actual),
    _: UsuarioActual = Depends(gerencia_actual),
):
    # Las del calendario las vuelve a crear el sistema (al encender el
    # módulo o al arrancar): borrarlas no serviría. Pausarlas sí.
    if herramientas_calendario.es_gestionada(tool_key):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Esta herramienta es parte del calendario: no se puede eliminar. "
                "Puedes pausarla, o apagar el calendario."
            ),
        )
    await execute(
        "DELETE FROM tenant_tools WHERE tenant_id = $1 AND tool_key = $2",
        tenant_id,
        tool_key,
    )


# ============================================================
# Base de datos externa (tool_type db_externa) — MySQL / PostgreSQL
# ============================================================
TEXTO_VERSION_DB = "2026-11-16"


def _err(estado: int, codigo: str, mensaje: str, **extra) -> HTTPException:
    return HTTPException(status_code=estado, detail={"codigo": codigo, "mensaje": mensaje, **extra})


async def _exigir_db_externa(tenant_id: UUID) -> None:
    """503 sin clave de cifrado; 402 si el plan del negocio no incluye la familia."""
    if not secretos.disponible():
        raise _err(503, "db_externa_no_disponible", "Esta herramienta no está disponible por ahora.")
    async with conexion() as conn:
        familias = await herramientas_plan.familias_aplicables(conn, tenant_id)
    if "db_externa" not in familias:
        raise _err(
            402,
            "herramienta_no_incluida_en_plan",
            "Conectar una base de datos no está incluido en tu plan. Mejora tu plan para usarla.",
        )


def _limitar_pruebas(tenant_id: UUID) -> None:
    # Cada prueba abre una conexión hacia un host que escribe el cliente: se acota por negocio.
    limite_publico.limitar_por_clave(
        str(tenant_id), 10, 60, "db_externa_probar", "Demasiados intentos de conexión. Espera un minuto."
    )


def _destino(datos: DbConexionIn | DbProbarIn | EditarDbExternaIn, password: str) -> db_externa.Destino:
    return db_externa.Destino(
        motor=datos.motor,
        host=datos.host.strip(),
        puerto=datos.puerto or db_externa.PUERTO_POR_MOTOR[datos.motor],
        base=datos.base.strip(),
        usuario=datos.usuario.strip(),
        password=password,
        ssl_modo=datos.ssl_modo,
    )


async def _probar_o_error(destino: db_externa.Destino) -> dict:
    try:
        return await db_externa.probar(destino)
    except db_externa.ErrorDb as e:
        raise _err(400, e.codigo, e.mensaje)


async def _usadas(conn, tenant_id: UUID) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM tenant_tools WHERE tenant_id = $1 AND tool_type = 'db_externa'", tenant_id
    )


@router.get("/db-externa/info", response_model=DbExternaInfoOut)
async def db_externa_info(tenant_id: UUID = Depends(tenant_actual)):
    """Lo que el asistente necesita antes de empezar (plan, cupo, IP para el firewall)."""
    async with conexion() as conn:
        familias = await herramientas_plan.familias_aplicables(conn, tenant_id)
        usadas = await _usadas(conn, tenant_id)
    return DbExternaInfoOut(
        disponible=secretos.disponible(),
        incluida_en_plan="db_externa" in familias,
        ip_salida=settings.DB_EXTERNA_IP_SALIDA or None,
        conexiones_usadas=usadas,
        conexiones_max=settings.DB_EXTERNA_MAX_CONEXIONES,
        motores=list(db_externa.MOTORES),
        texto_version=TEXTO_VERSION_DB,
    )


@router.get("/db-externa/{tool_key}")
async def db_externa_detalle(
    tool_key: str,
    tenant_id: UUID = Depends(tenant_actual),
    _: UsuarioActual = Depends(gerencia_actual),
):
    """La configuración NO secreta de una conexión, para reabrir el asistente en modo edición."""
    fila = await fetch_one(
        """
        SELECT display_name, description, config FROM tenant_tools
         WHERE tenant_id = $1 AND tool_key = $2 AND tool_type = 'db_externa'
        """,
        tenant_id,
        tool_key,
    )
    if fila is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Herramienta no encontrada")
    c = fila["config"] or {}
    return {
        "display_name": fila["display_name"],
        "description": fila["description"],
        "motor": c.get("motor"),
        "host": c.get("host"),
        "puerto": c.get("puerto"),
        "base": c.get("base"),
        "usuario": c.get("usuario"),
        "ssl_modo": c.get("ssl_modo", "require"),
        "estado": c.get("estado") or "ok",
        "ultimo_error": c.get("ultimo_error"),
        "objetos": [
            {"objeto": o["objeto"], "descripcion": o.get("descripcion", ""), "columnas": [x["nombre"] for x in o["columnas"]]}
            for o in c.get("objetos") or []
        ],
    }


@router.post("/db-externa/probar")
async def db_externa_probar(
    datos: DbProbarIn,
    tenant_id: UUID = Depends(tenant_actual),
    _: UsuarioActual = Depends(gerencia_actual),
):
    """
    Prueba la conexión sin guardar nada y devuelve lo que el asistente muestra: las
    tablas/vistas visibles (con columnas) y si el usuario puede escribir. Al editar,
    sin `password` se usa la guardada de `tool_key` (del mismo negocio).
    """
    await _exigir_db_externa(tenant_id)
    _limitar_pruebas(tenant_id)
    password = datos.password
    if password is None:
        if not datos.tool_key:
            raise _err(400, "falta_password", "Escribe la contraseña para probar la conexión.")
        password = await secretos.leer(tenant_id, datos.tool_key)
        if password is None:
            raise _err(400, "falta_password", "Escribe la contraseña para probar la conexión.")
    return await _probar_o_error(_destino(datos, password))


def _config_db(datos, objetos: list[dict], permisos: dict) -> dict:
    return {
        "motor": datos.motor,
        "host": datos.host.strip(),
        "puerto": datos.puerto or db_externa.PUERTO_POR_MOTOR[datos.motor],
        "base": datos.base.strip(),
        "usuario": datos.usuario.strip(),
        "ssl_modo": datos.ssl_modo,
        "objetos": objetos,
        "estado": "ok",
        "permisos_escritura": bool(permisos["hay"] or not permisos["verificable"]),
    }


async def _validar_y_registrar(
    request: Request,
    conn,
    usuario: UsuarioActual,
    tenant_id: UUID,
    tool_key: str,
    datos,
    resultado: dict,
) -> list[dict]:
    """Cruza la selección con la base real y exige/registra las aceptaciones. Corre en la transacción."""
    if not datos.acepta_responsabilidad:
        raise _err(
            400,
            "falta_aceptar_responsabilidad",
            "Debes aceptar que eres responsable de los datos que el agente podrá consultar.",
        )
    try:
        objetos = db_externa.armar_objetos_permitidos(
            resultado["objetos"], [o.model_dump() for o in datos.objetos]
        )
    except db_externa.SeleccionInvalida as e:
        raise _err(400, "seleccion_invalida", str(e))

    permisos = resultado["permisos_escritura"]
    en_riesgo = permisos["hay"] or not permisos["verificable"]
    if en_riesgo and not datos.acepta_riesgo_escritura:
        raise _err(
            409,
            "permisos_escritura",
            "El usuario de esta base puede modificar datos. Usa uno de solo lectura, o confirma que asumes el riesgo.",
            detalle=permisos["detalle"],
        )

    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else None
    )
    agente = (request.headers.get("user-agent") or "")[:300]
    registros = [("responsabilidad", {"motor": datos.motor, "host": datos.host.strip(), "base": datos.base.strip()})]
    if en_riesgo:
        registros.append(
            ("permisos_escritura", {"permisos": permisos["detalle"], "verificable": permisos["verificable"]})
        )
    for tipo, detalle in registros:
        await conn.execute(
            """
            INSERT INTO db_externa_aceptaciones
                (tenant_id, tool_key, portal_user_id, email, tipo, texto_version, detalle, ip, user_agent)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
            """,
            tenant_id, tool_key, usuario.id, usuario.email, tipo, TEXTO_VERSION_DB, detalle, ip, agente,
        )
    return objetos


def _salida_db(fila) -> HerramientaOut:
    return HerramientaOut(
        tool_key=fila["tool_key"],
        tool_type=fila["tool_type"],
        display_name=fila["display_name"],
        description=fila["description"],
        is_enabled=fila["is_enabled"],
        last_verified_at=fila["last_verified_at"],
        **_extra_db(fila["tool_type"], fila["config"]),
    )


@router.post("/db-externa", response_model=HerramientaOut, status_code=201)
async def db_externa_conectar(
    datos: ConectarDbExternaIn,
    request: Request,
    tenant_id: UUID = Depends(tenant_actual),
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    await _exigir_db_externa(tenant_id)
    _limitar_pruebas(tenant_id)
    resultado = await _probar_o_error(_destino(datos, datos.password))
    tool_key = await _tool_key_disponible(tenant_id, _slug(datos.display_name))

    async with conexion() as conn:
        async with conn.transaction():
            # Serializa las altas del negocio: el cupo se comprueba y se inserta sin carreras.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"db_externa:{tenant_id}")
            if await _usadas(conn, tenant_id) >= settings.DB_EXTERNA_MAX_CONEXIONES:
                raise _err(
                    409,
                    "limite_conexiones",
                    f"Ya tienes {settings.DB_EXTERNA_MAX_CONEXIONES} bases conectadas. Elimina una para conectar otra.",
                )
            objetos = await _validar_y_registrar(request, conn, usuario, tenant_id, tool_key, datos, resultado)
            config = _config_db(datos, objetos, resultado["permisos_escritura"])
            fila = await conn.fetchrow(
                """
                INSERT INTO tenant_tools
                    (tenant_id, tool_key, tool_type, display_name, description,
                     parametros_schema, config, is_enabled, last_verified_at)
                VALUES ($1, $2, 'db_externa', $3, $4, $5::jsonb, $6::jsonb, true, NOW())
                RETURNING tool_key, tool_type, display_name, description, is_enabled, last_verified_at, config
                """,
                tenant_id, tool_key, datos.display_name, datos.description,
                db_externa.parametros_schema(objetos), config,
            )
            await secretos.guardar(conn, tenant_id, tool_key, datos.password)
    log.info("db_externa conectada: tenant=%s herramienta=%s motor=%s", tenant_id, tool_key, datos.motor)
    return _salida_db(fila)


@router.put("/db-externa/{tool_key}", response_model=HerramientaOut)
async def db_externa_editar(
    tool_key: str,
    datos: EditarDbExternaIn,
    request: Request,
    tenant_id: UUID = Depends(tenant_actual),
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    """Cambia la conexión o las tablas visibles. Sin `password` se conserva la guardada."""
    existe = await fetch_one(
        "SELECT 1 FROM tenant_tools WHERE tenant_id = $1 AND tool_key = $2 AND tool_type = 'db_externa'",
        tenant_id,
        tool_key,
    )
    if existe is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Herramienta no encontrada")
    await _exigir_db_externa(tenant_id)
    _limitar_pruebas(tenant_id)
    password = datos.password
    if password is None:
        password = await secretos.leer(tenant_id, tool_key)
        if password is None:
            raise _err(400, "falta_password", "Escribe la contraseña de nuevo para reconectar.")
    resultado = await _probar_o_error(_destino(datos, password))

    async with conexion() as conn:
        async with conn.transaction():
            objetos = await _validar_y_registrar(request, conn, usuario, tenant_id, tool_key, datos, resultado)
            config = _config_db(datos, objetos, resultado["permisos_escritura"])
            fila = await conn.fetchrow(
                """
                UPDATE tenant_tools
                   SET config = $3::jsonb, parametros_schema = $4::jsonb,
                       last_verified_at = NOW(), updated_at = NOW()
                 WHERE tenant_id = $1 AND tool_key = $2
                RETURNING tool_key, tool_type, display_name, description, is_enabled, last_verified_at, config
                """,
                tenant_id, tool_key, config, db_externa.parametros_schema(objetos),
            )
            if datos.password is not None:
                await secretos.guardar(conn, tenant_id, tool_key, datos.password)
    return _salida_db(fila)


async def _verificar_db_externa(tenant_id: UUID, tool_key: str, config: dict) -> HerramientaOut:
    """«Probar de nuevo»: reconecta con lo guardado y devuelve la herramienta a «ok» si responde."""
    await _exigir_db_externa(tenant_id)
    _limitar_pruebas(tenant_id)
    password = await secretos.leer(tenant_id, tool_key)
    if password is None:
        raise _err(400, "falta_password", "Faltan las credenciales: edita la conexión y escribe la contraseña.")
    await _probar_o_error(db_externa.destino_desde_config(config, password))
    await db_externa.marcar_ok(tenant_id, tool_key)
    fila = await fetch_one(
        """
        SELECT tool_key, tool_type, display_name, description, is_enabled, last_verified_at, config
          FROM tenant_tools WHERE tenant_id = $1 AND tool_key = $2
        """,
        tenant_id, tool_key,
    )
    return _salida_db(fila)


# ============================================================
# Herramientas por número e Instagram/Facebook (spec 005a, RF-6, RF-17; T5)
# ============================================================
def _herramienta_out(f: dict) -> HerramientaNumeroOut:
    return HerramientaNumeroOut(
        tool_key=f["tool_key"],
        tool_type=f.get("tool_type"),
        display_name=f.get("display_name"),
        description=f.get("description"),
        familia=f.get("familia"),
        activa=bool(f["activa"]),
        permitida_por_plan=bool(f["permitida_por_plan"]),
    )


@router.get("/numeros/{numero_id}", response_model=list[HerramientaNumeroOut])
async def listar_de_numero(
    numero_id: UUID,
    tenant_id: UUID = Depends(tenant_actual),
):
    """Herramientas del negocio con `activa` en ese número y `permitida_por_plan`."""
    async with conexion() as conn:
        await numeros.numero_del_negocio(conn, tenant_id, numero_id)
        return [_herramienta_out(f) for f in await herramientas_plan.catalogo(conn, tenant_id, numero_id)]


@router.put("/numeros/{numero_id}", response_model=list[HerramientaNumeroOut])
async def editar_de_numero(
    numero_id: UUID,
    datos: HerramientasNumeroIn,
    tenant_id: UUID = Depends(tenant_actual),
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    """Activa/desactiva herramientas en un número (402 si el plan no la incluye)."""
    autor = Autor.gerencia(usuario.id, usuario.email)
    async with conexion() as conn:
        if datos.activar:
            await herramientas_plan.activar_herramientas_de_numero(
                conn, tenant_id, numero_id, datos.activar, autor
            )
        if datos.desactivar:
            await herramientas_plan.desactivar_herramientas_de_numero(
                conn, tenant_id, numero_id, datos.desactivar, autor
            )
        return [_herramienta_out(f) for f in await herramientas_plan.catalogo(conn, tenant_id, numero_id)]


@router.get("/social", response_model=list[HerramientaNumeroOut])
async def listar_social(tenant_id: UUID = Depends(tenant_actual)):
    """Conjunto de Instagram, Facebook y el widget (RF-17)."""
    async with conexion() as conn:
        return [_herramienta_out(f) for f in await herramientas_plan.catalogo(conn, tenant_id, None)]


@router.put("/social", response_model=list[HerramientaNumeroOut])
async def editar_social(
    datos: HerramientasNumeroIn,
    tenant_id: UUID = Depends(tenant_actual),
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    autor = Autor.gerencia(usuario.id, usuario.email)
    async with conexion() as conn:
        if datos.activar:
            await herramientas_plan.activar_herramientas_social(conn, tenant_id, datos.activar, autor)
        if datos.desactivar:
            await herramientas_plan.desactivar_herramientas_social(conn, tenant_id, datos.desactivar, autor)
        return [_herramienta_out(f) for f in await herramientas_plan.catalogo(conn, tenant_id, None)]
