# -*- coding: utf-8 -*-
"""
Структурированное логирование в JSON формате.
"""

import logging
import json
from datetime import datetime
from typing import Any, Dict, Optional


class JSONFormatter(logging.Formatter):
    """Форматер логов в JSON."""
    
    def format(self, record: logging.LogRecord) -> str:
        log_data: Dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        
        # Добавляем extra данные если есть
        if hasattr(record, "extra_data") and record.extra_data:
            log_data["extra"] = record.extra_data
        
        # Добавляем информацию об ошибке если есть
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        return json.dumps(log_data, ensure_ascii=False)


def setup_logger(name: str, level: str = "INFO") -> logging.Logger:
    """Настраивает логгер с JSON форматом.
    
    Args:
        name: Имя логгера
        level: Уровень логирования (INFO, DEBUG, ERROR и т.д.)
    
    Returns:
        Настроенный логгер
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    
    # Консольный обработчик
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(JSONFormatter())
    logger.addHandler(console_handler)
    
    return logger


def log_extra(logger: logging.Logger, level: str, message: str, **kwargs) -> None:
    """Логирует сообщение с дополнительными данными.
    
    Args:
        logger: Логгер
        level: Уровень логирования
        message: Сообщение
        **kwargs: Дополнительные данные
    """
    record = logger.makeRecord(
        logger.name,
        getattr(logging, level.upper()),
        "unknown",
        0,
        message,
        (),
        None,
    )
    record.extra_data = kwargs
    logger.handle(record)
