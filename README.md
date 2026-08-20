# PowerBI AI-ассистент — backend-сервис

Заменяет всю логику из Make (роутер, генерация DAX, вызов Power BI, форматирование
ответа) одним HTTP-эндпоинтом. Make остаётся только "проводом" между Telegram и
этим сервисом.

## Что это даёт по сравнению со сценарием в Make

- Экранирование JSON решается языком автоматически — весь класс ошибок,
  с которыми мы столкнулись в Make, здесь просто не существует.
- Схему по всем таблицам (`schema.py`) удобно редактировать и версионировать
  в коде, а не перестраивать Data Structure в интерфейсе Make.
- Нормальные логи: видно точный текст DAX-запроса и ответа Power BI при ошибке.

---

## Шаг 1 — настройка Power BI (service principal)

Раньше через Make вы заходили в Power BI под своим личным аккаунтом (OAuth).
Для серверного сервиса без участия человека нужен **service principal**
(специальное серверное "служебное" приложение). Это отдельная настройка,
делается один раз.

1. **Зарегистрировать приложение в Azure AD**
   Портал Azure → Microsoft Entra ID → App registrations → New registration.
   Дайте любое имя, например `PowerBI-Bot-App`. После создания сохраните
   **Application (client) ID** и **Directory (tenant) ID** — они видны на
   странице приложения.

2. **Создать client secret**
   В том же приложении → Certificates & secrets → New client secret.
   Сохраните значение секрета сразу — второй раз оно не покажется.

3. **Создать группу безопасности и добавить туда приложение**
   Microsoft Entra ID → Groups → New group → добавить в участники ваше
   приложение `PowerBI-Bot-App`.

4. **Разрешить service principal'ам использовать Power BI API**
   Power BI Admin portal (app.powerbi.com/admin-portal, нужны права
   администратора Power BI) → Tenant settings → Developer settings →
   "Allow service principals to use Power BI APIs" → включить и указать
   созданную группу безопасности.

5. **Дать приложению доступ к рабочей области с датасетом**
   В Power BI Service откройте нужный workspace → Access → добавьте
   `PowerBI-Bot-App` как Member или Contributor.

Подробная официальная инструкция:
https://learn.microsoft.com/en-us/rest/api/power-bi/

---

## Шаг 2 — настройка окружения

```bash
cp .env.example .env
```

Заполните `.env`:

- `ANTHROPIC_API_KEY` — из https://console.anthropic.com/
- `POWERBI_TENANT_ID`, `POWERBI_CLIENT_ID`, `POWERBI_CLIENT_SECRET` — из шага 1
- `POWERBI_DATASET_ID` — уже известный ID: `690df9f0-812f-4057-bf07-99dc55e41f1d`
- `WEBHOOK_SECRET` — придумайте случайную строку для защиты вебхука

---

## Шаг 3 — запуск локально (проверить, что всё работает)

```bash
pip install -r requirements.txt --break-system-packages
python app.py
```

Проверка:

```bash
curl -X POST http://localhost:8080/webhook \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: <ваш WEBHOOK_SECRET>" \
  -d '{"message": "сумма продаж сегодня"}'
```

Должны получить `{"reply": "..."}`.

---

## Шаг 4 — деплой

Проще всего — **Render.com** (бесплатный Web Service, ничего не нужно
конфигурировать вручную):

1. Залейте эту папку в GitHub-репозиторий.
2. На render.com → New → Web Service → подключить репозиторий.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app`
5. В разделе Environment добавьте все переменные из `.env`.
6. После деплоя вы получите постоянный URL вида
   `https://ваш-сервис.onrender.com`.

---

## Шаг 5 — обновить сценарий в Make

Замените всю цепочку модулей (Gemini роутер → Parse JSON → Router →
Power BI → Gemini форматтер) на:

1. **Telegram: Watch Updates** (как было)
2. **HTTP: Make a request**
   - URL: `https://ваш-сервис.onrender.com/webhook`
   - Method: POST
   - Headers: `Content-Type: application/json`, `X-Webhook-Secret: <ваш секрет>`
   - Body (Raw JSON): `{"message": "{{2.message.text}}"}`
3. **Telegram: Send Reply Message**
   - Text: `{{3.data.reply}}` (поле `reply` из ответа HTTP-модуля)

Сценарий станет короче в разы и перестанет ломаться на экранировании JSON.

---

## Как обновлять схему данных

Когда добавляете новую таблицу или магазин — редактируйте только
`schema.py`. В `app.py` ничего трогать не нужно, промпт для DAX собирается
из схемы автоматически при каждом запуске.
