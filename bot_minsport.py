"""
Эхо-бот MAX Bot API с меню, удалением сообщений и приветствием по имени.
Переменные окружения: MAX_BOT_TOKEN, WEBHOOK_SECRET; опционально MAX_USE_BEARER=1.
"""
from __future__ import annotations
import os
import json
import hmac
import logging

import requests
from flask import Flask, request, jsonify

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("max_bot")

app = Flask(__name__)

MAX_API = "https://platform-api.max.ru"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
TOKEN = (os.environ.get("MAX_BOT_TOKEN") or "").strip()
USE_BEARER = os.environ.get("MAX_USE_BEARER", "").lower() in ("1", "true", "yes")

_SEEN_MID = set()
_SEEN_MAX = 500
_USER_LAST_MID = {}


def auth_value() -> str:
    if USE_BEARER and not TOKEN.lower().startswith("bearer "):
        return f"Bearer {TOKEN}"
    return TOKEN


def api_headers():
    return {
        "Authorization": auth_value(),
        "Content-Type": "application/json",
    }


def extract_message_payload(data: dict):
    """Возвращает (user_id, user_name, chat_id, chat_type, text, mid) из Update."""
    if data.get("update_type") != "message_created":
        return None, None, None, None, None, None
    msg = data.get("message")
    if not isinstance(msg, dict):
        return None, None, None, None, None, None
    recipient = msg.get("recipient")
    if not isinstance(recipient, dict):
        return None, None, None, None, None, None
    body = msg.get("body")
    if not isinstance(body, dict):
        return None, None, None, None, None, None

    chat_id = recipient.get("chat_id")
    chat_type = recipient.get("chat_type")
    text = (body.get("text") or "").strip()
    mid = body.get("mid")

    user_id = None
    user_name = "Пользователь" # Значение по умолчанию

    sender = msg.get("sender")
    if isinstance(sender, dict) and not sender.get("is_bot"):
        user_id = sender.get("user_id")
        # Пытаемся получить имя, иначе берем username, иначе оставляем дефолт
        user_name = sender.get("first_name") or sender.get("username") or "Пользователь"

    return user_id, user_name, chat_id, chat_type, text, mid


def build_inline_keyboard(buttons_data: list[list[dict]]) -> dict:
    keyboard = []
    for row in buttons_data:
        keyboard_row = []
        for btn in row:
            keyboard_row.append({
                "type": "callback",
                "text": btn["text"],
                "payload": btn["payload"]
            })
        keyboard.append(keyboard_row)

    return {
        "attachments": [
            {
                "type": "inline_keyboard",
                "payload": {"buttons": keyboard}
            }
        ]
    }


def delete_max_message(message_id: int, user_id: int | None = None, chat_id: int | None = None):
    url = f"{MAX_API}/messages"
    params = {"message_id": int(message_id)}
    if user_id is not None:
        params["user_id"] = int(user_id)
    elif chat_id is not None:
        params["chat_id"] = int(chat_id)

    try:
        r = requests.delete(url, headers=api_headers(), params=params, timeout=10)
        if not r.ok:
            logger.warning("Не удалось удалить сообщение %s: %s", message_id, r.text[:200])
    except requests.RequestException as e:
        logger.exception("Ошибка сети при DELETE /messages: %s", e)


def send_max_message_with_body(user_id: int | None, chat_id: int | None, recipient_chat_type: str | None, body: dict) -> int | None:
    url = f"{MAX_API}/messages"
    params = {}
    ct = (recipient_chat_type or "").strip().lower()

    if ct in ("chat", "channel", "group", "dialog") and chat_id is not None:
        params["chat_id"] = int(chat_id)
    elif user_id is not None:
        params["user_id"] = int(user_id)
    elif chat_id is not None:
        params["chat_id"] = int(chat_id)
    else:
        logger.warning("send_max_message_with_body: нет user_id и chat_id")
        return None

    try:
        r = requests.post(url, headers=api_headers(), params=params, json=body, timeout=15)
        if r.ok:
            response_data = r.json()
            return response_data.get("message_id") or response_data.get("mid")
        else:
            logger.error("messages API: %s %s", r.status_code, r.text[:500])
            return None
    except requests.RequestException as e:
        logger.exception("Ошибка сети при POST /messages: %s", e)
        return None


def send_max_message(user_id: int | None, chat_id: int | None, recipient_chat_type: str | None, text: str) -> None:
    send_max_message_with_body(user_id, chat_id, recipient_chat_type, {"text": text})


def remember_mid(mid) -> bool:
    if not mid:
        return False
    if mid in _SEEN_MID:
        return True
    if len(_SEEN_MID) >= _SEEN_MAX:
        _SEEN_MID.clear()
    _SEEN_MID.add(mid)
    return False


# ==============================================================================
# ФУНКЦИИ ЭКРАНОВ (МЕНЮ)
# ==============================================================================

def send_start_menu(user_id: int, user_name: str, chat_id: int | None = None, chat_type: str | None = None):
    # 1. Удаляем предыдущее сообщение
    prev_mid = _USER_LAST_MID.get(user_id)
    if prev_mid:
        delete_max_message(prev_mid, user_id=user_id, chat_id=chat_id)

    # 2. Формируем текст с именем пользователя
    # !!! СЮДА ВСТАВЛЯТЬ СВОЙ ТЕКСТ ПРИВЕТСТВИЯ !!!
    welcome_text = f"""Привет, {user_name}! Вас приветствует бот-помощник Минспорта Оренбургской области
                        Чем я могу помочь?

                        Выберите категорию, которая Вас интересует:"""

    # !!! СЮДА ВСТАВЛЯТЬ СВОИ КНОПКИ !!!
    buttons_data = [
        [
            {"text": "Категория 1", "payload": "category_1"},
            {"text": "Категория 2", "payload": "category_2"}
        ],
        [
            {"text": "ℹ️ О нас", "payload": "about"}
        ]
    ]

    body = {"text": welcome_text}
    body.update(build_inline_keyboard(buttons_data))

    new_mid = send_max_message_with_body(user_id, chat_id, chat_type, body)
    if new_mid:
        _USER_LAST_MID[user_id] = new_mid


def send_category_1_menu(user_id: int, user_name: str, chat_id: int | None = None, chat_type: str | None = None):
    prev_mid = _USER_LAST_MID.get(user_id)
    if prev_mid:
        delete_max_message(prev_mid, user_id=user_id, chat_id=chat_id)

    body = {"text": f"Отличный выбор, {user_name}! Вы в Категории 1. Что делаем дальше?"}
    body.update(build_inline_keyboard([
        [{"text": "🔙 Назад в главное меню", "payload": "start"}],
        [{"text": "✅ Подтвердить", "payload": "confirm_cat1"}]
    ]))

    new_mid = send_max_message_with_body(user_id, chat_id, chat_type, body)
    if new_mid:
        _USER_LAST_MID[user_id] = new_mid

# ==============================================================================


@app.route("/webhook", methods=["POST", "HEAD"])
def webhook():
    if request.method == "HEAD":
        return "", 200

    if WEBHOOK_SECRET:
        got = request.headers.get("X-Max-Bot-Api-Secret", "")
        if not hmac.compare_digest(got, WEBHOOK_SECRET):
            return jsonify({"error": "forbidden"}), 403

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": True}), 200

    update_type = data.get("update_type")

    # --- 1. ОБРАБОТКА НАЖАТИЯ НА КНОПКУ (Callback) ---
    if update_type == "callback_query":
        callback_data = data.get("callback_query", {})
        payload = callback_data.get("payload")

        # Извлекаем объект пользователя из callback_query
        user_obj = callback_data.get("user") or data.get("user") or {}
        user_id = user_obj.get("user_id")
        # MAX API присылает имя в first_name или username
        user_name = user_obj.get("first_name") or user_obj.get("username") or "Пользователь"
        message_id = callback_data.get("message_id")

        logger.info("Получен callback_query: payload=%s, user_id=%s, name=%s", payload, user_id, user_name)

        if user_id:
            if message_id:
                delete_max_message(message_id, user_id=user_id)

            if payload == "start":
                send_start_menu(user_id, user_name)
            elif payload == "category_1":
                send_category_1_menu(user_id, user_name)
            elif payload == "category_2":
                send_max_message(user_id, None, None, f"{user_name}, здесь будет меню Категории 2")
            elif payload == "about":
                send_max_message(user_id, None, None, f"{user_name}, мы — лучшая компания!")
            else:
                logger.warning("Необработанный payload: %s", payload)

        return jsonify({"ok": True}), 200

    # --- 2. ОБРАБОТКА ОБЫЧНЫХ СООБЩЕНИЙ ---
    if update_type == "message_created":
        user_id, user_name, chat_id, chat_type, user_text, mid = extract_message_payload(data)

        if mid and remember_mid(mid):
            logger.info("Пропуск дубликата по mid=%s", mid)
            return jsonify({"ok": True}), 200

        if user_text and user_id is not None:
            text_lower = user_text.lower().strip()

            if mid:
                delete_max_message(mid, user_id=user_id, chat_id=chat_id)

            if text_lower in ("/start", "старт", "start"):
                send_start_menu(user_id, user_name, chat_id, chat_type)
            else:
                send_max_message(
                    user_id, chat_id, chat_type,
                    f"{user_name}, пожалуйста, используйте кнопки меню для навигации."
                )
        elif user_id is None and chat_id is None:
            logger.warning("Не удалось извлечь user_id/chat_id; payload=%s", json.dumps(data, ensure_ascii=False)[:1200])

    elif update_type == "bot_started":
        logger.info("bot_started: %s", json.dumps(data, ensure_ascii=False)[:500])

    return jsonify({"ok": True}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Задайте MAX_BOT_TOKEN")
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
