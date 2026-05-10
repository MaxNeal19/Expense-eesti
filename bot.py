import os
import logging
import json
import base64
import requests
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)
import gspread
from google.oauth2.service_account import Credentials

# ─── Логирование ───────────────────────────────────────────────────────────────
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Переменные окружения ───────────────────────────────────────────────────────
TELEGRAM_TOKEN         = os.environ['TELEGRAM_TOKEN']
GEMINI_API_KEY         = os.environ['GEMINI_API_KEY']
GOOGLE_SHEETS_ID       = os.environ['GOOGLE_SHEETS_ID']
GOOGLE_CREDENTIALS_JSON = os.environ['GOOGLE_CREDENTIALS_JSON']  # JSON строка

# ─── Google Sheets ──────────────────────────────────────────────────────────────
SHEET_HEADERS = ['Дата', 'Магазин', 'Категория', 'Товары', 'Сумма', 'Валюта', 'Комментарий', 'Добавлено']

def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=[
            'https://spreadsheets.google.com/feeds',
            'https://www.googleapis.com/auth/drive'
        ]
    )
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(GOOGLE_SHEETS_ID)

    # Проверяем/создаём лист "Расходы"
    try:
        sheet = spreadsheet.worksheet('Расходы')
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet('Расходы', rows=1000, cols=10)

    # Заголовки, если таблица пустая
    if not sheet.row_values(1):
        sheet.append_row(SHEET_HEADERS)
        # Форматирование заголовка (жирный)
        sheet.format('A1:H1', {'textFormat': {'bold': True}})

    return sheet


def add_to_sheet(data: dict, comment: str = ''):
    sheet = get_sheet()
    row = [
        data.get('date') or datetime.now().strftime('%d.%m.%Y'),
        data.get('store', ''),
        data.get('category', 'Другое'),
        data.get('items_summary', ''),
        data.get('total', ''),
        data.get('currency', 'RUB'),
        comment,
        datetime.now().strftime('%d.%m.%Y %H:%M'),
    ]
    sheet.append_row(row)


def get_monthly_stats() -> str:
    sheet = get_sheet()
    all_rows = sheet.get_all_records()

    current_month = datetime.now().strftime('%m.%Y')
    month_rows = []
    for row in all_rows:
        date_str = str(row.get('Дата', ''))
        if len(date_str) >= 7 and date_str[3:] == current_month:
            month_rows.append(row)

    if not month_rows:
        return "За текущий месяц записей нет."

    total = 0.0
    by_category = {}
    for row in month_rows:
        try:
            amount = float(str(row.get('Сумма', 0)).replace(',', '.'))
            total += amount
            cat = row.get('Категория', 'Другое')
            by_category[cat] = by_category.get(cat, 0) + amount
        except (ValueError, TypeError):
            pass

    lines = [f"📊 *Статистика за {datetime.now().strftime('%B %Y')}*\n"]
    for cat, amt in sorted(by_category.items(), key=lambda x: -x[1]):
        lines.append(f"  {cat}: *{amt:.2f} руб.*")
    lines.append(f"\n💰 *Итого: {total:.2f} руб.*")
    lines.append(f"🧾 Записей: {len(month_rows)}")
    return "\n".join(lines)


def get_last_records(n=5) -> str:
    sheet = get_sheet()
    all_rows = sheet.get_all_records()
    last = all_rows[-n:] if len(all_rows) >= n else all_rows
    last = list(reversed(last))

    if not last:
        return "Записей пока нет."

    lines = ["📋 *Последние записи:*\n"]
    for row in last:
        lines.append(
            f"• {row.get('Дата', '—')} | {row.get('Магазин', '—')} | "
            f"{row.get('Сумма', '—')} {row.get('Валюта', 'руб.')}"
        )
    return "\n".join(lines)


# ─── Gemini Vision ──────────────────────────────────────────────────────────────
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/"
    "models/gemini-1.5-flash:generateContent?key={key}"
)

RECEIPT_PROMPT = """Проанализируй изображение чека или квитанции. Верни ТОЛЬКО JSON (без markdown, без пояснений):
{
  "date": "дата в формате ДД.ММ.ГГГГ или null",
  "store": "название магазина/заведения или null",
  "category": "одна из: Продукты, Кафе/Рестораны, Аптека, Одежда/Обувь, Транспорт, Электроника, Развлечения, Другое",
  "items": [{"name": "товар", "price": 0.00, "qty": 1}],
  "items_summary": "краткий список товаров через запятую (макс 100 символов)",
  "total": 0.00,
  "currency": "RUB"
}
Если что-то не читается — ставь null. Числа всегда как число, не строка."""


def analyze_with_gemini(image_bytes: bytes) -> dict:
    image_b64 = base64.b64encode(image_bytes).decode('utf-8')

    payload = {
        "contents": [{
            "parts": [
                {"text": RECEIPT_PROMPT},
                {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}}
            ]
        }],
        "generationConfig": {"temperature": 0.1}
    }

    url = GEMINI_URL.format(key=GEMINI_API_KEY)
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()

    text = resp.json()['candidates'][0]['content']['parts'][0]['text']
    text = text.strip().replace('```json', '').replace('```', '').strip()
    return json.loads(text)


# ─── Обработчики Telegram ───────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я веду учёт твоих расходов.\n\n"
        "📸 *Как пользоваться:*\n"
        "Просто отправь фото или скриншот чека — я распознаю и занесу в таблицу.\n\n"
        "📌 *Команды:*\n"
        "/stats — статистика за текущий месяц\n"
        "/last — последние 5 записей\n"
        "/manual — добавить запись вручную\n"
        "/help — помощь",
        parse_mode='Markdown'
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Загружаю статистику...")
    try:
        text = get_monthly_stats()
        await update.message.reply_text(text, parse_mode='Markdown')
    except Exception as e:
        logger.error(e)
        await update.message.reply_text("❌ Не удалось загрузить статистику.")


async def cmd_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = get_last_records()
        await update.message.reply_text(text, parse_mode='Markdown')
    except Exception as e:
        logger.error(e)
        await update.message.reply_text("❌ Не удалось загрузить записи.")


async def cmd_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✏️ *Добавить вручную*\n\n"
        "Отправь сообщение в формате:\n"
        "`сумма магазин категория`\n\n"
        "Например:\n"
        "`850 Пятёрочка Продукты`\n"
        "`1200 Аптека Вита Аптека`",
        parse_mode='Markdown'
    )
    context.user_data['awaiting_manual'] = True


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *Помощь*\n\n"
        "Отправляй фото или скриншот чека — бот распознает:\n"
        "• Название магазина\n"
        "• Дату покупки\n"
        "• Список товаров\n"
        "• Итоговую сумму\n"
        "• Категорию расходов\n\n"
        "Всё автоматически записывается в Google Таблицу 📊",
        parse_mode='Markdown'
    )


async def process_image(image_bytes: bytes, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Общая логика обработки изображения"""
    try:
        data = analyze_with_gemini(image_bytes)

        # Кнопки подтверждения/исправления категории
        categories = ['Продукты', 'Кафе/Рестораны', 'Аптека', 'Одежда/Обувь',
                      'Транспорт', 'Электроника', 'Развлечения', 'Другое']
        keyboard = []
        row = []
        for i, cat in enumerate(categories):
            marker = '✅ ' if cat == data.get('category') else ''
            row.append(InlineKeyboardButton(f"{marker}{cat}", callback_data=f"cat:{cat}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("💾 Сохранить как есть", callback_data="save")])

        items_text = ""
        if data.get('items'):
            for item in data['items'][:6]:
                items_text += f"  • {item.get('name', '?')}: {item.get('price', '?')} руб.\n"

        reply = (
            f"🧾 *Распознан чек:*\n\n"
            f"🏪 Магазин: {data.get('store') or '—'}\n"
            f"📅 Дата: {data.get('date') or '—'}\n"
            f"🏷 Категория: {data.get('category') or '—'}\n"
            f"🛒 Товары:\n{items_text or '  не распознаны\n'}"
            f"💰 *Итого: {data.get('total') or '?'} {data.get('currency') or 'руб.'}*\n\n"
            f"Подтверди категорию или исправь:"
        )

        # Сохраняем данные во временное хранилище
        context.user_data['pending_receipt'] = data

        await update.message.reply_text(
            reply,
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    except json.JSONDecodeError:
        await update.message.reply_text(
            "⚠️ Gemini вернул неожиданный ответ. Попробуй ещё раз или отправь более чёткое фото."
        )
    except Exception as e:
        logger.error(f"Ошибка обработки: {e}")
        await update.message.reply_text(
            "❌ Не удалось распознать чек.\n\n"
            "Советы:\n"
            "• Сделай более чёткое фото\n"
            "• Убедись, что весь чек в кадре\n"
            "• Попробуй /manual для ручного ввода"
        )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Распознаю чек...")
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = bytes(await file.download_as_bytearray())
    await process_image(image_bytes, update, context)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if doc.mime_type and doc.mime_type.startswith('image/'):
        await update.message.reply_text("⏳ Распознаю скриншот...")
        file = await context.bot.get_file(doc.file_id)
        image_bytes = bytes(await file.download_as_bytearray())
        await process_image(image_bytes, update, context)
    else:
        await update.message.reply_text("Пожалуйста, отправь изображение чека (фото или скриншот).")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной ввод: 850 Пятёрочка Продукты"""
    if not context.user_data.get('awaiting_manual'):
        await update.message.reply_text(
            "Отправь фото чека или используй /manual для ручного ввода."
        )
        return

    text = update.message.text.strip()
    parts = text.split(maxsplit=2)

    if len(parts) < 2:
        await update.message.reply_text("Формат: `сумма магазин категория`\nПример: `850 Пятёрочка Продукты`",
                                        parse_mode='Markdown')
        return

    try:
        amount = float(parts[0].replace(',', '.'))
        store = parts[1]
        category = parts[2] if len(parts) > 2 else 'Другое'

        data = {
            'date': datetime.now().strftime('%d.%m.%Y'),
            'store': store,
            'category': category,
            'items_summary': 'ручной ввод',
            'total': amount,
            'currency': 'RUB'
        }
        add_to_sheet(data, comment='ручной ввод')
        context.user_data['awaiting_manual'] = False

        await update.message.reply_text(
            f"✅ Добавлено!\n💰 {amount:.2f} руб. — {store} ({category})"
        )
    except ValueError:
        await update.message.reply_text("❌ Первым числом укажи сумму. Пример: `850 Пятёрочка Продукты`",
                                        parse_mode='Markdown')


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = context.user_data.get('pending_receipt', {})
    action = query.data

    if action.startswith('cat:'):
        new_cat = action[4:]
        data['category'] = new_cat
        context.user_data['pending_receipt'] = data
        await query.edit_message_text(
            query.message.text.replace(
                f"🏷 Категория: {data.get('category') or '—'}",
                f"🏷 Категория: {new_cat}"
            ) + f"\n\n✏️ Категория изменена на *{new_cat}*.\nНажми «Сохранить» ↓",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💾 Сохранить", callback_data="save")
            ]])
        )

    elif action == 'save':
        try:
            add_to_sheet(data)
            context.user_data.pop('pending_receipt', None)
            await query.edit_message_text(
                f"✅ *Сохранено в таблицу!*\n\n"
                f"🏪 {data.get('store') or '—'}\n"
                f"🏷 {data.get('category') or '—'}\n"
                f"💰 {data.get('total') or '?'} {data.get('currency') or 'руб.'}",
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(e)
            await query.edit_message_text("❌ Ошибка при сохранении. Попробуй ещё раз.")


# ─── Запуск ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("last", cmd_last))
    app.add_handler(CommandHandler("manual", cmd_manual))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Бот запущен ✅")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
