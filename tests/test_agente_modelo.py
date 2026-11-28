"""
Tests de qué parámetros viajan a cada modelo (services/agente/modelo.py).

Lo que importa si falla: mandar un parámetro que el modelo no admite da 400 y
el cliente se queda sin respuesta.
"""

import pytest

from services.agente import modelo
from services.limites_agente import MODELOS_AGENTE


@pytest.mark.parametrize("nombre", MODELOS_AGENTE)
def test_todo_el_catalogo_tiene_capacidades_conocidas(nombre):
    """Un modelo del catálogo sin fila caería en DESCONOCIDO: sin effort ni visión."""
    assert modelo.capacidades_de(nombre) is not modelo.DESCONOCIDO


@pytest.mark.parametrize("nombre", ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"])
def test_generacion_5_5_manda_effort_explicito_y_nunca_thinking_ni_temperature(nombre):
    params = modelo.parametros_modelo(nombre, temperature=0.7)
    # Explícito: Opus 5.5 y Haiku 5.5 usan `medium` por defecto, no `low`.
    assert params == {"output_config": {"effort": modelo.EFFORT_POR_DEFECTO}}


@pytest.mark.parametrize("nombre", ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"])
def test_generacion_5_5_admite_imagen_y_pdf(nombre):
    cap = modelo.capacidades_de(nombre)
    assert cap.vision and cap.pdf
