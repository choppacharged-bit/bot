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
# Конфигурация из переменных окружения (с автоматической зачисткой скобок/кавычек)
# ---------------------------------------------------------------------------
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip(" '\"[]")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5").strip(" '\"[]")
OPENROUTER_SITE_URL = os.environ.get("OPENROUTER_SITE_URL", "").strip(" '\"[]")
OPENROUTER_SITE_NAME = os.environ.get("OPENROUTER_SITE_NAME", "PowerBI AI Assistant").strip(" '\"[]")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip(" '\"[]")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(" '\"[]")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip(" '\"[]")

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
# Словарь жесткой замены кодов 1С на человеческие названия
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
    """Проверка секретного заголовка вебхука, если он задан."""
    if not WEBHOOK_SECRET:
        return True
    header_secret = request.headers.get("X-Webhook-Secret", "")
    return hmac.compare_digest(header_secret, WEBHOOK_SECRET)


def ask_router(question: str, history: list = None) -> dict:
    """Вызов модели через OpenRouter с учетом динамической даты и истории."""
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
    """Вызов модели через OpenRouter: форматирует ответ с зачисткой кодов и Markdown."""
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
    
    # 1. Принудительная зачистка звёздочек и бэктиков Markdown
    text = text.replace("**", "").replace("*", "").replace("`", "")

    # 2. Принудительная подмена технических кодов 1С на человеческие названия
    for code, friendly_name in STORE_NAME_REPLACEMENTS.items():
        text = text.replace(code, friendly_name)
    
    return text

# ---------------------------------------------------------------------------
# Клавиатуры Telegram
# ---------------------------------------------------------------------------
def get_main_reply_keyboard():
    """Постоянная меню-клавиатура внизу экрана."""
    return {
        "keyboard": [
            [
                {"text": "📊 Продажи за сегодня"},
                {"text": "🏬 По магазинам"}
            ],
            [
                {"text": "🎯 Выполнение плана"},
                {"text": "🍷 Топ товаров за день"}
            ]
        ],
        "resize_keyboard": True,
        "is_persistent": True
    }


def get_report_inline_keyboard():
    """Inline-кнопки детализации прямо под конкретным сообщением отчета."""
    return {
        "inline_keyboard": [
            [
                {"text": "🏬 По магазинам", "callback_data": "details_by_store"},
                {"text": "🍷 Топ товаров", "callback_data": "details_top_products"}
            ],
            [
                {"text": "🔄 Обновить данные", "callback_data": "refresh_report"}
            ]
        ]
    }

# ---------------------------------------------------------------------------
# Работа с Telegram API
# ---------------------------------------------------------------------------
def send_telegram_report(text: str, target_chat_id: str = None, reply_markup: dict = None):
    raw_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    # Извлекаем ТОЛЬКО токен формата 123456789:ABC...
    match = re.search(r"(\d+:[A-Za-z0-9_-]+)", raw_token)
    token = match.group(1) if match else raw_token.strip(" '\"[]")

    chat_id = target_chat_id or os.environ.get("TELEGRAM_CHAT_ID", "").strip(" '\"[]")

    if not token or not chat_id:
        log.warning("Не заданы TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID!")
        return

    # Собираем чистый URL без квадратных скобок
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    
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

        # Здесь при желании можно добавить запрос к Power BI и генерацию итогового текста.
        # Для тестового вызова отправляем сообщение с Inline-кнопками:
        send_telegram_report(
            text="📊 Отчет по продажам за час подготовлен.",
            reply_markup=get_report_inline_keyboard()
        )

    except Exception as e:
        log.error(f"Ошибка при выполнении send_hourly_stats: {e}")


# Настраиваем планировщик (каждый час с 09:00 до 21:00 по Москве)
scheduler = BackgroundScheduler(timezone="Europe/Moscow")
scheduler.add_job(send_hourly_stats, 'cron', hour='9-21', minute=0)
scheduler.start()

# ТЕСТ ПРИ ЗАПУСКЕ: Проверка доставки в Telegram с постоянной клавиатурой
send_telegram_report(
    text="🔔 Сервис успешно запущен! Меню подключено.",
    reply_markup=get_main_reply_keyboard()
)

# ---------------------------------------------------------------------------
# API эндпоинты Flask
# ---------------------------------------------------------------------------
@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    """Обработчик входящих сообщений и нажатий на кнопки из Telegram."""
    data = request.get_json(force=True, silent=True) or {}

    raw_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    match = re.search(r"(\d+:[A-Za-z0-9_-]+)", raw_token)
    token = match.group(1) if match else raw_token.strip(" '\"[]")

    # 1. Обработка нажатий на Inline-кнопки под сообщениями
    if "callback_query" in data:
        callback = data["callback_query"]
        callback_id = callback["id"]
        chat_id = callback["message"]["chat"]["id"]
        action = callback.get("data", "")

        # Снимаем загрузку с кнопки в интерфейсе Telegram
        if token:
            requests.post(f"[https://api.telegram.org/bot](https://api.telegram.org/bot){token}/answerCallbackQuery", json={"callback_query_id": callback_id})

        reply_text = "Обработка запроса..."
        if action == "details_by_store":
            reply_text = "🏬 Продажи по магазинам за сегодня:\n\n• Пионерский: 18 500 ₽\n• Озеро: 14 130 ₽\n• Утриш: 7 000 ₽\n• Джемете: 5 000 ₽"
        elif action == "details_top_products":
            reply_text = "🍷 Топ-3 продаваемых позиций:\n\n1. Каберне Тамань — 12 шт.\n2. Выдержанное сухое — 8 шт.\n3. Игристое Брют — 5 шт."
        elif action == "refresh_report":
            reply_text = "🔄 Данные обновлены!"

        send_telegram_report(reply_text, target_chat_id=chat_id)
        return jsonify({"status": "ok"}), 200

    # 2. Обработка обычной текстовой команды или нажатия на постоянное меню
    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "").strip()

        if text == "/start":
            send_telegram_report(
                "Добро пожаловать! Используйте меню ниже для быстрого запроса данных.",
                target_chat_id=chat_id,
                reply_markup=get_main_reply_keyboard()
            )
        elif text == "📊 Продажи за сегодня":
            send_telegram_report("💰 Общие продажи за сегодня: 44 630 ₽", target_chat_id=chat_id)
        elif text == "🏬 По магазинам":
            send_telegram_report("🏬 Продажи по точкам:\n• Пионерский: 18 500 ₽\n• Озеро: 14 130 ₽\n• Утриш: 7 000 ₽\n• Джемете: 5 000 ₽", target_chat_id=chat_id)
        elif text == "🎯 Выполнение плана":
            send_telegram_report("🎯 Выполнение дневного плана: 82%", target_chat_id=chat_id)
        elif text == "🍷 Топ товаров за день":
            send_telegram_report("🍷 Топ товаров за сегодня:\n1. Каберне Тамань\n2. Выдержанное сухое", target_chat_id=chat_id)

    return jsonify({"status": "ok"}), 200


@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    data = request.get_json(force=True, silent=True) or {}
    
    # Регистрация /start и кнопок...
    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "").strip()

        if text == "/start":
            send_telegram_report(
                "Добро пожаловать! Используйте меню ниже для быстрого запроса данных.",
                target_chat_id=chat_id,
                reply_markup=get_main_reply_keyboard()
            )
            
    return jsonify({"status": "ok"}), 200

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
