"""
Chat Bot for MAX Messenger API
Features: Inline keyboards, message deletion (app-like UI), personalized greetings.
"""
from __future__ import annotations
import os
import json
import hmac
import logging
import requests
from flask import Flask, request, jsonify

# ==============================================================================
# CONFIGURATION & LOGGING
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    handlers=[
        logging.FileHandler("/app/bot.log", encoding='utf-8'), # Чистое имя файла
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

MAX_API = "https://platform-api.max.ru"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
TOKEN = os.environ.get("MAX_BOT_TOKEN", "").strip()
USE_BEARER = os.environ.get("MAX_USE_BEARER", "").lower() in ("1", "true", "yes")

# Хранилище ID последнего сообщения для каждого пользователя (для эффекта "приложения")
_USER_LAST_MID: dict[int, str] = {}


# ==============================================================================
# HELPERS
# ==============================================================================
def _get_auth_headers() -> dict:
    auth = f"Bearer {TOKEN}" if USE_BEARER and not TOKEN.lower().startswith("bearer ") else TOKEN
    return {"Authorization": auth, "Content-Type": "application/json"}


def _get_request_params(user_id: int | None, chat_id: int | None, chat_type: str | None) -> dict:
    """Определяет правильные query-параметры для API MAX."""
    ct = (chat_type or "").strip().lower()
    if ct == "dialog" and user_id:
        return {"user_id": user_id}
    if ct in ("chat", "channel", "group") and chat_id:
        return {"chat_id": chat_id}
    if user_id:
        return {"user_id": user_id}
    if chat_id:
        return {"chat_id": chat_id}
    return {}


def _build_keyboard(buttons_data: list[list[dict]]) -> dict:
    """Собирает inline-клавиатуру из двумерного массива кнопок."""
    keyboard = [[{"type": "callback", "text": btn["text"], "payload": btn["payload"]} for btn in row] for row in buttons_data]
    return {"attachments": [{"type": "inline_keyboard", "payload": {"buttons": keyboard}}]}


# ==============================================================================
# API INTERACTIONS
# ==============================================================================
def delete_message(message_id: str, user_id: int | None = None, chat_id: int | None = None):
    """Удаляет сообщение бота."""
    params = {"message_id": str(message_id)}
    params.update(_get_request_params(user_id, chat_id, None))
    
    try:
        r = requests.delete(f"{MAX_API}/messages", headers=_get_auth_headers(), params=params, timeout=10)
        if r.ok:
            logger.info("Сообщение %s удалено", message_id)
        else:
            logger.warning("Ошибка удаления %s: %s", message_id, r.text[:100])
    except requests.RequestException as e:
        logger.error("Network error deleting message: %s", e)


def send_message(user_id: int | None, chat_id: int | None, chat_type: str | None, body: dict) -> str | None:
    """Отправляет сообщение и возвращает его MID."""
    params = _get_request_params(user_id, chat_id, chat_type)
    if not params:
        logger.error("Cannot send message: missing user_id and chat_id")
        return None
        
    try:
        r = requests.post(f"{MAX_API}/messages", headers=_get_auth_headers(), params=params, json=body, timeout=15)
        if r.ok:
            data = r.json().get("message", {}).get("body", {})
            return data.get("mid") or r.json().get("message", {}).get("message_id")
        logger.error("API Error %s: %s", r.status_code, r.text[:200])
        return None
    except requests.RequestException as e:
        logger.error("Network error sending message: %s", e)
        return None


# ==============================================================================
# SCREENS / MENUS
# ==============================================================================
def show_start_screen(user_id: int, user_name: str, chat_id: int | None = None, chat_type: str | None = None):
    if prev_mid := _USER_LAST_MID.get(user_id):
        delete_message(prev_mid, user_id=user_id, chat_id=chat_id)

    # !!! МЕНЯЙ ТЕКСТ И КНОПКИ ГЛАВНОГО МЕНЮ ЗДЕСЬ !!!
    text = f"👋 Привет, {user_name}! Добро пожаловать. Выберите категорию:"
    buttons = [
        [{"text": "📁 Категория 1", "payload": "category_1"}, {"text": "📁 Категория 2", "payload": "category_2"}],
        [{"text": "ℹ️ О нас", "payload": "about"}]
    ]
    
    body = {"text": text}
    body.update(_build_keyboard(buttons))
    
    if new_mid := send_message(user_id, chat_id, chat_type, body):
        _USER_LAST_MID[user_id] = new_mid


def show_category_1_screen(user_id: int, user_name: str, chat_id: int | None = None, chat_type: str | None = None):
    if prev_mid := _USER_LAST_MID.get(user_id):
        delete_message(prev_mid, user_id=user_id, chat_id=chat_id)

    # !!! МЕНЯЙ ТЕКСТ И КНОПКИ ЭТОГО ЭКРАНА ЗДЕСЬ !!!
    body = {"text": f"Отличный выбор, {user_name}! Вы в Категории 1."}
    body.update(_build_keyboard([[{"text": "🔙 Назад", "payload": "start"}]]))
    
    if new_mid := send_message(user_id, chat_id, chat_type, body):
        _USER_LAST_MID[user_id] = new_mid


# ==============================================================================
# WEBHOOK HANDLERS
# ==============================================================================
def _handle_callback(data: dict) -> tuple:
    callback = data.get("callback", {})
    user = callback.get("user", {})
    user_id = user.get("user_id")
    user_name = user.get("first_name") or user.get("username") or "Пользователь"
    payload = callback.get("payload")
    message_id = data.get("message", {}).get("body", {}).get("mid")

    if not user_id:
        return jsonify({"ok": True}), 200

    logger.info("Кнопка нажата: payload='%s', user=%s", payload, user_name)
    
    if message_id:
        delete_message(message_id, user_id=user_id)

    if payload == "start":
        show_start_screen(user_id, user_name)
    elif payload == "category_1":
        show_category_1_screen(user_id, user_name)
    elif payload == "category_2":
        send_message(user_id, None, None, {"text": f"{user_name}, здесь будет меню Категории 2"})
    elif payload == "about":
        send_message(user_id, None, None, {"text": f"{user_name}, мы — лучшая компания!"})
    else:
        logger.warning("Необработанный payload: %s", payload)

    return jsonify({"ok": True}), 200


def _handle_bot_started(data: dict) -> tuple:
    user = data.get("user", {})
    if user_id := user.get("user_id"):
        user_name = user.get("first_name") or user.get("username") or "Пользователь"
        logger.info("Бот запущен пользователем: %s", user_name)
        show_start_screen(user_id, user_name)
    return jsonify({"ok": True}), 200


def _handle_message_created(data: dict) -> tuple:
    msg = data.get("message", {})
    sender = msg.get("sender", {})
    body = msg.get("body", {})
    
    if not sender or sender.get("is_bot"):
        return jsonify({"ok": True}), 200

    user_id = sender.get("user_id")
    user_name = sender.get("first_name") or sender.get("username") or "Пользователь"
    chat_id = msg.get("recipient", {}).get("chat_id")
    chat_type = msg.get("recipient", {}).get("chat_type")
    text = (body.get("text") or "").strip().lower()

    if user_id and text:
        logger.info("Сообщение от %s: '%s'", user_name, text)
        # На любой текст показываем главное меню (или можно добавить логику команд)
        show_start_screen(user_id, user_name, chat_id, chat_type)
        
    return jsonify({"ok": True}), 200


# ==============================================================================
# FLASK ROUTES
# ==============================================================================
@app.route("/webhook", methods=["POST", "HEAD"])
def webhook():
    if request.method == "HEAD":
        return "", 200

    if WEBHOOK_SECRET and not hmac.compare_digest(request.headers.get("X-Max-Bot-Api-Secret", ""), WEBHOOK_SECRET):
        logger.warning("Отклонен запрос: неверный WEBHOOK_SECRET")
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": True}), 200

    update_type = data.get("update_type")
    logger.debug("Получено событие: %s", update_type)

    if update_type == "message_callback":
        return _handle_callback(data)
    if update_type == "bot_started":
        return _handle_bot_started(data)
    if update_type == "message_created":
        return _handle_message_created(data)

    return jsonify({"ok": True}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Ошибка: Не задана переменная окружения MAX_BOT_TOKEN")
    
    port = int(os.environ.get("PORT", "3000"))
    logger.info("Запуск сервера на порту %d", port)
    app.run(host="0.0.0.0", port=port)
