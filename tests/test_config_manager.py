import pytest
from config_manager import ConfigManager

REQUIRED_ENV = {
    'TELEGRAM_BOT_TOKEN': 'test-token-123',
    'TELEGRAM_CHAT_ID': '1000',
    'MESHTASTIC_HOST': '192.168.1.10',
}


@pytest.fixture
def base_env(monkeypatch):
    """Set all required env vars and clear optional ones that might leak from host environment."""
    for key in (
        'TELEGRAM_AUTHORIZED_USERS',
        'MESHTASTIC_LOCAL_NODES',
        'MESHTASTIC_PORT',
        'LOG_LEVEL',
        'LOG_LEVEL_TELEGRAM',
        'LOG_LEVEL_HTTPX',
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


def test_creates_with_required_env(base_env):
    cfg = ConfigManager()
    assert cfg.get('telegram.bot_token') == 'test-token-123'


def test_missing_required_raises(monkeypatch):
    for key in REQUIRED_ENV:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises((ValueError, KeyError)):
        ConfigManager()


def test_default_port(base_env):
    cfg = ConfigManager()
    assert cfg.get('meshtastic.port') == 4403


def test_dotted_key_with_default(base_env):
    cfg = ConfigManager()
    assert cfg.get('nonexistent.key', 'fallback') == 'fallback'
