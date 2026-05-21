import logging
import os
import re
import sys
from typing import Any, Optional, Dict


class ConfigManager:
    def __init__(self) -> None:
        self._config: Dict[str, Any] = self._load_from_env()
        self._setup_logging()

    def _load_from_env(self) -> Dict[str, Any]:
        def _require(name: str) -> str:
            value = os.environ.get(name)
            if not value:
                raise ValueError(f"Required environment variable '{name}' is not set")
            return value

        return {
            'telegram': {
                'bot_token': _require('TELEGRAM_BOT_TOKEN'),
                'chat_id': int(_require('TELEGRAM_CHAT_ID')),
            },
            'meshtastic': {
                'host': _require('MESHTASTIC_HOST'),
                'port': int(os.environ.get('MESHTASTIC_PORT', '4403')),
            },
            'logging': {
                'level': os.environ.get('LOG_LEVEL', 'info'),
                'level_telegram': os.environ.get('LOG_LEVEL_TELEGRAM', 'warn'),
                'level_httpx': os.environ.get('LOG_LEVEL_HTTPX', 'warn'),
            },
        }

    def get(self, key: str, default: Optional[Any] = None) -> Any:
        parts = key.split('.')
        node: Any = self._config
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                if default is None:
                    raise KeyError(f"Configuration key '{key}' not found and no default value provided")
                return default
            node = node[part]
        return node

    def _setup_logging(self) -> None:
        log_level = self._parse_log_level(self.get('logging.level', 'INFO'))
        log_level_telegram = self._parse_log_level(self.get('logging.level_telegram', 'WARN'))
        log_level_httpx = self._parse_log_level(self.get('logging.level_httpx', 'WARN'))

        formatter = SensitiveFormatter('%(asctime)s %(levelname)s %(name)s - %(message)s')

        root = logging.getLogger()
        root.handlers.clear()

        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        root.addHandler(handler)
        root.setLevel(log_level)

        logging.getLogger('telegram').setLevel(log_level_telegram)
        logging.getLogger('httpx').setLevel(log_level_httpx)

    def _parse_log_level(self, level: Any) -> int:
        if isinstance(level, str):
            try:
                return getattr(logging, level.upper())
            except AttributeError:
                logging.warning(f"Invalid log level: {level}. Defaulting to INFO.")
                return logging.INFO
        elif isinstance(level, int):
            return level
        else:
            logging.warning(f"Invalid log level type: {type(level)}. Defaulting to INFO.")
            return logging.INFO


class SensitiveFormatter(logging.Formatter):
    def __init__(self, fmt: Optional[str] = None, datefmt: Optional[str] = None):
        super().__init__(fmt, datefmt)
        self.sensitive_patterns = [
            (re.compile(r'(https://api\.telegram\.org/bot)([A-Za-z0-9:_-]{35,})(/\w+)'), r'\1[redacted]\3')
        ]

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        for pattern, replacement in self.sensitive_patterns:
            message = pattern.sub(replacement, message)
        return message


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
