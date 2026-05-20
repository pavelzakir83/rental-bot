"""
Rental Bot v2 — финальная версия
- Объекты загружаются через Excel (4 колонки)
- Входящие: фото/PDF → Claude определяет тип → запись в БД
- Определение арендатора по имени отправителя (прямое и пересланное)
- Аренда: сальдо план/факт
- ЖКУ: квитанция + оплата, точное совпадение сумм
- Напоминания: аренда за 3 дня, ЖКУ 15-го числа
- Хранилище: Railway Volume /data/rental.db
"""

import os
import io
import logging
from datetime import datetime, date, timedelta

from dotenv import load_dotenv
from telegram import (
    Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

import database as db
import parser as claude_parser
import formatter

load_dotenv()

TOKEN       = os.getenv("TELEGRAM_TOKEN")
OWNER_ID    = int(os.getenv("OWNER_CHAT_ID"))
REMIND_HOUR = 7   # UTC = 10:00 МСК

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

OWNER_FILTER = filters.Chat(OWNER_ID)

# ── Главное меню ──────────────────────────────────────────────────────────────
MENU = ReplyKeyboardMarkup([
    ["📊 Баланс", "📋 Объекты"],
    ["📅 Отчёт за месяц", "📜 История"],
], resize_keyboard=True)

# ─────────────────────────────────────────────────────────────────────────────
# Определение отправителя
# ─────────────────────────────────────────────────────────────────────────────

def get_sender_name(update: Update) -> str | None:
    """Возвращает имя отправителя — из пересланного сообщения или текущего."""
    msg = update.message
    if not msg:
        return None
    # Пересланное сообщение (PTB v20.7)
    if getattr(msg, 'forward_from', None):
        return msg.forward_from.full_name
    if getattr(msg, 'forward_sender_name', None):
        return msg.forward_sender_name
    if getattr(msg, 'forward_from_chat', None):
        chat = msg.forward_from_chat
        return chat.title or chat.username or str(chat.id)
    # Прямое сообщение
    user = update.effective_user
    if user:
        return user.full_name
    return None

# ─────────────────────────────────────────────────────────────────────────────
# СТАРТ
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏠 *Учёт арендных платежей*\n\n"
        "Загрузи Excel с объектами — и начинай пересылать квитанции и чеки.\n\n"
        "*Формат Excel (4 колонки):*\n"
        "Объект | Арендатор | Аренда | День оплаты\n\n"
        "Пример:\n"
        "Кв Ленина 10-5 | Иванов Дмитрий | 25000 | 10",
        parse_mode="Markdown",
        reply_markup=MENU,
    )

# ─────────────────────────────────────────────────────────────────────────────
# EXCEL IMPORT
# ─────────────────────────────────────────────────────────────────────────────

async def handle_excel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Читаю файл...")
    file = await ctx.bot.get_file(update.message.document.file_id)
    data = await file.download_as_bytearray()

    try:
        import openpyxl
        wb   = openpyxl.load_workbook(io.BytesIO(bytes(data)))
        ws   = wb.active
        rows = list(ws.iter_rows(values_only=True))
    except Exception as e:
        await update.message.reply_text(f"❌ Не удалось прочитать файл: {e}")
        return

    chat_id  = update.effective_chat.id
    created = updated = unchanged = errors = 0

    for i, row in enumerate(rows, start=1):
        # Пропускаем заголовок и пустые строки
        if not row or not row[0]:
            continue
        if i == 1 and isinstance(row[0], str) and row[0].lower() in ("объект", "object", "название"):
            continue
        try:
            name        = str(row[0]).strip()
            tenant_name = str(row[1]).strip()
            plan_rent   = float(str(row[2]).replace("\xa0","").replace(" ","").replace(",","."))
            due_day     = int(row[3])
            assert 1 <= due_day <= 31
        except Exception:
            errors += 1
            log.warning(f"Строка {i} пропущена: {row}")
            continue

        _, status = db.upsert_object(chat_id, name, tenant_name, plan_rent, due_day)
        if status == "created":   created   += 1
        elif status == "updated": updated   += 1
        else:                     unchanged += 1

    await update.message.reply_text(
        f"✅ *Импорт завершён*\n\n"
        f"➕ Добавлено: {created}\n"
        f"✏️ Обновлено: {updated}\n"
        f"━ Без изменений: {unchanged}\n"
        + (f"⚠️ Ошибок: {errors}" if errors else ""),
        parse_mode="Markdown",
        reply_markup=MENU,
    )

# ─────────────────────────────────────────────────────────────────────────────
# ОБРАБОТКА ВХОДЯЩИХ ПЛАТЕЖЕЙ
# ─────────────────────────────────────────────────────────────────────────────

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 Обрабатываю...")
    photo = update.message.photo[-1]
    file  = await ctx.bot.get_file(photo.file_id)
    data  = await file.download_as_bytearray()
    try:
        parsed = claude_parser.parse_photo(bytes(data))
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
        return
    await process_parsed(update, ctx, parsed)


async def handle_pdf(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📄 Читаю PDF...")
    file = await ctx.bot.get_file(update.message.document.file_id)
    data = await file.download_as_bytearray()
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(bytes(data))) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        if not text.strip():
            raise ValueError("Пустой текст в PDF")
        parsed = claude_parser.parse_text(text)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
        return
    await process_parsed(update, ctx, parsed)


async def process_parsed(update: Update, ctx: ContextTypes.DEFAULT_TYPE, parsed: dict):
    """Центральная логика обработки распознанного документа."""
    doc_type = parsed.get("doc_type", "unknown")
    amount   = parsed.get("amount")
    chat_id  = update.effective_chat.id
    sender   = get_sender_name(update)

    if not amount or amount <= 0:
        await update.message.reply_text(
            "❓ Не удалось определить сумму. Убедись что сумма видна на документе."
        )
        return

    # Найти объект по имени отправителя
    obj = db.find_object_by_tenant(chat_id, sender)
    if not obj and parsed.get("object_hint"):
        # Попробовать по подсказке из документа
        objects = db.get_objects(chat_id)
        hint = parsed["object_hint"].lower()
        for o in objects:
            if hint in o["name"].lower() or o["name"].lower() in hint:
                obj = o
                break

    if not obj:
        # Объект не найден — предложить выбор
        objects = db.get_objects(chat_id)
        if not objects:
            await update.message.reply_text(
                f"❓ Объекты не найдены. Загрузи Excel с объектами.\n"
                f"Распознана сумма: {amount:,.0f} ₽"
            )
            return
        ctx.user_data["pending"] = parsed
        ctx.user_data["pending_sender"] = sender
        kb = [[InlineKeyboardButton(o["name"], callback_data=f"obj:{o['id']}")] for o in objects]
        await update.message.reply_text(
            f"❓ Арендатор «{sender or 'неизвестен'}» не найден в базе\n"
            f"Сумма: *{amount:,.0f} ₽*\n\n"
            f"К какому объекту отнести?",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown",
        )
        return

    # Если тип не определён — спросить
    if doc_type == "unknown":
        ctx.user_data["pending"] = parsed
        ctx.user_data["pending_obj_id"] = obj["id"]
        ctx.user_data["pending_sender"] = sender
        kb = [[
            InlineKeyboardButton("🏠 Аренда",       callback_data="type:rent_payment"),
            InlineKeyboardButton("📋 Квитанция ЖКУ", callback_data="type:utility_bill"),
            InlineKeyboardButton("💳 Оплата ЖКУ",   callback_data="type:utility_payment"),
        ]]
        await update.message.reply_text(
            f"❓ Что это за документ?\n"
            f"📍 {obj['name']}  |  {amount:,.0f} ₽",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    await record_payment(update, obj, parsed, doc_type, sender)


async def record_payment(update_or_query, obj: dict, parsed: dict,
                         doc_type: str, sender: str):
    """Записывает платёж нужного типа и отвечает."""
    amount  = parsed["amount"]
    period  = parsed.get("period") or datetime.now().strftime("%Y-%m")
    dt      = parsed.get("date")   or datetime.now().strftime("%Y-%m-%d")
    notes   = parsed.get("notes")

    reply = getattr(update_or_query, "message", None) or update_or_query

    if doc_type == "rent_payment":
        pid = db.add_rent_payment(obj["id"], amount, dt, period, sender, notes)
        msg = formatter.rent_payment_msg(obj, pid, amount, period, sender)
        await reply.reply_text(msg, parse_mode="Markdown")

    elif doc_type == "utility_bill":
        bid = db.add_utility_bill(obj["id"], amount, period, dt, notes)
        msg = formatter.utility_bill_msg(obj, bid, amount, period)
        await reply.reply_text(msg, parse_mode="Markdown")

    elif doc_type == "utility_payment":
        bill = db.find_matching_bill(obj["id"], period, amount)
        pid  = db.add_utility_payment(
            obj["id"], bill["id"] if bill else None,
            amount, dt, period, sender, notes
        )
        msg = formatter.utility_payment_msg(obj, pid, amount, period, sender, bill is not None)
        await reply.reply_text(msg, parse_mode="Markdown")


# Callback: выбор объекта (когда арендатор не найден)
async def cb_obj_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    obj_id = int(update.callback_query.data.split(":")[1])
    obj    = db.get_object(obj_id)
    parsed = ctx.user_data.get("pending", {})
    sender = ctx.user_data.get("pending_sender")
    dt     = parsed.get("doc_type", "unknown")

    if dt == "unknown":
        ctx.user_data["pending_obj_id"] = obj_id
        kb = [[
            InlineKeyboardButton("🏠 Аренда",       callback_data="type:rent_payment"),
            InlineKeyboardButton("📋 Квитанция ЖКУ", callback_data="type:utility_bill"),
            InlineKeyboardButton("💳 Оплата ЖКУ",   callback_data="type:utility_payment"),
        ]]
        await update.callback_query.edit_message_text(
            f"Объект выбран: *{obj['name']}*\n{parsed['amount']:,.0f} ₽\n\nТип документа?",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown",
        )
        return

    amount  = parsed["amount"]
    period  = parsed.get("period") or datetime.now().strftime("%Y-%m")
    dt_     = parsed.get("date")   or datetime.now().strftime("%Y-%m-%d")
    notes   = parsed.get("notes")

    if dt == "rent_payment":
        pid = db.add_rent_payment(obj["id"], amount, dt_, period, sender, notes)
        msg = formatter.rent_payment_msg(obj, pid, amount, period, sender)
    elif dt == "utility_bill":
        pid = db.add_utility_bill(obj["id"], amount, period, dt_, notes)
        msg = formatter.utility_bill_msg(obj, pid, amount, period)
    else:
        bill = db.find_matching_bill(obj["id"], period, amount)
        pid  = db.add_utility_payment(obj["id"], bill["id"] if bill else None, amount, dt_, period, sender, notes)
        msg  = formatter.utility_payment_msg(obj, pid, amount, period, sender, bill is not None)

    await update.callback_query.edit_message_text(msg, parse_mode="Markdown")


# Callback: выбор типа документа
async def cb_type_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    doc_type = update.callback_query.data.split(":")[1]
    parsed   = ctx.user_data.get("pending", {})
    sender   = ctx.user_data.get("pending_sender")
    obj_id   = ctx.user_data.get("pending_obj_id")
    obj      = db.get_object(obj_id)

    parsed["doc_type"] = doc_type
    amount  = parsed["amount"]
    period  = parsed.get("period") or datetime.now().strftime("%Y-%m")
    dt      = parsed.get("date")   or datetime.now().strftime("%Y-%m-%d")
    notes   = parsed.get("notes")

    if doc_type == "rent_payment":
        pid = db.add_rent_payment(obj["id"], amount, dt, period, sender, notes)
        msg = formatter.rent_payment_msg(obj, pid, amount, period, sender)
    elif doc_type == "utility_bill":
        pid = db.add_utility_bill(obj["id"], amount, period, dt, notes)
        msg = formatter.utility_bill_msg(obj, pid, amount, period)
    else:
        bill = db.find_matching_bill(obj["id"], period, amount)
        pid  = db.add_utility_payment(obj["id"], bill["id"] if bill else None, amount, dt, period, sender, notes)
        msg  = formatter.utility_payment_msg(obj, pid, amount, period, sender, bill is not None)

    await update.callback_query.edit_message_text(msg, parse_mode="Markdown")


# ─────────────────────────────────────────────────────────────────────────────
# РОУТЕР ДОКУМЕНТОВ
# ─────────────────────────────────────────────────────────────────────────────

async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doc  = update.message.document
    name = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()

    if name.endswith(".xlsx") or "spreadsheet" in mime or "excel" in mime:
        await handle_excel(update, ctx)
    elif name.endswith(".pdf") or "pdf" in mime:
        await handle_pdf(update, ctx)
    else:
        await update.message.reply_text(
            "Поддерживаются:\n• .xlsx — загрузка объектов\n• .pdf — квитанции и чеки"
        )

# ─────────────────────────────────────────────────────────────────────────────
# КОМАНДЫ / КНОПКИ МЕНЮ
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = formatter.balance_message(update.effective_chat.id)
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_objects(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    objects = db.get_objects(update.effective_chat.id)
    if not objects:
        await update.message.reply_text("Объектов нет. Загрузи Excel.")
        return
    lines = ["📋 *Объекты:*\n"]
    for o in objects:
        lines.append(
            f"*{o['name']}*\n"
            f"  Арендатор: {o['tenant_name']}\n"
            f"  Аренда: {o['plan_rent']:,.0f} ₽  |  Срок: до {o['due_day']}-го"
        )
    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    periods = [
        now.strftime("%Y-%m"),
        (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m"),
    ]
    kb = [[InlineKeyboardButton(p, callback_data=f"report:{p}")] for p in periods]
    await update.message.reply_text("За какой месяц?", reply_markup=InlineKeyboardMarkup(kb))


async def cb_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    period  = update.callback_query.data.split(":")[1]
    chat_id = update.effective_chat.id
    objects = db.get_objects(chat_id)
    lines   = [f"📋 *Отчёт за {period}*\n"]

    for o in objects:
        plan     = o["plan_rent"]
        rent_paid = db.get_rent_paid(o["id"], period)
        rent_delta = rent_paid - plan
        r_icon = "✅" if rent_delta >= 0 else ("🟡" if rent_paid > 0 else "🔴")

        lines.append(f"{r_icon} *{o['name']}* ({o['tenant_name']})")
        lines.append(f"  Аренда: {rent_paid:,.0f} / {plan:,.0f} ₽  ({rent_delta:+,.0f} ₽)")

        for rp in db.get_rent_payments(o["id"], period):
            lines.append(f"    • {rp['date']}  {rp['amount']:,.0f} ₽")

        bills = db.get_utility_bills(o["id"], period)
        pays  = db.get_utility_payments(o["id"], period)
        if bills or pays:
            lines.append("  ЖКУ:")
            for b in bills:
                matched = any(p["bill_id"] == b["id"] for p in pays)
                icon    = "✅" if matched else "⏳"
                lines.append(f"    {icon} Квитанция: {b['amount']:,.0f} ₽")
            for p in pays:
                linked = "✅" if p["bill_id"] else "ℹ️"
                lines.append(f"    {linked} Оплата: {p['amount']:,.0f} ₽")
        lines.append("")

    await update.callback_query.edit_message_text(
        "\n".join(lines) or "Нет данных.", parse_mode="Markdown"
    )


async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    objects = db.get_objects(update.effective_chat.id)
    if not objects:
        await update.message.reply_text("Объектов нет.")
        return
    kb = [[InlineKeyboardButton(o["name"], callback_data=f"hist:{o['id']}")] for o in objects]
    await update.message.reply_text("По какому объекту?", reply_markup=InlineKeyboardMarkup(kb))


async def cb_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    obj_id = int(update.callback_query.data.split(":")[1])
    obj    = db.get_object(obj_id)
    msg    = formatter.object_history(obj)
    await update.callback_query.edit_message_text(msg, parse_mode="Markdown")


async def menu_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📊 Баланс":           await cmd_balance(update, ctx)
    elif text == "📋 Объекты":         await cmd_objects(update, ctx)
    elif text == "📅 Отчёт за месяц":  await cmd_report(update, ctx)
    elif text == "📜 История":         await cmd_history(update, ctx)

# ─────────────────────────────────────────────────────────────────────────────
# НАПОМИНАНИЯ
# ─────────────────────────────────────────────────────────────────────────────

async def remind_rent(ctx: ContextTypes.DEFAULT_TYPE):
    """Ежедневно: аренда через 3 дня."""
    today       = date.today()
    remind_date = today + timedelta(days=3)
    period      = today.strftime("%Y-%m")

    for obj in db.get_all_active_objects():
        if obj["due_day"] != remind_date.day:
            continue
        if db.has_rent_payment(obj["id"], period):
            continue
        try:
            await ctx.bot.send_message(
                chat_id=obj["chat_id"],
                text=(
                    f"⏰ Напоминание об аренде\n\n"
                    f"Объект: {obj['name']}\n"
                    f"Арендатор: {obj['tenant_name']}\n"
                    f"Срок: {obj['due_day']}-е число\n"
                    f"Ожидается: {obj['plan_rent']:,.0f} руб\n\n"
                    f"Аренда за {period} ещё не поступала."
                )
            )
        except Exception as e:
            log.error(f"remind_rent error obj {obj['id']}: {e}")


async def remind_utility(ctx: ContextTypes.DEFAULT_TYPE):
    """15-го числа: напомнить про ЖКУ если квитанция не пришла за прошлый месяц."""
    today = date.today()
    if today.day != 15:
        return
    prev  = (today.replace(day=1) - timedelta(days=1))
    period = prev.strftime("%Y-%m")

    for obj in db.get_all_active_objects():
        if db.has_utility_bill(obj["id"], period):
            continue
        try:
            await ctx.bot.send_message(
                chat_id=obj["chat_id"],
                text=(
                    f"📋 Квитанция ЖКУ не получена\n\n"
                    f"Объект: {obj['name']}\n"
                    f"Арендатор: {obj['tenant_name']}\n"
                    f"Период: {period}\n\n"
                    f"Ожидаем квитанцию от УК."
                )
            )
        except Exception as e:
            log.error(f"remind_utility error obj {obj['id']}: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# ЗАПУСК
# ─────────────────────────────────────────────────────────────────────────────

def main():
    db.init()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start,   filters=OWNER_FILTER))
    app.add_handler(CommandHandler("balance", cmd_balance, filters=OWNER_FILTER))
    app.add_handler(CommandHandler("objects", cmd_objects, filters=OWNER_FILTER))
    app.add_handler(CommandHandler("report",  cmd_report,  filters=OWNER_FILTER))
    app.add_handler(CommandHandler("history", cmd_history, filters=OWNER_FILTER))

    app.add_handler(MessageHandler(OWNER_FILTER & filters.PHOTO,        handle_photo))
    app.add_handler(MessageHandler(OWNER_FILTER & filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(OWNER_FILTER & filters.TEXT & ~filters.COMMAND, menu_router))

    app.add_handler(CallbackQueryHandler(cb_report,      pattern="^report:"))
    app.add_handler(CallbackQueryHandler(cb_obj_select,  pattern="^obj:"))
    app.add_handler(CallbackQueryHandler(cb_type_select, pattern="^type:"))
    app.add_handler(CallbackQueryHandler(cb_history,     pattern="^hist:"))

    import datetime as dt_module
    app.job_queue.run_daily(remind_rent,    time=dt_module.time(hour=REMIND_HOUR, minute=0))
    app.job_queue.run_daily(remind_utility, time=dt_module.time(hour=REMIND_HOUR, minute=5))

    log.info("Бот v2 запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
