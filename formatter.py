from datetime import datetime
import database as db


def rent_payment_msg(obj: dict, pid: int, amount: float, period: str, sender: str) -> str:
    paid  = db.get_rent_paid(obj["id"], period)
    plan  = obj["plan_rent"]
    delta = paid - plan
    lines = [
        f"✅ *Аренда записана* #{pid}",
        f"📍 {obj['name']}",
        f"👤 {sender}" if sender else "",
        f"💰 {amount:,.0f} ₽",
        f"📅 Период: {period}",
        "",
        f"📊 *Аренда за {period}:*",
        f"  Оплачено: {paid:,.0f} / {plan:,.0f} ₽",
        f"{'✅ Переплата: +' if delta >= 0 else '⚠️ Долг: '}{abs(delta):,.0f} ₽",
    ]
    return "\n".join(l for l in lines if l != "")


def utility_bill_msg(obj: dict, bid: int, amount: float, period: str) -> str:
    return (
        f"📋 *Квитанция ЖКУ записана* #{bid}\n"
        f"📍 {obj['name']}\n"
        f"💰 Начислено: {amount:,.0f} ₽\n"
        f"📅 Период: {period}\n"
        f"⏳ Ожидается оплата"
    )


def utility_payment_msg(obj: dict, pid: int, amount: float, period: str,
                        sender: str, bill_matched: bool) -> str:
    status = "✅ Совпадает с квитанцией" if bill_matched else "ℹ️ Квитанция не найдена — записано отдельно"
    lines = [
        f"💳 *Оплата ЖКУ записана* #{pid}",
        f"📍 {obj['name']}",
        f"👤 {sender}" if sender else "",
        f"💰 {amount:,.0f} ₽",
        f"📅 Период: {period}",
        status,
    ]
    return "\n".join(l for l in lines if l != "")


def balance_message(chat_id: int) -> str:
    period  = datetime.now().strftime("%Y-%m")
    objects = db.get_objects(chat_id)
    if not objects:
        return "Объектов нет. Загрузи Excel через меню."

    lines = [f"📊 *Баланс за {period}*\n"]
    total_plan = total_paid = 0.0

    for o in objects:
        plan  = o["plan_rent"]
        paid  = db.get_rent_paid(o["id"], period)
        delta = paid - plan
        total_plan += plan
        total_paid += paid

        icon = "✅" if delta >= 0 else ("🟡" if paid > 0 else "🔴")
        lines.append(f"{icon} *{o['name']}* ({o['tenant_name']})")
        lines.append(f"  Аренда: {paid:,.0f} / {plan:,.0f} ₽  ({delta:+,.0f} ₽)")

        bills = db.get_utility_bills(o["id"], period)
        pays  = db.get_utility_payments(o["id"], period)
        if bills:
            billed = sum(b["amount"] for b in bills)
            paid_u = sum(p["amount"] for p in pays)
            u_icon = "✅" if paid_u >= billed else "⏳"
            lines.append(f"  ЖКУ: {u_icon} {paid_u:,.0f} / {billed:,.0f} ₽")
        lines.append("")

    td = total_paid - total_plan
    lines += [
        "─────────────────",
        f"Аренда итого: {total_paid:,.0f} / {total_plan:,.0f} ₽",
        f"{'✅ +' if td >= 0 else '⚠️ '}{abs(td):,.0f} ₽",
    ]
    return "\n".join(lines)


def object_history(obj: dict) -> str:
    rent_pays  = db.get_rent_payments(obj["id"])
    util_bills = db.get_utility_bills(obj["id"])
    util_pays  = db.get_utility_payments(obj["id"])

    lines = [f"📜 *История: {obj['name']}*\n"]

    if rent_pays:
        lines.append("*Аренда:*")
        for p in rent_pays[-20:]:
            lines.append(f"  {p['period']} | {p['date']} | {p['amount']:,.0f} ₽")
        lines.append("")

    if util_bills:
        lines.append("*Квитанции ЖКУ:*")
        for b in util_bills[-20:]:
            paid = any(p["bill_id"] == b["id"] for p in util_pays)
            icon = "✅" if paid else "⏳"
            lines.append(f"  {icon} {b['period']} | {b['amount']:,.0f} ₽")
        lines.append("")

    if util_pays:
        lines.append("*Оплаты ЖКУ:*")
        for p in util_pays[-20:]:
            linked = "✅" if p["bill_id"] else "ℹ️"
            lines.append(f"  {linked} {p['period']} | {p['date']} | {p['amount']:,.0f} ₽")

    return "\n".join(lines) if len(lines) > 1 else f"История по {obj['name']} пуста."
