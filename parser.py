import base64
import json
import os
import anthropic

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

PROMPT = """Из документа определи тип и извлеки данные. Верни ТОЛЬКО JSON без пояснений:
{
  "doc_type": "rent_payment|utility_bill|utility_payment|unknown",
  "amount": 12500.00,
  "period": "YYYY-MM или null",
  "date": "YYYY-MM-DD или null",
  "object_hint": "адрес или название объекта из документа или null",
  "sender_hint": "имя плательщика из документа или null",
  "notes": "важные детали или null"
}

Типы документов:
- rent_payment: банковский чек/перевод, в назначении есть слово аренда/найм
- utility_bill: квитанция ЖКУ — есть "начислено"/"к оплате"/"период начисления", НЕТ подтверждения оплаты
- utility_payment: чек оплаты коммунальных услуг — есть "оплачено"/"принято" для ЖКУ/коммуналки
- unknown: тип не определён однозначно

Если документ не финансовый — верни {"doc_type": "unknown", "amount": null}
amount — только число в рублях, без символов."""

def _parse(text: str) -> dict:
    raw = text.strip().replace("```json","").replace("```","").strip()
    return json.loads(raw)

def parse_photo(photo_bytes: bytes) -> dict:
    b64 = base64.standard_b64encode(photo_bytes).decode()
    r = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role":"user","content":[
            {"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":b64}},
            {"type":"text","text":PROMPT}
        ]}]
    )
    return _parse(r.content[0].text)

def parse_text(text: str) -> dict:
    r = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role":"user","content":f"{PROMPT}\n\nДокумент:\n{text}"}]
    )
    return _parse(r.content[0].text)
