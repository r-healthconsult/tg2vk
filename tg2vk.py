#!/usr/bin/env python3
"""
tg2vk: переносит новые посты Telegram-канала на стену группы ВКонтакте.

Запуск:
  python3 tg2vk.py           работать постоянно (на своём сервере)
  python3 tg2vk.py --once    забрать накопившиеся посты и выйти (GitHub Actions, cron)
  python3 tg2vk.py --check   проверить ключи и права, ничего не публикуя

Настройки берутся из переменных окружения или из файла .env рядом со скриптом
(образец в .env.example). Сторонние библиотеки не нужны, хватает Python 3.8+.
"""

import argparse
import http.client
import json
import logging
import mimetypes
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

log = logging.getLogger("tg2vk")

HTTP_TIMEOUT = 60       # секунд на обычный запрос
LONG_POLL = 25          # столько секунд Telegram держит запрос, ожидая новых постов
ALBUM_WAIT = 5          # пауза, чтобы альбом успел прийти целиком
MAX_ATTACHMENTS = 10    # больше ВК к одной записи не прикрепит
MEDIA_LINK_TEXT = "Фото и видео к посту: {link}"
OTHER_MEDIA = ("video", "animation", "document", "audio", "voice", "video_note",
               "poll", "location", "venue", "contact", "paid_media")
VK_IMAGE_TYPES = ("image/jpeg", "image/png", "image/gif")


# ---------------------------------------------------------------- HTTP ----

class NetworkError(Exception):
    """Сервер недоступен: таймаут, обрыв, DNS."""


class Http:
    """Минимальный HTTP-клиент на стандартной библиотеке.

    Прокси берётся из переменных HTTPS_PROXY / HTTP_PROXY, если они заданы.
    """

    def request(self, method, url, data=None, headers=None, timeout=HTTP_TIMEOUT):
        hdrs = {"User-Agent": "tg2vk/1.0"}
        hdrs.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read()
            except (OSError, http.client.HTTPException):
                return e.code, b""
        except (OSError, http.client.HTTPException, socket.timeout) as e:
            raise NetworkError(str(getattr(e, "reason", e) or type(e).__name__)) from None

    def post_json(self, url, payload, timeout=HTTP_TIMEOUT):
        body = json.dumps(payload).encode("utf-8")
        return self.request("POST", url, body, {"Content-Type": "application/json"}, timeout)

    def post_form(self, url, fields, timeout=HTTP_TIMEOUT):
        body = urllib.parse.urlencode(fields).encode("utf-8")
        return self.request("POST", url, body,
                            {"Content-Type": "application/x-www-form-urlencoded"}, timeout)

    def post_file(self, url, field, filename, content, timeout=HTTP_TIMEOUT):
        boundary = uuid.uuid4().hex
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        head = ("--{b}\r\nContent-Disposition: form-data; name=\"{f}\"; filename=\"{n}\"\r\n"
                "Content-Type: {c}\r\n\r\n").format(b=boundary, f=field, n=filename, c=ctype)
        tail = "\r\n--{b}--\r\n".format(b=boundary)
        body = head.encode("utf-8") + content + tail.encode("utf-8")
        return self.request("POST", url, body,
                            {"Content-Type": "multipart/form-data; boundary=" + boundary}, timeout)

    def get(self, url, timeout=HTTP_TIMEOUT):
        return self.request("GET", url, None, None, timeout)


def _pause(attempt):
    time.sleep(min(2 ** attempt, 30))


# ------------------------------------------------------------ Telegram ----

class TelegramError(Exception):
    def __init__(self, method, code, description):
        super().__init__("Telegram {}: {} {}".format(method, code, description).strip())
        self.code = code
        self.description = description or ""

    @property
    def fatal(self):
        # Неверный токен или бот уже запущен в другом месте: повторять бессмысленно.
        return self.code in (401, 404, 409)


class Telegram:
    def __init__(self, http_client, token, api_base="https://api.telegram.org"):
        self.http = http_client
        base = api_base.rstrip("/")
        self.api = "{}/bot{}".format(base, token)
        self.files = "{}/file/bot{}".format(base, token)

    def call(self, method, params=None, timeout=HTTP_TIMEOUT, attempts=5):
        for attempt in range(attempts):
            try:
                status, body = self.http.post_json("{}/{}".format(self.api, method),
                                                   params or {}, timeout)
                data = json.loads(body.decode("utf-8"))
            except (NetworkError, ValueError) as e:
                if attempt == attempts - 1:
                    raise TelegramError(method, "network", "нет связи с Telegram ({})".format(e))
                _pause(attempt)
                continue
            if data.get("ok"):
                return data.get("result")
            code = data.get("error_code", status)
            if code == 429 and attempt < attempts - 1:
                time.sleep(int((data.get("parameters") or {}).get("retry_after", 5)) + 1)
                continue
            if isinstance(code, int) and code >= 500 and attempt < attempts - 1:
                _pause(attempt)
                continue
            raise TelegramError(method, code, data.get("description", ""))
        raise TelegramError(method, "retry", "слишком много повторов")

    def get_updates(self, offset=None, timeout=0):
        params = {"timeout": timeout, "allowed_updates": ["channel_post"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", params, timeout=HTTP_TIMEOUT + timeout) or []

    def download(self, file_id):
        """Скачивает файл из Telegram. Bot API отдаёт файлы до 20 МБ."""
        info = self.call("getFile", {"file_id": file_id}) or {}
        path = info.get("file_path")
        if not path:
            raise TelegramError("getFile", "no_path", "Telegram не отдал файл")
        status, body = self.http.get("{}/{}".format(self.files, path))
        if status != 200:
            raise TelegramError("download", status, "не удалось скачать файл")
        return body, path.rsplit("/", 1)[-1]


# ------------------------------------------------------------------ VK ----

class VkError(Exception):
    def __init__(self, method, code, message):
        super().__init__("ВК {}: [{}] {}".format(method, code, message))
        self.method = method
        self.code = code
        self.message = message or ""


class VK:
    TRANSIENT = {1, 6, 10}          # неизвестная ошибка, слишком частые запросы, сбой ВК
    NO_ACCESS = {5, 15, 27, 28, 214}  # ключ не подходит или нет прав на стену

    def __init__(self, http_client, version="5.199", api_base="https://api.vk.com/method"):
        self.http = http_client
        self.version = version
        self.api = api_base.rstrip("/")

    def call(self, method, token, _attempts=5, **params):
        fields = {k: v for k, v in params.items() if v is not None and v != ""}
        fields["access_token"] = token
        fields["v"] = self.version
        for attempt in range(_attempts):
            try:
                _, body = self.http.post_form("{}/{}".format(self.api, method), fields)
                data = json.loads(body.decode("utf-8"))
            except (NetworkError, ValueError) as e:
                if attempt == _attempts - 1:
                    raise VkError(method, "network", "нет связи с ВКонтакте ({})".format(e))
                _pause(attempt)
                continue
            if "error" in data:
                err = data["error"] or {}
                code = err.get("error_code")
                if code in self.TRANSIENT and attempt < _attempts - 1:
                    _pause(attempt)
                    continue
                raise VkError(method, code, err.get("error_msg", ""))
            return data.get("response")
        raise VkError(method, "retry", "слишком много повторов")

    def group_info(self, token, group=None):
        res = self.call("groups.getById", token, group_id=group)
        groups = res.get("groups") if isinstance(res, dict) else res
        if not groups:
            raise VkError("groups.getById", "empty", "сообщество не найдено")
        return groups[0]

    def upload_wall_photo(self, token, group_id, content, filename):
        """Загружает фото для записи на стене. Работает только с ключом пользователя."""
        server = self.call("photos.getWallUploadServer", token, group_id=group_id)
        try:
            status, body = self.http.post_file(server["upload_url"], "photo", filename, content)
            up = json.loads(body.decode("utf-8"))
        except ValueError:
            raise VkError("upload", "bad_response", "сервер загрузки ответил не JSON")
        if not up.get("photo") or up.get("photo") == "[]":
            raise VkError("upload", up.get("error", "empty"), "фото не принято")
        saved = self.call("photos.saveWallPhoto", token, group_id=group_id,
                          server=up["server"], photo=up["photo"], hash=up["hash"])
        photo = saved[0]
        return "photo{}_{}".format(photo["owner_id"], photo["id"])

    def wall_post(self, token, group_id, message, attachments):
        res = self.call("wall.post", token, owner_id=-int(group_id), from_group=1,
                        message=message, attachments=",".join(attachments))
        return res["post_id"]


# ------------------------------------------------------- Текст и медиа ----

def tg_to_plain(text, entities):
    """Текст поста без разметки. Скрытые ссылки становятся видимыми: «текст (адрес)»."""
    if not text:
        return ""
    links = sorted((e for e in entities or ()
                    if e.get("type") == "text_link" and e.get("url")
                    and not e["url"].startswith("tg://")),
                   key=lambda e: e["offset"])
    if not links:
        return text
    raw = text.encode("utf-16-le")  # Telegram считает смещения в единицах UTF-16

    def cut(a, b=None):
        return raw[a * 2:None if b is None else b * 2].decode("utf-16-le")

    try:
        parts, pos = [], 0
        for e in links:
            start, end = e["offset"], e["offset"] + e["length"]
            if start < pos:
                continue  # вложенные или пересекающиеся ссылки
            parts.append(cut(pos, start))
            anchor, url = cut(start, end), e["url"]
            core = anchor.rstrip()
            tail = anchor[len(core):]
            if url in anchor:
                parts.append(anchor)
            elif not core:
                parts.append(url + tail)
            else:
                parts.append("{} ({}){}".format(core, url, tail))
            pos = end
        parts.append(cut(pos))
        return "".join(parts)
    except UnicodeDecodeError:
        return text


def post_link(msg):
    chat = msg.get("chat") or {}
    mid = msg.get("message_id")
    if chat.get("username"):
        return "https://t.me/{}/{}".format(chat["username"], mid)
    cid = str(chat.get("id", ""))
    if cid.startswith("-100"):
        return "https://t.me/c/{}/{}".format(cid[4:], mid)
    return ""


def extract(msgs):
    """Текст, фото (file_id) и признак прочих медиа для поста или альбома."""
    text = ""
    for m in msgs:
        if m.get("text"):
            text = tg_to_plain(m["text"], m.get("entities"))
            break
        if m.get("caption"):
            text = tg_to_plain(m["caption"], m.get("caption_entities"))
            break
    if not text:
        poll = next((m["poll"] for m in msgs if m.get("poll")), None)
        if poll and poll.get("question"):
            text = "Опрос: " + poll["question"]
    photos, other = [], False
    for m in msgs:
        doc = m.get("document") or {}
        if m.get("photo"):
            photos.append(m["photo"][-1]["file_id"])  # последний размер самый большой
        elif doc.get("mime_type") in VK_IMAGE_TYPES and "animation" not in m:
            photos.append(doc["file_id"])
        elif any(k in m for k in OTHER_MEDIA):
            other = True
    return text.strip(), photos, other, post_link(msgs[0])


# ----------------------------------------------------------- Состояние ----

class State:
    """Какие посты уже опубликованы: защита от повторов после сбоя."""
    KEEP = 2000

    def __init__(self, path):
        self.path = Path(path)
        self.posted = {}
        try:
            self.posted = dict(json.loads(self.path.read_text(encoding="utf-8")).get("posted", {}))
        except FileNotFoundError:
            pass
        except (ValueError, OSError, AttributeError, TypeError) as e:
            log.warning("Файл %s не прочитан (%s), начинаю с чистого листа", self.path, e)

    def mark(self, key, post_id):
        self.posted[key] = post_id
        extra = len(self.posted) - self.KEEP
        for k in list(self.posted)[:max(extra, 0)]:
            del self.posted[k]
        self.save()

    def save(self):
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"posted": self.posted}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(str(tmp), str(self.path))


# ---------------------------------------------------------- Публикация ----

class RetryLater(Exception):
    """Нет связи с Telegram или ВК. Пост не подтверждён и придёт снова."""


class Reposter:
    def __init__(self, cfg, tg, vk, state):
        self.cfg, self.tg, self.vk, self.state = cfg, tg, vk, state
        self.group_id = None
        self._foreign_chats = set()

    def setup(self):
        info = self.vk.group_info(self.cfg.vk_group_token, self.cfg.vk_group)
        self.group_id = int(info["id"])
        log.info("Канал %s -> группа ВК «%s» (id %s)",
                 self.cfg.tg_channel, info.get("name"), self.group_id)

    def is_our_channel(self, msg):
        chat = msg.get("chat") or {}
        ch = self.cfg.tg_channel
        ours = ((chat.get("username") or "").lower() == ch.lower()
                or str(chat.get("id")) == ch)
        if not ours and chat.get("id") not in self._foreign_chats:
            self._foreign_chats.add(chat.get("id"))
            log.info("Пропускаю посты чужого чата «%s»", chat.get("title") or chat.get("id"))
        return ours

    def safe_publish(self, msgs):
        """Публикует пост или альбом. False, если не получилось; RetryLater, если нет связи."""
        try:
            return self.publish(msgs)
        except NetworkError as e:
            raise RetryLater(str(e))
        except (TelegramError, VkError) as e:
            if e.code == "network":
                raise RetryLater(str(e))
            log.error("Пост %s не опубликован: %s", post_link(msgs[0]) or "?", e)
            hint = explain(e)
            if hint:
                log.error(hint)
            return False

    def publish(self, msgs):
        msgs = sorted(msgs, key=lambda m: m.get("message_id", 0))
        first = msgs[0]
        key = "{}:{}".format(first["chat"]["id"], first["message_id"])
        if key in self.state.posted:
            log.info("Пост %s уже в ВК, пропускаю", key)
            return True

        text, photos, other, link = extract(msgs)
        attachments, lost = [], other
        if photos and self.cfg.vk_user_token:
            for file_id in photos[:MAX_ATTACHMENTS]:
                try:
                    content, name = self.tg.download(file_id)
                    attachments.append(self.vk.upload_wall_photo(
                        self.cfg.vk_user_token, self.group_id, content, name))
                except (TelegramError, VkError) as e:
                    if e.code == "network":
                        raise
                    log.warning("Фото не загрузилось: %s", e)
                    lost = True
            lost = lost or len(photos) > MAX_ATTACHMENTS
        elif photos:
            lost = True

        use_link = lost and bool(link) and self.cfg.media_fallback != "none"
        if not text and not attachments and not use_link:
            log.info("Пост %s без текста и медиа, пропускаю", key)
            return True

        message = text
        if self.cfg.footer:
            message = "{}\n\n{}".format(message, self.cfg.footer) if message else self.cfg.footer
        post_id = self._post(message, attachments, link if use_link else None)
        self.state.mark(key, post_id)
        log.info("Опубликовано: %s -> https://vk.com/wall-%s_%s",
                 link or key, self.group_id, post_id)
        return True

    def _post(self, message, attachments, link):
        """Пробует варианты: ссылка вложением, ссылка в тексте, без ссылки."""
        variants = []
        if link:
            if len(attachments) < MAX_ATTACHMENTS:
                variants.append((message, attachments + [link]))
            line = MEDIA_LINK_TEXT.format(link=link)
            variants.append(("{}\n\n{}".format(message, line) if message else line, attachments))
        if message or attachments:
            variants.append((message, attachments))
        for i, (msg, att) in enumerate(variants):
            try:
                return self._wall_post(msg, att)
            except VkError as e:
                last = i == len(variants) - 1
                if last or e.code == "network" or e.code in VK.NO_ACCESS or e.code in (9, 14):
                    raise
                log.warning("ВК не принял запись со ссылкой (%s), пробую иначе", e)

    def _wall_post(self, message, attachments):
        try:
            return self.vk.wall_post(self.cfg.vk_group_token, self.group_id, message, attachments)
        except VkError as e:
            if e.code in VK.NO_ACCESS and self.cfg.vk_user_token:
                log.warning("Ключ сообщества не смог опубликовать (%s), пробую ключ пользователя", e)
                return self.vk.wall_post(self.cfg.vk_user_token, self.group_id, message, attachments)
            raise


def fetch(tg, timeout):
    """Неподтверждённые посты. Если последний из них свежая часть альбома, ждём остальные."""
    batch = tg.get_updates(None, timeout)
    for _ in range(3):
        last = (batch[-1].get("channel_post") or {}) if batch else {}
        if not last.get("media_group_id") or time.time() - last.get("date", 0) > 15:
            break
        time.sleep(ALBUM_WAIT)
        more = tg.get_updates(None, 0)
        grew = len(more) > len(batch)
        batch = more
        if not grew:
            break
    return batch


def group_units(updates, reposter):
    """Склеивает части альбомов. Возвращает посты (списки сообщений) в исходном порядке."""
    units, albums = [], {}
    for upd in updates:
        msg = upd.get("channel_post")
        if not msg or not reposter.is_our_channel(msg):
            continue
        gid = msg.get("media_group_id")
        if gid:
            if gid not in albums:
                albums[gid] = []
                units.append(albums[gid])
            albums[gid].append(msg)
        else:
            units.append([msg])
    return units


def drain(reposter, tg, timeout):
    """Публикует всё накопившееся и подтверждает Telegram получение. Возвращает число неудач."""
    failed = 0
    while True:
        batch = fetch(tg, timeout)
        timeout = 0
        if not batch:
            return failed
        for unit in group_units(batch, reposter):
            if not reposter.safe_publish(unit):
                failed += 1
        # Подтверждение: после этого вызова Telegram больше не отдаст эти посты.
        tg.get_updates(batch[-1]["update_id"] + 1, 0)


def run_once(reposter, tg):
    try:
        failed = drain(reposter, tg, 0)
    except RetryLater as e:
        log.error("Нет связи (%s). Посты дождутся следующего запуска.", e)
        return 1
    finally:
        reposter.state.save()
    if failed:
        log.error("Не опубликовано постов: %s, подробности выше.", failed)
        return 1
    log.info("Готово.")
    return 0


def run_forever(reposter, tg):
    log.info("Жду новых постов. Остановить: Ctrl+C")
    while True:
        try:
            drain(reposter, tg, LONG_POLL)
        except RetryLater as e:
            log.warning("Нет связи (%s), повтор через минуту", e)
            time.sleep(60)
        except TelegramError as e:
            if e.fatal:
                raise
            log.warning("%s; повтор через 30 секунд", e)
            time.sleep(30)


# ------------------------------------------------------------ Проверка ----

def run_check(cfg, tg, vk):
    problems = []

    def good(text):
        print("[ok] " + text)

    def bad(text, err=None):
        problems.append(text)
        print("[!!] " + text)
        hint = explain(err) if err else None
        if hint:
            print("     " + hint)

    print("Telegram")
    try:
        me = tg.call("getMe")
        good("бот @{}".format(me.get("username")))
        if (tg.call("getWebhookInfo") or {}).get("url"):
            bad("у бота включён webhook: его использует другой сервис, создайте отдельного бота")
        ref = cfg.tg_channel if re.fullmatch(r"-?\d+", cfg.tg_channel) else "@" + cfg.tg_channel
        chat = tg.call("getChat", {"chat_id": ref})
        good("канал «{}»".format(chat.get("title")))
        try:
            status = tg.call("getChatMember", {"chat_id": chat["id"], "user_id": me["id"]}).get("status")
        except TelegramError:
            status = None
        if status in ("administrator", "creator"):
            good("бот администратор канала")
        else:
            bad("бот не администратор канала: добавьте его в администраторы, иначе посты до него не дойдут")
    except TelegramError as e:
        bad(str(e), e)

    print("ВКонтакте")
    group_id = None
    try:
        info = vk.group_info(cfg.vk_group_token, cfg.vk_group)
        group_id = int(info["id"])
        good("группа «{}» (id {})".format(info.get("name"), group_id))
        try:
            own = vk.group_info(cfg.vk_group_token, None)
            if int(own["id"]) != group_id:
                bad("ключ создан в другом сообществе: «{}»".format(own.get("name")))
        except VkError:
            pass
        try:
            # Пустая запись заведомо не пройдёт: по коду ошибки видно, есть ли права.
            vk.call("wall.post", cfg.vk_group_token, owner_id=-group_id, from_group=1)
        except VkError as e:
            if e.code == 100:
                good("ключ сообщества может публиковать на стене")
            elif e.code in VK.NO_ACCESS:
                bad("ключ сообщества не может публиковать на стене ({})".format(e.message), e)
            else:
                print("[--] публикацию проверить не удалось: {}".format(e))
    except VkError as e:
        bad(str(e), e)

    if cfg.vk_user_token:
        try:
            users = vk.call("users.get", cfg.vk_user_token) or []
            if not users:
                bad("VK_USER_TOKEN не похож на ключ пользователя (возможно, это ключ сообщества)")
            else:
                good("ключ пользователя: {} {}".format(users[0].get("first_name", ""),
                                                       users[0].get("last_name", "")).strip())
                if group_id:
                    vk.call("photos.getWallUploadServer", cfg.vk_user_token, group_id=group_id)
                    good("фото будут загружаться в ВК")
        except VkError as e:
            bad("ключ пользователя не подходит для загрузки фото ({})".format(e.message), e)
    else:
        print("[--] ключ пользователя не задан: посты с фото и видео выйдут со ссылкой на оригинал")

    print()
    print("Всё готово." if not problems else "Есть проблемы, см. строки с [!!].")
    return not problems


def explain(err):
    """Подсказка на русском для частых ошибок."""
    code = getattr(err, "code", None)
    if isinstance(err, TelegramError):
        if code in (401, 404):
            return "Telegram не принял TG_BOT_TOKEN: скопируйте токен из @BotFather заново."
        if code == 409 and "webhook" in err.description.lower():
            return "У бота включён webhook другого сервиса. Создайте для репоста отдельного бота."
        if code == 409:
            return "Бот уже запущен в другом месте. Оставьте один запуск: сервер или GitHub."
        if code == 400 and "chat not found" in err.description.lower():
            return "Канал не найден: проверьте TG_CHANNEL (ник канала, например ipico_news)."
        if code == "network":
            return ("Нет связи с api.telegram.org. Из российских дата-центров Telegram часто недоступен: "
                    "запускайте бота в GitHub Actions, на зарубежном сервере или через HTTPS_PROXY.")
    if isinstance(err, VkError):
        if err.method == "groups.getById" and code in (100, "empty"):
            return "Группа не найдена: проверьте VK_GROUP_ID (адрес группы, например ipico)."
        if code == 5:
            return "ВК не принял ключ: он неверный или отозван. Создайте новый."
        if code == 27:
            return "Этот метод ВК не работает с ключом сообщества, нужен ключ пользователя."
        if code in (15, 214):
            return ("Нет прав на стену: ключ должен быть создан в этом сообществе с доступом к стене, "
                    "а записи на стене должны быть разрешены.")
        if code == 9:
            return "ВК временно ограничил число публикаций. Попробуйте позже."
        if code == 14:
            return "ВК запросил капчу. Повторите позже."
        if code == "network":
            return "Нет связи с api.vk.com."
    return None


# ------------------------------------------------------------ Настройки ----

def normalize_channel(value):
    v = re.sub(r"^(https?://)?(t\.me|telegram\.me)/(s/)?", "", value.strip(), flags=re.I)
    return v.strip("/").lstrip("@")


def normalize_group(value):
    v = re.sub(r"^(https?://)?(m\.)?vk\.(com|ru)/", "", value.strip(), flags=re.I).strip("/")
    m = re.fullmatch(r"(club|public|event)(\d+)", v, flags=re.I)
    return (m.group(2) if m else v.lstrip("-")) or None


class Config:
    def __init__(self, env):
        def get(key, default=""):
            return (env.get(key) or default).strip()

        self.tg_token = get("TG_BOT_TOKEN")
        self.tg_channel = normalize_channel(get("TG_CHANNEL"))
        self.vk_group_token = get("VK_GROUP_TOKEN")
        self.vk_user_token = get("VK_USER_TOKEN")
        self.vk_group = normalize_group(get("VK_GROUP_ID"))
        self.vk_version = get("VK_API_VERSION", "5.199")
        self.footer = get("FOOTER").replace("\\n", "\n")
        self.media_fallback = get("MEDIA_FALLBACK", "link").lower()
        self.state_file = get("STATE_FILE", "state.json")
        self.tg_api = get("TG_API_BASE", "https://api.telegram.org")
        self.vk_api = get("VK_API_BASE", "https://api.vk.com/method")

    def missing(self):
        required = (("TG_BOT_TOKEN", self.tg_token), ("TG_CHANNEL", self.tg_channel),
                    ("VK_GROUP_TOKEN", self.vk_group_token))
        return [name for name, value in required if not value]


def load_dotenv(path):
    """Читает KEY=VALUE из .env. Переменные окружения важнее файла."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="tg2vk", description="Репост новых постов Telegram-канала в группу ВКонтакте.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="забрать накопившиеся посты и выйти")
    mode.add_argument("--check", action="store_true", help="проверить ключи и права, ничего не публикуя")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stdout)
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    cfg = Config(os.environ)
    missing = cfg.missing()
    if missing:
        log.error("Не заданы настройки: %s (см. README)", ", ".join(missing))
        return 2

    http_client = Http()
    tg = Telegram(http_client, cfg.tg_token, cfg.tg_api)
    vk = VK(http_client, cfg.vk_version, cfg.vk_api)
    if args.check:
        return 0 if run_check(cfg, tg, vk) else 1

    reposter = Reposter(cfg, tg, vk, State(cfg.state_file))
    try:
        reposter.setup()
        if args.once:
            return run_once(reposter, tg)
        run_forever(reposter, tg)
        return 0
    except (TelegramError, VkError) as e:
        log.error("%s", e)
        hint = explain(e)
        if hint:
            log.error(hint)
        return 2
    except KeyboardInterrupt:
        log.info("Остановлено")
        return 0


if __name__ == "__main__":
    sys.exit(main())
