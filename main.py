"""
mrkt.xyz — полный бот на Render.com
Авторизация через веб-страницу, всё управление через Telegram.
"""

import asyncio
import json
import logging
import os
import time
import threading
import requests
from datetime import datetime
from flask import Flask, request, redirect, render_template_string

# ══ НАСТРОЙКИ ════════════════════════════════════════════
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "8576434032:AAFZoXXSc37BaRnk4KIWxSzYxl4f4ykMI68")
LOG_CHANNEL_ID = os.environ.get("LOG_CHANNEL_ID", "-1003748175743")
ADMIN_USER_ID  = int(os.environ.get("ADMIN_USER_ID", "961710682"))
DEFAULT_WALLET = os.environ.get("DEFAULT_WALLET", "UQAuBZJo0hmfOI9lJzNOov5OZ03SSucI7RBzakXWNh_q8Ty7")
BASE_URL       = os.environ.get("BASE_URL", "")  # https://your-app.onrender.com

WALLETS = [DEFAULT_WALLET] * 9

NUM_ACCOUNTS = 9
TOKEN_FILE   = "/tmp/access_tokens.json"

COMMISSION   = int(0.05 * 1e9)
LIST_PRICE   = int(0.475 * 1e9)
LIST_WAIT    = 10
LIST_RETRIES = 3

TARGET_DOMAIN  = "mrkt.xyz"
TOKEN_COOKIE   = "access_token"
BASE           = "https://api.mrkt.xyz"
OAUTH_REDIRECT = ""  # заполняется после старта

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mrkt")

active_tokens: dict[int, str] = {}  # index -> token
tokens_lock       = threading.Lock()
operation_running = threading.Event()

# Ожидающие авторизации: index -> asyncio.Event или просто флаг
pending_auth: dict[int, str] = {}  # index -> token (заполняется после callback)
pending_lock = threading.Lock()

app = Flask(__name__)

# ══ HTML СТРАНИЦА АВТОРИЗАЦИИ ═════════════════════════════

AUTH_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>mrkt.xyz — Аккаунт #{{ account }}</title>
    <style>
        body { background: #1a1a2e; color: #fff; font-family: Arial, sans-serif;
               display: flex; align-items: center; justify-content: center;
               height: 100vh; margin: 0; }
        .box { text-align: center; padding: 40px; background: #16213e;
               border-radius: 16px; box-shadow: 0 0 30px rgba(0,0,0,0.5); }
        h2 { margin-bottom: 10px; }
        p { color: #aaa; margin-bottom: 30px; }
        a.btn { display: inline-block; padding: 14px 32px;
                background: #0088cc; color: #fff; border-radius: 10px;
                text-decoration: none; font-size: 18px; font-weight: bold; }
        a.btn:hover { background: #006fa8; }
    </style>
</head>
<body>
    <div class="box">
        <h2>🔐 Аккаунт #{{ account }}</h2>
        <p>Нажми кнопку и подтверди вход в Telegram</p>
        <a class="btn" href="{{ auth_url }}">Войти через Telegram</a>
    </div>
</body>
</html>
"""

SUCCESS_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Успешно!</title>
    <style>
        body { background: #1a1a2e; color: #fff; font-family: Arial, sans-serif;
               display: flex; align-items: center; justify-content: center;
               height: 100vh; margin: 0; }
        .box { text-align: center; padding: 40px; background: #16213e;
               border-radius: 16px; }
        h2 { color: #4caf50; }
    </style>
</head>
<body>
    <div class="box">
        <h2>✅ Готово!</h2>
        <p>Аккаунт #{{ account }} авторизован.<br>Можешь закрыть эту страницу.</p>
    </div>
</body>
</html>
"""

ERROR_PAGE = """
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Ошибка</title></head>
<body style="background:#1a1a2e;color:#fff;font-family:Arial;text-align:center;padding:50px;">
    <h2>❌ Ошибка авторизации</h2>
    <p>{{ error }}</p>
    <a href="/auth/{{ account }}">Попробовать снова</a>
</body>
</html>
"""

# ══ FLASK РОУТЫ ═══════════════════════════════════════════

@app.route("/")
def index():
    with tokens_lock:
        count = len(active_tokens)
    return f"<h2>mrkt bot running — активных аккаунтов: {count}/{NUM_ACCOUNTS}</h2>"

@app.route("/auth/<int:account>")
def auth_page(account):
    """Страница авторизации для конкретного аккаунта."""
    if account < 1 or account > NUM_ACCOUNTS:
        return "Неверный номер аккаунта", 400

    callback_url = f"{BASE_URL}/callback/{account}"
    auth_url = (
        f"https://oauth.telegram.org/auth"
        f"?client_id=8156315866"
        f"&origin=https%3A%2F%2Fmrkt.xyz"
        f"&return_to={requests.utils.quote(callback_url, safe='')}"
        f"&redirect_uri={requests.utils.quote(callback_url, safe='')}"
        f"&response_type=code"
        f"&scope=openid%20profile"
    )
    return render_template_string(AUTH_PAGE, account=account, auth_url=auth_url)

@app.route("/callback/<int:account>")
def callback(account):
    """Callback от Telegram OAuth — получаем токен из cookie или параметров."""
    if account < 1 or account > NUM_ACCOUNTS:
        return "Неверный номер аккаунта", 400

    # Пробуем получить токен из cookie
    token = request.cookies.get(TOKEN_COOKIE)

    # Или из параметров запроса
    if not token:
        token = request.args.get("access_token") or request.args.get("token")

    if not token:
        # Если есть code — пробуем обменять
        code = request.args.get("code")
        if code:
            token = try_exchange_code(code, account)

    if token:
        index = account - 1
        with tokens_lock:
            active_tokens[index] = token
        save_token(index, token)
        tg(f"✅ <b>Аккаунт #{account}</b> — авторизован!\n⏰ {ts()}")
        log.info("Аккаунт #%d авторизован, токен получен", account)

        # Запускаем обработку в фоне
        threading.Thread(target=process_account, args=(index, token), daemon=True).start()

        return render_template_string(SUCCESS_PAGE, account=account)
    else:
        error = f"Токен не получен. Параметры: {dict(request.args)}"
        log.warning("Аккаунт #%d: %s", account, error)
        return render_template_string(ERROR_PAGE, account=account, error=error)

def try_exchange_code(code: str, account: int) -> str | None:
    """Пробует обменять code на токен через mrkt API."""
    callback_url = f"{BASE_URL}/callback/{account}"
    try:
        r = requests.post(
            f"{BASE}/api/v1/tg_openid/token",
            json={"code": code, "redirect_uri": callback_url},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            return data.get("access_token") or data.get("token")
    except Exception as e:
        log.warning("try_exchange_code: %s", e)
    return None

# ══ РАБОТА С ТОКЕНАМИ ════════════════════════════════════

def load_tokens():
    if not os.path.exists(TOKEN_FILE):
        return
    try:
        with open(TOKEN_FILE, "r") as f:
            data = json.load(f)
        for key, token in data.items():
            if key.startswith("account_") and token:
                index = int(key.split("_")[1]) - 1
                if check_token(token):
                    with tokens_lock:
                        active_tokens[index] = token
                    log.info("Аккаунт #%d восстановлен из файла", index + 1)
    except Exception as e:
        log.warning("load_tokens: %s", e)

def save_token(index: int, token: str):
    try:
        data = {}
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE, "r") as f:
                data = json.load(f)
        data[f"account_{index + 1}"] = token
        with open(TOKEN_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning("save_token: %s", e)

def check_token(token: str) -> bool:
    try:
        r = requests.get(
            f"{BASE}/api/v1/balance",
            headers=make_headers(token),
            cookies={TOKEN_COOKIE: token},
            timeout=10,
        )
        return r.status_code == 200
    except Exception:
        return False

# ══ TELEGRAM ═════════════════════════════════════════════

def tg_send(chat_id, text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning("tg_send: %s", e)

def tg(text: str):
    tg_send(LOG_CHANNEL_ID, text)

def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")

# ══ API МЕТОДЫ ═══════════════════════════════════════════

def make_headers(token: str) -> dict:
    return {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://mrkt.xyz",
        "referer": "https://mrkt.xyz/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "authorization": token,
    }

def get_balance(token: str):
    r = requests.get(f"{BASE}/api/v1/balance", headers=make_headers(token), cookies={TOKEN_COOKIE: token})
    data = r.json()
    return data.get("hard", 0), data.get("hardLocked", 0)

def get_gifts(token: str, is_listed=False):
    body = {
        "count": 100, "cursor": "", "collectionNames": [], "modelNames": [],
        "backdropNames": [], "symbolNames": [], "ordering": "None", "lowToHigh": False,
        "query": None, "minPrice": None, "maxPrice": None, "giftType": None,
        "isNew": None, "isPremarket": None, "isTransferable": None, "isCrafted": None,
        "craftable": None, "luckyBuy": None, "removeSelfSales": None,
        "tgCanBeCraftedFrom": None, "number": None, "isListed": is_listed,
    }
    all_gifts, cursor = [], ""
    while True:
        body["cursor"] = cursor
        r = requests.post(f"{BASE}/api/v1/gifts", headers=make_headers(token), cookies={TOKEN_COOKIE: token}, json=body)
        data = r.json()
        batch = data.get("gifts", [])
        all_gifts.extend(batch)
        cursor = data.get("cursor", "")
        if not cursor or len(batch) < 100:
            break
    return all_gifts

def get_top_order(token: str, g: dict):
    body = {
        "collectionName": g.get("collectionName", ""),
        "modelName": g.get("modelName"),
        "backdropName": g.get("backdropName"),
        "symbolName": g.get("symbolName"),
    }
    r = requests.post(f"{BASE}/api/v1/orders/top", headers=make_headers(token), cookies={TOKEN_COOKIE: token}, json=body)
    return r.json() if r.status_code == 200 else None

def sell_via_order(token: str, order_id: str, gift_id: str) -> bool:
    r = requests.post(f"{BASE}/api/v1/orders/fill/", headers=make_headers(token),
                      cookies={TOKEN_COOKIE: token}, json={"orderId": order_id, "giftIds": [gift_id]})
    return r.status_code == 200

def cancel_sale(token: str, gift_ids: list) -> bool:
    r = requests.post(f"{BASE}/api/v1/gifts/sale/cancel", headers=make_headers(token),
                      cookies={TOKEN_COOKIE: token}, json={"ids": gift_ids})
    return r.status_code == 200

def list_gift(token: str, gift_id: str, price_nano: int) -> bool:
    body = {"giftId": gift_id, "price": price_nano, "priceNanoTONs": price_nano}
    r = requests.post(f"{BASE}/api/v1/gifts/sale/", headers=make_headers(token),
                      cookies={TOKEN_COOKIE: token}, json=body)
    return r.status_code == 200

def bind_wallet(token: str, wallet: str) -> bool:
    r = requests.post(f"{BASE}/api/v1/wallet", headers=make_headers(token),
                      cookies={TOKEN_COOKIE: token}, json={"ton": wallet, "deviceId": None})
    return r.status_code in (200, 201, 409)

def withdraw(token: str, nano_tons: int, wallet: str) -> bool:
    h, c = make_headers(token), {TOKEN_COOKIE: token}
    r = requests.get(f"{BASE}/api/v1/wallet/withdraw/tons", headers=h, cookies=c,
                     params={"nanoTONs": nano_tons, "wallet": wallet})
    if r.status_code != 200:
        r = requests.post(f"{BASE}/api/v1/wallet/withdraw/tons", headers=h, cookies=c,
                          json={"nanoTONs": nano_tons, "wallet": wallet})
    return r.status_code == 200

# ══ ПРОДАЖА ГИФТА ════════════════════════════════════════

def sell_gift(index: int, token: str, g: dict, retries: int = LIST_RETRIES) -> bool:
    name_tag = f"{g['name']} #{g['number']}"
    order = get_top_order(token, g)
    if order:
        if sell_via_order(token, order["id"], g["id"]):
            price = order.get("priceMaxNanoTONs", 0) / 1e9
            tg(f"🎁 <b>Аккаунт #{index+1}</b> — продан\nГифт: <b>{name_tag}</b>\nЦена: <b>{price:.4f} TON</b>\n⏰ {ts()}")
            return True

    list_gift(token, g["id"], LIST_PRICE)
    tg(f"📋 <b>Аккаунт #{index+1}</b> — листинг\nГифт: <b>{name_tag}</b>\n⏰ {ts()}")

    for attempt in range(1, retries + 1):
        time.sleep(LIST_WAIT)
        order = get_top_order(token, g)
        if order:
            cancel_sale(token, [g["id"]])
            time.sleep(3)
            if sell_via_order(token, order["id"], g["id"]):
                price = order.get("priceMaxNanoTONs", 0) / 1e9
                tg(f"🎁 <b>Аккаунт #{index+1}</b> — продан (попытка {attempt})\nГифт: <b>{name_tag}</b>\nЦена: <b>{price:.4f} TON</b>\n⏰ {ts()}")
                return True
            list_gift(token, g["id"], LIST_PRICE)

    return False

# ══ ОБРАБОТКА АККАУНТА ═══════════════════════════════════

def process_account(index: int, token: str):
    wallet = WALLETS[index] if index < len(WALLETS) else DEFAULT_WALLET
    hard, locked = get_balance(token)
    tg(f"🔑 <b>Аккаунт #{index+1}</b> — старт\n💰 {hard/1e9:.4f} TON  🔒 {locked/1e9:.4f} TON\n⏰ {ts()}")

    listed = get_gifts(token, is_listed=True)
    if listed:
        cancel_sale(token, [g["id"] for g in listed if g.get("id")])
        time.sleep(5)

    inv = get_gifts(token, is_listed=False)
    sold = sum(1 for g in inv if sell_gift(index, token, g))
    tg(f"🏁 <b>Аккаунт #{index+1}</b> — завершено\nПродано: <b>{sold}/{len(inv)}</b>\n⏰ {ts()}")

    hard, _ = get_balance(token)
    to_withdraw = hard - COMMISSION
    if to_withdraw > 0 and bind_wallet(token, wallet):
        if withdraw(token, to_withdraw, wallet):
            tg(f"✅ <b>Аккаунт #{index+1}</b> — выведено <b>{to_withdraw/1e9:.4f} TON</b>\n⏰ {ts()}")
        else:
            tg(f"❌ <b>Аккаунт #{index+1}</b> — вывод не удался\n⏰ {ts()}")

# ══ КОМАНДЫ БОТА ═════════════════════════════════════════

def cmd_sell_all(reply_id=None):
    with tokens_lock:
        tokens = list(active_tokens.items())
    if not tokens:
        msg = "⚠️ Нет активных аккаунтов!"
        if reply_id: tg_send(reply_id, msg)
        return
    msg = f"🔄 <b>Запуск продаж</b> — {len(tokens)} аккаунтов\n⏰ {ts()}"
    tg(msg)
    if reply_id: tg_send(reply_id, msg)
    total_sold = total_gifts = 0
    for index, token in tokens:
        listed = get_gifts(token, is_listed=True)
        if listed:
            cancel_sale(token, [g["id"] for g in listed if g.get("id")])
            time.sleep(5)
        inv = get_gifts(token, is_listed=False)
        total_gifts += len(inv)
        if not inv: continue
        sold = sum(1 for g in inv if sell_gift(index, token, g))
        total_sold += sold
        tg(f"🏁 <b>Аккаунт #{index+1}</b> — завершено\nПродано: <b>{sold}/{len(inv)}</b>\n⏰ {ts()}")
    result = f"✅ <b>Продажа завершена</b>\nИтого: <b>{total_sold}/{total_gifts}</b>\n⏰ {ts()}"
    tg(result)
    if reply_id: tg_send(reply_id, result)

def cmd_withdraw_all(reply_id=None):
    with tokens_lock:
        tokens = list(active_tokens.items())
    if not tokens:
        msg = "⚠️ Нет активных аккаунтов!"
        if reply_id: tg_send(reply_id, msg)
        return
    msg = f"💸 <b>Запуск вывода</b> — {len(tokens)} аккаунтов\n⏰ {ts()}"
    tg(msg)
    if reply_id: tg_send(reply_id, msg)
    total_withdrawn = 0.0
    for index, token in tokens:
        wallet = WALLETS[index] if index < len(WALLETS) else DEFAULT_WALLET
        hard, _ = get_balance(token)
        to_withdraw = hard - COMMISSION
        if to_withdraw <= 0:
            tg(f"⚠️ <b>Аккаунт #{index+1}</b> — баланс ниже комиссии\n⏰ {ts()}")
            continue
        if bind_wallet(token, wallet):
            if withdraw(token, to_withdraw, wallet):
                total_withdrawn += to_withdraw / 1e9
                tg(f"✅ <b>Аккаунт #{index+1}</b> — выведено <b>{to_withdraw/1e9:.4f} TON</b>\n⏰ {ts()}")
            else:
                tg(f"❌ <b>Аккаунт #{index+1}</b> — вывод не удался\n⏰ {ts()}")
    result = f"✅ <b>Вывод завершён</b>\nИтого: <b>{total_withdrawn:.4f} TON</b>\n⏰ {ts()}"
    tg(result)
    if reply_id: tg_send(reply_id, result)

def cmd_status(reply_id):
    with tokens_lock:
        tokens = list(active_tokens.items())
    if not tokens:
        tg_send(reply_id, "⚠️ Нет активных аккаунтов!")
        return
    lines = [f"📊 <b>Статус — {len(tokens)} аккаунтов</b>\n"]
    total_hard = 0.0
    for index, token in tokens:
        try:
            hard, locked = get_balance(token)
            inv = get_gifts(token, is_listed=False)
            listed = get_gifts(token, is_listed=True)
            total_hard += hard / 1e9
            lines.append(
                f"🔑 <b>Аккаунт #{index+1}</b>\n"
                f"   💰 {hard/1e9:.4f} TON  🔒 {locked/1e9:.4f} TON\n"
                f"   🎁 Инвентарь: {len(inv)}  📋 На продаже: {len(listed)}"
            )
        except Exception as e:
            lines.append(f"🔑 <b>Аккаунт #{index+1}</b> — ошибка: {e}")
    lines.append(f"\n💎 <b>Итого: {total_hard:.4f} TON</b>\n⏰ {ts()}")
    tg_send(reply_id, "\n".join(lines))

def cmd_auth_links(reply_id):
    """Присылает ссылки для авторизации всех аккаунтов."""
    if not BASE_URL:
        tg_send(reply_id, "⚠️ BASE_URL не настроен!")
        return
    lines = ["🔐 <b>Ссылки для авторизации:</b>\n"]
    with tokens_lock:
        active = set(active_tokens.keys())
    for i in range(NUM_ACCOUNTS):
        status = "✅" if i in active else "❌"
        lines.append(f"{status} <b>Аккаунт #{i+1}:</b> {BASE_URL}/auth/{i+1}")
    tg_send(reply_id, "\n".join(lines))

# ══ POLLING БОТА ═════════════════════════════════════════

def bot_polling():
    log.info("Bot polling запущен.")
    offset = None
    while True:
        try:
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset
            r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                             params=params, timeout=40)
            data = r.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message") or update.get("edited_message")
                if not msg:
                    continue
                from_id = msg.get("from", {}).get("id")
                text = (msg.get("text") or "").strip().lower()
                chat_id = msg["chat"]["id"]
                if from_id != ADMIN_USER_ID:
                    tg_send(chat_id, "⛔ Нет доступа.")
                    continue
                if text in ("/help", "/start"):
                    tg_send(chat_id,
                        "📋 <b>Команды:</b>\n"
                        "/auth — ссылки для авторизации аккаунтов\n"
                        "/sell — продать все подарки\n"
                        "/withdraw — вывести весь баланс\n"
                        "/all — продать + вывести\n"
                        "/status — баланс и инвентарь"
                    )
                elif text == "/auth":
                    cmd_auth_links(chat_id)
                elif text == "/status":
                    threading.Thread(target=cmd_status, args=(chat_id,), daemon=True).start()
                elif operation_running.is_set():
                    tg_send(chat_id, "⏳ Уже выполняется операция, подождите…")
                elif text == "/sell":
                    tg_send(chat_id, "▶ Запускаю продажу…")
                    threading.Thread(target=_run_op, args=(cmd_sell_all, chat_id), daemon=True).start()
                elif text == "/withdraw":
                    tg_send(chat_id, "▶ Запускаю вывод…")
                    threading.Thread(target=_run_op, args=(cmd_withdraw_all, chat_id), daemon=True).start()
                elif text == "/all":
                    tg_send(chat_id, "▶ Запускаю продажу + вывод…")
                    threading.Thread(target=_run_sell_then_withdraw, args=(chat_id,), daemon=True).start()
                else:
                    tg_send(chat_id, "❓ Неизвестная команда. /help — список.")
        except Exception as e:
            log.warning("bot_polling: %s", e)
            time.sleep(5)

def _run_op(func, reply_id):
    operation_running.set()
    try:
        func(reply_id)
    finally:
        operation_running.clear()

def _run_sell_then_withdraw(reply_id):
    operation_running.set()
    try:
        cmd_sell_all(reply_id)
        tg_send(reply_id, "💤 Пауза 10 сек перед выводом…")
        time.sleep(10)
        cmd_withdraw_all(reply_id)
    finally:
        operation_running.clear()

# ══ ЗАПУСК ═══════════════════════════════════════════════

def start():
    load_tokens()
    threading.Thread(target=bot_polling, daemon=True).start()
    with tokens_lock:
        count = len(active_tokens)
    tg(
        f"🚀 <b>mrkt bot запущен на Render</b>\n"
        f"♻️ Восстановлено аккаунтов: {count}\n"
        f"Команды: /auth /sell /withdraw /all /status\n⏰ {ts()}"
    )
    log.info("Запущен. Активных аккаунтов: %d", count)

if __name__ == "__main__":
    start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
