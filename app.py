#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PowerBI AI-ассистент — Telegram бот для аналитики.
Поддерживает работу через Make.com Webhook / Power BI REST API.
Содержит статические DAX-шаблоны для кнопок меню во избежание галлюцинаций LLM.
"""

import os
import json
import logging
import hmac
import requests
from datetime import datetime
from typing import Optional, Dict, Any

from flask import Flask, request, jsonify
from openai import OpenAI
from apscheduler.schedulers.background import BackgroundScheduler

# Импортируем модули проекта
from config import AppConfig
from logger import setup_logger, log_extra
from utils import clean_token, extract_json_from_text
from services import RouterService, FormatterService
from telegram_client import TelegramClient
from schema import build_schema_prompt, STORE_CODE_MAP

# ============================================================================
# ИНИЦИАЛИЗАЦИЯ
# ============================================================================

app = Flask(__name__)
log = setup_logger("bot")

try:
    config = AppConfig.from_env()
    log_extra(log, "INFO", "Configuration loaded", port=config.port)
except ValueError as e:
    log.error(f"Configuration error: {str(e)}", exc_info=True)
    raise

# Инициализация OpenAI / OpenRouter
try:
    extra_headers = {}
    if config.openrouter.site_url:
        extra_headers["HTTP-Referer"] = config.openrouter.site_url
    if config.openrouter.site_name:
        extra_headers["X-Title"] = config.openrouter.site_name

    openai_client = OpenAI(
        base_url=config.openrouter.base_url,
        api_key=config.openrouter.api_key,
        timeout=config.openrouter.timeout,
    )
    
    log_extra(
        log,
        "INFO",
        "OpenRouter client initialized",
        model=config.openrouter.model,
    )
except Exception as e:
    log.error(f"Failed to initialize OpenRouter client: {str(e)}", exc_info=True)
    raise

# Инициализация сервисов
try:
    router_service = RouterService(openai_client, config.openrouter.model)
    formatter_service = FormatterService(
        openai_client,
        config.openrouter.model,
        STORE_CODE_MAP,
    )
    telegram_client = TelegramClient(config.telegram.bot_token)
    
    log.info("Services initialized successfully")
except Exception as e:
    log.error(f"Failed to initialize services: {str(e)}", exc_info=True)
    raise

# ============================================================================
# СТАТИЧЕСКИЕ DAX ШАБЛОНЫ ДЛЯ КНОПОК (100% ДЕТЕРМИНИРОВАННОСТЬ)
# ============================================================================

STATIC_DAX_MAP: Dict[str, Dict[str, str]] = {
    "sales_by_store_today": {
        "title": "Продажи по магазинам за сегодня",
        "dax": """
EVALUATE
SUMMARIZECOLUMNS(
    'Валовая приыбль'[МагазинКод],
    FILTER('Календарь', 'Календарь'[Дата] = TODAY()),
    "Выручка", SUM('Валовая приыбль'[СуммаПродажи]),
    "Количество_чеков", DISTINCTCOUNT('Валовая приыбль'[НомерЧека])
)
"""
    },
    "sales_yesterday": {
        "title": "Продажи за вчера по магазинам",
        "dax": """
EVALUATE
SUMMARIZECOLUMNS(
    'Валовая приыбль'[МагазинКод],
    FILTER('Календарь', 'Календарь'[Дата] = TODAY() - 1),
    "Выручка", SUM('Валовая приыбль'[СуммаПродажи]),
    "Количество_чеков", DISTINCTCOUNT('Валовая приыбль'[НомерЧека])
)
"""
    },
    "sales_month": {
        "title": "Продажи за этот месяц по магазинам",
        "dax": """
EVALUATE
SUMMARIZECOLUMNS(
    'Валовая приыбль'[МагазинКод],
    DATESMTD('Календарь'[Дата]),
    "Выручка", SUM('Валовая приыбль'[СуммаПродажи]),
    "Валовая_Прибыль", SUM('Валовая приыбль'[ВаловаяПрибыль])
)
"""
    },
    "sales_pionersky": {
        "title": "Продажи магазина Пионерский за сегодня",
        "dax": """
EVALUATE
CALCULATETABLE(
    SUMMARIZECOLUMNS(
        'Валовая приыбль'[МагазинКод],
        "Выручка", SUM('Валовая приыбль'[СуммаПродажи]),
        "Количество_чеков", DISTINCTCOUNT('Валовая приыбль'[НомерЧека])
    ),
    'Валовая приыбль'[МагазинКод] = "ОП_5_Анапа",
    'Календарь'[Дата] = TODAY()
)
"""
    },
    "sales_ozero": {
        "title": "Продажи магазина Озеро за сегодня",
        "dax": """
EVALUATE
CALCULATETABLE(
    SUMMARIZECOLUMNS(
        'Валовая приыбль'[МагазинКод],
        "Выручка", SUM('Валовая приыбль'[СуммаПродажи]),
        "Количество_чеков", DISTINCTCOUNT('Валовая приыбль'[НомерЧека])
    ),
    'Валовая приыбль'[МагазинКод] = "Гебея озеро",
    'Календарь'[Дата] = TODAY()
)
"""
    },
    "sales_utrish": {
        "title": "Продажи магазина Утриш за сегодня",
        "dax": """
EVALUATE
CALCULATETABLE(
    SUMMARIZECOLUMNS(
        'Валовая приыбль'[МагазинКод],
        "Выручка", SUM('Валовая приыбль'[СуммаПродажи]),
        "Количество_чеков", DISTINCTCOUNT('Валовая приыбль'[НомерЧека])
    ),
    'Валовая приыбль'[МагазинКод] = "ОП_8_Утриш",
    'Календарь'[Дата] = TODAY()
)
"""
    },
    "sales_dzhemete": {
        "title": "Продажи магазина Джемете за сегодня",
        "dax": """
EVALUATE
CALCULATETABLE(
    SUMMARIZECOLUMNS(
        'Валовая приыбль'[МагазинКод],
        "Выручка", SUM('Валовая приыбль'[СуммаПродажи]),
        "Количество_чеков", DISTINCTCOUNT('Валовая приыбль'[НомерЧека])
    ),
    'Валовая приыбль'[МагазинКод] = "ОП_1 Анапа Ленинградская",
    'Календарь'[Дата] = TODAY()
)
"""
    },
    "expenses_by_store": {
        "title": "Расходы по магазинам за этот месяц",
        "dax": """
EVALUATE
SUMMARIZECOLUMNS(
    'Расходы'[МагазинКод],
    DATESMTD('Календарь'[Дата]),
    "Сумма_Расходов", SUM('Расходы'[Сумма])
)
"""
    },
    "expenses_by_category": {
        "title": "Расходы по статьям за этот месяц",
        "dax": """
EVALUATE
SUMMARIZECOLUMNS(
    'Расходы'[СтатьяРасходов],
    DATESMTD('Календарь'[Дата]),
    "Сумма_Расходов", SUM('Расходы'[Сумма])
)
"""
    },
    "expenses_today": {
        "title": "Расходы за сегодня",
        "dax": """
EVALUATE
SUMMARIZECOLUMNS(
    'Расходы'[СтатьяРасходов],
    FILTER('Календарь', 'Календарь'[Дата] = TODAY()),
    "Сумма_Расходов", SUM('Расходы'[Сумма])
)
"""
    },
    "expenses_month": {
        "title": "Расходы за этот месяц всего",
        "dax": """
EVALUATE
CALCULATE TABLE(
    ROW("Всего_Расходов", SUM('Расходы'[Сумма])),
    DATESMTD('Календарь'[Дата])
)
"""
    },
    "salary_period": {
        "title": "Зарплата за текущий расчётный период",
        "dax": """
EVALUATE
SUMMARIZECOLUMNS(
    'Сотруник'[ФИО],
    'РасчетНачислений'[МагазинКод],
    DATESMTD('Календарь'[Дата]),
    "Начислено", SUM('РасчетНачислений'[Сумма])
)
"""
    },
    "salary_by_emp": {
        "title": "Зарплата по сотрудникам за этот месяц",
        "dax": """
EVALUATE
SUMMARIZECOLUMNS(
    'Сотруник'[ФИО],
    DATESMTD('Календарь'[Дата]),
    "Начислено", SUM('РасчетНачислений'[Сумма])
)
"""
    },
    "plan_status": {
        "title": "Выполнение плана по магазинам за сегодня",
        "dax": """
EVALUATE
SUMMARIZECOLUMNS(
    'Валовая приыбль'[МагазинКод],
    FILTER('Календарь', 'Календарь'[Дата] = TODAY()),
    "Факт_Выручка", SUM('Валовая приыбль'[СуммаПродажи]),
    "План_Выручка", SUM('Планы'[ПланСумма])
)
"""
    },
    "top_products_today": {
        "title": "Топ 5 продаваемых товаров за сегодня",
        "dax": """
EVALUATE
TOPN(
    5,
    SUMMARIZECOLUMNS(
        'Валовая приыбль'[Номенклатура],
        FILTER('Календарь', 'Календарь'[Дата] = TODAY()),
        "Выручка", SUM('Валовая приыбль'[СуммаПродажи]),
        "Количество", SUM('Валовая приыбль'[Количество])
    ),
    [Выручка],
    DESC
)
"""
    }
}

# ============================================================================
# КЛАВИАТУРЫ TELEGRAM
# ============================================================================

def get_main_reply_keyboard():
    return {
        "keyboard": [
            [{"text": "📊 Продажи"}, {"text": "💸 Расходы"}],
            [{"text": "💼 Зарплата"}, {"text": "🎯 План и Итоги"}]
        ],
        "resize_keyboard": True,
        "is_persistent": True
    }


def get_sales_inline_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "🏬 По магазинам (сегодня)", "callback_data": "sales_by_store_today"},
                {"text": "📅 За вчера", "callback_data": "sales_yesterday"}
            ],
            [
                {"text": "📆 Этот месяц", "callback_data": "sales_month"},
                {"text": "📍 Пионерский", "callback_data": "sales_pionersky"}
            ],
            [
                {"text": "📍 Озеро", "callback_data": "sales_ozero"},
                {"text": "📍 Утриш", "callback_data": "sales_utrish"},
                {"text": "📍 Джемете", "callback_data": "sales_dzhemete"}
            ]
        ]
    }


def get_expenses_inline_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "🏬 По магазинам (месяц)", "callback_data": "expenses_by_store"},
                {"text": "📂 По статьям", "callback_data": "expenses_by_category"}
            ],
            [
                {"text": "📅 За сегодня", "callback_data": "expenses_today"},
                {"text": "📆 За этот месяц", "callback_data": "expenses_month"}
            ]
        ]
    }


def get_salary_inline_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "🧮 Расчётный период", "callback_data": "salary_period"},
                {"text": "👥 По сотрудникам", "callback_data": "salary_by_emp"}
            ]
        ]
    }


def get_plans_inline_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "🎯 Выполнение плана", "callback_data": "plan_status"},
                {"text": "🍷 Топ товаров за день", "callback_data": "top_products_today"}
            ]
        ]
    }

# ============================================================================
# ВЫПОЛНЕНИЕ DAX ЗАПРОСА С ДЕТАЛЬНЫМ ЛОГИРОВАНИЕМ
# ============================================================================

def execute_powerbi_dax(dax_query: str) -> Dict[str, Any]:
    """Выполнение DAX-запроса через Webhook Make или Azure REST API."""
    webhook_url = (
        os.getenv("PBI_WEBHOOK_URL") 
        or os.getenv("MAKE_WEBHOOK_URL") 
        or config.powerbi.webhook_url 
        or ""
    ).strip()

    log.info(f"Executing DAX payload:\n{dax_query.strip()}")

    # 1. Запрос через Make Webhook
    if webhook_url:
        try:
            log.info(f"Sending DAX to Webhook: {webhook_url[:35]}...")
            payload = {
                "dax": dax_query,
                "query": dax_query
            }
            res = requests.post(webhook_url, json=payload, timeout=25)
            
            log.info(f"PowerBI Webhook raw response (HTTP {res.status_code}): {res.text[:800]}")
            res.raise_for_status()
            
            try:
                return res.json()
            except Exception:
                return {"results": [{"tables": [{"rows": [{"value": res.text}]}]}]}

        except Exception as e:
            log.error(f"Error executing via Webhook: {str(e)}", exc_info=True)
            return {"error": f"Ошибка Webhook Make.com: {str(e)}"}

    # 2. Прямой REST API через Azure AD
    tenant_id = (os.getenv("PBI_TENANT_ID") or os.getenv("POWERBI_TENANT_ID") or config.powerbi.tenant_id or "").strip()
    client_id = (os.getenv("PBI_CLIENT_ID") or os.getenv("POWERBI_CLIENT_ID") or config.powerbi.client_id or "").strip()
    client_secret = (os.getenv("PBI_CLIENT_SECRET") or os.getenv("POWERBI_CLIENT_SECRET") or config.powerbi.client_secret or "").strip()
    dataset_id = (os.getenv("PBI_DATASET_ID") or os.getenv("POWERBI_DATASET_ID") or config.powerbi.dataset_id or "").strip()

    missing = []
    if not tenant_id:
        missing.append("PBI_TENANT_ID")
    if not client_id:
        missing.append("PBI_CLIENT_ID")
    if not client_secret:
        missing.append("PBI_CLIENT_SECRET")
    if not dataset_id:
        missing.append("PBI_DATASET_ID")

    if missing:
        log.warning(f"Power BI credentials missing: {', '.join(missing)}")
        return {"error": f"Отсутствуют переменные окружения: {', '.join(missing)} (или PBI_WEBHOOK_URL)"}

    try:
        token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        token_data = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://analysis.windows.net/powerbi/api/.default"
        }
        
        token_res = requests.post(token_url, data=token_data, timeout=10)
        token_res.raise_for_status()
        access_token = token_res.json().get("access_token")

        pbi_url = f"https://api.powerbi.com/v1.0/myorg/datasets/{dataset_id}/executeQueries"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        body = {
            "queries": [{"query": dax_query}],
            "serializerSettings": {"includeNulls": True}
        }

        pbi_res = requests.post(pbi_url, headers=headers, json=body, timeout=20)
        log.info(f"PowerBI Direct API raw response (HTTP {pbi_res.status_code}): {pbi_res.text[:800]}")
        pbi_res.raise_for_status()
        return pbi_res.json()

    except requests.exceptions.HTTPError as http_err:
        err_msg = f"HTTP {http_err.response.status_code}: {http_err.response.text}"
        log.error(f"Power BI API HTTP Error: {err_msg}")
        return {"error": err_msg}
    except Exception as e:
        log.error(f"Error executing Power BI REST API query: {str(e)}", exc_info=True)
        return {"error": str(e)}

# ============================================================================
# ОБРАБОТКА ЗАПРОСОВ (ДИНАМИЧЕСКИЕ И СТАЦИОНАРНЫЕ)
# ============================================================================

def process_static_action(chat_id: str, action_key: str) -> None:
    """Выполнение захардкоженного DAX-запроса для кнопок меню."""
    action_info = STATIC_DAX_MAP.get(action_key)
    if not action_info:
        log.error(f"Unknown static action key: {action_key}")
        telegram_client.send_message(chat_id, "⚠️ Выбран неизвестный отчет.")
        return

    title = action_info["title"]
    dax_query = action_info["dax"]

    log_extra(log, "INFO", "Executing static menu action", action=action_key, title=title)
    pbi_result = execute_powerbi_dax(dax_query)

    if "error" in pbi_result:
        reply_text = f"⚠️ Ошибка выполнения отчета '{title}':\n{pbi_result['error']}"
    else:
        formatted = formatter_service.format_answer(title, pbi_result)
        reply_text = formatted.reply

    telegram_client.send_message(chat_id, reply_text)


def process_analytics_query(
    chat_id: str,
    user_text: str,
    reply_markup: Optional[Dict[str, Any]] = None
) -> None:
    """Обработка свободного текстового запроса через GPT-роутер."""
    try:
        routed = router_service.route_question(user_text)
        log_extra(log, "INFO", "Router finished", route=routed.route, dax_present=bool(routed.dax))

        if routed.route == "powerbi" and routed.dax:
            pbi_result = execute_powerbi_dax(routed.dax)

            if "error" in pbi_result:
                reply_text = f"⚠️ Ошибка выполнения запроса в Power BI: {pbi_result['error']}"
            else:
                formatted = formatter_service.format_answer(user_text, pbi_result)
                reply_text = formatted.reply
        else:
            reply_text = routed.message or "Не удалось обработать запрос."

        telegram_client.send_message(chat_id, reply_text, reply_markup=reply_markup)

    except Exception as e:
        log.error(f"Error in process_analytics_query for '{user_text}': {str(e)}", exc_info=True)
        telegram_client.send_message(
            chat_id,
            "❌ Ошибка при обработке запроса. Попробуйте позже.",
            reply_markup=reply_markup
        )

# ============================================================================
# АВТОРИЗАЦИЯ И SCHEDULER
# ============================================================================

def check_auth() -> bool:
    if not config.telegram.webhook_secret:
        return True
    header_secret = request.headers.get("X-Webhook-Secret", "")
    return hmac.compare_digest(header_secret, config.telegram.webhook_secret)


def send_hourly_stats():
    try:
        log.info("Starting hourly stats report...")
        if config.telegram.chat_id:
            process_static_action(config.telegram.chat_id, "sales_by_store_today")
        else:
            log.warning("Chat ID for hourly stats is not configured")
    except Exception as e:
        log.error(f"Error in send_hourly_stats: {str(e)}", exc_info=True)


scheduler = BackgroundScheduler(timezone="Europe/Moscow")
scheduler.add_job(send_hourly_stats, 'cron', hour='9-21', minute=0)
scheduler.start()

try:
    if config.telegram.chat_id:
        telegram_client.send_message(
            config.telegram.chat_id,
            "🔔 Сервис успешно запущен! Меню подключено.",
            get_main_reply_keyboard()
        )
except Exception as e:
    log.error(f"Failed to send startup message: {str(e)}")

# ============================================================================
# ЭНДПОИНТЫ
# ============================================================================

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "service": "powerbi-bot"
    }), 200


@app.route("/debug-env", methods=["GET"])
def debug_env():
    webhook = (os.getenv("PBI_WEBHOOK_URL") or os.getenv("MAKE_WEBHOOK_URL") or "").strip()
    return jsonify({
        "has_webhook": bool(webhook),
        "webhook_preview": webhook[:35] + "..." if webhook else "ОТСУТСТВУЕТ",
        "has_tenant_id": bool(os.getenv("PBI_TENANT_ID") or os.getenv("POWERBI_TENANT_ID")),
        "has_client_id": bool(os.getenv("PBI_CLIENT_ID") or os.getenv("POWERBI_CLIENT_ID")),
        "has_client_secret": bool(os.getenv("PBI_CLIENT_SECRET") or os.getenv("POWERBI_CLIENT_SECRET")),
        "has_dataset_id": bool(os.getenv("PBI_DATASET_ID") or os.getenv("POWERBI_DATASET_ID")),
    }), 200


@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    if not check_auth():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception as e:
        log.error(f"Failed to parse JSON: {str(e)}")
        return jsonify({"status": "error", "message": "Invalid JSON"}), 400
    
    # Обработка нажатий на Inline-кнопки
    if "callback_query" in data:
        try:
            callback = data["callback_query"]
            callback_id = callback.get("id")
            chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
            action = callback.get("data", "")
            
            if not callback_id or not chat_id:
                return jsonify({"status": "ok"})
            
            telegram_client.answer_callback_query(callback_id)
            
            # Перехватываем кнопки меню в статический DAX
            if action in STATIC_DAX_MAP:
                process_static_action(chat_id, action)
            else:
                process_analytics_query(chat_id, action)

            return jsonify({"status": "ok"})
        
        except Exception as e:
            log.error(f"Error processing callback_query: {str(e)}", exc_info=True)
            return jsonify({"status": "ok"})
    
    # Обработка обычных сообщений
    if "message" in data:
        try:
            msg = data["message"]
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = msg.get("text", "").strip()
            
            if not chat_id or not text:
                return jsonify({"status": "ok"})
            
            if text == "/start":
                telegram_client.send_message(
                    chat_id,
                    "Привет! Я аналитический бот. Выберите раздел меню ниже:",
                    get_main_reply_keyboard()
                )
                return jsonify({"status": "ok"})
            
            if text == "📊 Продажи":
                telegram_client.send_message(
                    chat_id,
                    "Выберите нужный отчет по продажам:",
                    get_sales_inline_keyboard()
                )
                return jsonify({"status": "ok"})
            
            if text == "💸 Расходы":
                telegram_client.send_message(
                    chat_id,
                    "Выберите нужный отчет по расходам:",
                    get_expenses_inline_keyboard()
                )
                return jsonify({"status": "ok"})
            
            if text == "💼 Зарплата":
                telegram_client.send_message(
                    chat_id,
                    "Выберите нужный отчет по зарплате:",
                    get_salary_inline_keyboard()
                )
                return jsonify({"status": "ok"})
            
            if text == "🎯 План и Итоги":
                telegram_client.send_message(
                    chat_id,
                    "Выберите нужный отчет по планам и итогам:",
                    get_plans_inline_keyboard()
                )
                return jsonify({"status": "ok"})
            
            # Свободный текстовый вопрос отправляем в GPT-роутер
            process_analytics_query(chat_id, text)
            return jsonify({"status": "ok"})
        
        except Exception as e:
            log.error(f"Error processing message: {str(e)}", exc_info=True)
            return jsonify({"status": "ok"})
    
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    log_extra(log, "INFO", "Starting bot server", port=config.port, debug=config.debug)
    app.run(host="0.0.0.0", port=config.port, debug=config.debug)
