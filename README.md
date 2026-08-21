# PowerBI AI-ассистент — backend-сервис (упрощённая версия)

Этот сервис **не подключается к Power BI напрямую** и не требует
service principal, прав администратора тенанта или доступа к
Microsoft 365 admin center. Вызов к Power BI остаётся внутри Make —
там, где OAuth-подключение уже настроено и работает.

Сервис берёт на себя только две вещи, которые в Make было неудобно делать:

1. **Генерацию DAX-запроса** по вопросу пользователя (используя схему
   модели из `schema.py`).
2. **Форматирование ответа** — превращает сырой JSON из Power BI в
   дружелюбную фразу.

---

## Шаг 1 — настройка окружения

```bash
cp .env.example .env
```

Заполните `.env`:

- `ANTHROPIC_API_KEY` — из https://console.anthropic.com/
- `WEBHOOK_SECRET` — придумайте случайную строку для защиты вебхука

Больше ничего настраивать не нужно — никаких Power BI credentials.

---

## Шаг 2 — запуск локально (проверить, что всё работает)

```bash
pip install -r requirements.txt --break-system-packages
python app.py
```

Проверка первого эндпоинта:

```bash
curl -X POST http://localhost:8080/generate-dax \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: <ваш WEBHOOK_SECRET>" \
  -d '{"message": "сумма продаж сегодня"}'
```

Должны получить что-то вроде:
```json
{"route": "powerbi", "message": "", "dax": "EVALUATE ROW(\"Result\", CALCULATE(SUM('Продажи'[Сумма]), 'Продажи'[Дата]=TODAY()))"}
```

Проверка второго эндпоинта:

```bash
curl -X POST http://localhost:8080/format-answer \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: <ваш WEBHOOK_SECRET>" \
  -d '{"message": "сумма продаж сегодня", "powerbi_result": {"results":[{"tables":[{"rows":[{"[Result]": 45000}]}]}]}}'
```

Должны получить `{"reply": "..."}`.

---

## Шаг 3 — деплой

**Render.com** (бесплатный Web Service):

1. Залейте эту папку в GitHub-репозиторий.
2. На render.com → New → Web Service → подключить репозиторий.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app`
5. В разделе Environment добавьте `ANTHROPIC_API_KEY` и `WEBHOOK_SECRET`.
6. После деплоя получите постоянный URL вида
   `https://ваш-сервис.onrender.com`.

---

## Шаг 4 — обновить сценарий в Make

Сценарий остаётся почти таким же, как сейчас (с рабочим Power BI
модулем!), просто **вместо блока "Gemini роутер → Parse JSON → Router"**
добавляем один HTTP-вызов к нашему сервису, а после Power BI —
ещё один HTTP-вызов для форматирования:

1. **Telegram: Watch Updates** (как есть)
2. **HTTP: Make a request** → `/generate-dax`
   - URL: `https://ваш-сервис.onrender.com/generate-dax`
   - Method: POST
   - Headers: `Content-Type: application/json`, `X-Webhook-Secret: <секрет>`
   - Body: `{"message": "{{2.message.text}}"}`
3. **Router** (Make) — на два пути, как раньше:
   - если `{{3.data.route}}` = `powerbi` → идём в Power BI
   - если `{{3.data.route}}` = `chat` → сразу Telegram Send Reply с текстом `{{3.data.message}}`
4. **Microsoft Power BI: Make an API Call** — тот же самый модуль, что
   у вас уже работает, просто подставляем `{{3.data.dax}}` в тело запроса
   (то же место, что и раньше — `"query": "{{3.data.dax}}"`).
5. **HTTP: Make a request** → `/format-answer`
   - Body: `{"message": "{{2.message.text}}", "powerbi_result": {{4.body}}}`
6. **Telegram: Send Reply Message**
   - Text: `{{5.data.reply}}`

Это уже проверенная связка — единственное, что меняется по сравнению с
текущим рабочим сценарием, это откуда берётся DAX и кто формулирует
финальный ответ. Сам вызов к Power BI и вся авторизация остаются
нетронутыми.

---

## Как обновлять схему данных

Когда добавляете новую таблицу или магазин — редактируйте только
`schema.py`. В `app.py` ничего трогать не нужно.
