def test_api_module_has_all():
    """Verify api module exports __all__."""
    from pymodules.api import __all__ as api_all
    assert "ModuleRouter" in api_all

def test_db_module_has_all():
    """Verify db module exports __all__."""
    from pymodules.db import __all__ as db_all
    assert "BaseRepository" in db_all

def test_messaging_module_has_all():
    """Verify messaging module exports __all__."""
    from pymodules.messaging import __all__ as messaging_all
    assert "MessageBroker" in messaging_all

def test_discovery_module_has_all():
    """Verify discovery module exports __all__."""
    from pymodules.discovery import __all__ as discovery_all
    assert "ServiceRegistry" in discovery_all
