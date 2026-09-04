# -*- coding: utf-8 -*-
"""
Главный точка входа приложения Flask: обработка вебхуков и связка сервисов.
"""

import os
import json
import requests
from flask import Flask, request, jsonify
from openai import OpenAI

from logger import setup_logger
from services import RouterService, FormatterService
from schema import KNOWN_STORES

log = setup_logger("app")

app = Flask(__name__)

# Загрузка конфигурации из переменных окружения
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet")

# Переменные для подключения Power BI REST API
PBI_TENANT_ID = os.getenv("PBI_TENANT_ID")
PBI_CLIENT_ID = os.getenv("PBI_CLIENT_ID")
PBI_CLIENT_SECRET = os.getenv("PBI_CLIENT_SECRET")
PBI_DATASET_ID = os.getenv("PBI_DATASET_ID")

# Инициализация клиентов
openai_client = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="[https://openrouter.ai/api/v1](https://openrouter.ai/api/v1)"
)

router_service = RouterService(openai_client=openai_client, model=OPENROUTER_MODEL)
formatter_service = FormatterService(
    openai_client=openai_client,
    model=OPENROUTER_MODEL,
    store_replacements=KNOWN_STORES
)


def check_auth(req: request) -> bool:
    """Проверка авторизации входящего вебхука."""
    if not WEBHOOK_SECRET:
        return True
    header_secret = req.headers.get("X-Webhook-Secret")
    return header_secret == WEBHOOK_SECRET


def send_telegram_message(chat_id: str, text: str) -> bool:
    """Отправка сообщения пользователю через Telegram Bot API."""
    url = f"[https://api.telegram.org/bot](https://api.telegram.org/bot){TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Ошибка отправки сообщения в Telegram: {str(e)}")
        # Резервная попытка отправки без parse_mode в случае ошибки разметки
        payload.pop("parse_mode", None)
        try:
            requests.post(url, json=payload, timeout=10)
            return True
        except Exception as ex:
            log.error(f"Критическая ошибка отправки в Telegram: {str(ex)}")
            return False


def execute_powerbi_dax(dax_query: str) -> dict:
    """Выполнение DAX-запроса через Power BI REST API."""
    if not all([PBI_TENANT_ID, PBI_CLIENT_ID, PBI_CLIENT_SECRET, PBI_DATASET_ID]):
        log.warning("Переменные Power BI API не заполнены в Environment")
        return {"error": "Power BI credentials missing"}

    try:
        # 1. Получение OAuth2 токена Azure AD
        token_url = f"[https://login.microsoftonline.com/](https://login.microsoftonline.com/){PBI_TENANT_ID}/oauth2/v2.0/token"
        token_data = {
            "grant_type": "client_credentials",
            "client_id": PBI_CLIENT_ID,
            "client_secret": PBI_CLIENT_SECRET,
            "scope": "[https://analysis.windows.net/powerbi/api/.default](https://analysis.windows.net/powerbi/api/.default)"
        }
        
        token_res = requests.post(token_url, data=token_data, timeout=10)
        token_res.raise_for_status()
        access_token = token_res.json().get("access_token")

        # 2. Выполнение запроса к датасету Power BI
        pbi_url = f"[https://api.powerbi.com/v1.0/myorg/datasets/](https://api.powerbi.com/v1.0/myorg/datasets/){PBI_DATASET_ID}/executeQueries"
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
        log.error(f"Ошибка выполнения Power BI API: {str(e)}", exc_info=True)
        return {"error": str(e)}


@app.route("/", methods=["GET", "HEAD"])
def healthcheck():
    """Эндпоинт проверки работоспособности сервиса."""
    return jsonify({"status": "ok", "service": "analytics-bot"}), 200


@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    """Основной обработчик входящих сообщений."""
    if not check_auth(request):
        log.warning("Отклонен неавторизованный запрос")
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    
    # Извлечение данных сообщения
    message = data.get("message") or data.get("post_data", {})
    if not message and "text" in data:
        message = data

    user_text = message.get("text", "").strip()
    chat_id = str(message.get("chat", {}).get("id") or message.get("chat_id") or "")

    if not user_text or not chat_id:
        return jsonify({"status": "ignored", "reason": "Empty text or chat_id"}), 200

    log.info(f"Получено сообщение: '{user_text}' от chat_id: {chat_id}")

    try:
        # Шаг 1: Маршрутизация и получение DAX через RouterService
        router_res = router_service.route_question(question=user_text)
        log.info(f"Результат роутера: route={router_res.route}")

        if router_res.route == "chat":
            # Не требует обращения к базе данных
            send_telegram_message(chat_id, router_res.message)
            return jsonify({"status": "success", "route": "chat"}), 200

        elif router_res.route == "powerbi":
            # Шаг 2: Выполнение сгенерированного DAX в Power BI API
            log.info(f"Выполнение DAX-запроса: {router_res.dax}")
            pbi_result = execute_powerbi_dax(router_res.dax)

            # Шаг 3: Передача сырых данных в FormatterService
            formatter_res = formatter_service.format_answer(
                question=user_text,
                powerbi_result=pbi_result
            )

            # Шаг 4: Отправка готового человекочитаемого ответа в Telegram
            send_telegram_message(chat_id, formatter_res.reply)
            return jsonify({"status": "success", "route": "powerbi"}), 200

        else:
            send_telegram_message(chat_id, "Не удалось определить тип запроса.")
            return jsonify({"status": "unknown_route"}), 200

    except Exception as e:
        log.error(f"Ошибка при обработке запроса: {str(e)}", exc_info=True)
        send_telegram_message(chat_id, "⚠️ Произошла ошибка при получении данных. Попробуйте позже.")
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
