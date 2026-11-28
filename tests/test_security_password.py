"""security.verify_password nunca levanta: un hash ausente o corrupto es False."""

import pytest

from security import hash_password, verify_password


def test_coincide_y_no_coincide():
    h = hash_password("Clave.123")
    assert verify_password("Clave.123", h) is True
    assert verify_password("otra", h) is False


@pytest.mark.parametrize("hashed", [None, "", "no-es-un-hash-argon2"])
def test_sin_hash_valido_es_false_y_no_excepcion(hashed):
    """NULL = cuenta creada con Google (sql/13); texto basura = fila corrupta."""
    assert verify_password("Clave.123", hashed) is False
