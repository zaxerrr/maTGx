"""Setup dialog in the bot's private chat + discovery of the bot's groups.

Only TG_BOT_TOKEN is needed to start. The first user to send /start becomes
the owner (unless TG_ALLOWED_USER_ID pins it). From the menu the owner adds
MAX accounts — by phone number + confirmation code or by pasting a token —
and binds each account to a forum supergroup the bot is an admin in.

Telegram has no "list my chats" call for bots, so groups are learned from
``my_chat_member`` updates (bot added / promoted / removed) and from any
message the bot sees in a group.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.accounts import Account
from app.login import CodeRejected, LoginError, PhoneLogin, check_token, profile_name

log = logging.getLogger(__name__)

REGISTRY_KEY = "bridge"
DIALOG_TTL_SEC = 600
MAX_CODE_ATTEMPTS = 3

B = InlineKeyboardButton


def _bridge(context):
    return context.bot_data.get(REGISTRY_KEY)


def _kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


# ── owner gate ────────────────────────────────────────────────────

async def _owner_gate(update: Update, context, claim: bool = False) -> bool:
    """True if the user is the owner. With ``claim`` the first user to ask
    becomes the owner (when TG_ALLOWED_USER_ID doesn't pin one)."""
    br = _bridge(context)
    user = update.effective_user
    if br is None or user is None:
        return False
    if br.owner_id is None and claim:
        br.store.set_owner(user.id)
        log.warning("Bot owner claimed by Telegram user %s (@%s)", user.id, user.username)
    if br.owner_id == user.id:
        return True
    if update.effective_message:
        await update.effective_message.reply_text(
            "Этот мост уже настроен другим пользователем." if br.owner_id
            else "Отправьте /start, чтобы стать владельцем моста."
        )
    return False


# ── menu rendering ────────────────────────────────────────────────

def _account_status(br, acc: Account) -> str:
    rt = br.runtimes.get(acc.id)
    if not acc.has_credentials:
        return "⚪ нет токена MAX"
    if acc.group_id is None:
        return "⚠️ не выбрана группа"
    if not acc.enabled:
        return "⏸ выключен"
    if rt is None:
        return "⏹ не запущен"
    return {
        "online": "🟢 онлайн",
        "starting": "🟡 подключается",
        "auth_failed": "🔴 ошибка входа",
    }.get(rt.status, rt.status)


def _group_label(br, group_id) -> str:
    if group_id is None:
        return "—"
    g = br.store.get_group(group_id)
    return f"«{escape(g.title)}»" if g and g.title else f"<code>{group_id}</code>"


def render_menu(br) -> tuple[str, InlineKeyboardMarkup]:
    accounts = br.store.accounts()
    if not accounts:
        text = ("🤖 <b>Мост MAX ↔ Telegram</b>\n\n"
                "MAX-аккаунт не подключён. Как войти?")
        acc_id = br.store.new_id()
        return text, _kb([
            [B("📱 По номеру телефона", callback_data=f"acc:phone:{acc_id}")],
            [B("🔑 Прислать токен", callback_data=f"acc:token:{acc_id}")],
        ])
    lines = ["🤖 <b>Мост MAX ↔ Telegram</b>\n", "Аккаунты MAX:"]
    rows = []
    for acc in accounts:
        lines.append(f"• <b>{escape(acc.title)}</b> (<code>{acc.id}</code>) — "
                     f"{_account_status(br, acc)}, группа {_group_label(br, acc.group_id)}")
        rows.append([B(f"⚙️ {acc.title}", callback_data=f"acc:open:{acc.id}")])
    rows.append([B("➕ Добавить аккаунт MAX", callback_data="acc:add")])
    rows.append([B("🔄 Обновить", callback_data="menu")])
    return "\n".join(lines), _kb(rows)


def render_account(br, acc: Account) -> tuple[str, InlineKeyboardMarkup]:
    rt = br.runtimes.get(acc.id)
    lines = [f"⚙️ <b>{escape(acc.title)}</b> (<code>{acc.id}</code>)",
             f"Статус: {_account_status(br, acc)}",
             f"Группа: {_group_label(br, acc.group_id)}"]
    if acc.phone:
        lines.append(f"Номер: <code>{escape(acc.phone)}</code>")
    if rt and rt.status_detail:
        lines.append(f"<i>{escape(rt.status_detail[:300])}</i>")
    return "\n".join(lines), _kb([
        [B("🔑 Войти заново", callback_data=f"acc:relogin:{acc.id}"),
         B("👥 Группа", callback_data=f"acc:group:{acc.id}")],
        [B("🗑 Удалить аккаунт", callback_data=f"acc:del:{acc.id}")],
        [B("« Назад", callback_data="menu")],
    ])


def render_login_choice(acc_id: str) -> tuple[str, InlineKeyboardMarkup]:
    return "Как войти в MAX?", _kb([
        [B("📱 По номеру телефона", callback_data=f"acc:phone:{acc_id}")],
        [B("🔑 Прислать токен", callback_data=f"acc:token:{acc_id}")],
        [B("« Назад", callback_data="menu")],
    ])


GROUP_HELP = (
    "Нужной группы нет в списке? Создайте супергруппу, включите в настройках "
    "<b>«Темы»</b> и добавьте меня администратором с правом "
    "<b>«Управление темами»</b> — я пришлю сюда уведомление."
)


def render_groups(br, acc_id: str) -> tuple[str, InlineKeyboardMarkup]:
    groups = br.store.groups()
    rows = []
    lines = ["👥 <b>Выберите группу для аккаунта</b>\n"]
    for g in groups:
        if g.ready:
            mark = "✅"
        elif g.is_forum is False:
            mark = "⚠️ темы выключены"
        elif g.can_manage_topics is False:
            mark = "⚠️ нет права «Управление темами»"
        else:
            mark = "❔ не проверена"
        used = [a.title for a in br.store.accounts_in_group(g.id) if a.id != acc_id]
        extra = f" · уже: {escape(', '.join(used))}" if used else ""
        lines.append(f"• {escape(g.title or str(g.id))} — {mark}{extra}")
        rows.append([B(f"{'✅' if g.ready else '⚠️'} {g.title or g.id}",
                       callback_data=f"grp:set:{acc_id}:{g.id}")])
    if not groups:
        lines.append("Я пока не состою ни в одной группе.")
    lines.append("\n" + GROUP_HELP)
    rows.append([B("🔄 Проверить группы", callback_data=f"grp:check:{acc_id}")])
    rows.append([B("« Назад", callback_data="menu")])
    return "\n".join(lines), _kb(rows)


# ── group discovery ───────────────────────────────────────────────

def _can_manage_topics(member) -> bool:
    return (member.status == ChatMemberStatus.ADMINISTRATOR
            and bool(getattr(member, "can_manage_topics", False)))


async def probe_group(bot, br, group_id: int):
    """Refresh what we know about a group: title, forum flag, bot rights.
    Each API call is independent, so one failing (e.g. a getChat field this
    library version can't parse) doesn't hide the other result."""
    title = is_forum = can_manage = None
    try:
        chat = await bot.get_chat(group_id)
        title, is_forum = chat.title, bool(getattr(chat, "is_forum", False))
    except Exception as e:
        log.info("probe_group(%s): getChat failed: %s", group_id, e)
    try:
        can_manage = _can_manage_topics(await bot.get_chat_member(group_id, bot.id))
    except Exception as e:
        log.info("probe_group(%s): getChatMember failed: %s", group_id, e)
    return br.store.remember_group(group_id, title=title, is_forum=is_forum,
                                   can_manage_topics=can_manage)


async def _on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The bot was added to / promoted in / removed from a group."""
    br = _bridge(context)
    cmu = update.my_chat_member
    if br is None or cmu is None or cmu.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    chat = cmu.chat
    status = cmu.new_chat_member.status
    if status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        br.store.forget_group(chat.id)
        affected = br.store.accounts_in_group(chat.id)
        for acc in affected:
            await br.stop_account(acc.id)
        if affected:
            await br.notify_owner(
                f"⚠️ Меня удалили из группы «{escape(chat.title or str(chat.id))}». "
                f"Остановлены аккаунты: {escape(', '.join(a.title for a in affected))}. "
                "Выберите им новую группу в /start.")
        return

    # The update itself carries the forum flag and the bot's new rights.
    g = br.store.remember_group(chat.id, title=chat.title,
                                is_forum=bool(getattr(chat, "is_forum", False)),
                                can_manage_topics=_can_manage_topics(cmu.new_chat_member))
    title = escape(chat.title or str(chat.id))
    if g is not None and g.ready:
        free = [a for a in br.store.accounts() if a.group_id is None]
        bound = br.store.accounts_in_group(chat.id)
        for acc in bound:           # rights just fixed → start what isn't running
            if acc.id not in br.runtimes:
                await br.start_account(acc.id)
        text = f"✅ Группа «{title}» готова: темы включены, права есть."
        buttons = [[B(f"Привязать «{a.title}»", callback_data=f"grp:set:{a.id}:{chat.id}")]
                   for a in free]
        buttons.append([B("Открыть меню", callback_data="menu")])
        await br.notify_owner(text, buttons)
    else:
        hint = []
        if g is None or not g.is_forum:
            hint.append("включите в настройках группы «Темы»")
        if g is None or not g.can_manage_topics:
            hint.append("сделайте меня администратором с правом «Управление темами»")
        await br.notify_owner(
            f"👋 Меня добавили в «{title}». Чтобы вести там чаты MAX, "
            + " и ".join(hint) + ".",
            [[B("🔄 Проверить снова", callback_data="grp:check:-")]],
        )


async def _on_group_seen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remember groups the bot sees traffic in (e.g. added before the owner
    existed). Runs in a separate handler group — never blocks routing."""
    br = _bridge(context)
    chat = update.effective_chat
    if br is None or chat is None or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if br.store.get_group(chat.id) is None:
        await probe_group(context.bot, br, chat.id)


async def _on_migrate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Enabling topics turns a basic group into a supergroup with a new id."""
    br = _bridge(context)
    msg = update.effective_message
    if br is None or msg is None or not msg.migrate_to_chat_id:
        return
    old, new = msg.chat_id, msg.migrate_to_chat_id
    br.store.forget_group(old)
    await probe_group(context.bot, br, new)
    for acc in br.store.accounts_in_group(old):
        acc.group_id = new
        br.store.put(acc)
        await br.start_account(acc.id)
    log.info("Group %s migrated to supergroup %s", old, new)


# ── private chat: commands & buttons ──────────────────────────────

async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _cmd_start_payload(update, context):
        return
    if not await _owner_gate(update, context, claim=True):
        return
    br = _bridge(context)
    await _cancel_dialog(br, update.effective_user.id)
    text, kb = render_menu(br)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


def _login_target(br) -> str | None:
    """Which account /login should (re)log into, or None if ambiguous."""
    accounts = br.store.accounts()
    if not accounts:
        return br.store.new_id()
    if len(accounts) == 1:
        return accounts[0].id
    return None


async def _cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/login [phone]`` — phone + code login without going through the menu.

    One account (or none) → logs into it directly; several → asks which.
    With a phone number in the command the code is requested right away.
    """
    if not await _owner_gate(update, context, claim=True):
        return
    br = _bridge(context)
    uid = update.effective_user.id
    message = update.effective_message
    await _cancel_dialog(br, uid)
    acc_id = _login_target(br)
    if acc_id is None:
        rows = [[B(f"🔑 {a.title} — {_account_status(br, a)}",
                   callback_data=f"acc:phone:{a.id}")] for a in br.store.accounts()]
        rows.append([B("➕ Новый аккаунт", callback_data=f"acc:phone:{br.store.new_id()}")])
        await message.reply_text("В какой аккаунт MAX войти?", reply_markup=_kb(rows))
        return
    phone = " ".join(context.args or []).strip()
    _start_dialog(br, uid, step="phone", acc_id=acc_id)
    if phone:
        await _step_phone(br, br.dialogs[uid], message, phone_text=phone)
        return
    await message.reply_text(
        "📱 Пришлите номер телефона аккаунта MAX, например <code>+79991234567</code>.\n"
        "Отмена — /cancel", parse_mode=ParseMode.HTML)


async def _cmd_login_in_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Never take a phone or a code in a group: members would see them (and
    their notifications keep the text even after the bot deletes it)."""
    message = update.effective_message
    if message is None:
        return
    try:
        await message.delete()          # drop a number typed after /login
    except Exception:
        pass
    username = context.bot.username
    await context.bot.send_message(
        chat_id=message.chat_id,
        message_thread_id=message.message_thread_id if message.is_topic_message else None,
        text="🔐 Вход в MAX — только в личке с ботом: номер и код в группе увидят все участники.",
        reply_markup=_kb([[B("Войти в личке", url=f"https://t.me/{username}?start=login")]]),
    )


async def _cmd_start_payload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Deep link t.me/<bot>?start=login → straight into /login."""
    if (context.args or [None])[0] == "login":
        context.args = []
        await _cmd_login(update, context)
        return True
    return False


async def _cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _owner_gate(update, context):
        return
    await _cancel_dialog(_bridge(context), update.effective_user.id)
    await update.effective_message.reply_text("Отменено. Меню — /start")


async def _edit(query, text, kb=None):
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                      disable_web_page_preview=True)
    except Exception:
        await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                       disable_web_page_preview=True)


async def _on_setup_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await _owner_gate(update, context):
        return
    br = _bridge(context)
    uid = update.effective_user.id
    parts = (query.data or "").split(":")

    if parts[0] == "menu":
        await _cancel_dialog(br, uid)
        await _edit(query, *render_menu(br))
        return

    if parts[0] == "acc":
        action = parts[1]
        acc_id = parts[2] if len(parts) > 2 else None
        acc = br.store.get(acc_id) if acc_id else None
        if action == "add":
            await _edit(query, *render_login_choice(br.store.new_id()))
        elif action == "open" and acc:
            await _edit(query, *render_account(br, acc))
        elif action == "relogin":
            await _edit(query, *render_login_choice(acc_id))
        elif action == "phone":
            _start_dialog(br, uid, step="phone", acc_id=acc_id)
            await _edit(query, "📱 Пришлите номер телефона аккаунта MAX, например "
                               "<code>+79991234567</code>.\nОтмена — /cancel")
        elif action == "token":
            _start_dialog(br, uid, step="token", acc_id=acc_id)
            await _edit(query,
                        "🔑 Пришлите значение <code>__oneme_auth</code> из web.max.ru "
                        "(F12 → Application → Local Storage). Можно целиком JSON или "
                        "только строку из поля <code>token</code>.\n"
                        "Сообщение я сразу удалю. Отмена — /cancel")
        elif action == "group" and acc:
            await _edit(query, *render_groups(br, acc.id))
        elif action == "del" and acc:
            await _edit(query, f"Удалить аккаунт «{escape(acc.title)}» из моста? "
                               "Топики в Telegram останутся.",
                        _kb([[B("🗑 Удалить", callback_data=f"acc:delok:{acc.id}"),
                              B("Отмена", callback_data=f"acc:open:{acc.id}")]]))
        elif action == "delok" and acc:
            await br.stop_account(acc.id)
            br.store.remove(acc.id)
            await _edit(query, *render_menu(br))
        return

    if parts[0] == "grp":
        action = parts[1]
        if action == "check":
            for g in br.store.groups():
                await probe_group(context.bot, br, g.id)
            acc_id = parts[2] if len(parts) > 2 else "-"
            if acc_id != "-" and br.store.get(acc_id):
                await _edit(query, *render_groups(br, acc_id))
            else:
                await _edit(query, *render_menu(br))
        elif action == "set" and len(parts) == 4:
            await _bind_group(query, context, br, parts[2], int(parts[3]))


async def _bind_group(query, context, br, acc_id: str, group_id: int) -> None:
    acc = br.store.get(acc_id)
    if acc is None:
        await _edit(query, "Аккаунт не найден.", _kb([[B("Меню", callback_data="menu")]]))
        return
    g = await probe_group(context.bot, br, group_id)
    if g is None or not g.ready:
        await _edit(query,
                    "⚠️ Эта группа пока не подходит: нужны включённые «Темы» и права "
                    "администратора «Управление темами» у меня.",
                    _kb([[B("🔄 Проверить снова", callback_data=f"grp:set:{acc_id}:{group_id}")],
                         [B("« К списку групп", callback_data=f"acc:group:{acc_id}")]]))
        return
    old_group = acc.group_id
    neighbours = [a for a in br.store.accounts()
                  if a.id != acc.id and a.group_id in (group_id, old_group)]
    before = {a.id: br.title_prefix(a) for a in neighbours}
    acc.group_id = group_id
    br.store.put(acc)
    await br.start_account(acc.id)
    # Neighbours restart only if their topic-name prefix changed (a group
    # became shared or stopped being shared) — no needless reconnects.
    for a in neighbours:
        if br.title_prefix(a) != before[a.id] and a.id in br.runtimes:
            await br.start_account(a.id)
    await _edit(query,
                f"✅ Аккаунт «{escape(acc.title)}» привязан к группе «{escape(g.title)}». "
                "Новые чаты MAX появятся там отдельными темами.",
                _kb([[B("Меню", callback_data="menu")]]))


# ── private chat: dialog steps ────────────────────────────────────

def _start_dialog(br, uid: int, **state) -> None:
    old = br.dialogs.get(uid)
    if old and old.get("login"):
        asyncio.create_task(old["login"].close())
    state["expires"] = asyncio.get_running_loop().time() + DIALOG_TTL_SEC
    br.dialogs[uid] = state


async def _cancel_dialog(br, uid: int) -> None:
    state = br.dialogs.pop(uid, None)
    if state and state.get("login"):
        await state["login"].close()


async def _delete_quietly(message) -> None:
    try:
        await message.delete()
    except Exception:
        log.debug("Could not delete a secret-bearing message", exc_info=True)


def parse_token_input(text: str) -> tuple[str | None, str | None]:
    """Accept a raw token, ``token device_id``, or the __oneme_auth JSON."""
    text = (text or "").strip().strip('"').strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            return None, None
        token = data.get("token")
        device = data.get("deviceId") or data.get("device_id")
        return (token if isinstance(token, str) else None,
                device if isinstance(device, str) else None)
    parts = text.split()
    if len(parts) == 2:
        return parts[0].strip('"'), parts[1].strip('"')
    if len(parts) == 1 and len(parts[0]) >= 20:
        return parts[0], None
    return None, None


_UUIDISH = re.compile(r"^[0-9a-fA-F-]{16,64}$")


async def _on_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    br = _bridge(context)
    if br is None:
        return
    user = update.effective_user
    message = update.effective_message
    state = br.dialogs.get(user.id) if user else None
    if not state:
        if br.owner_id == (user.id if user else None):
            await message.reply_text("Меню — /start")
        return
    if not await _owner_gate(update, context):
        return
    if asyncio.get_running_loop().time() > state["expires"]:
        await _cancel_dialog(br, user.id)
        await message.reply_text("Время на ввод истекло. Начните заново: /start")
        return

    step = state["step"]
    if step == "phone":
        await _step_phone(br, state, message)
    elif step == "code":
        await _delete_quietly(message)
        await _step_code(br, state, message, user.id)
    elif step == "token":
        await _delete_quietly(message)
        await _step_token(br, state, message, user.id)
    elif step == "device":
        await _delete_quietly(message)
        await _step_device(br, state, message, user.id)


async def _step_phone(br, state, message, phone_text: str | None = None) -> None:
    login = PhoneLogin(proxy=br.max_proxy)
    try:
        phone = await login.start(phone_text if phone_text is not None else message.text)
    except LoginError as e:
        await message.reply_text(f"⚠️ {e}\nПришлите номер ещё раз или /cancel.")
        return
    except Exception as e:
        log.exception("Phone login start failed")
        await message.reply_text(f"⚠️ Не удалось связаться с MAX: {e}")
        return
    state.update(step="code", login=login, phone=phone, attempts=0)
    await message.reply_text(
        f"📨 Код отправлен на <code>{escape(phone)}</code> — по SMS или в приложение MAX.\n"
        "Пришлите его сюда (сообщение с кодом я удалю). Отмена — /cancel",
        parse_mode=ParseMode.HTML)


async def _step_code(br, state, message, uid: int) -> None:
    login: PhoneLogin = state["login"]
    try:
        token, device_id, profile = await login.finish(message.text)
    except CodeRejected as e:
        state["attempts"] = state.get("attempts", 0) + 1
        if state["attempts"] >= MAX_CODE_ATTEMPTS:
            await _cancel_dialog(br, uid)
            await message.reply_text(f"⚠️ {e}. Попытки кончились — начните заново: /start")
        else:
            await message.reply_text(f"⚠️ {e}. Пришлите код ещё раз.")
        return
    except LoginError as e:
        await _cancel_dialog(br, uid)
        await message.reply_text(f"⚠️ {e}\nНачните заново: /start")
        return
    await _cancel_dialog(br, uid)
    await _save_credentials(br, message, state["acc_id"], token, device_id, profile,
                            phone=state.get("phone", ""))


async def _step_token(br, state, message, uid: int) -> None:
    token, device_id = parse_token_input(message.text)
    if not token:
        await message.reply_text("Не похоже на токен MAX. Пришлите ещё раз или /cancel.")
        return
    if not device_id:
        state.update(step="device", token=token)
        await message.reply_text(
            "Теперь пришлите <code>__oneme_device_id</code> из того же браузера "
            "(токен привязан к устройству).", parse_mode=ParseMode.HTML)
        return
    await _finish_token(br, message, uid, state["acc_id"], token, device_id)


async def _step_device(br, state, message, uid: int) -> None:
    device_id = (message.text or "").strip().strip('"')
    if not _UUIDISH.match(device_id):
        await message.reply_text("Не похоже на device id. Пришлите ещё раз или /cancel.")
        return
    await _finish_token(br, message, uid, state["acc_id"], state["token"], device_id)


async def _finish_token(br, message, uid, acc_id, token, device_id) -> None:
    await _cancel_dialog(br, uid)
    note = await message.reply_text("⏳ Проверяю токен в MAX…")
    try:
        profile = await check_token(token, device_id, br.max_proxy)
    except LoginError as e:
        await note.edit_text(f"❌ MAX не принял токен: {escape(str(e))}\nМеню — /start",
                             parse_mode=ParseMode.HTML)
        return
    except Exception as e:
        log.exception("Token check failed")
        await note.edit_text(f"⚠️ Не удалось проверить токен: {escape(str(e))}",
                             parse_mode=ParseMode.HTML)
        return
    await _save_credentials(br, message, acc_id, token, device_id, profile)


async def _save_credentials(br, message, acc_id, token, device_id, profile, phone="") -> None:
    acc = br.store.get(acc_id) or Account(id=acc_id)
    acc.token, acc.device_id = token, device_id
    acc.name = profile_name(profile) if profile else (acc.name or acc_id)
    if phone:
        acc.phone = phone
    br.store.put(acc)
    log.info("MAX credentials saved for account %s (%s)", acc.id, acc.name)

    if acc.group_id is not None:
        await br.start_account(acc.id)
        await message.reply_text(
            f"✅ Вход выполнен: <b>{escape(acc.title)}</b>. Мост перезапущен "
            f"в группе {_group_label(br, acc.group_id)}.",
            parse_mode=ParseMode.HTML, reply_markup=_kb([[B("Меню", callback_data="menu")]]))
        return
    text, kb = render_groups(br, acc.id)
    await message.reply_text(f"✅ Вход выполнен: <b>{escape(acc.title)}</b>.\n\n{text}",
                             parse_mode=ParseMode.HTML, reply_markup=kb)


# ── registration ──────────────────────────────────────────────────

PRIVATE_COMMANDS = [
    ("start", "Меню: аккаунты MAX, группы"),
    ("login", "Войти в MAX по номеру и коду"),
    ("cancel", "Прервать ввод"),
]
GROUP_COMMANDS = [
    ("help", "Справка"),
    ("profile", "Профиль собеседника (в теме)"),
    ("intro", "Перепостить карточку (в теме)"),
    ("rm", "Удалить своё сообщение в MAX (ответом)"),
    ("bind", "Привязать тему к чату MAX"),
    ("add", "Открыть ссылку max.ru/join"),
    ("del", "Удалить тему"),
    ("login", "Войти в MAX (откроет личку)"),
]


async def publish_commands(bot) -> None:
    """Show the commands in Telegram's "/" menu (private vs. group lists)."""
    from telegram import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats
    try:
        await bot.set_my_commands([BotCommand(c, d) for c, d in PRIVATE_COMMANDS],
                                  scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_commands([BotCommand(c, d) for c, d in GROUP_COMMANDS],
                                  scope=BotCommandScopeAllGroupChats())
    except Exception:
        log.exception("set_my_commands failed")


def register_setup_handlers(app: Application) -> None:
    private = filters.ChatType.PRIVATE
    # Discovery runs in its own handler group so it never blocks routing.
    app.add_handler(ChatMemberHandler(_on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER),
                    group=-1)
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, _on_migrate), group=-1)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, _on_group_seen), group=-2)

    app.add_handler(CommandHandler(["start", "menu", "accounts"], _cmd_start, filters=private))
    app.add_handler(CommandHandler("cancel", _cmd_cancel, filters=private))
    app.add_handler(CommandHandler("login", _cmd_login, filters=private))
    app.add_handler(CommandHandler("login", _cmd_login_in_group, filters=filters.ChatType.GROUPS))
    app.add_handler(CallbackQueryHandler(_on_setup_button, pattern=r"^(menu|acc:|grp:)"))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, _on_private_text))
