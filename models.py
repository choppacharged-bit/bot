# -*- coding: utf-8 -*-
"""
Pydantic модели для валидации входных данных.
"""

from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field, ConfigDict, field_validator


class RouterRequest(BaseModel):
    """Запрос к маршрутизатору DAX."""
    message: str = Field(..., min_length=1, max_length=1000)
    history: Optional[List[Dict[str, str]]] = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "message": "продажи по магазинам за сегодня",
                "history": [
                    {"role": "user", "content": "привет"},
                    {"role": "assistant", "content": "привет!"}
                ]
            }
        }
    )


class RouterResponse(BaseModel):
    """Ответ маршрутизатора."""
    route: str = Field(..., pattern="^(powerbi|chat)$")
    message: str = ""
    dax: str = ""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "route": "powerbi",
                "message": "",
                "dax": "EVALUATE ROW(\"Result\", SUM('Продажи'[Сумма]))"
            }
        }
    )


class FormatterRequest(BaseModel):
    """Запрос к форматтеру ответа."""
    message: str = Field(..., min_length=1)
    powerbi_result: Dict[str, Any]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "message": "продажи сегодня",
                "powerbi_result": {
                    "results": [
                        {
                            "tables": [
                                {
                                    "rows": [
                                        {"[Result]": 45000}
                                    ]
                                }
                            ]
                        }
                    ]
                }
            }
        }
    )


class FormatterResponse(BaseModel):
    """Ответ форматтера."""
    reply: str = Field(..., min_length=1)


class TelegramMessage(BaseModel):
    """Сообщение Telegram."""
    chat_id: str
    text: str = Field(..., min_length=1, max_length=4096)
    reply_markup: Optional[Dict[str, Any]] = None


class ErrorResponse(BaseModel):
    """Стандартный ответ об ошибке."""
    status: str = "error"
    message: str
    details: Optional[Dict[str, Any]] = None
