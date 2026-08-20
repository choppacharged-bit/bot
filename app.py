# -*- coding: utf-8 -*-
"""
PowerBI AI-ассистент — backend-сервис.

Принимает вопрос пользователя (обычно из Telegram через Make), сам решает
относится ли вопрос к продажам/бизнес-данным, при необходимости генерирует
DAX-запрос по схеме модели (см. schema.py), выполняет его через Power BI
Execute Queries REST API, и формулирует человеческий ответ через Claude.

Запуск локально:
    pip install -r requirements.txt --break-system-packages
    cp .env.example .env   # и заполнить значения
    python app.py

Деплой — см. README.md (рекомендуется Render.com, бесплатный Web Service).
"""

import os
import json
import logging

import requests
from flask import Flask, request, jsonify
from anthropic import Anthropic

from schema import build_schema_prompt, KNOWN_STORES

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("powerbi-bot")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Конфигурация из переменных окружения
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

POWERBI_TENANT_ID = os.environ["POWERBI_TENANT_ID"]
POWERBI_CLIENT_ID = os.environ["POWERBI_CLIENT_ID"]
POWERBI_CLIENT_SECRET = os.environ["POWERBI_CLIENT_SECRET"]
POWERBI_DATASET_ID = os.environ["POWERBI_DATASET_ID"]

# Общий секретный токен, чтобы Make (или кто угодно) не мог дёргать
# ваш вебхук без авторизации. Придумайте любую длинную случайную строку
# и укажите её и здесь, и в заголовке запроса из Make.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

SCHEMA_PROMPT = build_schema_prompt()

ROUTER_SYSTEM_PROMPT = f"""Ты — маршрутизатор и генератор DAX-запросов для Power BI бота магазинов.

Отвечай ТОЛЬКО валидным JSON, без markdown, без пояснений, без ```.

{SCHEMA_PROMPT}

Правила генерации DAX:
- Всегда используй шаблон: EVALUATE ROW("Result", <выражение>)
- Без фильтров: EVALUATE ROW("Result", SUM('Таблица'[Колонка]))
- С фильтрами: EVALUATE ROW("Result", CALCULATE(SUM('Таблица'[Колонка]), <условия через &&>))
- период "сегодня" -> [Дата]=TODAY()
- период "вчера" -> [Дата]=TODAY()-1
- период "этот месяц" -> MONTH([Дата])=MONTH(TODAY()) && YEAR([Дата])=YEAR(TODAY())
- период "прошлый месяц" -> MONTH([Дата])=MONTH(EDATE(TODAY(),-1)) && YEAR([Дата])=YEAR(EDATE(TODAY(),-1))
- Если вопрос про зарплату за расчётный период — используй готовую меру
  EVALUATE ROW("Result", [Сумма Итого по ЗП]) вместо ручного пересчёта.
- Если вопрос про выручку для зарплаты (2%) и магазин Озеро или Джемете —
  используй объединённое значение 'Озеро+Джемете' в таблице 'Выручки',
  а не считай магазины по отдельности.
- Никогда не придумывай магазины, которых нет в списке известных: {', '.join(KNOWN_STORES)}.
- Если магазин в вопросе не входит в этот список — верни route "chat" с вежливым
  уточнением, что такого магазина нет.

Если вопрос относится к продажам/расходам/долгам/зарплате/остаткам — верни:
{{"route": "powerbi", "message": "", "dax": "<готовый DAX-запрос>"}}

Если вопрос НЕ относится к данным (обычный разговор) — верни:
{{"route": "chat", "message": "<готовый ответ пользователю>", "dax": ""}}

ВАЖНО: ключи всегда route, message, dax — на английском, без вариаций."""

FORMAT_SYSTEM_PROMPT = """Ты — персональный AI-помощник владельца сети магазинов.

Получаешь исходный вопрос пользователя и уже посчитанный результат из Power BI
(в виде JSON). Твоя задача — ответить естественно и дружелюбно, назвав
итоговую цифру из результата.

Правила:
- не отвечай как робот, используй разговорный русский;
- отвечай коротко, 1-3 предложения;
- не придумывай данные, используй только переданное значение;
- если результат 0, null или данных нет — так и скажи: "Данных за этот период не найдено."

Никогда не пиши "Согласно данным", "Результат запроса", "В базе данных", "Аналитика показывает" —
общайся как живой человек.
"""


def get_powerbi_token() -> str:
    """Получает OAuth-токен для Power BI через service principal (client credentials)."""
    url = f"https://login.microsoftonline.com/{POWERBI_TENANT_ID}/oauth2/v2.0/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": POWERBI_CLIENT_ID,
        "client_secret": POWERBI_CLIENT_SECRET,
        "scope": "https://analysis.windows.net/powerbi/api/.default",
    }
    resp = requests.post(url, data=data, timeout=15)
    resp.raise_for_status()
    return resp.json()["access_token"]


def ask_router(question: str) -> dict:
    """Первый вызов Claude: решает маршрут и генерирует DAX."""
    resp = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=ROUTER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": question}],
    )
    text = resp.content[0].text.strip()
    # На случай если модель всё же обернёт в ```json ... ```
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


def run_dax(dax_query: str) -> dict:
    """Выполняет DAX-запрос через Power BI Execute Queries REST API."""
    token = get_powerbi_token()
    url = f"https://api.powerbi.com/v1.0/myorg/datasets/{POWERBI_DATASET_ID}/executeQueries"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {
        "queries": [{"query": dax_query}],
        "serializerSettings": {"includeNulls": True},
    }
    resp = requests.post(url, headers=headers, json=body, timeout=30)
    if not resp.ok:
        log.error("Power BI error %s: %s", resp.status_code, resp.text)
        resp.raise_for_status()
    return resp.json()


def ask_formatter(question: str, powerbi_result: dict) -> str:
    """Второй вызов Claude: превращает сырой результат в дружелюбный ответ."""
    user_content = (
        f"Вопрос пользователя: {question}\n"
        f"Результат из Power BI (JSON): {json.dumps(powerbi_result, ensure_ascii=False)}"
    )
    resp = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=FORMAT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    return resp.content[0].text.strip()


@app.route("/webhook", methods=["POST"])
def webhook():
    if WEBHOOK_SECRET:
        if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
            return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(force=True, silent=True) or {}
    question = (payload.get("message") or "").strip()
    if not question:
        return jsonify({"error": "no message provided"}), 400

    try:
        routed = ask_router(question)
    except Exception:
        log.exception("Router call failed")
        return jsonify({"reply": "Не смог разобрать вопрос, попробуйте переформулировать."})

    if routed.get("route") != "powerbi":
        return jsonify({"reply": routed.get("message") or "Не совсем понял вопрос."})

    dax = routed.get("dax", "")
    if not dax:
        return jsonify({"reply": "Не смог составить запрос к данным по этому вопросу."})

    try:
        powerbi_result = run_dax(dax)
    except Exception:
        log.exception("Power BI call failed. DAX was: %s", dax)
        return jsonify({"reply": "Не удалось получить данные из Power BI, попробуйте ещё раз."})

    try:
        reply = ask_formatter(question, powerbi_result)
    except Exception:
        log.exception("Formatter call failed")
        reply = "Получил данные, но не смог красиво их оформить. Попробуйте ещё раз."

    return jsonify({"reply": reply})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
