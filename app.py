import os
import json
import logging
import re
import hmac

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
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5")
OPENROUTER_SITE_URL = os.environ.get("OPENROUTER_SITE_URL", "")
OPENROUTER_SITE_NAME = os.environ.get("OPENROUTER_SITE_NAME", "PowerBI AI Assistant")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
    timeout=20.0, # Таймаут 20 сек
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

КОМПЛЕКСНЫЕ ЗАПРОСЫ И "ВСЁ СРАЗУ":
- Если пользователь просит "всё сразу", "полную аналитику" или несколько показателей одновременно, собирай их в ОДИН EVALUATE ROW с несколькими колонками:
  EVALUATE ROW(
    "Продажи", CALCULATE(SUM('Продажи'[Сумма]), ...),
    "Расходы", CALCULATE(SUM('Расходы'[Сумма]), ...),
    "ВаловаяПрибыль", CALCULATE(SUM('Валовая приыбль'[Валовая прибыль, руб.]), ...),
    "КолвоЧеков", CALCULATE(SUM('Чеки'[Кол-во чеков]), ...)
  )

КОНТЕКСТ ДИАЛОГА:
- Учитывай историю диалога! Если пользователь пишет "а сравни с прошлым месяцем" или "а что по расходы?", смотри, о каком магазине шла речь в предыдущих сообщениях, и подставляй его код.
- Никогда не переспрашивай магазин, если он уже упоминался ранее в истории контекста.

ОБЩИЕ ПРАВИЛА:
- Если вопрос про зарплату за расчётный период — используй готовую меру EVALUATE ROW("Result", [Сумма Итого по ЗП]).
- Если вопрос про выручку для зарплаты (2%) и магазин Озеро или Джемете — используй 'Озеро+Джемете' в 'Выручки'.
- Никогда не придумывай магазины, которых нет в списке: {', '.join(KNOWN_STORES)}.
- КРИТИЧЕСКИ ВАЖНО: всегда переводи название магазина в код (например, "Джемете" -> "ОП_1 Анапа Ленинградская").

Если вопрос относится к данным — верни:
{{"route": "powerbi", "message": "", "dax": "<готовый DAX-запрос>"}}

Если вопрос НЕ относится к данным — верни:
{{"route": "chat", "message": "<готовый ответ пользователю>", "dax": ""}}

ВАЖНО: ключи всегда route, message, dax — на английском."""

FORMAT_SYSTEM_PROMPT = """Ты — персональный AI-помощник владельца сети магазинов.

Получаешь исходный вопрос пользователя и уже посчитанный результат из Power BI (в виде JSON). Твоя задача — ответить естественно и дружелюбно, назвав итоговые цифры.

Правила:
- не отвечай как робот, используй разговорный русский;
- отвечай коротко, 1-3 предложения;
- не придумывай данные, используй только переданное значение;
- если результат 0, null или данных нет — так и скажи: "Данных за этот период не найдено."

Никогда не пиши "Согласно данным", "Результат запроса", "В базе данных" — общайся как живой человек.
"""


def check_auth():
    if WEBHOOK_SECRET:
        header_secret = request.headers.get("X-Webhook-Secret", "")
        return hmac.compare_digest(header_secret, WEBHOOK_SECRET)
    return True


def ask_router(question: str, history: list = None) -> dict:
    """Вызов модели через OpenRouter с учетом истории сообщений."""
    messages = [{"role": "system", "content": ROUTER_SYSTEM_PROMPT}]

    # Добавляем историю переписки, если Make её передал
    if history and isinstance(history, list):
        for msg in history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if content:
                messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": question})

    resp = client.chat.completions.create(
        model=OPENROUTER_MODEL,
        max_tokens=1000,
        extra_headers=EXTRA_HEADERS,
        messages=messages,
    )
    text = resp.choices[0].message.content.strip()

    # Надежно извлекаем JSON из ответа
    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if json_match:
        text = json_match.group(0)

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
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(force=True, silent=True) or {}
    question = (payload.get("message") or "").strip()
    history = payload.get("history", [])

    if not question:
        return jsonify({"error": "no message provided"}), 400

    try:
        routed = ask_router(question, history)
    except Exception:
        log.exception("Router call failed")
        return jsonify(
            {
                "route": "chat",
                "message": "Не смог разобрать вопрос, попробуйте переформулировать.",
                "dax": "",
            }
        )

    return jsonify(routed)


@app.route("/format-answer", methods=["POST"])
def format_answer():
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
