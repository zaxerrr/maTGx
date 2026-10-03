# MAX2TG-Bridge — контекст для AI-ассистентов

Этот файл — короткая шпаргалка для будущих сессий ассистента: что такое проект, что уже сделано, какие подводные камни.

## Что это

Двусторонний мост MAX (`ws-api.oneme.ru`) ↔ Telegram через форум-топики супергруппы. Каждый MAX-чат = свой topic в Telegram. Userbot подключается WebSocket'ом к MAX по `__oneme_auth` токену, Telegram-side — обычный bot через python-telegram-bot polling.

**Мульти-аккаунт.** В `.env` обязателен только `TG_BOT_TOKEN`. Владелец (первый `/start` в личке или `TG_ALLOWED_USER_ID`) настраивает в личке с ботом аккаунты MAX (вход по номеру+коду или токеном) и привязывает каждый к форум-супергруппе. Одна группа может обслуживать несколько аккаунтов — темы тогда получают префикс `<аккаунт> · `. Старый `.env` (`MAX_TOKEN`/`MAX_DEVICE_ID`/`TG_CHAT_ID`) бутстрапит аккаунт `main` со старыми путями `state/topics.json` / `state/messages.db`.

Основан на [Aist/max2tg](https://github.com/Aist/max2tg), но переписан вокруг форум-топиков и расширен. Лицензия MIT.

## Структура

```
app/
  main.py          # entry: load .env → MaxClient + Telegram Application
  config.py        # Settings dataclass + load_settings()
  max_client.py    # WS клиент MAX. Opcodes, retry, reconnect, upload_*
  max_listener.py  # MAX → TG handler (incoming), auto-topic creation
  resolver.py      # кеш контактов / чатов (chats_raw, contacts_raw)
  tg_sender.py     # TelegramSender + ensure_topic (create/rename)
  tg_handler.py    # TG → MAX handler + команды /bind, /add, /profile, /intro, /del, /help
  topics.py        # TopicStore: JSON-карта max_chat_id ↔ thread_id
  msgmap.py        # MessageMap: SQLite tg_msg_id ↔ (max_chat_id, max_msg_id) + последний текст
  login.py         # PhoneLogin (двухшаговый вход op 17/18, держит WS между шагами) + CLI `python -m app.login`
  accounts.py      # AccountStore: state/accounts.json (0600) — owner_id, аккаунты MAX, известные группы
  bridge.py        # Bridge: по MaxClient+TelegramSender+TopicStore+MessageMap на аккаунт; resolve_topic(group, thread)
  setup_bot.py     # диалог в личке (/start, кнопки acc:/grp:), обнаружение групп (my_chat_member, миграция)
tests/             # 359 pytest, asyncio_mode=auto
docs/cover.jpg     # обложка README
state/             # runtime (топик-карта), gitignored
logs/              # логи, gitignored
```

## Протокол MAX (наши находки)

WebSocket: `wss://ws-api.oneme.ru/websocket`, `Origin: https://web.max.ru`.

Пакет: `{"ver":11,"cmd":0,"seq":N,"opcode":OP,"payload":{...}}`. Ответ: `cmd=1` (OK) или `cmd=3` (error).

| Opcode | Назначение |
|---|---|
| 1 | HEARTBEAT_PING (раз в 30 сек) |
| 6 | HANDSHAKE |
| 17 | START_AUTH `{phone, type:"START_AUTH", language}` → `{token}` (код уходит в SMS/приложение). На несуществующий номер: cmd=3 «Проверка не пройдена» |
| 18 | CHECK_CODE `{token, verifyCode, authTokenType:"CHECK_CODE"}` → `tokenAttrs.LOGIN.token` = MAX_TOKEN (deviceId из handshake = MAX_DEVICE_ID) |
| 19 | AUTH_SNAPSHOT (логин + первый snapshot чатов). Невалидный токен → cmd=3 `error: login.token` (проверено вживую) |
| 32 | CONTACT_GET — возвращает `{names, baseUrl, photoId}`, **НЕ возвращает phone/about** |
| 35 | CONTACT_PRESENCE |
| 48 | CHAT_GET |
| 57 | open by link — работает только для `/join/<token>` (group/channel); `/u/<token>` падает с `not.found / chat namespace` |
| 64 | SEND_MESSAGE (text, elements, attaches, link). Ответ: `link:{type:REPLY, messageId:"<id>"}`. В ответе сервера `message.id` — id отправленного |
| 66 | DELETE_MESSAGE `{chatId, messageIds:[str], forMe}` |
| 65 | ATTACH_TYPING ("я загружаю PHOTO/AUDIO/...") |
| 67 | EDIT_MESSAGE `{chatId, messageId:str, text, elements, attachments:[]}` |
| 80 | PHOTO_UPLOAD_URL → `{count:1}` → `{url}` → POST → `{photos:{...:{token}}}` → attach `{_type:PHOTO, photoToken}` |
| 82 | VIDEO_UPLOAD_URL → `{count:1}` → `{info:[{url, videoId, token}]}` → POST → ждать `op=136` с `videoId` → attach `{_type:VIDEO, videoId, token}` |
| 83 | VIDEO_DOWNLOAD_URL `{videoId, chatId, messageId}` → `{MP4_720: url, MP4_480: url, cache, EXTERNAL}` |
| 84 | **CALLS** `createJoinLink` — НЕ аудио (как мы предполагали) |
| 85 | **CALLS** `getOkCallData` — тоже не аудио |
| 86 | Что-то с upload, требует `count + show + chatId`, возвращает `{}` |
| 87 | FILE_UPLOAD_URL → `{count:1}` → `{info:[{url, fileId}]}` → POST → ждать DISPATCH `op=136` → attach `{_type:FILE, fileId}` |
| 88 | FILE_DOWNLOAD_URL `{fileId, chatId, messageId}` → `{url}` |
| 128 | DISPATCH (incoming сообщения от MAX) |
| 136 | UPLOAD_READY (server подтверждает обработку загруженного файла) |

### Элементы форматирования в `SEND_MESSAGE.message.elements`

`{type, from, length}` — `from` это codepoint-индекс (НЕ UTF-16; Telegram-side юзает UTF-16, конвертация в `tg_handler._utf16_to_char_offset`).

| TG MessageEntityType | MAX element type |
|---|---|
| BOLD | `STRONG` |
| ITALIC | `EMPHASIZED` |
| STRIKETHROUGH | `STRIKETHROUGH` |
| UNDERLINE | `UNDERLINE` |
| CODE | `MONOSPACED` |
| PRE | `MONOSPACED` (MAX WS не имеет code-block) |
| BLOCKQUOTE / EXPANDABLE_BLOCKQUOTE | — (WS отвергает `BLOCKQUOTE`, уходит обычным текстом) |
| TEXT_LINK | `LINK` с `attributes.url` |

`CODE_BLOCK` отвергается валидацией (`No enum constant`). Имена `EMPHASIS`, `EM`, `ITALIC` тоже отвергались — пришли к `EMPHASIZED` (как в `max-botapi-python.enums.text_style`).

### Attach типы (входящие из MAX)

`PHOTO` (baseUrl/baseRawUrl + photoToken), `VIDEO` (thumbnail), `FILE` (url + name + size), `AUDIO` (url), `STICKER` (url), `SHARE` (url + title + description), `LOCATION` (lat/lon), `CONTACT` (name + phone), `UNSUPPORTED` (новый voice — `audioId + token + duration + wave`, опкод download неизвестен).

### Особенности WS

- `proto.payload` ошибки **закрывают WS** для некоторых опкодов (мост авто-реконнектится через 5 сек). Это мешает массовому пробингу: после первой неудачи остальные опкоды успевают только таймаут схватить.
- Токен MAX молча ротируется при логине в web.max.ru с другого устройства: handshake проходит, AUTH_SNAPSHOT не приходит (или приходит `cmd=3`). Watchdog в `_heartbeat_loop` ловит оба случая (таймаут `AUTH_TIMEOUT_SEC`), шлёт алерт через `on_auth_failed` и переподключается с backoff до `MAX_RECONNECT_SEC`. Решение — освежить `MAX_TOKEN`.
- Пинг (op=1) ждёт ответа `PING_TIMEOUT_SEC`; нет ответа → WS закрывается и переоткрывается (half-open TCP).
- `on_ready` запускается задачей, а не await'ом в цикле чтения: внутри него идут RPC, ответы на которые читает тот же цикл.

## Telegram-side нюансы

- Бот должен быть **админом супергруппы с правом «Управление темами»** (`can_manage_topics: true`) — иначе `create_forum_topic` падает.
- Супергруппа должна быть **forum-enabled** (включены Topics).
- Доступные реакции в чате — по умолчанию ограничены, для 👀 нужно «Все эмодзи» в настройках.
- Bot API лимит загрузки файла — 20 МБ.
- Bot **не может** ставить custom-emoji реакции (нужен Premium).

## Команды (в супергруппе)

- `/bind <chat_id|URL> [title]` — ручная привязка топика к MAX-чату.
- `/add <https://max.ru/join/...>` — резолв инвайт-ссылки + создание топика. `/u/<token>` пока не поддерживается.
- `/profile` — в топике, профиль собеседника (имя/id/аватар).
- `/intro` — перепост закреплённой карточки.
- `/rm` — ответом на своё сообщение: удалить его в MAX (op 66).
- `/del` — удалить топик с подтверждением (inline-кнопки).
- `/help` — справка.
- В личке: `/start` (меню: аккаунты, вход, группы), `/login [номер]` (вход по номеру+коду; при нескольких аккаунтах спросит какой), `/cancel`. `/login` в группе — удаляет сообщение и даёт deep-link `t.me/<bot>?start=login`. Меню команд публикуется через `set_my_commands` (`publish_commands`).

## Состояние / runtime

- `state/accounts.json` — владелец, аккаунты (токены!), группы. Права 600, атомарная запись.
- `state/accounts/<id>/topics.json|messages.db` — состояние аккаунтов, добавленных через бота; у `main` — старые пути в корне `state/`.
- `state/topics.json` — JSON-карта `max_chat_id ↔ {topic_id, title}`. Атомарно перезаписывается (tmpfile + os.replace). Critical для непересоздавания топиков. Том должен быть mounted в docker-compose.
- `state/messages.db` — SQLite-карта сообщений для ответов/правок/`/rm`, хранит ~50k последних строк.
- `logs/max2tg.log` — RotatingFileHandler 10MB × 5.

## Поток сообщений (важные инварианты)

- Один `Application` (polling) на всё. `bot_data["bridge"]` = `Bridge`. Хендлеры тем ищут аккаунт через `Bridge.resolve_topic(chat_id, thread_id)`; `/bind` и `/add` — через `_group_target` (при нескольких аккаунтах в группе — первый аргумент = id/имя аккаунта). Без `bridge` в bot_data работает старый одиночный режим (`build_tg_app`, ключи `max_client`/`topic_store`/…) — на нём держатся старые тесты.
- Писать в MAX может только владелец (`_is_allowed`). Пока владельца нет — отправка заблокирована.
- Группы бот узнаёт из `my_chat_member` (флаг форума и права берутся из самого апдейта), миграции group→supergroup (`migrate_to_chat_id`) и любого сообщения в группе (handler group -2). Bot API не умеет «список моих чатов».
- Сообщения с кодом/токеном в личке бот удаляет сразу. Диалог живёт 10 мин (`DIALOG_TTL_SEC`), `PhoneLogin` пингует MAX, пока ждёт код.

- MAX → TG: `handle_message` держит `asyncio.Lock` на чат — сообщения одного чата обрабатываются строго по порядку, топик+интро создаются один раз.
- Сообщение MAX с уже известным id = повторная доставка (текст тот же → пропуск) или правка (текст другой → `sender.edit_text`, иначе ответ «✏️ Изменено»).
- Все `TelegramSender.send*` возвращают первое отправленное `Message` (или None) — это нужно для msgmap.
- `TelegramSender._deliver` ловит BadRequest «thread not found» → пересоздаёт топик (`on_topic_recreated` постит интро) и шлёт туда.
- Скачивание из MAX ограничено `TG_UPLOAD_LIMIT` (50 МБ); из TG — `TG_BOT_DOWNLOAD_LIMIT` (20 МБ).

## Тесты

`pytest -q` → 359 passed. asyncio_mode=auto. Покрытие: TopicStore, config, listener helpers (форматирование размеров, throttle), tg_handler (роутинг команд, маршрутизация медиа), max_client опкоды + авторизация/watchdog/backoff + edit/delete/video/file RPC, tg_sender (split_html, пересоздание топика, reply, edit), msgmap, сквозные потоки listener/handler.

## Деплой

Docker. `docker-compose.yml` биндит `./logs:/app/logs` и `./state:/app/state`. `docker-entrypoint.sh` делает chown этих папок и запускает приложение от пользователя `app` (uid 10001) через `setpriv`. SIGTERM (`docker stop`) корректно гасит мост. Алёрт о подключении/обрыве идёт в General-топик.

## Что осталось / known issues

- Голосовые TG → MAX: уходят как `.ogg` файл (FILE), не как voice bubble. Опкод нативной audio-upload неизвестен (issue в vkmax #14 — без ответа). Hunting requires browser-side network capture.
- Voice MAX → TG: для нового `_type=UNSUPPORTED` нет рабочего download-опкода. Опкод 84/85 — calls service. Probing блокирован WS-disconnect на proto.payload.
- `/u/<token>` (user share) — opcode 57 ищет в chat-namespace. Server hint «No link or token found» для `{token}` payload — обманчив, реально опкод хочет только `link` URL.
- Удаление сообщений в MAX не зеркалится: событие удаления не найдено (смотреть `<<< EVENT` в логах). Правки MAX ловятся только если MAX повторно шлёт op=128 с тем же message id — не подтверждено на живом аккаунте.
- Регистрация нового номера не реализована: протокол шага «имя/фамилия после CHECK_CODE» неизвестен (ни в vkmax, ни у нас). `check_code` распознаёт ответ без `tokenAttrs.LOGIN`, но с REGISTER-подобным ключом, и бросает `NotRegistered` с подсказкой зарегистрироваться в приложении. Чтобы реализовать — нужна запись WS-кадров регистрации из DevTools web.max.ru.
- Phone/about для контакта — `CONTACT_GET` не возвращает. Нужен другой опкод (предположительно тот же, что юзает web.max.ru при открытии профиля справа).

## Если нужно ребутнуть знание о репо

```bash
# Локально
git status
pytest -q

# Прод (путь по README — /opt/max2tg)
cd /opt/max2tg && docker compose ps && docker compose logs --tail=50 max2tg

# Структура развёртывания
# - Контейнер max2tg-max2tg-1, образ собран из ./Dockerfile (один этап на python:3.12-slim; раньше копировали glibc-колёса в alpine — C-расширения aiohttp там не грузились).
```

## Ссылки

- Upstream: [Aist/max2tg](https://github.com/Aist/max2tg)
- Reference opcode-doc: [nsdkinx/vkmax](https://github.com/nsdkinx/vkmax) (особенно [docs/opcodes.md](https://github.com/nsdkinx/vkmax/blob/main/docs/opcodes.md))
- Официальный бот-API MAX: [max-messenger/max-botapi-python](https://github.com/max-messenger/max-botapi-python) — там же `enums/text_style.py` с правильными именами стилей
- Альтернативный мост: [mimimiartartart/MaxToTelegramBridge](https://github.com/mimimiartartart/MaxToTelegramBridge) (one-topic-per-всё, аналогичные паттерны)
