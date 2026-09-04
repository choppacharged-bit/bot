# -*- coding: utf-8 -*-
"""
Конфигурация приложения из переменных окружения.
Все настройки в одном месте, без хардкода.
"""

import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class OpenRouterConfig:
    """Конфигурация OpenRouter API."""
    api_key: str
    model: str = "anthropic/claude-sonnet-4.5"
    base_url: str = "https://openrouter.ai/api/v1"
    timeout: float = 20.0
    site_url: Optional[str] = None
    site_name: Optional[str] = None

    @classmethod
    def from_env(cls) -> "OpenRouterConfig":
        """Создаёт конфиг из переменных окружения."""
        api_key = os.environ.get("OPENROUTER_API_KEY", "").strip(" '\"[]")
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY не установлен!")
        
        return cls(
            api_key=api_key,
            model=os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5").strip(" '\"[]"),
            site_url=os.environ.get("OPENROUTER_SITE_URL", "").strip(" '\"[]") or None,
            site_name=os.environ.get("OPENROUTER_SITE_NAME", "").strip(" '\"[]") or None,
        )


@dataclass
class TelegramConfig:
    """Конфигурация Telegram бота."""
    bot_token: str
    chat_id: str
    webhook_secret: Optional[str] = None

    @classmethod
    def from_env(cls) -> "TelegramConfig":
        """Создаёт конфиг из переменных окружения."""
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(" '\"[]")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip(" '\"[]")
        
        if not bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN не установлен!")
        if not chat_id:
            raise ValueError("TELEGRAM_CHAT_ID не установлен!")
        
        return cls(
            bot_token=bot_token,
            chat_id=chat_id,
            webhook_secret=os.environ.get("WEBHOOK_SECRET", "").strip(" '\"[]") or None,
        )


@dataclass
class AppConfig:
    """Основная конфигурация приложения."""
    openrouter: OpenRouterConfig
    telegram: TelegramConfig
    port: int = 10000
    debug: bool = False
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "AppConfig":
        """Создаёт конфиг из переменных окружения."""
        return cls(
            openrouter=OpenRouterConfig.from_env(),
            telegram=TelegramConfig.from_env(),
            port=int(os.environ.get("PORT", 10000)),
            debug=os.environ.get("DEBUG", "false").lower() == "true",
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )
