"""Pruebas unitarias de la alta disponibilidad del ControlNode (hito 3).

No dependen de Docker: usan recarga de módulos y TestClient de FastAPI.
"""

import importlib
import os

import pytest


def _reload_config(monkeypatch, **env):
    """Recarga dfsha.server.config con un entorno dado y devuelve el módulo."""
    from dfsha.server import config as config_module

    for key in (
        "DFSHA_CONTROL_URL",
        "DFSHA_CONTROL_URLS",
        "DFSHA_HA_ROLE",
        "DFSHA_HA_PEER_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(config_module)


@pytest.fixture
def restore_config():
    """Deja dfsha.server.config como estaba al terminar la prueba.

    Limpia el entorno de HA antes de recargar: una prueba pudo dejar un valor
    inválido (p. ej. DFSHA_HA_ROLE) y recargar sin limpiarlo re-dispararía el
    error de validación durante el teardown.
    """
    yield
    for key in (
        "DFSHA_CONTROL_URL",
        "DFSHA_CONTROL_URLS",
        "DFSHA_HA_ROLE",
        "DFSHA_HA_PEER_URL",
    ):
        os.environ.pop(key, None)
    from dfsha.server import config as config_module

    importlib.reload(config_module)


# --- R8.1: lista de ControlNodes retrocompatible --------------------------------


def test_control_urls_una_sola_url(monkeypatch, restore_config):
    config = _reload_config(monkeypatch, DFSHA_CONTROL_URL="http://control:8000")
    assert config.CONTROL_URLS == ["http://control:8000"]


def test_control_urls_lista(monkeypatch, restore_config):
    config = _reload_config(
        monkeypatch,
        DFSHA_CONTROL_URLS="http://a:8000, http://b:8000/",
    )
    assert config.CONTROL_URLS == ["http://a:8000", "http://b:8000"]


def test_control_urls_combina_sin_duplicar(monkeypatch, restore_config):
    config = _reload_config(
        monkeypatch,
        DFSHA_CONTROL_URLS="http://a:8000,http://b:8000",
        DFSHA_CONTROL_URL="http://a:8000",
    )
    # 'a' no se duplica aunque aparezca en ambas variables.
    assert config.CONTROL_URLS == ["http://a:8000", "http://b:8000"]


def test_control_urls_vacio(monkeypatch, restore_config):
    config = _reload_config(monkeypatch)
    assert config.CONTROL_URLS == []


# --- R1: validación de DFSHA_HA_ROLE --------------------------------------------


def test_ha_role_default_primary(monkeypatch, restore_config):
    config = _reload_config(monkeypatch)
    assert config.HA_ROLE == "primary"


def test_ha_role_standby(monkeypatch, restore_config):
    config = _reload_config(monkeypatch, DFSHA_HA_ROLE="standby")
    assert config.HA_ROLE == "standby"


def test_ha_role_invalido_falla(monkeypatch, restore_config):
    with pytest.raises(ValueError):
        _reload_config(monkeypatch, DFSHA_HA_ROLE="jefe")
