#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Сервисы для обработки аналитических вопросов:
1. RouterService — определение маршрута (powerbi vs conversation) и генерация DAX.
2. FormatterService — превращение сырого ответа Power BI в красивый текст для Telegram.
"""

import json
from typing import Dict, Any, Optional
from dataclasses import dataclass
from openai import OpenAI

from logger import setup_logger, log_extra
from utils import extract_json_from_text
from schema import build_schema_prompt

log = setup_logger("services")


@dataclass
class RouteResult:
    route: str  # "powerbi" или "conversation"
    dax: Optional[str] = None
    message: Optional[str] = None


@dataclass
class FormatResult:
    reply: str


class RouterService:
    """Сервис маршрутизации вопросов пользователей."""

    def __init__(self, openai_client: OpenAI, model_name: str):
        self.client = openai_client
        self.model_name = model_name
        self.system_prompt = build_schema_prompt()

    def route_question(self, user_question: str) -> RouteResult:
        """Анализирует вопрос пользователя и формирует DAX или текстовый ответ."""
        try:
            log_extra(log, "INFO", "Routing user question", question=user_question)

            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user_question}
                ],
                temperature=0.1,
                max_tokens=1000,
            )

            raw_content = response.choices[0].message.content or ""
            log_extra(log, "DEBUG", "Raw LLM router response", raw_response=raw_content)

            json_data = extract_json_from_text(raw_content)
            if not json_data:
                log.warning("Failed to parse JSON from LLM router response")
                return RouteResult(
                    route="conversation",
                    message="Не удалось разобрать ответ аналитического модуля."
                )

            route = json_data.get("route", "conversation")
            dax = json_data.get("dax")
            message = json_data.get("message")

            # Логируем сгенерированный DAX для отладки
            if dax:
                log_extra(log, "INFO", "Generated DAX query", dax=dax[:500])

            return RouteResult(
                route=route,
                dax=dax,
                message=message
            )

        except Exception as e:
            log.error(f"Error in RouterService.route_question: {str(e)}", exc_info=True)
            return RouteResult(
                route="conversation",
                message="Произошла ошибка при формировании запроса к базе данных."
            )


class FormatterService:
    """Сервис форматирования ответа Power BI в человекочитаемый текст."""

    def __init__(self, openai_client: OpenAI, model_name: str, store_map: Dict[str, str]):
        self.client = openai_client
        self.model_name = model_name
        self.store_map = store_map
        self.reverse_store_map = {v: k for k, v in store_map.items()}

    def format_answer(self, user_question: str, pbi_result: Dict[str, Any]) -> FormatResult:
        """Форматирует данные из Power BI в финальный ответ для Telegram."""
        try:
            log_extra(log, "INFO", "Formatting PBI result", question=user_question)

            system_prompt = (
                "Ты — финансовый аналитик сети торговых точек. "
                "Твоя задача — взять сырые данные из Power BI и сформировать четкий, "
                "лаконичный и структурированный ответ для Telegram на русском языке.\n\n"
                "Правила форматирования:\n"
                "1. Используй emoji для наглядности (📊, 💰, 🏬, 📉, 📈).\n"
                "2. Форматируй числа: разделяй тысячи пробелами, деньги указывай в рублях (руб.).\n"
                "3. Подводи итоговые суммы, если в данных несколько магазинов или позиций.\n"
                "4. Отвечай кратко, сразу к сути, без приветствий и лишнего вступления.\n"
                "5. Заменяй системные коды магазинов на их понятные названия, если они встречаются."
            )

            user_payload = {
                "question": user_question,
                "powerbi_raw_data": pbi_result,
                "store_name_mapping": self.reverse_store_map
            }

            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)}
                ],
                temperature=0.2,
                max_tokens=1000,
            )

            formatted_text = response.choices[0].message.content or "Нет данных для отображения."
            return FormatResult(reply=formatted_text)

        except Exception as e:
            log.error(f"Error in FormatterService.format_answer: {str(e)}", exc_info=True)
            return FormatResult(reply="Ошибка при обработке результатов отчета.")
