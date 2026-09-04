# -*- coding: utf-8 -*-
"""
Вспомогательные функции.
"""

import re
from typing import Optional


def clean_token(raw_token: str) -> str:
    """Извлекает чистый токен Telegram вида 123456:ABC...
    
    Args:
        raw_token: Сырой токен из конфига
    
    Returns:
        Очищенный токен или пустая строка если не валидный
    
    Examples:
        >>> clean_token("123456:ABC-DEF")
        '123456:ABC-DEF'
        >>> clean_token("\"123456:ABC-DEF\"")
        '123456:ABC-DEF'
        >>> clean_token("")
        ''
    """
    if not raw_token:
        return ""
    
    # Пытаемся найти паттерн токена
    match = re.search(r"(\d+:[A-Za-z0-9_-]+)", raw_token)
    if match:
        return match.group(1)
    
    # Если не нашли, просто очищаем
    return raw_token.strip(" '\"[]")


def extract_json_from_text(text: str) -> Optional[dict]:
    """Извлекает JSON из текста.
    
    Args:
        text: Текст который может содержать JSON
    
    Returns:
        Распарсенный JSON или None если не найден
    
    Examples:
        >>> extract_json_from_text('{"key": "value"}')
        {'key': 'value'}
        >>> extract_json_from_text('```json\\n{"key": "value"}\\n```')
        {'key': 'value'}
    """
    import json
    
    if not text:
        return None
    
    # Пытаемся найти JSON в тексте
    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            return None
    
    return None


def format_number(value: float) -> str:
    """Форматирует число с пробелами для разделения тысяч.
    
    Args:
        value: Число для форматирования
    
    Returns:
        Отформатированная строка
    
    Examples:
        >>> format_number(1147125)
        '1 147 125'
        >>> format_number(45000.5)
        '45 000.5'
    """
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return value
    
    # Форматируем с разделением тысяч
    if isinstance(value, float) and value % 1 != 0:
        # С десятичными
        return f"{value:,.1f}".replace(",", " ")
    else:
        # Целые числа
        return f"{int(value):,}".replace(",", " ")
