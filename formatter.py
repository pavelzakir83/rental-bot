from datetime import datetime
import database as db

def saldo_message(obj: dict, parsed: dict, payment_id: int) -> str:
    period = parsed.get("period") or datetime.now().strftime("%Y-%m")
    s      = db.get_monthly_summary(obj["id"], period)

    plan_total  = obj["plan_rent"] + obj["plan_utility"]
    paid_total  = s["paid_rent"] + s["paid_utility"]
    delta       = paid_total - plan_total

    lines = [
        f"✅ *Платёж записан* #{payment_id}",
        f"📍 {obj['name']}",
        f"📅 Период: {period}",
        f"💰 {parsed['amount']:,.0f} ₽  ({parsed.get('payment_type','?')})",
        "",
        f"📊 *Сальдо за {period}:*",
        f"  Аренда:  {s['paid_rent']:,.0f} / {obj['plan_rent']:,.0f} ₽",
        f"  ЖКУ:     {s['paid_utility']:,.0f} / {obj['plan_utility']:,.0f} ₽",
        f"  Итого:   {paid_total:,.0f} / {plan_total:,.0f} ₽",
        "",
        f"{'✅ Переплата: +' if delta >= 0 else '⚠️ Долг: '}{abs(delta):,.0f} ₽",
    ]
    if parsed.get("payer"):
        lines.append(f"👤 {parsed['payer']}")
    if parsed.get("notes"):
        lines.append(f"📝 {parsed['notes']}")
    return "\n".join(lines)

def balance_message(chat_id: int, period: str = None) -> str:
    period = period or datetime.now().strftime("%Y-%m")
    objects = db.get_objects(chat_id)
    if not objects:
        return "Объектов нет. Добавь через меню → Добавить объект."

    lines = [f"📊 *Баланс за {period}*\n"]
    total_plan = total_paid = 0

    for o in objects:
        s     = db.get_monthly_summary(o["id"], period)
        plan  = o["plan_rent"] + o["plan_utility"]
        paid  = s["paid_rent"] + s["paid_utility"]
        delta = paid - plan
        total_plan += plan
        total_paid += paid

        icon = "✅" if delta >= 0 else ("🟡" if paid > 0 else "🔴")
        lines.append(
            f"{icon} *{o['name']}*\n"
            f"  {paid:,.0f} / {plan:,.0f} ₽"
            + (f"  \\(+{delta:,.0f}\\)" if delta >= 0 else f"  \\({delta:,.0f}\\)")
        )

    td = total_paid - total_plan
    lines += [
        "",
        f"─────────────────",
        f"Итого: {total_paid:,.0f} / {total_plan:,.0f} ₽",
        f"{'✅ +' if td >= 0 else '⚠️ '}{abs(td):,.0f} ₽",
    ]
    return "\n".join(lines)
