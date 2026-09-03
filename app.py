import os
import json
import logging
import re
import hmac
import requests
from datetime import datetime

from flask import Flask, request, jsonify
from openai import OpenAI
from apscheduler.schedulers.background import BackgroundScheduler

from schema import build_schema_prompt, KNOWN_STORES

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("powerbi-bot")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Конфигурация из переменных окружения
# ---------------------------------------------------------------------------
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip(" '\"[]")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5").strip(" '\"[]")
OPENROUTER_SITE_URL = os.environ.get("OPENROUTER_SITE_URL", "").strip(" '\"[]")
OPENROUTER_SITE_NAME = os.environ.get("OPENROUTER_SITE_NAME", "PowerBI AI Assistant").strip(" '\"[]")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip(" '\"[]")

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
    timeout=20.0,
)

EXTRA_HEADERS = {}
if OPENROUTER_SITE_URL:
    EXTRA_HEADERS["HTTP-Referer"] = OPENROUTER_SITE_URL
if OPENROUTER_SITE_NAME:
    EXTRA_HEADERS["X-Title"] = OPENROUTER_SITE_NAME

SCHEMA_PROMPT = build_schema_prompt()

# ---------------------------------------------------------------------------
# Вспомогательная функция очистки токена
# ---------------------------------------------------------------------------
def clean_token(raw_token: str) -> str:
    """Извлекает чистый токен Telegram вида 123456:ABC..."""
    if not raw_token:
        return ""
    match = re.search(r"(\d+:[A-Za-z0-9_-]+)", raw_token)
    return match.group(1) if match else raw_token.strip(" '\"[]")

# ---------------------------------------------------------------------------
# Словарь замены кодов 1С
# ---------------------------------------------------------------------------
STORE_NAME_REPLACEMENTS = {
    "ОП_5_Анапа": "Пионерский",
    "ОП_5 Анапа": "Пионерский",
    "ОП 5 Анапа (Пионерский)": "Пионерский",
    "ОП 5 Анапа": "Пионерский",
    "Гебея озеро": "Озеро",
    "ОП_8_Утриш": "Утриш",
    "ОП_8 Утриш": "Утриш",
    "ОП 8 Утриш": "Утриш",
    "ОП_1 Анапа Ленинградская ул, д. 81": "Джемете",
    "ОП_1 Анапа Ленинградская": "Джемете",
    "ОП 1 Анапа Ленинградская": "Джемете",
    "Ленинградская 81": "Джемете",
    "Ленинградская": "Джемете",
    "Анапа": "Пионерский",
}

# ---------------------------------------------------------------------------
# Системные промпты
# ---------------------------------------------------------------------------
ROUTER_SYSTEM_PROMPT = f"""Ты — маршрутизатор и генератор DAX-запросов для Power BI бота магазинов.

Отвечай ТОЛЬКО валидным JSON, без markdown, без пояснений, без ```.

{SCHEMA_PROMPT}

ПРАВИЛА ОПРЕДЕЛЕНИЯ ПЕРИОДОВ И ДАТ:
- Запомни названия колонок с датами в таблицах:
  • В 'Продажи', 'Расходы', 'Чеки', 'Валовая приыбль', 'Выплаченое зп', 'Смены' колонка называется [Дата]
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

КОМПЛЕКСНЫЕ ЗАПРОСЫ И "ВСЁ СРАЗУ":
- Если пользователь просит "всё сразу", "полную аналитику" или несколько показателей одновременно, собирай их в ОДИН EVALUATE ROW с несколькими колонками:
  EVALUATE ROW(
    "Продажи", CALCULATE(SUM('Продажи'[Сумма]), ...),
    "Расходы", CALCULATE(SUM('Расходы'[Сумма]), ...),
    "ВаловаяПрибыль", CALCULATE(SUM('Валовая приыбль'[Валовая прибыль, руб.]), ...),
    "КолвоЧеков", CALCULATE(SUM('Чеки'[Кол-во чеков]), ...)
  )

КОНТЕКСТ И ИЗОЛЯЦИЯ МАГАЗИНОВ (КРИТИЧЕСКИ ВАЖНО):
- Учитывай историю диалога! Если пользователь пишет "а сравни с прошлым месяцем" или "а распиши расходы", смотри, о каком магазине шла речь ранее, и подставляй его код.
- Никогда не переспрашивай магазин, если он уже упоминался в истории переписки.
- Если пользователь спрашивает про конкретный магазин (например "Пионерский"):
  ВСЕ метрики в EVALUATE ROW (Продажи, Расходы, Прибыль, Чеки, Остатки, ЗП) ОБЯЗАНЫ содержать фильтр по коду этого магазина! Не мешай данные этого магазина с общими данными сети.
- При разбивке "по магазинам" не подставляй итоговые общие суммы сети к отдельным точкам. Каждая точка должна иметь свои изолированные CALCULATE().

ОБЩИЕ ПРАВИЛА DAX:
- Всегда используй шаблон: EVALUATE ROW("Result", <выражение>)
- Без фильтров: EVALUATE ROW("Result", SUM('Таблица'[Колонка]))
- С фильтрами: EVALUATE ROW("Result", CALCULATE(SUM('Таблица'[Колонка]), <условия через &&>))
- Если вопрос про зарплату за расчётный период — используй готовую меру EVALUATE ROW("Result", [Сумма Итого по ЗП]).
- Если вопрос про выручку для зарплаты (2%) и магазин Озеро или Джемете — используй 'Озеро+Джемете' в 'Выручки'.
- КРИТИЧЕСКИ ВАЖНО: всегда переводи человеческое название магазина в реальный код (например, "Джемете" -> "ОП_1 Анапа Ленинградская").
- КРИТИЧЕСКИ ВАЖНО: Если пользователь сначала спросил "че сегодня по продажам?", а затем пишет "распиши по магазинам", ты ОБЯЗАН сохранить фильтр по дате [Дата]=TODAY() и сгруппировать продажи по магазинам именно за сегодня!

Если вопрос относится к данным — верни:
{{"route": "powerbi", "message": "", "dax": "<готовый DAX-запрос>"}}

Если вопрос НЕ относится к данным — верни:
{{"route": "chat", "message": "<готовый ответ пользователю>", "dax": ""}}

ВАЖНО: ключи всегда route, message, dax — на английском."""


FORMAT_SYSTEM_PROMPT = """Ты — аккуратный персональный AI-ассистент владельца сети магазинов.

Твоя задача — принять сырые данные из Power BI (в формате JSON) и оформить чистый, визуально приятный ответ для Telegram.

СТРОГИЙ СЛОВАРЬ ЗАМЕНЫ КОДОВ:
В исходных данных из Power BI приходят технические названия из 1С. Ты ОБЯЗАН переводить их в человеческие:
• Вместо "ОП_5_Анапа" или "Анапа" — ВСЕГДА пиши "Пионерский"
• Вместо "Гебея озеро" — ВСЕГДА пиши "Озеро"
• Вместо "ОП_8_Утриш" — ВСЕГДА пиши "Утриш"
• Вместо "ОП_1 Анапа Ленинградская..." или "Ленинградская 81" — ВСЕГДА пиши "Джемете"

ПРАВИЛА ОФОРМЛЕНИЯ:
1. НИКОГДА не используй звёздочки (**текст** или *текст*) и решётки (#).
2. Используй структурированные списки с эмодзи (🏬, 💰, 💳, 📦, 📊, 📉).
3. Суммы разделяй пробелами (например: 1 147 125 ₽).
4. Запрещено приписывать одинаковые общие расходы к разным магазинам — если в JSON данные различаются или их нет, указывай точные цифры из JSON.

Пример ответа:
💰 Продажи за сегодня:

🏬 Пионерский: 18 500 ₽
🏬 Озеро: 14 130 ₽
🏬 Утриш: 7 000 ₽
🏬 Джемете: 5 000 ₽

💳 Итого: 44 630 ₽
"""

# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------
def check_auth() -> bool:
    if not WEBHOOK_SECRET:
        return True
    header_secret = request.headers.get("X-Webhook-Secret", "")
    return hmac.compare_digest(header_secret, WEBHOOK_SECRET)


def ask_router(question: str, history: list = None) -> dict:
    now = datetime.now()
    date_context = f"\nТЕКУЩАЯ ДАТА СЕРВЕРА: {now.strftime('%d.%m.%Y')}, Месяц: {now.month}, Год: {now.year}\n"
    dynamic_system_prompt = ROUTER_SYSTEM_PROMPT + date_context

    messages = [{"role": "system", "content": dynamic_system_prompt}]

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

    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if json_match:
        text = json_match.group(0)

    return json.loads(text)


def ask_formatter(question: str, powerbi_result) -> str:
    user_content = (
        f"Вопрос пользователя: {question}\n"
        f"Результат из Power BI (JSON): {json.dumps(powerbi_result, ensure_ascii=False)}"
    )
    resp = client.chat.completions.create(
        model=OPENROUTER_MODEL,
        max_tokens=1000,
        extra_headers=EXTRA_HEADERS,
        messages=[
            {"role": "system", "content": FORMAT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    )
    text = resp.choices[0].message.content.strip()
    text = text.replace("**", "").replace("*", "").replace("`", "")

    for code, friendly_name in STORE_NAME_REPLACEMENTS.items():
        text = text.replace(code, friendly_name)

    return text

# ---------------------------------------------------------------------------
# Клавиатуры Telegram (Reply + Inline)
# ---------------------------------------------------------------------------
def get_main_reply_keyboard():
    """Нижнее постоянное меню"""
    return {
        "keyboard": [
            [{"text": "📊 Продажи"}, {"text": "💸 Расходы"}],
            [{"text": "💼 Зарплата"}, {"text": "🎯 План и Итоги"}]
        ],
        "resize_keyboard": True,
        "is_persistent": True
    }

def get_sales_inline_keyboard():
    """Подменю продаж"""
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
    """Подменю расходов"""
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
    """Подменю зарплаты"""
    return {
        "inline_keyboard": [
            [
                {"text": "🧮 Расчётный период", "callback_data": "salary_period"},
                {"text": "👥 По сотрудникам", "callback_data": "salary_by_emp"}
            ]
        ]
    }

def get_plans_inline_keyboard():
    """Подменю планов и итогов"""
    return {
        "inline_keyboard": [
            [
                {"text": "🎯 Выполнение плана", "callback_data": "plan_status"},
                {"text": "🍷 Топ товаров за день", "callback_data": "top_products_today"}
            ]
        ]
    }

# ---------------------------------------------------------------------------
# Работа с Telegram API
# ---------------------------------------------------------------------------
def send_telegram_report(text: str, target_chat_id: str = None, reply_markup: dict = None):
    token = clean_token(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    chat_id = target_chat_id or os.environ.get("TELEGRAM_CHAT_ID", "").strip(" '\"[]")

    if not token or not chat_id:
        log.warning("Не заданы TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID!")
        return

  TOKEN = os.getenv("TELEGRAM_TOKEN")
url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

# Вызов запроса
response = requests.post(url, json=payload)
    payload = {
        "chat_id": chat_id,
        "text": text
    }

    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            log.info(f"Отправка в Telegram прошла успешно (200 OK) для чата {chat_id}")
        else:
            log.error(f"Ошибка Telegram API ({res.status_code}) для чата {chat_id}: {res.text}")
    except Exception as e:
        log.error(f"Ошибка соединения с Telegram: {e}")

def send_hourly_stats():
    """Фоновая задача: запуск авто-отчета по расписанию."""
    try:
        log.info("Запуск регулярного часового отчета по продажам...")
        routed = ask_router("продажи по магазинам за сегодня")
        dax_query = routed.get("dax", "")

        if not dax_query:
            log.warning("Маршрутизатор не вернул DAX для авто-отчета.")
            return

        send_telegram_report(
            text="📊 Отчет по продажам за час подготовлен.",
            reply_markup=get_sales_inline_keyboard()
        )

    except Exception as e:
        log.error(f"Ошибка при выполнении send_hourly_stats: {e}")


scheduler = BackgroundScheduler(timezone="Europe/Moscow")
scheduler.add_job(send_hourly_stats, 'cron', hour='9-21', minute=0)
scheduler.start()

send_telegram_report(
    text="🔔 Сервис успешно запущен! Меню подключено.",
    reply_markup=get_main_reply_keyboard()
)

# ---------------------------------------------------------------------------
# API эндпоинты Flask
# ---------------------------------------------------------------------------
@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    if not check_auth():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    data = request.get_json(force=True, silent=True) or {}
    token = clean_token(os.environ.get("TELEGRAM_BOT_TOKEN", ""))

    # 1. Обработка Inline-кнопок
    if "callback_query" in data:
        callback = data["callback_query"]
        callback_id = callback["id"]
        chat_id = callback["message"]["chat"]["id"]
        action = callback.get("data", "")

        if token:
            requests.post(
                f"[https://api.telegram.org/bot](https://api.telegram.org/bot){token}/answerCallbackQuery",
                json={"callback_query_id": callback_id}
            )

        query_map = {
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

        user_text = query_map.get(action, "Продажи за сегодня")
        routed = ask_router(user_text)

        reply_text = routed.get("message") or f"Сгенерирован DAX для '{user_text}':\n`{routed.get('dax', '')}`"
        send_telegram_report(text=reply_text, target_chat_id=chat_id)
        return jsonify({"status": "ok"})

    # 2. Обработка обычных сообщений
    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "").strip()

        if text == "/start":
            send_telegram_report(
                text="Привет! Я аналитический бот. Выберите раздел меню ниже:",
                target_chat_id=chat_id,
                reply_markup=get_main_reply_keyboard()
            )
            return jsonify({"status": "ok"})

        if text == "📊 Продажи":
            send_telegram_report(
                text="Выберите нужный отчет по продажам:",
                target_chat_id=chat_id,
                reply_markup=get_sales_inline_keyboard()
            )
            return jsonify({"status": "ok"})

        if text == "💸 Расходы":
            send_telegram_report(
                text="Выберите нужный отчет по расходам:",
                target_chat_id=chat_id,
                reply_markup=get_expenses_inline_keyboard()
            )
            return jsonify({"status": "ok"})

        if text == "💼 Зарплата":
            send_telegram_report(
                text="Выберите нужный отчет по зарплате:",
                target_chat_id=chat_id,
                reply_markup=get_salary_inline_keyboard()
            )
            return jsonify({"status": "ok"})

        if text == "🎯 План и Итоги":
            send_telegram_report(
                text="Выберите нужный отчет по планам и итогам:",
                target_chat_id=chat_id,
                reply_markup=get_plans_inline_keyboard()
            )
            return jsonify({"status": "ok"})

        # Произвольный вопрос пользователя
        routed = ask_router(text)
        reply_text = routed.get("message") or f"Запрос: {text}\nDAX:\n`{routed.get('dax', '')}`"

        send_telegram_report(text=reply_text, target_chat_id=chat_id)

    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
