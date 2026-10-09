"""
Chat Bot for MAX Messenger API
Features:
  - YAML-driven content (screens.yaml)
  - Auto-navigation buttons (Back / Home)
  - Image loading from URLs with caching
  - Markdown formatting support
  - Message deletion for app-like UX
"""
from __future__ import annotations
import os
import hmac
import logging

import yaml
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

# ==============================================================================
# ЗАГРУЗКА КОНТЕНТА ИЗ YAML
# ==============================================================================
SCREENS_FILE = "/app/screens.yaml"


def _load_screens() -> dict:
    try:
        with open(SCREENS_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        logger.info("Загружено экранов из YAML: %d", len(data))
        return data
    except Exception as e:
        logger.error("Ошибка загрузки screens.yaml: %s", e)
        return {}


SCREENS = _load_screens()

# ==============================================================================
# СОСТОЯНИЕ ПОЛЬЗОВАТЕЛЕЙ
# ==============================================================================
_USER_LAST_MID: dict[int, str] = {}
_USER_HISTORY: dict[int, list[str]] = {}
_IMAGE_TOKEN_CACHE: dict[str, str] = {}

BACK_PAYLOAD = "__BACK__"
HOME_PAYLOAD = "__HOME__"


# ==============================================================================
# HELPERS
# ==============================================================================
def _get_auth_headers() -> dict:
    auth = f"Bearer {TOKEN}" if USE_BEARER and not TOKEN.lower().startswith("bearer ") else TOKEN
    return {"Authorization": auth, "Content-Type": "application/json"}


def _get_request_params(user_id: int | None, chat_id: int | None, chat_type: str | None) -> dict:
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


def _build_navigation_buttons(user_id: int, current_screen: str) -> list[list[dict]]:
    history = _USER_HISTORY.get(user_id, [])
    nav_buttons = []

    if len(history) > 2:
        nav_buttons.append([{"text": "🔙 Назад", "payload": BACK_PAYLOAD}])

    if current_screen != "start":
        nav_buttons.append([{"text": "🏠 Главное меню", "payload": HOME_PAYLOAD}])

    return nav_buttons


# ==============================================================================
# ЗАГРУЗКА МЕДИА
# ==============================================================================
def upload_image_from_url(image_url: str) -> str | None:
    if image_url in _IMAGE_TOKEN_CACHE:
        return _IMAGE_TOKEN_CACHE[image_url]

    try:
        r = requests.post(
            f"{MAX_API}/uploads",
            params={"type": "image"},
            headers={"Authorization": _get_auth_headers()["Authorization"]},
            timeout=15
        )
        if not r.ok:
            logger.error("Не удалось получить upload URL: %s", r.text[:200])
            return None
        upload_url = r.json().get("url")
        if not upload_url:
            logger.error("В ответе /uploads нет поля 'url'")
            return None

        img_response = requests.get(image_url, timeout=30, stream=True)
        if not img_response.ok:
            logger.error("Не удалось скачать изображение %s: %s", image_url, img_response.status_code)
            return None

        filename = image_url.split("/")[-1].split("?")[0] or "image.jpg"
        valid_ext = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".heic", ".tiff")
        if not any(filename.lower().endswith(ext) for ext in valid_ext):
            filename += ".jpg"

        content_type = img_response.headers.get("Content-Type", "image/jpeg")
        files = {"data": (filename, img_response.content, content_type)}
        upload_resp = requests.post(upload_url, files=files, timeout=60)
        if not upload_resp.ok:
            logger.error("Не удалось загрузить файл в MAX: %s", upload_resp.text[:200])
            return None

        token = upload_resp.json().get("token")
        if not token:
            logger.error("В ответе загрузки нет токена")
            return None

        _IMAGE_TOKEN_CACHE[image_url] = token
        return token

    except requests.RequestException as e:
        logger.exception("Сетевая ошибка при загрузке фото %s: %s", image_url, e)
        return None


# ==============================================================================
# API INTERACTIONS
# ==============================================================================
def delete_message(message_id: str, user_id: int | None = None, chat_id: int | None = None):
    params = {"message_id": str(message_id)}
    params.update(_get_request_params(user_id, chat_id, None))
    try:
        r = requests.delete(f"{MAX_API}/messages", headers=_get_auth_headers(), params=params, timeout=10)
        if not r.ok:
            logger.warning("Ошибка удаления сообщения %s", message_id)
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
def navigate_to(user_id: int, user_name: str, screen_id: str,
                chat_id: int | None = None, chat_type: str | None = None,
                is_back: bool = False):
    screen = SCREENS.get(screen_id)
    if not screen:
        logger.error("Экран '%s' не найден в YAML!", screen_id)
        return

    if prev_mid := _USER_LAST_MID.get(user_id):
        delete_message(prev_mid, user_id=user_id, chat_id=chat_id)

    text = screen.get("text", "").format(name=user_name)
    body = {"text": text}
    if "format" in screen:
        body["format"] = screen["format"]

    attachments = []

    image_url = screen.get("image")
    if image_url:
        token = upload_image_from_url(image_url)
        if token:
            attachments.append({"type": "image", "payload": {"token": token}})
        else:
            logger.warning("Не удалось загрузить фото для экрана '%s'", screen_id)

    if user_id not in _USER_HISTORY:
        _USER_HISTORY[user_id] = []

    if not is_back:
        if not _USER_HISTORY[user_id] or _USER_HISTORY[user_id][-1] != screen_id:
            _USER_HISTORY[user_id].append(screen_id)

    if len(_USER_HISTORY[user_id]) > 10:
        _USER_HISTORY[user_id] = _USER_HISTORY[user_id][-10:]

    user_buttons = list(screen.get("buttons", []))
    nav_buttons = _build_navigation_buttons(user_id, screen_id)
    all_buttons = user_buttons + nav_buttons
    if all_buttons:
        attachments.append(_build_keyboard(all_buttons)["attachments"][0])

    if attachments:
        body["attachments"] = attachments

    new_mid = send_message(user_id, chat_id, chat_type, body)
    if new_mid:
        _USER_LAST_MID[user_id] = new_mid


def handle_back_navigation(user_id: int, user_name: str,
                           chat_id: int | None = None, chat_type: str | None = None):
    history = _USER_HISTORY.get(user_id, [])

    if len(history) <= 1:
        return

    history.pop()
    prev_screen = history[-1]
    navigate_to(user_id, user_name, prev_screen, chat_id, chat_type, is_back=True)


def handle_home_navigation(user_id: int, user_name: str,
                           chat_id: int | None = None, chat_type: str | None = None):
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

    msg_recipient = data.get("message", {}).get("recipient", {})
    chat_id = msg_recipient.get("chat_id")
    chat_type = msg_recipient.get("chat_type")

    if not user_id:
        return jsonify({"ok": True}), 200

    if payload == BACK_PAYLOAD:
        handle_back_navigation(user_id, user_name, chat_id, chat_type)
    elif payload == HOME_PAYLOAD:
        handle_home_navigation(user_id, user_name, chat_id, chat_type)
    elif payload in SCREENS:
        navigate_to(user_id, user_name, payload, chat_id, chat_type)
    else:
        logger.warning("Необработанный payload: %s", payload)
        navigate_to(user_id, user_name, "start", chat_id, chat_type)

    return jsonify({"ok": True}), 200


def _handle_bot_started(data: dict) -> tuple:
    user = data.get("user", {})
    if user_id := user.get("user_id"):
        user_name = user.get("first_name") or user.get("username") or "Пользователь"
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
