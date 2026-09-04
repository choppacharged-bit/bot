# -*- coding: utf-8 -*-
"""
Бизнес-логика: работа с OpenRouter API и форматирование.
"""

import json
from datetime import datetime
from typing import Optional, List, Dict, Any
from openai import OpenAI, APIError, APIConnectionError, RateLimitError

from logger import setup_logger, log_extra
from utils import extract_json_from_text
from models import RouterResponse, FormatterResponse
from schema import build_schema_prompt, KNOWN_STORES

log = setup_logger("services")

# Системные промпты
ROUTER_SYSTEM_PROMPT_TEMPLATE = """Ты — маршрутизатор и генератор DAX-запросов для Power BI бота магазинов.

Отвечай ТОЛЬКО валидным JSON, без markdown, без пояснений, без ```.

{schema}

ПРАВИЛА ОПРЕДЕЛЕНИЯ ПЕРИОДОВ И ДАТ:
- Запомни названия колонок с датами в таблицах:
  • В 'Продажи', 'Расходы', 'Чеки', 'Валовая прибыль', 'Выплаченное зп', 'Смены' колонка называется [Дата]
  • В таблице 'Выручки' колонка называется [Дата.1] (ВАЖНО!)
- Если пользователь указывает конкретный месяц (например, "за август", "за июль"):
  Используй: MONTH(<КолонкаДаты>) = <НомерМесяца> && YEAR(<КолонкаДаты>) = <Год>
  Игнорируй TODAY(), бери именно названный месяц!
- Период "этот месяц" / "текущий месяц":
  Используй: MONTH(<КолонкаДаты>) = MONTH(TODAY()) && YEAR(<КолонкаДаты>) = YEAR(TODAY())
- Период "прошлый месяц":
  Используй: MONTH(<КолонкаДаты>) = MONTH(EDATE(TODAY(),-1)) && YEAR(<КолонкаДаты>) = YEAR(EDATE(TODAY(),-1))
- период "сегодня" -> <КолонкаДаты>=TODAY()
- период "вчера" -> <КолонкаДаты>=TODAY()-1

ОБЩИЕ ПРАВИЛА DAX:
- Всегда используй шаблон: EVALUATE ROW("Result", <выражение>)
- Без фильтров: EVALUATE ROW("Result", SUM('Таблица'[Колонка]))
- С фильтрами: EVALUATE ROW("Result", CALCULATE(SUM('Таблица'[Колонка]), <условия через &&>))
- КРИТИЧЕСКИ ВАЖНО: всегда переводи человеческое название магазина в реальный код

Если вопрос относится к данным — верни:
{{"route": "powerbi", "message": "", "dax": "<готовый DAX-запрос>"}}

Если вопрос НЕ относится к данным — верни:
{{"route": "chat", "message": "<готовый ответ пользователю>", "dax": ""}}

ВАЖНО: ключи всегда route, message, dax — на английском."""

FORMAT_SYSTEM_PROMPT = """Ты — аккуратный персональный AI-ассистент владельца сети магазинов.

Твоя задача — принять сырые данные из Power BI (в формате JSON) и оформить чистый, визуально приятный ответ для Telegram.

ПРАВИЛА ОФОРМЛЕНИЯ:
1. НИКОГДА не используй звёздочки (**текст** или *текст*) и решётки (#).
2. Используй структурированные списки с эмодзи (🏬, 💰, 💳, 📦, 📊, 📉).
3. Суммы разделяй пробелами (например: 1 147 125 ₽).
"""


class RouterService:
    """Сервис маршрутизации и генерации DAX запросов."""
    
    def __init__(self, openai_client: OpenAI, model: str):
        """Инициализирует сервис.
        
        Args:
            openai_client: Клиент OpenAI/OpenRouter
            model: Имя модели
        """
        self.client = openai_client
        self.model = model
        self.schema_prompt = build_schema_prompt()
    
    def route_question(
        self,
        question: str,
        history: Optional[List[Dict[str, str]]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> RouterResponse:
        """Маршрутизирует вопрос и генерирует DAX или chat ответ.
        
        Args:
            question: Вопрос пользователя
            history: История диалога
            extra_headers: Дополнительные заголовки для API
        
        Returns:
            RouterResponse с route, message и dax
        
        Raises:
            APIError: При ошибке OpenRouter API
            ValueError: При невалидном JSON ответе
        """
        try:
            now = datetime.now()
            date_context = f"\nТЕКУЩАЯ ДАТА СЕРВЕРА: {now.strftime('%d.%m.%Y')}, Месяц: {now.month}, Год: {now.year}\n"
            dynamic_system_prompt = ROUTER_SYSTEM_PROMPT_TEMPLATE.format(schema=self.schema_prompt) + date_context
            
            messages = [{"role": "system", "content": dynamic_system_prompt}]
            
            # Добавляем историю
            if history and isinstance(history, list):
                for msg in history:
                    role = msg.get("role", "user")
                    content = msg.get("content", "")
                    if content:
                        messages.append({"role": role, "content": content})
            
            messages.append({"role": "user", "content": question})
            
            # Вызываем API с принудительной валидацией JSON
            response = self.client.chat.completions.create(
                model=self.model,
                max_tokens=1000,
                response_format={"type": "json_object"},
                extra_headers=extra_headers or {},
                messages=messages,
            )
            
            text = response.choices[0].message.content.strip()
            log_extra(log, "INFO", "Router response", model=self.model, tokens_used=response.usage.total_tokens)
            
            # Извлекаем JSON из ответа
            json_data = extract_json_from_text(text)
            if not json_data:
                log.error(f"Не удалось распарсить JSON из ответа: {text[:100]}")
                raise ValueError(f"Invalid JSON response: {text[:100]}")
            
            # Валидируем ответ через Pydantic
            return RouterResponse(**json_data)
            
        except (APIConnectionError, RateLimitError) as e:
            log.error(f"API connection error: {str(e)}")
            raise
        except APIError as e:
            log.error(f"OpenRouter API error: {str(e)}")
            raise
        except Exception as e:
            log.error(f"Unexpected error in router: {str(e)}", exc_info=True)
            raise


class FormatterService:
    """Сервис форматирования ответов из Power BI."""
    
    def __init__(self, openai_client: OpenAI, model: str, store_replacements: Dict[str, str]):
        """Инициализирует сервис.
        
        Args:
            openai_client: Клиент OpenAI/OpenRouter
            model: Имя модели
            store_replacements: Словарь замены кодов магазинов
        """
        self.client = openai_client
        self.model = model
        self.store_replacements = store_replacements
    
    def format_answer(
        self,
        question: str,
        powerbi_result: Dict[str, Any],
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> FormatterResponse:
        """Форматирует ответ из Power BI в человекочитаемый текст.
        
        Args:
            question: Исходный вопрос пользователя
            powerbi_result: Результат из Power BI
            extra_headers: Дополнительные заголовки для API
        
        Returns:
            FormatterResponse с отформатированным текстом
        
        Raises:
            APIError: При ошибке OpenRouter API
        """
        try:
            user_content = (
                f"Вопрос пользователя: {question}\n"
                f"Результат из Power BI (JSON): {json.dumps(powerbi_result, ensure_ascii=False)}"
            )
            
            response = self.client.chat.completions.create(
                model=self.model,
                max_tokens=1000,
                extra_headers=extra_headers or {},
                messages=[
                    {"role": "system", "content": FORMAT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
            )
            
            text = response.choices[0].message.content.strip()
            
            # Очищаем форматирование
            text = text.replace("**", "").replace("*", "").replace("`", "").replace("#", "")
            
            # Применяем замены кодов магазинов
            for code, friendly_name in self.store_replacements.items():
                text = text.replace(code, friendly_name)
            
            log_extra(log, "INFO", "Formatter response", model=self.model, tokens_used=response.usage.total_tokens)
            
            return FormatterResponse(reply=text)
            
        except (APIConnectionError, RateLimitError) as e:
            log.error(f"API connection error: {str(e)}")
            raise
        except APIError as e:
            log.error(f"OpenRouter API error: {str(e)}")
            raise
        except Exception as e:
            log.error(f"Unexpected error in formatter: {str(e)}", exc_info=True)
            raise
