# -*- coding: utf-8 -*-
"""
PowerBI AI-ассистент — backend-сервис (упрощённая версия).

ВАЖНО: этот сервис НЕ обращается к Power BI напрямую и не требует
service principal / прав администратора тенанта. Вызов к Power BI
остаётся внутри Make, где OAuth-подключение уже настроено и работает.

Сервис отвечает только за две вещи, которые в Make было неудобно делать:

1. POST /generate-dax
   Принимает вопрос пользователя, решает, относится ли он к данным,
   и если да — генерирует готовый DAX-запрос по схеме модели
   (см. schema.py). Make сам вызывает Power BI с этим DAX-запросом.

2. POST /format-answer
   Принимает исходный вопрос и уже посчитанный Power BI результат,
   возвращает дружелюбный текстовый ответ для отправки в Telegram.

Модель вызывается через OpenRouter (https://openrouter.ai) — единый
OpenAI-совместимый API, через который можно достучаться до моделей
разных провайдеров (Anthropic, OpenAI, Google и т.д.) по одному ключу.

Запуск локально:
    pip install -r requirements.txt --break-system-packages
    cp .env.example .env   # и заполнить значения
    python app.py

Деплой — см. README.md (рекомендуется Render.com, бесплатный Web Service).
"""

import os
import json
import logging

from flask import Flask, request, jsonify
from openai import OpenAI

from schema import build_schema_prompt, KNOWN_STORES

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("powerbi-bot")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Конфигурация из переменных окружения
# ---------------------------------------------------------------------------
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

# Модель, которую дёргаем через OpenRouter. Можно поменять на любую другую
# из каталога https://openrouter.ai/models, не трогая код.
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5")

# Необязательные заголовки, которые OpenRouter использует для атрибуции
# приложения в своей статистике/лидербордах (можно оставить пустыми).
OPENROUTER_SITE_URL = os.environ.get("OPENROUTER_SITE_URL", "")
OPENROUTER_SITE_NAME = os.environ.get("OPENROUTER_SITE_NAME", "PowerBI AI Assistant")

# Общий секретный токен, чтобы Make (или кто угодно) не мог дёргать
# ваш вебхук без авторизации. Придумайте любую длинную случайную строку
# и укажите её и здесь, и в заголовке запроса из Make.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

EXTRA_HEADERS = {}
if OPENROUTER_SITE_URL:
    EXTRA_HEADERS["HTTP-Referer"] = OPENROUTER_SITE_URL
if OPENROUTER_SITE_NAME:
    EXTRA_HEADERS["X-Title"] = OPENROUTER_SITE_NAME

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
- КРИТИЧЕСКИ ВАЖНО: когда фильтруешь ЛЮБУЮ таблицу (Продажи, Расходы, Чеки,
  Валовая приыбль и т.д.) по конкретному магазину, НИКОГДА не подставляй в DAX
  человеческое название магазина напрямую (например [Магазин]="Джемете" —
  это НЕПРАВИЛЬНО и вернёт пустой результат). Вместо этого сначала переведи
  человеческое название в реальный код через таблицу соответствий выше, и уже
  этот код подставляй в фильтр (например [Магазин]="ОП_1 Анапа Ленинградская" для Джемете).
- Объединение "Озеро+Джемете" действует ТОЛЬКО для таблицы 'Выручки' при расчёте
  зарплаты (2%). Во всех остальных таблицах (Продажи, Расходы, Остатки и т.д.)
  Озеро и Джемете — это РАЗНЫЕ точки с разными кодами, считай их раздельно.

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


def check_auth():
    if WEBHOOK_SECRET:
        if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
            return False
    return True


def ask_router(question: str) -> dict:
    """Вызов модели через OpenRouter: решает маршрут и генерирует DAX по вопросу пользователя."""
    resp = client.chat.completions.create(
        model=OPENROUTER_MODEL,
        max_tokens=1000,
        extra_headers=EXTRA_HEADERS,
        messages=[
            {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
    )
    text = resp.choices[0].message.content.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


def ask_formatter(question: str, powerbi_result) -> str:
    """Вызов модели через OpenRouter: превращает сырой результат Power BI в дружелюбный ответ."""
    user_content = (
        f"Вопрос пользователя: {question}\n"
        f"Результат из Power BI (JSON): {json.dumps(powerbi_result, ensure_ascii=False)}"
    )
    resp = client.chat.completions.create(
        model=OPENROUTER_MODEL,
        max_tokens=500,
        extra_headers=EXTRA_HEADERS,
        messages=[
            {"role": "system", "content": FORMAT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    )
    return resp.choices[0].message.content.strip()


@app.route("/generate-dax", methods=["POST"])
def generate_dax():
    """
    Вход:  {"message": "сумма продаж сегодня"}
    Выход: {"route": "powerbi"|"chat", "message": "...", "dax": "..."}

    Make дальше сам смотрит на "route":
      - если "powerbi" — берёт "dax" и вызывает свой уже рабочий модуль
        Power BI Execute Queries;
      - если "chat" — сразу отправляет "message" пользователю, Power BI
        трогать не нужно.
    """
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(force=True, silent=True) or {}
    question = (payload.get("message") or "").strip()
    if not question:
        return jsonify({"error": "no message provided"}), 400

    try:
        routed = ask_router(question)
    except Exception:
        log.exception("Router call failed")
        return jsonify(
            {"route": "chat", "message": "Не смог разобрать вопрос, попробуйте переформулировать.", "dax": ""}
        )

    return jsonify(routed)


@app.route("/format-answer", methods=["POST"])
def format_answer():
    """
    Вход:  {"message": "сумма продаж сегодня", "powerbi_result": {...}}
    Выход: {"reply": "Сегодня продали на 45 000 ₽"}
    """
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(force=True, silent=True) or {}
    question = (payload.get("message") or "").strip()
    powerbi_result = payload.get("powerbi_result")

    if not question or powerbi_result is None:
        return jsonify({"error": "message and powerbi_result are required"}), 400

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


def check_auth():
    if WEBHOOK_SECRET:
        if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
            return False
    return True


def ask_router(question: str) -> dict:
    """Вызов модели через OpenRouter: решает маршрут и генерирует DAX по вопросу пользователя."""
    resp = client.chat.completions.create(
        model=OPENROUTER_MODEL,
        max_tokens=1000,
        extra_headers=EXTRA_HEADERS,
        messages=[
            {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
    )
    text = resp.choices[0].message.content.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


def ask_formatter(question: str, powerbi_result) -> str:
    """Вызов модели через OpenRouter: превращает сырой результат Power BI в дружелюбный ответ."""
    user_content = (
        f"Вопрос пользователя: {question}\n"
        f"Результат из Power BI (JSON): {json.dumps(powerbi_result, ensure_ascii=False)}"
    )
    resp = client.chat.completions.create(
        model=OPENROUTER_MODEL,
        max_tokens=500,
        extra_headers=EXTRA_HEADERS,
        messages=[
            {"role": "system", "content": FORMAT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    )
    return resp.choices[0].message.content.strip()


@app.route("/generate-dax", methods=["POST"])
def generate_dax():
    """
    Вход:  {"message": "сумма продаж сегодня"}
    Выход: {"route": "powerbi"|"chat", "message": "...", "dax": "..."}

    Make дальше сам смотрит на "route":
      - если "powerbi" — берёт "dax" и вызывает свой уже рабочий модуль
        Power BI Execute Queries;
      - если "chat" — сразу отправляет "message" пользователю, Power BI
        трогать не нужно.
    """
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(force=True, silent=True) or {}
    question = (payload.get("message") or "").strip()
    if not question:
        return jsonify({"error": "no message provided"}), 400

    try:
        routed = ask_router(question)
    except Exception:
        log.exception("Router call failed")
        return jsonify(
            {"route": "chat", "message": "Не смог разобрать вопрос, попробуйте переформулировать.", "dax": ""}
        )

    return jsonify(routed)


@app.route("/format-answer", methods=["POST"])
def format_answer():
    """
    Вход:  {"message": "сумма продаж сегодня", "powerbi_result": {...}}
    Выход: {"reply": "Сегодня продали на 45 000 ₽"}
    """
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(force=True, silent=True) or {}
    question = (payload.get("message") or "").strip()
    powerbi_result = payload.get("powerbi_result")

    if not question or powerbi_result is None:
        return jsonify({"error": "message and powerbi_result are required"}), 400

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
