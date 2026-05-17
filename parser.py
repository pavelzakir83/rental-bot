import base64
import json
import os
import anthropic

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

PROMPT = """Из документа/изображения извлеки данные о платеже. Верни ТОЛЬКО JSON без пояснений:
{
  "is_payment": true/false,
  "object_hint": "название объекта, адрес, номер квартиры — всё что поможет идентифицировать",
  "amount": 12500.00,
  "payment_type": "rent|utility|both|unknown",
  "date": "YYYY-MM-DD или null",
  "period": "YYYY-MM или null",
  "payer": "имя плательщика или null",
  "notes": "важные детали"
}
Если это НЕ платёж — {"is_payment": false}.
amount — только число в рублях."""

def _parse_response(text: str) -> dict:
    raw = text.strip().replace("```json","").replace("```","").strip()
    return json.loads(raw)

def parse_photo(photo_bytes: bytes) -> dict:
    b64 = base64.standard_b64encode(photo_bytes).decode()
    r = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=600,
        messages=[{"role":"user","content":[
            {"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":b64}},
            {"type":"text","text":PROMPT}
        ]}]
    )
    return _parse_response(r.content[0].text)

def parse_text(text: str) -> dict:
    r = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=600,
        messages=[{"role":"user","content":f"{PROMPT}\n\nДокумент:\n{text}"}]
    )
    return _parse_response(r.content[0].text)
