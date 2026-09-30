"""
Бот-напоминалка для Telegram. Версия 2.0.

Как он работает в двух словах:
1. Бот всё время спрашивает у серверов Telegram: «Есть новые сообщения?»
   Это называется polling (опрос). Библиотека telebot делает это за нас.
2. Когда приходит сообщение, команда или нажатие кнопки, telebot вызывает
   функцию-обработчик, которую мы привязали к этому событию.
3. Время из обычного текста («через 10 минут», «завтра в 9») разбирает
   соседний файл timeparser.py.
4. Напоминания хранятся в файле reminders.json, поэтому они не пропадают,
   если бота выключить и включить снова.
5. Параллельно работает отдельный поток (thread) — как второй работник,
   который раз в минуту смотрит на часы и рассылает напоминания, чьё время пришло.
"""

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from html import escape  # «обезвреживает» текст пользователя перед вставкой в HTML-сообщение
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import telebot  # библиотека pyTelegramBotAPI (в pip называется так, а импортируется как telebot)
from dotenv import load_dotenv  # читает настройки из файла .env
from telebot import types  # кнопки и клавиатуры
from telebot.apihelper import ApiTelegramException  # ошибка, которую присылает Telegram

# Наш собственный файл timeparser.py: разбор времени из текста
from timeparser import REPEAT_LABELS, WhenError, in_minutes, next_repeat, parse_when


# ===========================================================================
# 1. НАСТРОЙКИ
# ===========================================================================

# Папка, в которой лежит этот файл. Все остальные файлы (.env, reminders.json)
# ищем рядом с ним — тогда бот работает, даже если запустить его из другой папки.
BASE_DIR = Path(__file__).resolve().parent

# load_dotenv() читает файл .env и делает его строки доступными через os.getenv().
# Зачем так сложно, почему не написать токен прямо в коде? Токен — это пароль
# от бота. Если код попадёт на GitHub вместе с токеном, любой сможет управлять
# ботом. На компьютере токен лежит в файле .env, а на хостинге — в его настройках
# (Environment Variables): os.getenv() читает и оттуда, и оттуда.
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/Moscow").strip()
DATA_FILE = BASE_DIR / "reminders.json"
MAX_TEXT_LENGTH = 1000  # ограничим длину текста напоминания, чтобы сообщение точно влезло в Telegram
LIST_LIMIT = 20         # сколько напоминаний максимум показывать в /list (у сообщения есть предел длины)
KEEP_FIRED_DAYS = 3     # сколько дней помнить сработавшее напоминание, чтобы его можно было отложить

# logging — «журнал» программы: печатает в консоль, что происходит, с датой и временем.
# Удобнее, чем print(): сразу видно, когда случилось событие.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("reminder-bot")

# Проверяем настройки сразу при запуске и объясняем по-человечески, что не так.
# sys.exit("текст") печатает текст и завершает программу.
if not BOT_TOKEN or BOT_TOKEN == "вставьте_сюда_токен":
    sys.exit(
        "Не найден токен бота.\n"
        "Создайте файл .env по образцу .env.example и впишите туда строку BOT_TOKEN=ваш_токен.\n"
        "Как получить токен — написано в README.md."
    )

# Часовой пояс нужен, чтобы «18:30» означало 18:30 именно по вашему времени,
# а не по времени сервера, где запущен бот. Ростов-на-Дону живёт по Москве.
try:
    TZ = ZoneInfo(TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    sys.exit(
        f"Неизвестный часовой пояс «{TIMEZONE_NAME}» в файле .env.\n"
        "Пример правильного значения: TIMEZONE=Europe/Moscow"
    )

# Создаём объект бота. Через него мы и получаем сообщения, и отправляем ответы.
#
# parse_mode="HTML" включает оформление сообщений: <b>жирный</b>, <i>курсив</i>,
# <code>моноширинный</code> (такой текст в Telegram копируется одним нажатием).
#
# ВАЖНО: любой текст, который прислал пользователь, перед вставкой в сообщение
# пропускаем через escape(). Если человек напишет «купить <молоко>», без escape()
# Telegram примет <молоко> за HTML-тег, не поймёт его и откажется отправлять сообщение.
# escape() превращает < > & в безопасные &lt; &gt; &amp; — на экране они выглядят как обычно.
#
# Почему HTML, а не Markdown? В Markdown служебные символы — это * _ ` [ ],
# они постоянно встречаются в обычном тексте, и «обезвреживать» их сложнее.
try:
    bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
except ValueError:
    # telebot сам проверяет, похож ли токен на настоящий (например, есть ли в нём двоеточие)
    sys.exit("Токен выглядит неправильно. Скопируйте его из @BotFather ещё раз целиком.")


# ===========================================================================
# 2. ХРАНИЛИЩЕ НАПОМИНАНИЙ (файл reminders.json)
# ===========================================================================
#
# Все напоминания держим в памяти в словаре data, а при каждом изменении
# записываем его в файл. Файл выглядит так:
#
# {
#   "next_id": 4,                  <- номер, который получит следующее напоминание
#   "reminders": [
#     {
#       "id": 3,                   <- номер напоминания (его показывает /list)
#       "chat_id": 123456789,      <- в какой чат отправить напоминание
#       "text": "Позвонить маме",  <- что напомнить
#       "due": "2026-09-29T18:30:00+03:00",   <- когда: дата, время и часовой пояс
#       "repeat": "daily",         <- повтор: daily / weekdays / weekly (у разовых этого поля нет)
#       "fired_at": "2026-09-29T18:30:00+03:00"  <- есть только у уже сработавших разовых
#     }
#   ]
# }
#
# Почему номера не 1, 2, 3 по порядку в списке, а «сквозные»?
# Представьте: в /list было «1. Позвонить маме, 2. Купить хлеб». Пока вы печатаете
# /delete 2, первое напоминание срабатывает и исчезает — и «Купить хлеб» становится
# номером 1. Команда удалила бы не то. Сквозной номер закреплён за напоминанием
# навсегда, поэтому такой путаницы не бывает.
#
# Зачем хранить сработавшие напоминания (fired_at)? Чтобы работала кнопка «Отложить»:
# когда её нажимают, бот должен помнить текст напоминания. Через KEEP_FIRED_DAYS дней
# такие записи удаляются сами.

# Замок (lock). С данными работают сразу два потока: обработчики команд и проверка
# по времени. Если оба одновременно начнут менять список, данные могут испортиться.
# Конструкция `with data_lock:` пускает к данным только одного за раз — второй ждёт.
data_lock = threading.Lock()


def load_data():
    """Читает напоминания из файла. Если файла ещё нет — начинает с пустого списка."""
    empty = {"next_id": 1, "reminders": []}
    if not DATA_FILE.exists():
        return empty
    try:
        with DATA_FILE.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        # Файл испорчен (например, его отредактировали вручную и потеряли запятую).
        # Не удаляем его молча, а переименовываем — чтобы данные можно было спасти.
        backup = DATA_FILE.with_name("reminders.broken.json")
        DATA_FILE.replace(backup)
        log.error("Файл %s повреждён (%s). Сохранил копию как %s, начинаю с пустого списка.",
                  DATA_FILE.name, error, backup.name)
        return empty


def save_data():
    """Записывает напоминания в файл. Вызывать только внутри `with data_lock:`."""
    # Сначала пишем во временный файл, а потом одним действием подменяем им старый.
    # Если программа упадёт посреди записи, старый reminders.json останется целым.
    tmp_file = DATA_FILE.with_suffix(".tmp")
    with tmp_file.open("w", encoding="utf-8") as f:
        # ensure_ascii=False — чтобы русские буквы в файле были буквами, а не З...
        # indent=2 — отступы, чтобы файл было удобно читать глазами
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp_file.replace(DATA_FILE)


def is_active(reminder):
    """Активное напоминание — то, которое ещё ждёт своего времени."""
    return "fired_at" not in reminder


def create_reminder(chat_id, text, due, repeat=None):
    """Добавляет напоминание в список, сохраняет файл и возвращает созданное напоминание."""
    with data_lock:
        reminder = {
            "id": data["next_id"],
            "chat_id": chat_id,
            "text": text,
            "due": due.isoformat(),  # isoformat() превращает дату в строку для JSON
        }
        if repeat:
            reminder["repeat"] = repeat
        data["reminders"].append(reminder)
        data["next_id"] += 1
        save_data()
    log.info("Новое напоминание №%s для чата %s на %s%s", reminder["id"], chat_id, reminder["due"],
             f" (повтор: {repeat})" if repeat else "")
    return reminder


def find_reminder(chat_id, number):
    """Ищет напоминание по номеру в этом чате (и активное, и недавно сработавшее)."""
    with data_lock:
        for r in data["reminders"]:
            # Проверяем и номер, и чат: так никто не доберётся до чужого напоминания, подобрав номер
            if r["id"] == number and r["chat_id"] == chat_id:
                return r
    return None


def delete_reminder(chat_id, number, only_active=True):
    """Удаляет напоминание с таким номером из этого чата. Возвращает удалённое или None."""
    found = None
    with data_lock:
        for r in data["reminders"]:
            if r["id"] == number and r["chat_id"] == chat_id and (is_active(r) or not only_active):
                found = r
                break
        if found:
            data["reminders"].remove(found)
            save_data()
    return found


def set_repeat(chat_id, number, repeat):
    """Включает, меняет или выключает (repeat=None) повтор. Возвращает напоминание или None."""
    with data_lock:
        for r in data["reminders"]:
            if r["id"] == number and r["chat_id"] == chat_id and is_active(r):
                if repeat:
                    r["repeat"] = repeat
                    due = get_due(r)
                    # «По будням» для напоминания на субботу: переносим первый раз на понедельник
                    while repeat == "weekdays" and due.weekday() >= 5:
                        due += timedelta(days=1)
                    r["due"] = due.isoformat()
                else:
                    r.pop("repeat", None)
                save_data()
                return r
    return None


# Загружаем напоминания один раз при запуске бота
data = load_data()


# ===========================================================================
# 3. ВРЕМЯ
# ===========================================================================
# Разбор фраз вроде «через 2 часа» или «завтра в 9» живёт в файле timeparser.py.
# Здесь — только мелкие помощники для вывода времени на экран.

def now_local():
    return datetime.now(TZ)


def understand(text, allow_bare=False):
    """Ищет время в тексте. Возвращает результат разбора, None или бросает WhenError."""
    return parse_when(text, now_local(), allow_bare=allow_bare)


def get_due(reminder):
    """Достаёт из напоминания время срабатывания (в файле оно хранится строкой)."""
    return datetime.fromisoformat(reminder["due"]).astimezone(TZ)


def human_when(due):
    """Красиво пишет время: «сегодня в 18:30», «завтра в 09:00» или «05.10 в 12:00»."""
    today = now_local().date()
    time_str = due.strftime("%H:%M")
    if due.date() == today:
        return f"сегодня в {time_str}"
    if due.date() == today + timedelta(days=1):
        return f"завтра в {time_str}"
    date_str = due.strftime("%d.%m") if due.year == today.year else due.strftime("%d.%m.%Y")
    return f"{date_str} в {time_str}"


def capitalize(text):
    """Делает первую букву заглавной: «сегодня в 18:30» -> «Сегодня в 18:30»."""
    return text[:1].upper() + text[1:]


# ===========================================================================
# 4. КНОПКИ И ТЕКСТЫ СООБЩЕНИЙ
# ===========================================================================
#
# В Telegram есть два вида кнопок:
#
# 1) Reply-клавиатура — кнопки внизу экрана, вместо обычной клавиатуры телефона.
#    Нажатие такой кнопки просто отправляет её текст как обычное сообщение.
#    Поэтому ловим их обычным message_handler, сравнивая текст сообщения с текстом кнопки.
#
# 2) Inline-кнопки — прикреплены к конкретному сообщению (под ним).
#    Нажатие не отправляет сообщение в чат, а присылает боту «callback» —
#    короткую строку данных (callback_data, не длиннее 64 байт), которую мы сами
#    задали кнопке, например "del:3". Ловим их через callback_query_handler.

# Тексты кнопок вынесены в константы: они нужны и при создании клавиатуры,
# и в обработчиках — так точно не будет опечатки в одном из мест.
BTN_NEW = "➕ Новое напоминание"
BTN_LIST = "📋 Мои напоминания"
BTN_HELP = "❓ Помощь"

# Кнопки быстрого выбора времени: (надпись, фраза). Фразу разбирает тот же timeparser,
# что и текст от пользователя — поэтому добавить свою кнопку можно одной строкой.
QUICK_TIMES = [
    ("5 мин", "через 5 минут"),
    ("15 мин", "через 15 минут"),
    ("30 мин", "через 30 минут"),
    ("1 час", "через 1 час"),
    ("2 часа", "через 2 часа"),
    ("3 часа", "через 3 часа"),
    ("🌆 Вечером", "вечером"),
    ("🌅 Завтра утром", "завтра в 9:00"),
]

TIME_EXAMPLES = (
    "<code>через 45 минут</code> · <code>в 18:30</code> · <code>завтра в 9</code> · "
    "<code>в пятницу вечером</code> · <code>5 октября 12:00</code>"
)


def button(text, data_):
    return types.InlineKeyboardButton(text, callback_data=data_)


def main_keyboard():
    """Главное меню — reply-клавиатура внизу экрана."""
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,   # подогнать высоту кнопок под содержимое, иначе они огромные
        is_persistent=True,     # не прятать клавиатуру после нажатия
        input_field_placeholder="Например: через 10 минут позвонить",  # серая подсказка в поле ввода
    )
    keyboard.row(BTN_NEW)             # первый ряд — одна широкая кнопка
    keyboard.row(BTN_LIST, BTN_HELP)  # второй ряд — две кнопки
    return keyboard


def cancel_keyboard():
    """Одна inline-кнопка «Отмена» под сообщением."""
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(button("✖️ Отмена", "cancel"))
    return keyboard


def time_keyboard():
    """Inline-кнопки быстрого выбора времени. В callback_data — номер кнопки в списке QUICK_TIMES."""
    buttons = [button(label, f"time:{index}") for index, (label, _phrase) in enumerate(QUICK_TIMES)]
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(*buttons[0:3])  # звёздочка «раскладывает» список на отдельные аргументы
    keyboard.row(*buttons[3:6])
    keyboard.row(*buttons[6:8])
    keyboard.row(button("✖️ Отмена", "cancel"))
    return keyboard


def created_keyboard(reminder):
    """Кнопки под сообщением «напоминание создано»."""
    repeat = reminder.get("repeat")
    label = f"🔁 {capitalize(REPEAT_LABELS[repeat])}" if repeat else "🔁 Повторять"
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(button(label, f"rep:{reminder['id']}"), button("🗑 Удалить", f"rm:{reminder['id']}"))
    return keyboard


def repeat_keyboard(reminder):
    """Меню выбора повтора."""
    rid = reminder["id"]
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(button("Каждый день", f"setrep:{rid}:daily"), button("По будням", f"setrep:{rid}:weekdays"))
    keyboard.row(button("Каждую неделю", f"setrep:{rid}:weekly"), button("Не повторять", f"setrep:{rid}:none"))
    keyboard.row(button("← Назад", f"setrep:{rid}:keep"))
    return keyboard


def fired_keyboard(reminder):
    """Кнопки под сработавшим напоминанием: отложить или отметить выполненным."""
    rid = reminder["id"]
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(button("⏰ +10 мин", f"snz:{rid}:10"), button("⏰ +1 час", f"snz:{rid}:60"))
    keyboard.row(button("🌅 Завтра утром", f"snz:{rid}:tom"), button("✅ Готово", f"done:{rid}"))
    return keyboard


def text_error(text):
    """Проверяет текст напоминания. Возвращает описание ошибки или None, если всё хорошо."""
    if not text:
        return "Текст напоминания пустой."
    if len(text) > MAX_TEXT_LENGTH:
        return f"Слишком длинный текст: больше {MAX_TEXT_LENGTH} символов. Сократите, пожалуйста."
    return None


def created_text(reminder):
    """Сообщение «напоминание создано»."""
    lines = [f"✅ <b>Готово!</b> Напомню {human_when(get_due(reminder))}"]
    if reminder.get("repeat"):
        lines.append(f"🔁 Дальше — {REPEAT_LABELS[reminder['repeat']]}")
    lines += ["", f"📝 {escape(reminder['text'])}", f"🔢 Номер: <b>{reminder['id']}</b>"]
    return "\n".join(lines)


def render_list(chat_id):
    """Готовит текст списка напоминаний и inline-кнопки удаления. Возвращает (текст, клавиатура)."""
    with data_lock:
        # Берём только активные напоминания из этого чата: чужие показывать нельзя
        mine = [r for r in data["reminders"] if r["chat_id"] == chat_id and is_active(r)]

    if not mine:
        return ("📭 <b>Напоминаний нет</b>\n\n"
                "Просто напишите, что и когда напомнить, например:\n"
                "<code>через 10 минут выключить плиту</code>"), None

    mine.sort(key=get_due)  # сортируем по времени: ближайшие — сверху
    shown = mine[:LIST_LIMIT]

    lines = [f"📋 <b>Ваши напоминания</b> · {len(mine)}\n"]
    buttons = []
    for r in shown:
        due = get_due(r)
        text = r["text"] if len(r["text"]) <= 100 else r["text"][:100] + "…"  # длинное обрежем
        repeat = f" · 🔁 {REPEAT_LABELS[r['repeat']]}" if r.get("repeat") else ""
        lines.append(f"🕐 <b>{capitalize(human_when(due))}</b> · №{r['id']}{repeat}\n📝 {escape(text)}\n")
        buttons.append(button(f"🗑 №{r['id']} · {due:%H:%M}", f"del:{r['id']}"))

    if len(mine) > LIST_LIMIT:
        lines.append(f"<i>…и ещё {len(mine) - LIST_LIMIT}. Удалить их можно командой /delete номер</i>\n")
    lines.append("Нажмите 🗑, чтобы удалить напоминание.")

    keyboard = types.InlineKeyboardMarkup(row_width=2)  # по две кнопки в ряд
    keyboard.add(*buttons)
    return "\n".join(lines), keyboard


def edit_message(message, text, keyboard=None):
    """Меняет текст уже отправленного сообщения (например, список после удаления)."""
    try:
        bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=keyboard)
    except ApiTelegramException as error:
        # Если текст не изменился (например, кнопку нажали дважды), Telegram ругается
        # «message is not modified». Это не страшно — просто пропускаем.
        if "message is not modified" not in str(error.description):
            log.warning("Не удалось изменить сообщение: %s", error)


def set_buttons(chat_id, message_id, keyboard=None):
    """Меняет inline-кнопки у сообщения. Без keyboard — убирает их, чтобы не нажимали повторно."""
    if not message_id:
        return
    try:
        bot.edit_message_reply_markup(chat_id, message_id, reply_markup=keyboard)
    except ApiTelegramException:
        pass  # сообщение могло быть удалено или кнопок уже нет — ничего страшного


START_TEXT = (
    "👋 Привет, <b>{name}</b>!\n\n"
    "Я бот-напоминалка ⏰ Просто напишите мне, что и когда напомнить:\n\n"
    "<code>через 10 минут выключить плиту</code>\n"
    "<code>завтра в 9 позвонить врачу</code>\n"
    "<code>каждый день в 8:00 зарядка</code>\n\n"
    f"Или нажмите <b>{BTN_NEW}</b> внизу — спрошу всё по шагам.\n"
    "Все возможности — в /help"
)

HELP_TEXT = (
    "❓ <b>Как пользоваться ботом</b>\n\n"
    "<b>Проще всего — написать одним сообщением</b>\n"
    "<code>через 10 минут выключить плиту</code>\n"
    "<code>завтра в 9 позвонить врачу</code>\n"
    "<code>купить цветы в пятницу вечером</code>\n"
    "<code>5 октября 12:00 встреча</code>\n"
    "<code>каждый день в 8:00 зарядка</code>\n\n"
    "<b>Как можно указать время</b>\n"
    "⏱ через 5 минут · через 2 часа · через полчаса · через 3 дня\n"
    "🕐 в 18:30 · в 7 вечера · утром · вечером\n"
    "📅 завтра · послезавтра · в пятницу · 5 октября · 05.10 18:30\n"
    "🔁 каждый день · по будням · каждую пятницу · каждое утро\n\n"
    "<b>Когда напоминание пришло</b>\n"
    "Под ним есть кнопки: отложить на 10 минут, на час, до завтра — или отметить ✅ Готово.\n\n"
    "<b>Кнопки внизу экрана</b>\n"
    f"{BTN_NEW} — по шагам: сначала текст, потом время\n"
    f"{BTN_LIST} — список, удаление кнопкой 🗑\n"
    f"{BTN_HELP} — эта справка\n\n"
    "<b>Команды</b>\n"
    "/new — новое напоминание по шагам\n"
    "/list — все активные напоминания\n"
    "/delete — удалить по номеру: <code>/delete 3</code>\n"
    "/cancel — отменить создание напоминания\n"
    "/remind — то же, что написать без команды:\n"
    "<code>/remind через 2 часа Позвонить маме</code>\n\n"
    "<b>Полезно знать</b>\n"
    f"🌍 Время считаю по поясу <b>{TIMEZONE_NAME}</b>\n"
    "📅 Если указанное время сегодня уже прошло, напомню завтра\n"
    "🔢 Номера напоминаний не меняются, когда удаляете другие\n\n"
    "<i>Нажмите на пример в сером прямоугольнике — он скопируется.</i>"
)

TIME_HELP = (
    "⚠️ Не понял время. Нажмите кнопку выше или напишите, например:\n" + TIME_EXAMPLES
)


# ===========================================================================
# 5. КОМАНДЫ И КНОПКИ МЕНЮ
# ===========================================================================
#
# Строка @bot.message_handler(...) над функцией — это «декоратор».
# Он говорит telebot: «Когда придёт такое сообщение, вызови функцию ниже».
#   commands=["list"]            — сработает на команду /list
#   func=lambda m: m.text == ... — сработает, если функция вернула True (здесь: текст совпал с кнопкой)
# Декораторов над одной функцией может быть несколько — тогда она сработает на любой из них.
#
# В функцию передаётся message — всё о пришедшем сообщении:
#   message.text     — текст сообщения, например "/remind 18:30 Позвонить маме"
#   message.chat.id  — номер чата, куда отвечать
#   message.from_user.first_name — имя отправителя
#
# ВАЖНО: telebot проверяет обработчики сверху вниз и вызывает первый подходящий.
# Поэтому обработчик «любого текста» (раздел 6) стоит после всех остальных.

# Состояние диалога. Создание напоминания по шагам идёт в несколько сообщений,
# и между ними нужно помнить, на каком шаге каждый чат. Для этого словарь:
#   states[chat_id] = {"step": "text", "msg_id": 55}                    — ждём текст
#   states[chat_id] = {"step": "text", "msg_id": 55, "due": "...", "repeat": None}
#                                                  — ждём текст, время уже известно
#   states[chat_id] = {"step": "time", "text": "...", "msg_id": 56}     — ждём время
# msg_id — номер сообщения с кнопками, чтобы отличать свежие кнопки от старых.
# Храним в памяти: если перезапустить бота посреди диалога, начнёте заново — не страшно.
states = {}


def take_state(chat_id, step, msg_id=None):
    """Забирает (и удаляет) состояние диалога, если чат сейчас на нужном шаге. Иначе — None.

    Делаем это под замком: если человек очень быстро нажмёт кнопку дважды,
    два обработчика могут выполняться одновременно. Замок гарантирует, что
    состояние достанется только одному — и напоминание не создастся дважды.
    """
    with data_lock:
        st = states.get(chat_id)
        if st and st["step"] == step and (msg_id is None or st.get("msg_id") == msg_id):
            return states.pop(chat_id)
    return None


def reset_state(chat_id):
    """Прерывает начатое создание напоминания (если человек нажал другую команду или кнопку)."""
    st = states.pop(chat_id, None)
    if st:
        set_buttons(chat_id, st.get("msg_id"))


def ask_text(chat_id, due=None, repeat=None):
    """Шаг «Что напомнить?». Если время уже известно — запоминаем его вместе с шагом."""
    question = "📝 <b>Что напомнить?</b>" if due is None else f"📝 <b>Что напомнить {human_when(due)}?</b>"
    sent = bot.send_message(chat_id, f"{question}\n\nНапишите текст одним сообщением, например:\n<i>Позвонить маме</i>",
                            reply_markup=cancel_keyboard())
    states[chat_id] = {"step": "text", "msg_id": sent.message_id}
    if due is not None:
        states[chat_id].update(due=due.isoformat(), repeat=repeat)


def ask_time(chat_id, text):
    """Шаг «Когда напомнить?» с кнопками быстрого выбора."""
    sent = bot.send_message(
        chat_id,
        f"🕐 <b>Когда напомнить?</b>\n\n📝 {escape(text)}\n\n"
        f"Нажмите кнопку или напишите своё время, например:\n{TIME_EXAMPLES}",
        reply_markup=time_keyboard(),
    )
    states[chat_id] = {"step": "time", "text": text, "msg_id": sent.message_id}


def send_created(chat_id, text, due, repeat=None, reply_to=None):
    """Создаёт напоминание и отправляет подтверждение с кнопками «Повторять» и «Удалить»."""
    reminder = create_reminder(chat_id, text, due, repeat)
    if reply_to is not None:
        bot.reply_to(reply_to, created_text(reminder), reply_markup=created_keyboard(reminder))
    else:
        bot.send_message(chat_id, created_text(reminder), reply_markup=created_keyboard(reminder))
    return reminder


@bot.message_handler(commands=["start"])
def cmd_start(message):
    """/start — приветствие и главное меню с кнопками."""
    reset_state(message.chat.id)
    name = escape(message.from_user.first_name or "друг")
    bot.send_message(message.chat.id, START_TEXT.format(name=name), reply_markup=main_keyboard())


@bot.message_handler(commands=["help"])
@bot.message_handler(func=lambda m: m.text == BTN_HELP)
def cmd_help(message):
    """/help и кнопка «Помощь» — подробное описание всех возможностей."""
    reset_state(message.chat.id)
    bot.send_message(message.chat.id, HELP_TEXT, reply_markup=main_keyboard())


@bot.message_handler(commands=["new"])
@bot.message_handler(func=lambda m: m.text == BTN_NEW)
def cmd_new(message):
    """/new и кнопка «Новое напоминание» — шаг 1: спрашиваем текст."""
    reset_state(message.chat.id)
    ask_text(message.chat.id)


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    """/cancel — отменяет создание напоминания."""
    if message.chat.id in states:
        reset_state(message.chat.id)
        bot.send_message(message.chat.id, "✖️ Создание напоминания отменено.", reply_markup=main_keyboard())
    else:
        bot.send_message(message.chat.id, "Сейчас нечего отменять 🙂", reply_markup=main_keyboard())


@bot.message_handler(commands=["remind"])
def cmd_remind(message):
    """/remind через 2 часа Позвонить маме — создаёт напоминание одной строкой."""
    reset_state(message.chat.id)
    # split(maxsplit=1) отрезает команду от всего остального:
    # "/remind через 2 часа Позвонить маме" -> ["/remind", "через 2 часа Позвонить маме"]
    parts = message.text.split(maxsplit=1)
    usage = ("Напишите, когда и что напомнить, например:\n"
             "<code>/remind через 2 часа Позвонить маме</code>\n"
             "<code>/remind 18:30 Забрать заказ</code>")
    if len(parts) < 2:
        bot.reply_to(message, f"⚠️ {usage}")
        return  # return — выходим из функции, дальше не идём

    try:
        when = understand(parts[1])
    except WhenError as error:
        bot.reply_to(message, f"⚠️ {escape(str(error))}")
        return
    if when is None:
        bot.reply_to(message, f"⚠️ Не понял, когда напомнить. {usage}")
        return
    error = text_error(when.text)
    if error:
        bot.reply_to(message, f"⚠️ {error}\n\n{usage}")
        return

    send_created(message.chat.id, when.text, when.due, when.repeat, reply_to=message)


@bot.message_handler(commands=["list"])
@bot.message_handler(func=lambda m: m.text == BTN_LIST)
def cmd_list(message):
    """/list и кнопка «Мои напоминания» — список с кнопками удаления."""
    reset_state(message.chat.id)
    text, keyboard = render_list(message.chat.id)
    bot.send_message(message.chat.id, text, reply_markup=keyboard)


@bot.message_handler(commands=["delete"])
def cmd_delete(message):
    """/delete 3 — удаляет напоминание №3."""
    reset_state(message.chat.id)
    parts = message.text.split()
    # Разрешим писать и "/delete 3", и "/delete №3"
    number_text = parts[1].lstrip("№") if len(parts) > 1 else ""
    if not number_text.isdigit():  # isdigit() — «строка состоит только из цифр?»
        bot.reply_to(message, "⚠️ Укажите номер напоминания, например: <code>/delete 3</code>\n"
                              f"Номера видно в <b>{BTN_LIST}</b> — там же можно удалить кнопкой 🗑")
        return
    number = int(number_text)

    found = delete_reminder(message.chat.id, number)
    if found:
        bot.reply_to(message, f"🗑 Удалил напоминание <b>№{number}</b>\n📝 {escape(found['text'])}")
    else:
        bot.reply_to(message, f"⚠️ Напоминания №{number} нет. Проверьте номер в /list")


# ===========================================================================
# 6. ОБЫЧНЫЙ ТЕКСТ И INLINE-КНОПКИ
# ===========================================================================

# content_types=["text"] без других условий — ловит любой текст. Этот обработчик
# стоит последним среди message_handler, поэтому сюда попадает только то,
# что не подошло командам и кнопкам меню выше.
@bot.message_handler(content_types=["text"])
def on_text(message):
    """Любой текст: ответ в диалоге создания или напоминание одним сообщением."""
    chat_id = message.chat.id
    text = message.text.strip()
    st = states.get(chat_id)

    if text.startswith("/"):
        hint = "Ответьте на вопрос выше или нажмите /cancel" if st else "Список команд — /help"
        bot.reply_to(message, f"⚠️ Такой команды нет. {hint}")
        return

    if st and st["step"] == "time":
        answer_time(message, text)
    elif st and st["step"] == "text":
        answer_text(message, st, text)
    else:
        free_text(message, text)


def free_text(message, text):
    """Сообщение вне диалога: пробуем понять его как «когда + что»."""
    chat_id = message.chat.id
    try:
        when = understand(text)
    except WhenError as error:
        bot.reply_to(message, f"⚠️ {escape(str(error))}")
        return

    if when is None:
        # Времени в сообщении нет — считаем его текстом напоминания и спрашиваем, когда напомнить
        error = text_error(text)
        if error:
            bot.reply_to(message, f"⚠️ {error}")
            return
        ask_time(chat_id, text)
    elif not when.text:
        # Есть только время («через 5 минут») — спрашиваем, что напомнить
        ask_text(chat_id, when.due, when.repeat)
    else:
        error = text_error(when.text)
        if error:
            bot.reply_to(message, f"⚠️ {error}")
            return
        send_created(chat_id, when.text, when.due, when.repeat, reply_to=message)


def answer_text(message, st, text):
    """Шаг диалога «Что напомнить?»: получили текст."""
    chat_id = message.chat.id

    if "due" in st:
        # Время уже известно — сразу создаём
        error = text_error(text)
        if error:
            bot.reply_to(message, f"⚠️ {error}")
            return
        st = take_state(chat_id, "text")
        if st is None:
            return
        set_buttons(chat_id, st.get("msg_id"))
        send_created(chat_id, text, datetime.fromisoformat(st["due"]), st.get("repeat"))
        return

    # Человек мог написать всё сразу: «позвонить маме через 5 минут»
    try:
        when = understand(text)
    except WhenError as error:
        bot.reply_to(message, f"⚠️ {escape(str(error))}")
        return
    reminder_text = when.text if when and when.text else text
    error = text_error(reminder_text)
    if error:
        bot.reply_to(message, f"⚠️ {error}")
        return

    set_buttons(chat_id, st.get("msg_id"))  # у вопроса «Что напомнить?» кнопка «Отмена» больше не нужна
    if when and when.text:
        states.pop(chat_id, None)
        send_created(chat_id, when.text, when.due, when.repeat)
    else:
        ask_time(chat_id, text)


def answer_time(message, text):
    """Шаг диалога «Когда напомнить?»: время написали текстом."""
    chat_id = message.chat.id
    try:
        # allow_bare=True: бот сам спросил про время, поэтому «15 минут» без «через» тоже понятно
        when = understand(text, allow_bare=True)
    except WhenError as error:
        bot.reply_to(message, f"⚠️ {escape(str(error))}")
        return
    if when is None or when.text:
        bot.reply_to(message, TIME_HELP)
        return

    st = take_state(chat_id, "time")
    if st is None:
        return  # напоминание уже успели создать кнопкой
    set_buttons(chat_id, st["msg_id"])
    send_created(chat_id, st["text"], when.due, when.repeat)


# Обработчики нажатий inline-кнопок. В них приходит call:
#   call.data    — строка, которую мы записали в callback_data кнопки ("del:3", "time:2", "cancel")
#   call.message — сообщение, под которым была кнопка
# На каждое нажатие нужно ответить answer_callback_query — иначе у пользователя
# на кнопке будут бесконечно крутиться «часики». Текст ответа всплывает сверху на пару секунд.

@bot.callback_query_handler(func=lambda call: call.data.startswith("time:"))
def cb_time(call):
    """Нажата кнопка быстрого выбора времени."""
    chat_id = call.message.chat.id
    st = take_state(chat_id, "time", call.message.message_id)
    if st is None:
        # Кнопка от старого вопроса (или напоминание уже создано) — не создаём дубль
        bot.answer_callback_query(call.id, "Эта кнопка уже неактуальна")
        set_buttons(chat_id, call.message.message_id)
        return

    phrase = QUICK_TIMES[int(call.data.split(":", 1)[1])][1]  # "time:4" -> "через 2 часа"
    when = understand(phrase)
    reminder = create_reminder(chat_id, st["text"], when.due)
    bot.answer_callback_query(call.id, "✅ Напоминание создано")
    edit_message(call.message, created_text(reminder), created_keyboard(reminder))  # вопрос превращаем в подтверждение


@bot.callback_query_handler(func=lambda call: call.data == "cancel")
def cb_cancel(call):
    """Нажата кнопка «Отмена»."""
    chat_id = call.message.chat.id
    st = states.get(chat_id)
    if st and st.get("msg_id") == call.message.message_id:
        states.pop(chat_id, None)
    bot.answer_callback_query(call.id, "Отменено")
    edit_message(call.message, "✖️ <i>Создание напоминания отменено</i>")


@bot.callback_query_handler(func=lambda call: call.data.startswith("del:"))
def cb_delete(call):
    """Нажата кнопка 🗑 в списке напоминаний."""
    chat_id = call.message.chat.id
    number = int(call.data.split(":", 1)[1])  # "del:3" -> 3
    found = delete_reminder(chat_id, number)
    bot.answer_callback_query(call.id, f"🗑 Удалено №{number}" if found else "Уже удалено")
    # Обновляем список прямо в том же сообщении
    text, keyboard = render_list(chat_id)
    edit_message(call.message, text, keyboard)


@bot.callback_query_handler(func=lambda call: call.data.startswith("rm:"))
def cb_remove(call):
    """Нажата кнопка «Удалить» под сообщением о созданном напоминании."""
    chat_id = call.message.chat.id
    found = delete_reminder(chat_id, int(call.data.split(":", 1)[1]))
    if found:
        bot.answer_callback_query(call.id, "🗑 Удалено")
        edit_message(call.message, f"🗑 <i>Напоминание удалено</i>\n\n📝 {escape(found['text'])}")
    else:
        bot.answer_callback_query(call.id, "Этого напоминания уже нет")
        set_buttons(chat_id, call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("rep:"))
def cb_repeat_menu(call):
    """Нажата кнопка «Повторять»: показываем варианты."""
    chat_id = call.message.chat.id
    reminder = find_reminder(chat_id, int(call.data.split(":", 1)[1]))
    if reminder is None or not is_active(reminder):
        bot.answer_callback_query(call.id, "Это напоминание уже сработало или удалено")
        set_buttons(chat_id, call.message.message_id)
        return
    bot.answer_callback_query(call.id)
    set_buttons(chat_id, call.message.message_id, repeat_keyboard(reminder))


@bot.callback_query_handler(func=lambda call: call.data.startswith("setrep:"))
def cb_set_repeat(call):
    """Выбран вариант повтора."""
    chat_id = call.message.chat.id
    _prefix, rid, mode = call.data.split(":")  # "setrep:3:daily" -> ["setrep", "3", "daily"]
    if mode == "keep":
        reminder = find_reminder(chat_id, int(rid))
        if reminder is not None and not is_active(reminder):
            reminder = None
    else:
        reminder = set_repeat(chat_id, int(rid), None if mode == "none" else mode)
    if reminder is None:
        bot.answer_callback_query(call.id, "Это напоминание уже сработало или удалено")
        set_buttons(chat_id, call.message.message_id)
        return
    answers = {"keep": None, "none": "Повтор выключен"}
    bot.answer_callback_query(call.id, answers.get(mode, f"🔁 Буду напоминать {REPEAT_LABELS.get(mode, '')}"))
    edit_message(call.message, created_text(reminder), created_keyboard(reminder))


@bot.callback_query_handler(func=lambda call: call.data.startswith("snz:"))
def cb_snooze(call):
    """Нажата кнопка «Отложить» под сработавшим напоминанием."""
    chat_id = call.message.chat.id
    _prefix, rid, code = call.data.split(":")  # "snz:3:10" -> отложить №3 на 10 минут
    reminder = find_reminder(chat_id, int(rid))
    if reminder is None:
        bot.answer_callback_query(call.id, "Этого напоминания уже нет — создайте новое")
        set_buttons(chat_id, call.message.message_id)
        return

    due = understand("завтра в 9:00").due if code == "tom" else in_minutes(now_local(), int(code))
    # Отложенное — это новое разовое напоминание с тем же текстом.
    # У повторяющегося основное расписание при этом не трогаем.
    new = create_reminder(chat_id, reminder["text"], due)
    if not is_active(reminder):
        delete_reminder(chat_id, reminder["id"], only_active=False)  # сработавшую запись больше хранить незачем

    bot.answer_callback_query(call.id, "⏰ Отложено")
    edit_message(call.message,
                 f"⏰ <b>Отложено</b> — напомню {human_when(due)}\n\n"
                 f"📝 {escape(new['text'])}\n🔢 Номер: <b>{new['id']}</b>")


@bot.callback_query_handler(func=lambda call: call.data.startswith("done:"))
def cb_done(call):
    """Нажата кнопка «Готово» под сработавшим напоминанием."""
    chat_id = call.message.chat.id
    reminder = find_reminder(chat_id, int(call.data.split(":", 1)[1]))
    bot.answer_callback_query(call.id, "✅ Отлично!")
    if reminder is None:
        set_buttons(chat_id, call.message.message_id)
        return
    text = f"✅ <b>Выполнено</b>\n\n📝 <s>{escape(reminder['text'])}</s>"  # <s> — зачёркнутый текст
    if is_active(reminder) and reminder.get("repeat"):
        text += f"\n\n🔁 Следующее — {human_when(get_due(reminder))}"
    else:
        delete_reminder(chat_id, reminder["id"], only_active=False)
    edit_message(call.message, text)


# ===========================================================================
# 7. ПРОВЕРКА НАПОМИНАНИЙ ПО ВРЕМЕНИ (работает в отдельном потоке)
# ===========================================================================

def check_reminders():
    """Один проход: отправляет все напоминания, время которых уже наступило."""
    now = now_local()

    with data_lock:
        due_now = [r for r in data["reminders"] if is_active(r) and get_due(r) <= now]

    # Отправляем уже без замка: отправка по сети может занять пару секунд,
    # и не нужно всё это время мешать обработчикам команд.
    for r in due_now:
        due = get_due(r)
        text = f"⏰ <b>Напоминание!</b>\n\n📝 {escape(r['text'])}"
        if r.get("repeat"):
            text += f"\n🔁 {capitalize(REPEAT_LABELS[r['repeat']])}"
        # Если бот был выключен и напоминание опоздало, честно скажем об этом
        if now - due > timedelta(minutes=2):
            text += f"\n\n<i>⌛ Должно было прийти {due.strftime('%d.%m в %H:%M')}, но бот был выключен</i>"

        try:
            bot.send_message(r["chat_id"], text, reply_markup=fired_keyboard(r))
            log.info("Отправил напоминание №%s в чат %s", r["id"], r["chat_id"])
        except ApiTelegramException as error:
            # Ошибку прислал сам Telegram. 403 — пользователь заблокировал бота,
            # 400 — чат не найден. Повторять бесполезно, поэтому напоминание удалим.
            if error.error_code in (400, 403):
                log.warning("Не могу отправить напоминание №%s (%s), удаляю его", r["id"], error.description)
                with data_lock:
                    if r in data["reminders"]:
                        data["reminders"].remove(r)
                        save_data()
            else:
                log.warning("Telegram вернул ошибку для №%s, попробую через минуту: %s", r["id"], error)
            continue  # continue — переходим к следующему напоминанию
        except Exception as error:
            # Например, пропал интернет. Ничего не меняем — попробуем снова через минуту.
            log.warning("Не удалось отправить №%s, попробую через минуту: %s", r["id"], error)
            continue

        with data_lock:
            # Проверяем, что напоминание ещё в списке: его могли удалить, пока мы его отправляли
            if r in data["reminders"]:
                if r.get("repeat"):
                    # Повторяющееся переносим на следующий раз
                    r["due"] = next_repeat(due, r["repeat"], now).isoformat()
                else:
                    # Разовое помечаем сработавшим (а не удаляем) — чтобы работала кнопка «Отложить»
                    r["fired_at"] = now.isoformat(timespec="seconds")
                save_data()

    # Уборка: сработавшие напоминания старше KEEP_FIRED_DAYS дней больше не нужны
    with data_lock:
        border = now - timedelta(days=KEEP_FIRED_DAYS)
        old = [r for r in data["reminders"] if not is_active(r) and datetime.fromisoformat(r["fired_at"]) < border]
        if old:
            for r in old:
                data["reminders"].remove(r)
            save_data()


def reminder_loop():
    """Бесконечный цикл: проверяет напоминания в начале каждой минуты."""
    log.info("Проверка напоминаний запущена")
    while True:
        try:
            check_reminders()
        except Exception:
            # Любая неожиданная ошибка не должна «убить» поток — тогда напоминания
            # перестали бы приходить. Записываем ошибку в журнал и продолжаем.
            log.exception("Ошибка при проверке напоминаний")
        # Спим до начала следующей минуты. Если сейчас 18:29:42, поспим 18 секунд
        # и проверим ровно в 18:30:00 — напоминание придёт вовремя, а не «где-то в течение минуты».
        time.sleep(60 - datetime.now().second)


# ===========================================================================
# 8. ЗАПУСК
# ===========================================================================
#
# Блок `if __name__ == "__main__":` выполняется, только когда файл запускают
# напрямую (python bot.py), а не импортируют из другого файла.

def setup_profile():
    """Настраивает то, что видно в Telegram до начала общения: меню команд и описание бота."""
    bot.set_my_commands([
        types.BotCommand("start", "Главное меню"),
        types.BotCommand("new", "Новое напоминание по шагам"),
        types.BotCommand("list", "Мои напоминания"),
        types.BotCommand("remind", "Быстро: /remind через 2 часа текст"),
        types.BotCommand("delete", "Удалить: /delete номер"),
        types.BotCommand("cancel", "Отменить создание"),
        types.BotCommand("help", "Все возможности"),
    ])
    # Описание — текст на пустом экране чата, до нажатия «Старт»
    bot.set_my_description(
        "Напоминаю о важном в нужное время ⏰\n\n"
        "Просто напишите: «через 10 минут выключить плиту» или «завтра в 9 позвонить врачу».\n\n"
        "Умею повторять напоминания каждый день, по будням или раз в неделю и откладывать их одной кнопкой."
    )
    # Короткое описание — в профиле бота и в ссылке на него
    bot.set_my_short_description("Напоминалка: напишите «через 10 минут…» или «завтра в 9…» — напомню вовремя ⏰")


if __name__ == "__main__":
    # Проверяем токен: get_me() спрашивает у Telegram «кто я?».
    # Если токен неверный, Telegram ответит ошибкой 401.
    try:
        me = bot.get_me()
    except ApiTelegramException as error:
        if error.error_code == 401:
            sys.exit("Telegram не принял токен (ошибка 401). Проверьте BOT_TOKEN в файле .env.")
        raise

    try:
        setup_profile()
    except Exception as error:
        log.warning("Не удалось обновить меню и описание бота: %s", error)  # бот работает и без этого

    # Запускаем проверку напоминаний во втором потоке.
    # daemon=True — поток сам завершится, когда вы остановите бота (Ctrl+C).
    threading.Thread(target=reminder_loop, daemon=True).start()

    log.info("Бот @%s запущен. Активных напоминаний: %s. Остановить — Ctrl+C",
             me.username, sum(1 for r in data["reminders"] if is_active(r)))

    # infinity_polling — бесконечный опрос Telegram. Если пропадёт интернет,
    # telebot сам переподключится, бот не упадёт.
    try:
        bot.infinity_polling()
    except KeyboardInterrupt:
        pass
    log.info("Бот остановлен")
