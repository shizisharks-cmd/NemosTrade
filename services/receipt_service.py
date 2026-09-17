import io
from PIL import Image, ImageDraw, ImageFont

def generate_receipt_image(op_type: str, amount_str: str, client_str: str, date_str: str, method_str: str, tx_num: str) -> bytes:
    """Генерация фирменного электронного чека транзакции в темном стиле Nemos Trade."""
    w, h = 800, 520
    img = Image.new("RGB", (w, h), color="#0f1115")
    draw = ImageDraw.Draw(img)

    # Внешние рамки
    draw.rounded_rectangle([(15, 15), (w - 15, h - 15)], radius=20, outline="#242b35", width=2)
    draw.rounded_rectangle([(22, 22), (w - 22, h - 22)], radius=16, outline="#1b2028", width=1)

    # Шапка чека
    draw.rounded_rectangle([(30, 30), (w - 30, 110)], radius=12, fill="#161920", outline="#2a3240", width=1)

    try:
        font_logo = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 30)
        font_sub = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 15)
        font_lbl = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
        font_val = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 17)
        font_amt = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        font_foot = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13)
    except Exception:
        font_logo = font_sub = font_lbl = font_val = font_amt = font_foot = ImageFont.load_default()

    draw.text((50, 42), "NEMOS TRADE", fill="#00d2ff", font=font_logo)
    draw.text((50, 80), "ОФИЦИАЛЬНЫЙ ЭЛЕКТРОННЫЙ ЧЕК ПЛАТФОРМЫ", fill="#8a95a5", font=font_sub)
    draw.text((w - 200, 55), f"#{tx_num}", fill="#606f85", font=font_amt)

    rows = [
        ("ТИП ОПЕРАЦИИ:", op_type, "#ffffff"),
        ("СУММА:", amount_str, "#00ff88"),
        ("ПОЛУЧАТЕЛЬ / КЛИЕНТ:", client_str, "#ffffff"),
        ("СЕРВЕР:", "Grand Mobile #17", "#ffffff"),
        ("СПОСОБ:", method_str, "#ffffff"),
        ("ДАТА И ВРЕМЯ (МСК):", date_str, "#8a95a5"),
        ("СТАТУС:", "ВЫПОЛНЕНО / УСПЕШНО", "#00ff88")
    ]

    y = 135
    for lbl, val, color in rows:
        draw.text((50, y), lbl, fill="#6b798d", font=font_lbl)
        draw.text((320, y), val, fill=color, font=font_val if lbl != "СУММА:" else font_amt)
        draw.line([(50, y + 36), (w - 50, y + 36)], fill="#1c212a", width=1)
        y += 46

    draw.text((50, h - 45), "Проверено сервисом безопасности Nemos Trade  •  @NemosTrade", fill="#4d5868", font=font_foot)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
