#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Конфигурационное приложение.
Считывает переменные окружения и формирует единый объект AppConfig.
"""

import os
from dataclasses import dataclass
from typing import Optional, List
from dotenv import load_dotenv

# Загружаем .env для локальной разработки
load_dotenv()


@dataclass
class TelegramConfig:
    bot_token: str
    webhook_secret: Optional[str] = None
    chat_id: Optional[str] = None


@dataclass
class OpenRouterConfig:
    api_key: str
    model: str = "openai/gpt-4o-mini"
    base_url: str = "https://openrouter.ai/api/v1"
    timeout: int = 30
    site_url: Optional[str] = None
    site_name: Optional[str] = None


@dataclass
class PowerBIConfig:
    tenant_id: str
    client_id: str
    client_secret: str
    dataset_id: str

    @property
    def is_valid(self) -> bool:
        """Проверяет, что все 4 ключевые переменные заполнены."""
        return bool(
            self.tenant_id.strip()
            and self.client_id.strip()
            and self.client_secret.strip()
            and self.dataset_id.strip()
        )

    def get_missing_vars(self) -> List[str]:
        """Возвращает список отсутствующих переменных для информативного логирования."""
        missing = []
        if not self.tenant_id.strip():
            missing.append("PBI_TENANT_ID")
        if not self.client_id.strip():
            missing.append("PBI_CLIENT_ID")
        if not self.client_secret.strip():
            missing.append("PBI_CLIENT_SECRET")
        if not self.dataset_id.strip():
            missing.append("PBI_DATASET_ID")
        return missing


@dataclass
class AppConfig:
    telegram: TelegramConfig
    openrouter: OpenRouterConfig
    powerbi: PowerBIConfig
    port: int = 5000
    debug: bool = False

    @classmethod
    def from_env(cls) -> "AppConfig":
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or ""
        if not bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

        openrouter_key = os.getenv("OPENROUTER_API_KEY") or ""
        if not openrouter_key:
            raise ValueError("OPENROUTER_API_KEY environment variable is required")

        telegram = TelegramConfig(
            bot_token=bot_token,
            webhook_secret=os.getenv("WEBHOOK_SECRET"),
            chat_id=os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID"),
        )

        openrouter = OpenRouterConfig(
            api_key=openrouter_key,
            model=os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
            site_url=os.getenv("OPENROUTER_SITE_URL"),
            site_name=os.getenv("OPENROUTER_SITE_NAME"),
        )

        # Поддерживаем два стиля именования: PBI_* и POWERBI_*
        powerbi = PowerBIConfig(
            tenant_id=(os.getenv("PBI_TENANT_ID") or os.getenv("POWERBI_TENANT_ID") or "").strip(),
            client_id=(os.getenv("PBI_CLIENT_ID") or os.getenv("POWERBI_CLIENT_ID") or "").strip(),
            client_secret=(os.getenv("PBI_CLIENT_SECRET") or os.getenv("POWERBI_CLIENT_SECRET") or "").strip(),
            dataset_id=(os.getenv("PBI_DATASET_ID") or os.getenv("POWERBI_DATASET_ID") or "").strip(),
        )

        return cls(
            telegram=telegram,
            openrouter=openrouter,
            powerbi=powerbi,
            port=int(os.getenv("PORT", 5000)),
            debug=os.getenv("DEBUG", "false").lower() == "true",
        )
