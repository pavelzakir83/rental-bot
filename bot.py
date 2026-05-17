"""
Rental Bot — полная версия
Блок 1: Настройка объектов (диалог + кнопки)
Блок 2: Приём платежей (фото, скрин, PDF)
Блок 3: Сальдо и отчёты
Блок 4: Напоминания (за 3 дня до срока)
"""

import os
import io
import logging
from datetime import datetime, date, timedelta

from dotenv import load_dotenv
from telegram import (
    Update, ReplyKeyboardMarkup, ReplyKeyboardRemove,
    InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

import database as db
import parser as claude_parser
import formatter

load_dotenv()

TOKEN      = os.getenv("TELEGRAM_TOKEN")
OWNER_ID   = int(os.getenv("OWNER_CHAT_ID"))
REMIND_HOUR = 10  # время напоминаний (МСК = UTC+3, Railway работает в UTC)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Состояния ConversationHandler ────────────────────────────────────────────
(
    SETUP_NAME, SETUP_RENT, SETUP_UTILITY, SETUP_DAY,      # добавление объекта
    EDIT_CHOOSE, EDIT_NAME, EDIT_RENT, EDIT_UTILITY, EDIT_DAY,  # редактирование
) = range(9)

# ── Главное меню ──────────────────────────────────────────────────────────────
MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["📋 Мои объекты", "💰 Баланс"],
        ["➕ Добавить объект", "✏️ Изменить объект"],
        ["📊 Отчёт за месяц", "🗑 Удалить объект"],
    ],
    resize_keyboard=True,
)

def main_menu_msg():
    return "Выбери действие:"

# ─────────────────────────────────────────────────────────────────────────────
# СТАРТ / МЕНЮ
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏠 *Учёт арендных платежей*\n\n"
        "Пересылай квитанции \\(фото, скрин, PDF\\) — я распознаю и посчитаю сальдо\\.\n\n"
        "Начни с добавления объектов\\.",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )

async def btn_objects(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    objects = db.get_objects(update.effective_chat.id)
    if not objects:
        await update.message.reply_text("Объектов пока нет. Нажми ➕ Добавить объект.")
        return
    lines = ["📋 *Объекты:*\n"]
    for o in objects:
        plan = o["plan_rent"] + o["plan_utility"]
        lines.append(
            f"*{o['name']}*\n"
            f"  Аренда: {o['plan_rent']:,.0f} ₽  ЖКУ: {o['plan_utility']:,.0f} ₽\n"
            f"  Срок оплаты: {o['due_day']}-е число\n"
            f"  Итого план: {plan:,.0f} ₽"
        )
    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")

async def btn_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = formatter.balance_message(update.effective_chat.id)
    await update.message.reply_text(msg, parse_mode="Markdown")

async def btn_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    # Кнопки: текущий и предыдущий месяц
    periods = [
        now.strftime("%Y-%m"),
        (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m"),
    ]
    kb = [[InlineKeyboardButton(p, callback_data=f"report:{p}")] for p in periods]
    await update.message.reply_text(
        "За какой месяц?",
        reply_markup=InlineKeyboardMarkup(kb),
    )

async def cb_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    period = update.callback_query.data.split(":")[1]
    chat_id = update.effective_chat.id
    objects = db.get_objects(chat_id)
    lines = [f"📋 *Отчёт за {period}*\n"]
    for o in objects:
        s     = db.get_monthly_summary(o["id"], period)
        plan  = o["plan_rent"] + o["plan_utility"]
        paid  = s["paid_rent"] + s["paid_utility"]
        delta = paid - plan
        icon  = "✅" if delta >= 0 else ("🟡" if paid > 0 else "🔴")
        lines.append(f"{icon} *{o['name']}*")
        lines.append(f"  {paid:,.0f} / {plan:,.0f} ₽  ({delta:+,.0f} ₽)")
        payments = db.get_payments(o["id"], period)
        for p in payments:
            lines.append(f"  • {p['date']}  {p['amount']:,.0f} ₽  {p['payment_type']}")
        lines.append("")
    await update.callback_query.edit_message_text(
        "\n".join(lines) or "Нет данных.", parse_mode="Markdown"
    )

# ─────────────────────────────────────────────────────────────────────────────
# ДОБАВЛЕНИЕ ОБЪЕКТА — диалог
# ─────────────────────────────────────────────────────────────────────────────

async def setup_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "📝 *Новый объект*\n\nКак называется? Например: «Кв Ленина 10\\-5» или «Студия СЦ»",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return SETUP_NAME

async def setup_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["name"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ Название: *{ctx.user_data['name']}*\n\nСумма аренды в месяц \\(только цифры\\):",
        parse_mode="Markdown",
    )
    return SETUP_RENT

async def setup_rent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        ctx.user_data["rent"] = float(update.message.text.strip().replace(" ","").replace(",","."))
    except ValueError:
        await update.message.reply_text("Введи число. Например: 25000")
        return SETUP_RENT
    await update.message.reply_text(
        f"✅ Аренда: *{ctx.user_data['rent']:,.0f} ₽*\n\nПлановая сумма ЖКУ в месяц:",
        parse_mode="Markdown",
    )
    return SETUP_UTILITY

async def setup_utility(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        ctx.user_data["utility"] = float(update.message.text.strip().replace(" ","").replace(",","."))
    except ValueError:
        await update.message.reply_text("Введи число. Например: 3500")
        return SETUP_UTILITY
    await update.message.reply_text(
        f"✅ ЖКУ: *{ctx.user_data['utility']:,.0f} ₽*\n\nДо какого числа должна прийти оплата? (1–28)",
        parse_mode="Markdown",
    )
    return SETUP_DAY

async def setup_day(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        day = int(update.message.text.strip())
        assert 1 <= day <= 28
    except (ValueError, AssertionError):
        await update.message.reply_text("Введи число от 1 до 28.")
        return SETUP_DAY
    ctx.user_data["day"] = day
    d = ctx.user_data
    obj_id = db.add_object(
        update.effective_chat.id, d["name"], d["rent"], d["utility"], d["day"]
    )
    plan = d["rent"] + d["utility"]
    await update.message.reply_text(
        f"✅ *Объект добавлен!*\n\n"
        f"📍 {d['name']}\n"
        f"💰 Аренда: {d['rent']:,.0f} ₽  ЖКУ: {d['utility']:,.0f} ₽\n"
        f"📅 Срок оплаты: до {d['day']}-го числа\n"
        f"Итого план: {plan:,.0f} ₽/мес",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )
    return ConversationHandler.END

async def setup_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.", reply_markup=MAIN_MENU)
    return ConversationHandler.END

# ─────────────────────────────────────────────────────────────────────────────
# РЕДАКТИРОВАНИЕ ОБЪЕКТА — диалог
# ─────────────────────────────────────────────────────────────────────────────

async def edit_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    objects = db.get_objects(update.effective_chat.id)
    if not objects:
        await update.message.reply_text("Объектов нет. Сначала добавь.")
        return ConversationHandler.END
    kb = [[InlineKeyboardButton(o["name"], callback_data=f"editobj:{o['id']}")] for o in objects]
    await update.message.reply_text(
        "Какой объект изменить?",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return EDIT_CHOOSE

async def edit_choose(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    obj_id = int(update.callback_query.data.split(":")[1])
    ctx.user_data["edit_id"] = obj_id
    obj = db.get_object(obj_id)
    ctx.user_data["obj"] = obj
    await update.callback_query.edit_message_text(
        f"Редактирую: *{obj['name']}*\n\nНовое название (или /skip чтобы оставить):",
        parse_mode="Markdown",
    )
    return EDIT_NAME

async def edit_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    ctx.user_data["name"] = ctx.user_data["obj"]["name"] if t == "/skip" else t
    await update.message.reply_text(
        f"Сумма аренды (сейчас {ctx.user_data['obj']['plan_rent']:,.0f} ₽, или /skip):"
    )
    return EDIT_RENT

async def edit_rent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "/skip":
        ctx.user_data["rent"] = ctx.user_data["obj"]["plan_rent"]
    else:
        try:
            ctx.user_data["rent"] = float(t.replace(" ","").replace(",","."))
        except ValueError:
            await update.message.reply_text("Введи число или /skip")
            return EDIT_RENT
    await update.message.reply_text(
        f"Сумма ЖКУ (сейчас {ctx.user_data['obj']['plan_utility']:,.0f} ₽, или /skip):"
    )
    return EDIT_UTILITY

async def edit_utility(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "/skip":
        ctx.user_data["utility"] = ctx.user_data["obj"]["plan_utility"]
    else:
        try:
            ctx.user_data["utility"] = float(t.replace(" ","").replace(",","."))
        except ValueError:
            await update.message.reply_text("Введи число или /skip")
            return EDIT_UTILITY
    await update.message.reply_text(
        f"День оплаты (сейчас {ctx.user_data['obj']['due_day']}-е, или /skip):"
    )
    return EDIT_DAY

async def edit_day(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "/skip":
        ctx.user_data["day"] = ctx.user_data["obj"]["due_day"]
    else:
        try:
            day = int(t)
            assert 1 <= day <= 28
            ctx.user_data["day"] = day
        except (ValueError, AssertionError):
            await update.message.reply_text("Введи число 1–28 или /skip")
            return EDIT_DAY
    d = ctx.user_data
    db.update_object(d["edit_id"], d["name"], d["rent"], d["utility"], d["day"])
    await update.message.reply_text(
        f"✅ *Обновлено:*\n\n"
        f"📍 {d['name']}\n"
        f"💰 Аренда: {d['rent']:,.0f} ₽  ЖКУ: {d['utility']:,.0f} ₽\n"
        f"📅 Срок: до {d['day']}-го",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )
    return ConversationHandler.END

# ─────────────────────────────────────────────────────────────────────────────
# УДАЛЕНИЕ ОБЪЕКТА
# ─────────────────────────────────────────────────────────────────────────────

async def delete_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    objects = db.get_objects(update.effective_chat.id)
    if not objects:
        await update.message.reply_text("Объектов нет.")
        return
    kb = [[InlineKeyboardButton(f"🗑 {o['name']}", callback_data=f"delobj:{o['id']}")] for o in objects]
    await update.message.reply_text("Какой объект удалить?", reply_markup=InlineKeyboardMarkup(kb))

async def cb_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    obj_id = int(update.callback_query.data.split(":")[1])
    obj = db.get_object(obj_id)
    db.delete_object(obj_id)
    await update.callback_query.edit_message_text(f"🗑 Объект «{obj['name']}» удалён.")

# ─────────────────────────────────────────────────────────────────────────────
# ОБРАБОТКА ПЛАТЕЖЕЙ — фото / PDF
# ─────────────────────────────────────────────────────────────────────────────

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 Обрабатываю...")
    photo = update.message.photo[-1]
    file  = await ctx.bot.get_file(photo.file_id)
    data  = await file.download_as_bytearray()
    try:
        parsed = claude_parser.parse_photo(bytes(data))
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка распознавания: {e}")
        return
    await process_parsed(update, ctx, parsed)

async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.mime_type or "pdf" not in doc.mime_type.lower():
        return
    await update.message.reply_text("📄 Читаю PDF...")
    file = await ctx.bot.get_file(doc.file_id)
    data = await file.download_as_bytearray()
    # Извлекаем текст из PDF
    try:
        import pdfplumber, io
        with pdfplumber.open(io.BytesIO(bytes(data))) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        if not text.strip():
            raise ValueError("Пустой текст")
        parsed = claude_parser.parse_text(text)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка чтения PDF: {e}")
        return
    await process_parsed(update, ctx, parsed)

async def process_parsed(update: Update, ctx: ContextTypes.DEFAULT_TYPE, parsed: dict):
    if not parsed.get("is_payment"):
        await update.message.reply_text("🤔 Не похоже на платёжный документ. Попробуй другой файл.")
        return
    amount = parsed.get("amount", 0)
    if not amount or amount <= 0:
        await update.message.reply_text("❓ Не удалось определить сумму. Убедись что на документе видна сумма.")
        return

    chat_id = update.effective_chat.id
    obj = db.match_object(chat_id, parsed.get("object_hint"))

    if obj:
        _save_and_reply(update, parsed, obj)
    else:
        # Объект не распознан — предлагаем выбрать кнопками
        objects = db.get_objects(chat_id)
        if not objects:
            await update.message.reply_text(
                f"❓ Объект не найден. Сначала добавь объекты через меню.\n"
                f"Распознанная сумма: {amount:,.0f} ₽"
            )
            return
        ctx.user_data["pending_parsed"] = parsed
        kb = [[InlineKeyboardButton(o["name"], callback_data=f"assign:{o['id']}")] for o in objects]
        await update.message.reply_text(
            f"❓ Объект не распознан\n"
            f"Подсказка из документа: *{parsed.get('object_hint') or 'нет'}*\n"
            f"Сумма: *{amount:,.0f} ₽*\n\n"
            f"К какому объекту отнести?",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown",
        )

async def cb_assign(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    obj_id = int(update.callback_query.data.split(":")[1])
    obj    = db.get_object(obj_id)
    parsed = ctx.user_data.get("pending_parsed", {})
    pid    = _record_payment(parsed, obj)
    msg    = formatter.saldo_message(obj, parsed, pid)
    await update.callback_query.edit_message_text(msg, parse_mode="Markdown")

def _record_payment(parsed: dict, obj: dict) -> int:
    return db.add_payment(
        object_id    = obj["id"],
        amount       = parsed["amount"],
        payment_type = parsed.get("payment_type", "unknown"),
        date         = parsed.get("date") or datetime.now().strftime("%Y-%m-%d"),
        period       = parsed.get("period") or datetime.now().strftime("%Y-%m"),
        payer        = parsed.get("payer"),
        notes        = parsed.get("notes"),
    )

async def _save_and_reply(update: Update, parsed: dict, obj: dict):
    pid = _record_payment(parsed, obj)
    msg = formatter.saldo_message(obj, parsed, pid)
    await update.message.reply_text(msg, parse_mode="Markdown")

# ─────────────────────────────────────────────────────────────────────────────
# НАПОМИНАНИЯ — планировщик
# ─────────────────────────────────────────────────────────────────────────────

async def send_reminders(ctx: ContextTypes.DEFAULT_TYPE):
    """Запускается ежедневно. Проверяет: срок через 3 дня и нет оплаты."""
    today  = date.today()
    period = today.strftime("%Y-%m")
    remind_date = today + timedelta(days=3)

    for obj in db.get_all_active_objects():
        if obj["due_day"] != remind_date.day:
            continue
        if db.has_payment_this_period(obj["id"], period):
            continue
        plan = obj["plan_rent"] + obj["plan_utility"]
        try:
            date_str = remind_date.strftime('%d.%m.%Y')
            obj_name = obj['name']
            due_day = obj['due_day']
            msg = (
                "Напоминание об оплате\n\n"
                + "Объект: " + obj_name + "\n"
                + "Срок: " + str(due_day) + "-е число (" + date_str + ")\n"
                + "Ожидается: " + f"{plan:,.0f}" + " руб\n\n"
                + "Оплата за " + period + " ещё не поступала."
            )
            await ctx.bot.send_message(
                chat_id=obj["chat_id"],
                text=msg,
                parse_mode="Markdown",
            )
        except Exception as e:
            log.error(f"Reminder error for obj {obj['id']}: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# ROUTER — текстовые кнопки главного меню
# ─────────────────────────────────────────────────────────────────────────────

async def menu_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📋 Мои объекты":     await btn_objects(update, ctx)
    elif text == "💰 Баланс":         await btn_balance(update, ctx)
    elif text == "📊 Отчёт за месяц": await btn_report(update, ctx)
    elif text == "🗑 Удалить объект":  await delete_start(update, ctx)

# ─────────────────────────────────────────────────────────────────────────────
# ЗАПУСК
# ─────────────────────────────────────────────────────────────────────────────

def main():
    db.init()

    app = Application.builder().token(TOKEN).build()

    # Диалог: добавить объект
    setup_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Добавить объект$"), setup_start)],
        states={
            SETUP_NAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_name)],
            SETUP_RENT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_rent)],
            SETUP_UTILITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_utility)],
            SETUP_DAY:     [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_day)],
        },
        fallbacks=[CommandHandler("cancel", setup_cancel)],
    )

    # Диалог: изменить объект
    edit_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^✏️ Изменить объект$"), edit_start)],
        states={
            EDIT_CHOOSE:   [CallbackQueryHandler(edit_choose, pattern="^editobj:")],
            EDIT_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_name)],
            EDIT_RENT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_rent)],
            EDIT_UTILITY:  [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_utility)],
            EDIT_DAY:      [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_day)],
        },
        fallbacks=[CommandHandler("cancel", setup_cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(setup_conv)
    app.add_handler(edit_conv)
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(CallbackQueryHandler(cb_report,  pattern="^report:"))
    app.add_handler(CallbackQueryHandler(cb_delete,  pattern="^delobj:"))
    app.add_handler(CallbackQueryHandler(cb_assign,  pattern="^assign:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router))

    # Напоминания: каждый день в REMIND_HOUR:00 UTC
    app.job_queue.run_daily(
        send_reminders,
        time=__import__("datetime").time(hour=REMIND_HOUR, minute=0),
    )

    log.info("Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
