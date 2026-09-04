#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PowerBI AI-ассистент — Telegram бот для аналитики.
Улучшенная версия с модульной архитектурой и полной интеграцией Power BI API.

Архитектура:
- config.py: конфигурация из переменных окружения
- logger.py: структурированное JSON логирование
- utils.py: вспомогательные функции
- services.py: бизнес-логика (RouterService, FormatterService)
- telegram_client.py: работа с Telegram API
- schema.py: схема данных Power BI
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

# Импортируем наши модули
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

# Инициализируем клиент OpenRouter
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

# Инициализируем сервисы
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
# КЛАВИАТУРЫ TELEGRAM
# ============================================================================

def get_main_reply_keyboard():
    """Главное меню бота."""
    return {
        "keyboard": [
            [{"text": "📊 Продажи"}, {"text": "💸 Расходы"}],
            [{"text": "💼 Зарплата"}, {"text": "🎯 План и Итоги"}]
        ],
        "resize_keyboard": True,
        "is_persistent": True
    }


def get_sales_inline_keyboard():
    """Меню для продаж."""
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
    """Меню для расходов."""
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
    """Меню для зарплаты."""
    return {
        "inline_keyboard": [
            [
                {"text": "🧮 Расчётный период", "callback_data": "salary_period"},
                {"text": "👥 По сотрудникам", "callback_data": "salary_by_emp"}
            ]
        ]
    }


def get_plans_inline_keyboard():
    """Меню для плана и итогов."""
    return {
        "inline_keyboard": [
            [
                {"text": "🎯 Выполнение плана", "callback_data": "plan_status"},
                {"text": "🍷 Топ товаров за день", "callback_data": "top_products_today"}
            ]
        ]
    }

# ============================================================================
# ИНТЕГРАЦИЯ POWER BI REST API
# ============================================================================

def execute_powerbi_dax(dax_query: str) -> Dict[str, Any]:
    """Выполнение DAX-запроса через Power BI REST API."""
    tenant_id = os.getenv("PBI_TENANT_ID")
    client_id = os.getenv("PBI_CLIENT_ID")
    client_secret = os.getenv("PBI_CLIENT_SECRET")
    dataset_id = os.getenv("PBI_DATASET_ID")

    if not all([tenant_id, client_id, client_secret, dataset_id]):
        log.warning("Power BI API environment variables are not fully configured")
        return {"error": "Power BI credentials missing"}

    try:
        # 1. Получение OAuth2 токена доступа Azure AD
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

        # 2. Выполнение DAX-запроса в датасет Power BI
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
        pbi_res.raise_for_status()
        return pbi_res.json()

    except Exception as e:
        log.error(f"Error executing Power BI REST API query: {str(e)}", exc_info=True)
        return {"error": str(e)}

# ============================================================================
# ЕДИНЫЙ КОНВЕЙЕР ОБРАБОТКИ ЗАПРОСОВ
# ============================================================================

def process_analytics_query(
    chat_id: str,
    user_text: str,
    reply_markup: Optional[Dict[str, Any]] = None
) -> None:
    """Сквозная функция обработки пользовательского запроса."""
    try:
        # Шаг 1: Маршрутизация и генерация DAX через RouterService
        routed = router_service.route_question(user_text)
        log_extra(log, "INFO", "Router finished", route=routed.route, dax_present=bool(routed.dax))

        if routed.route == "powerbi" and routed.dax:
            # Шаг 2: Выполнение DAX в Power BI API
            pbi_result = execute_powerbi_dax(routed.dax)

            if "error" in pbi_result:
                reply_text = f"⚠️ Ошибка выполнения запроса в Power BI: {pbi_result['error']}"
            else:
                # Шаг 3: Форматирование данных через FormatterService
                formatted = formatter_service.format_answer(user_text, pbi_result)
                reply_text = formatted.reply
        else:
            # Обычный диалоговый ответ
            reply_text = routed.message or "Не удалось обработать запрос."

        # Шаг 4: Отправка итогового сообщения пользователю
        telegram_client.send_message(chat_id, reply_text, reply_markup=reply_markup)

    except Exception as e:
        log.error(f"Error in process_analytics_query for '{user_text}': {str(e)}", exc_info=True)
        telegram_client.send_message(
            chat_id,
            "❌ Ошибка при обработке запроса. Попробуйте позже.",
            reply_markup=reply_markup
        )

# ============================================================================
# ПРОВЕРКА АВТОРИЗАЦИИ
# ============================================================================

def check_auth() -> bool:
    """Проверяет аутентификацию webhook'а."""
    if not config.telegram.webhook_secret:
        return True
    
    header_secret = request.headers.get("X-Webhook-Secret", "")
    return hmac.compare_digest(header_secret, config.telegram.webhook_secret)

# ============================================================================
# АВТОМАТИЧЕСКИЕ ОТЧЕТЫ
# ============================================================================

def send_hourly_stats():
    """Отправляет часовой отчет по продажам."""
    try:
        log.info("Starting hourly stats report...")
        if config.telegram.chat_id:
            process_analytics_query(
                config.telegram.chat_id,
                "Продажи по магазинам за сегодня",
                reply_markup=get_sales_inline_keyboard()
            )
            log_extra(log, "INFO", "Hourly stats sent", chat_id=config.telegram.chat_id)
        else:
            log.warning("Chat ID for hourly stats is not configured")
    except Exception as e:
        log.error(f"Error in send_hourly_stats: {str(e)}", exc_info=True)

# ============================================================================
# ЗАПУСК SCHEDULER
# ============================================================================

scheduler = BackgroundScheduler(timezone="Europe/Moscow")
scheduler.add_job(send_hourly_stats, 'cron', hour='9-21', minute=0)
scheduler.start()

# Отправляем сообщение о запуске при старте
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
# API ЭНДПОИНТЫ
# ============================================================================

@app.route("/health", methods=["GET"])
def health():
    """Healthcheck эндпоинт."""
    return jsonify({
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "service": "powerbi-bot"
    }), 200


@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    """Главный webhook для обработки сообщений из Telegram или Make."""
    
    # Проверяем авторизацию
    if not check_auth():
        log_extra(log, "WARNING", "Unauthorized webhook request", ip=request.remote_addr)
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception as e:
        log.error(f"Failed to parse JSON: {str(e)}")
        return jsonify({"status": "error", "message": "Invalid JSON"}), 400
    
    # ========================================================================
    # ОБРАБОТКА CALLBACK QUERIES (INLINE КНОПКИ)
    # ========================================================================
    
    if "callback_query" in data:
        try:
            callback = data["callback_query"]
            callback_id = callback.get("id")
            chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
            action = callback.get("data", "")
            
            if not callback_id or not chat_id:
                log.error("Invalid callback_query structure")
                return jsonify({"status": "ok"})
            
            log_extra(
                log,
                "INFO",
                "Callback query received",
                chat_id=chat_id,
                action=action,
            )
            
            # Отвечаем на callback в Telegram API
            telegram_client.answer_callback_query(callback_id)
            
            # Маппируем actions на человекочитаемые запросы
            action_map = {
                "sales_by_store_today": "Продажи по магазинам за сегодня",
                "sales_yesterday": "Продажи за вчера по магазинам",
                "sales_month": "Продажи за этот месяц по магазинам",
                "sales_pionersky": "Продажи магазина Пионерский за сегодня",
                "sales_ozero": "Продажи магазина Озеро за сегодня",
                "sales_utrish": "Продажи магазина Утриш за сегодня",
                "sales_dzhemete": "Продажи магазина Джемете за сегодня",
                "expenses_by_store": "Расходы по магазинам за этот месяц",
                "expenses_by_category": "Расходы по статьям за этот месяц",
                "expenses_today": "Расходы за сегодня",
                "expenses_month": "Расходы за этот месяц всего",
                "salary_period": "Зарплата за текущий расчётный период",
                "salary_by_emp": "Зарплата по сотрудникам за этот месяц",
                "plan_status": "Выполнение плана по магазинам за сегодня",
                "top_products_today": "Топ 5 продаваемых товаров за сегодня"
            }
            
            user_text = action_map.get(action, "Продажи за сегодня")
            
            # Запускаем полный цикл обработки через Power BI API и FormatterService
            process_analytics_query(chat_id, user_text)
            return jsonify({"status": "ok"})
        
        except Exception as e:
            log.error(f"Error processing callback_query: {str(e)}", exc_info=True)
            return jsonify({"status": "ok"})
    
    # ========================================================================
    # ОБРАБОТКА ОБЫЧНЫХ СООБЩЕНИЙ
    # ========================================================================
    
    if "message" in data:
        try:
            msg = data["message"]
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = msg.get("text", "").strip()
            
            if not chat_id or not text:
                return jsonify({"status": "ok"})
            
            log_extra(log, "INFO", "Message received", chat_id=chat_id, text=text[:50])
            
            # Обработка главных команд и клавиатур меню
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
            
            # Произвольный текстовый вопрос или аналитический запрос
            process_analytics_query(chat_id, text)
            return jsonify({"status": "ok"})
        
        except Exception as e:
            log.error(f"Error processing message: {str(e)}", exc_info=True)
            return jsonify({"status": "ok"})
    
    return jsonify({"status": "ok"})


@app.errorhandler(404)
def not_found(error):
    """404 handler."""
    return jsonify({"status": "error", "message": "Not found"}), 404


@app.errorhandler(500)
def internal_error(error):
    """500 handler."""
    log.error(f"Internal server error: {str(error)}", exc_info=True)
    return jsonify({"status": "error", "message": "Internal server error"}), 500

# ============================================================================
# ЗАПУСК ПРИЛОЖЕНИЯ
# ============================================================================

if __name__ == "__main__":
    log_extra(log, "INFO", "Starting bot server", port=config.port, debug=config.debug)
    app.run(host="0.0.0.0", port=config.port, debug=config.debug)
