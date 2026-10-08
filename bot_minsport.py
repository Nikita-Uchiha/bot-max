"""
Chat Bot for MAX Messenger API
Features: Data-driven screens, navigation history (Back button), external links.
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
        logging.FileHandler("/app/bot.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

MAX_API = "https://platform-api.max.ru"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
TOKEN = os.environ.get("MAX_BOT_TOKEN", "").strip()
USE_BEARER = os.environ.get("MAX_USE_BEARER", "").lower() in ("1", "true", "yes")

# Хранилища состояния пользователей
_USER_LAST_MID: dict[int, str] = {}          # ID последнего сообщения для удаления
_USER_HISTORY: dict[int, list[str]] = {}     # Стек истории экранов для кнопки "Назад"


# ==============================================================================
# КОНФИГУРАЦИЯ ЭКРАНОВ (МЕНЮ)
# ==============================================================================
# !!! ДОБАВЛЯЙ НОВЫЕ ЭКРАНЫ СЮДА. Не нужно трогать основной код. !!!
# Доступные типы кнопок: 
# 1. {"text": "Текст", "payload": "уникальный_id"} -> вызывает переход внутри бота
# 2. {"text": "Текст", "type": "link", "url": "https://..."} -> открывает сайт (MAX обрабатывает сам)
SCREENS = {
    "start": {
        "text": "👋 Привет, {name}! Добро пожаловать. Выберите категорию:",
        "buttons": [
            [{"text": "📁 Категория 1", "payload": "cat1"}],
            [{"text": "📁 Категория 2", "payload": "cat2"}],
            [{"text": "🌐 Наш официальный сайт", "type": "link", "url": "https://minsport.orb.ru"}]
        ]
    },
    "cat1": {
        "text": "Вы находитесь в Категории 1. Здесь полезная информация.",
        "buttons": [
            [{"text": "📄 Подробнее", "payload": "cat1_detail"}],
            [{"text": "🔙 Назад", "payload": "__BACK__"}] # Специальный payload для возврата
        ]
    },
    "cat1_detail": {
        "text": "Это детальный экран Категории 1. Тут может быть любой текст.",
        "buttons": [
            [{"text": "🔙 Назад к Категории 1", "payload": "__BACK__"}]
        ]
    },
    "cat2": {
        "text": "Вы находитесь в Категории 2.",
        "buttons": [
            [{"text": "🔙 Назад в главное меню", "payload": "__BACK__"}]
        ]
    }
}


# ==============================================================================
# HELPERS
# ==============================================================================
def _get_auth_headers() -> dict:
    auth = f"Bearer {TOKEN}" if USE_BEARER and not TOKEN.lower().startswith("bearer ") else TOKEN
    return {"Authorization": auth, "Content-Type": "application/json"}


def _get_request_params(user_id: int | None, chat_id: int | None, chat_type: str | None) -> dict:
    ct = (chat_type or "").strip().lower()
    if ct == "dialog" and user_id: return {"user_id": user_id}
    if ct in ("chat", "channel", "group") and chat_id: return {"chat_id": chat_id}
    if user_id: return {"user_id": user_id}
    if chat_id: return {"chat_id": chat_id}
    return {}


def _build_keyboard(buttons_data: list[list[dict]]) -> dict:
    """Собирает клавиатуру, поддерживая как callback, так и link кнопки."""
    keyboard = []
    for row in buttons_data:
        keyboard_row = []
        for btn in row:
            if btn.get("type") == "link":
                keyboard_row.append({"type": "link", "text": btn["text"], "url": btn["url"]})
            else:
                keyboard_row.append({"type": "callback", "text": btn["text"], "payload": btn["payload"]})
        keyboard.append(keyboard_row)
    
    return {"attachments": [{"type": "inline_keyboard", "payload": {"buttons": keyboard}}]}


# ==============================================================================
# API INTERACTIONS
# ==============================================================================
def delete_message(message_id: str, user_id: int | None = None, chat_id: int | None = None):
    params = {"message_id": str(message_id)}
    params.update(_get_request_params(user_id, chat_id, None))
    try:
        r = requests.delete(f"{MAX_API}/messages", headers=_get_auth_headers(), params=params, timeout=10)
        if not r.ok:
            logger.warning("Ошибка удаления %s: %s", message_id, r.text[:100])
    except requests.RequestException as e:
        logger.error("Network error deleting message: %s", e)


def send_message(user_id: int | None, chat_id: int | None, chat_type: str | None, body: dict) -> str | None:
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
# NAVIGATION ENGINE
# ==============================================================================
def navigate_to(user_id: int, user_name: str, screen_id: str, chat_id: int | None = None, chat_type: str | None = None, is_back: bool = False):
    """Универсальная функция отрисовки любого экрана."""
    screen = SCREENS.get(screen_id)
    if not screen:
        logger.error("Экран '%s' не найден в конфигурации!", screen_id)
        return

    # 1. Удаляем старое сообщение
    if prev_mid := _USER_LAST_MID.get(user_id):
        delete_message(prev_mid, user_id=user_id, chat_id=chat_id)

    # 2. Форматируем текст (подставляем имя)
    text = screen["text"].format(name=user_name)
    body = {"text": text}

    # 3. Добавляем кнопки, если они есть
    if "buttons" in screen:
        body.update(_build_keyboard(screen["buttons"]))

    # 4. Отправляем новое сообщение
    new_mid = send_message(user_id, chat_id, chat_type, body)
    if new_mid:
        _USER_LAST_MID[user_id] = new_mid

    # 5. Управление историей (стеком)
    if user_id not in _USER_HISTORY:
        _USER_HISTORY[user_id] = []
    
    if not is_back:
        # Если идем вперед, добавляем текущий экран в историю
        _USER_HISTORY[user_id].append(screen_id)
    else:
        # Если это возврат назад, мы уже "попали" на предыдущий экран, 
        # но нам нужно убедиться, что он теперь является верхушкой стека.
        # (Логика pop происходит в обработчике __BACK__)
        pass


def handle_back_navigation(user_id: int, user_name: str, chat_id: int | None = None, chat_type: str | None = None):
    """Обрабатывает нажатие кнопки '__BACK__'."""
    history = _USER_HISTORY.get(user_id, [])
    
    if len(history) > 1:
        history.pop()  # Удаляем текущий экран из истории
        prev_screen = history[-1]  # Берем предыдущий
        logger.info("Возврат пользователя %s на экран: %s", user_id, prev_screen)
        navigate_to(user_id, user_name, prev_screen, chat_id, chat_type, is_back=True)
    else:
        # Если истории нет или мы уже в корне, идем на старт
        logger.info("История пуста, возврат на start для пользователя %s", user_id)
        _USER_HISTORY[user_id] = ["start"]
        navigate_to(user_id, user_name, "start", chat_id, chat_type, is_back=True)


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
    
    # Извлекаем chat_id и chat_type из контекста сообщения, если они там есть
    msg_recipient = data.get("message", {}).get("recipient", {})
    chat_id = msg_recipient.get("chat_id")
    chat_type = msg_recipient.get("chat_type")

    if not user_id:
        return jsonify({"ok": True}), 200

    logger.info("Кнопка нажата: payload='%s', user=%s", payload, user_name)
    
    if message_id:
        delete_message(message_id, user_id=user_id, chat_id=chat_id)

    # Маршрутизация без if/else лапши
    if payload == "__BACK__":
        handle_back_navigation(user_id, user_name, chat_id, chat_type)
    elif payload in SCREENS:
        # Если payload совпадает с именем экрана, просто переходим на него
        navigate_to(user_id, user_name, payload, chat_id, chat_type)
    else:
        logger.warning("Необработанный payload: %s", payload)
        # По умолчанию можно вернуть на старт или показать ошибку
        navigate_to(user_id, user_name, "start", chat_id, chat_type)

    return jsonify({"ok": True}), 200


def _handle_bot_started(data: dict) -> tuple:
    user = data.get("user", {})
    if user_id := user.get("user_id"):
        user_name = user.get("first_name") or user.get("username") or "Пользователь"
        logger.info("Бот запущен пользователем: %s", user_name)
        _USER_HISTORY[user_id] = ["start"] # Инициализируем историю
        navigate_to(user_id, user_name, "start")
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
        _USER_HISTORY[user_id] = ["start"]
        navigate_to(user_id, user_name, "start", chat_id, chat_type)
        
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
