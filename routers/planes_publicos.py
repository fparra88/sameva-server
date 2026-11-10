"""
Planes para la landing (anónimo). Solo datos de marketing; ver
services/planes_publicos.py y PlanPublicoOut.

Sin autenticación a propósito: la landing no tiene sesión. Lleva
Cache-Control de 60 s para que las visitas no peguen a la base cada vez; es
el retraso máximo entre un cambio en gerencia y lo que ve el público.
"""

from fastapi import APIRouter, Request, Response

from schemas import PlanesPublicosOut
from services.limite_publico import limitar_por_ip
from services.planes_publicos import planes_publicos

router = APIRouter(prefix="/planes", tags=["planes"])

# 30 por minuto y por IP: con la caché del navegador una visita real pide el
# endpoint una vez; esto solo frena scrapers y bucles.
LIMITE_PETICIONES = 30
LIMITE_VENTANA_S = 60


@router.get("/publicos", response_model=PlanesPublicosOut)
async def listar_planes_publicos(request: Request, response: Response) -> PlanesPublicosOut:
    limitar_por_ip(request, LIMITE_PETICIONES, LIMITE_VENTANA_S, "planes_publicos")
    response.headers["Cache-Control"] = "public, max-age=60"
    return PlanesPublicosOut(planes=await planes_publicos())
