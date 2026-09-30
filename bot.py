"""
Бот-напоминалка для Telegram.

Как он работает в двух словах:
1. Бот всё время спрашивает у серверов Telegram: «Есть новые сообщения?»
   Это называется polling (опрос). Библиотека telebot делает это за нас.
2. Когда приходит команда (/remind, /list, ...) или нажатие кнопки, telebot
   вызывает функцию-обработчик, которую мы привязали к этому событию.
3. Напоминания хранятся в файле reminders.json, поэтому они не пропадают,
   если бота выключить и включить снова.
4. Параллельно работает отдельный поток (thread) — как второй работник,
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


# ===========================================================================
# 1. НАСТРОЙКИ
# ===========================================================================

# Папка, в которой лежит этот файл. Все остальные файлы (.env, reminders.json)
# ищем рядом с ним — тогда бот работает, даже если запустить его из другой папки.
BASE_DIR = Path(__file__).resolve().parent

# load_dotenv() читает файл .env и делает его строки доступными через os.getenv().
# Зачем так сложно, почему не написать токен прямо в коде? Токен — это пароль
# от бота. Если код попадёт на GitHub вместе с токеном, любой сможет управлять
# ботом. Файл .env мы в GitHub не загружаем (он указан в .gitignore).
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/Moscow").strip()
DATA_FILE = BASE_DIR / "reminders.json"
MAX_TEXT_LENGTH = 1000  # ограничим длину текста напоминания, чтобы сообщение точно влезло в Telegram
LIST_LIMIT = 20         # сколько напоминаний максимум показывать в /list (у сообщения есть предел длины)

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
#       "due": "2026-09-29T18:30:00+03:00"   <- когда: дата, время и часовой пояс
#     }
#   ]
# }
#
# Почему номера не 1, 2, 3 по порядку в списке, а «сквозные»?
# Представьте: в /list было «1. Позвонить маме, 2. Купить хлеб». Пока вы печатаете
# /delete 2, первое напоминание срабатывает и исчезает — и «Купить хлеб» становится
# номером 1. Команда удалила бы не то. Сквозной номер закреплён за напоминанием
# навсегда, поэтому такой путаницы не бывает.

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


def create_reminder(chat_id, text, due):
    """Добавляет напоминание в список, сохраняет файл и возвращает созданное напоминание."""
    with data_lock:
        reminder = {
            "id": data["next_id"],
            "chat_id": chat_id,
            "text": text,
            "due": due.isoformat(),  # isoformat() превращает дату в строку для JSON
        }
        data["reminders"].append(reminder)
        data["next_id"] += 1
        save_data()
    log.info("Новое напоминание №%s для чата %s на %s", reminder["id"], chat_id, reminder["due"])
    return reminder


def delete_reminder(chat_id, number):
    """Удаляет напоминание с таким номером из этого чата. Возвращает удалённое или None."""
    found = None
    with data_lock:
        for r in data["reminders"]:
            # Проверяем и номер, и чат: так никто не удалит чужое напоминание, подобрав номер
            if r["id"] == number and r["chat_id"] == chat_id:
                found = r
                break
        if found:
            data["reminders"].remove(found)
            save_data()
    return found


# Загружаем напоминания один раз при запуске бота
data = load_data()


# ===========================================================================
# 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ВРЕМЕНИ
# ===========================================================================

def parse_time(text):
    """Превращает строку '18:30' в пару чисел (18, 30). Если время неправильное — возвращает None."""
    text = text.strip().replace(".", ":")  # разрешим писать и 18.30
    try:
        # strptime разбирает строку по шаблону: %H — часы (0–23), %M — минуты (0–59).
        # Если написать 25:00 или 18:75, будет ошибка ValueError.
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError:
        return None
    return parsed.hour, parsed.minute


def next_occurrence(hour, minute):
    """Ближайший момент с указанным временем: сегодня, а если уже прошло — завтра."""
    now = datetime.now(TZ)
    due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if due <= now:
        due += timedelta(days=1)
    return due


def after_minutes(minutes):
    """Момент «через N минут», округлённый вверх до целой минуты (проверка идёт раз в минуту)."""
    exact = datetime.now(TZ) + timedelta(minutes=minutes)
    due = exact.replace(second=0, microsecond=0)
    if due < exact:
        due += timedelta(minutes=1)
    return due


def get_due(reminder):
    """Достаёт из напоминания время срабатывания (в файле оно хранится строкой)."""
    return datetime.fromisoformat(reminder["due"]).astimezone(TZ)


def human_when(due):
    """Красиво пишет время: «сегодня в 18:30», «завтра в 09:00» или «05.10 в 12:00»."""
    today = datetime.now(TZ).date()
    time_str = due.strftime("%H:%M")
    if due.date() == today:
        return f"сегодня в {time_str}"
    if due.date() == today + timedelta(days=1):
        return f"завтра в {time_str}"
    return f"{due.strftime('%d.%m')} в {time_str}"


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

# Кнопки быстрого выбора времени: (надпись на кнопке, данные для callback)
QUICK_TIMES = [
    ("⏱ +10 мин", "+10"),
    ("⏱ +30 мин", "+30"),
    ("⏱ +1 час", "+60"),
    ("⏱ +3 часа", "+180"),
    ("🌅 Завтра в 9:00", "tomorrow"),
]


def main_keyboard():
    """Главное меню — reply-клавиатура внизу экрана."""
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,   # подогнать высоту кнопок под содержимое, иначе они огромные
        is_persistent=True,     # не прятать клавиатуру после нажатия
        input_field_placeholder="Выберите действие ниже 👇",  # серая подсказка в поле ввода
    )
    keyboard.row(BTN_NEW)             # первый ряд — одна широкая кнопка
    keyboard.row(BTN_LIST, BTN_HELP)  # второй ряд — две кнопки
    return keyboard


def cancel_keyboard():
    """Одна inline-кнопка «Отмена» под сообщением."""
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(types.InlineKeyboardButton("✖️ Отмена", callback_data="cancel"))
    return keyboard


def time_keyboard():
    """Inline-кнопки быстрого выбора времени."""
    buttons = [types.InlineKeyboardButton(label, callback_data=f"time:{code}") for label, code in QUICK_TIMES]
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(*buttons[:3])  # звёздочка «раскладывает» список на отдельные аргументы
    keyboard.row(*buttons[3:])
    keyboard.row(types.InlineKeyboardButton("✖️ Отмена", callback_data="cancel"))
    return keyboard


def due_from_quick(code):
    """Превращает данные кнопки ("+10", "tomorrow") в конкретный момент времени."""
    if code == "tomorrow":
        tomorrow = datetime.now(TZ) + timedelta(days=1)
        return tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)
    return after_minutes(int(code.lstrip("+")))


def text_error(text):
    """Проверяет текст напоминания. Возвращает описание ошибки или None, если всё хорошо."""
    if not text:
        return "Текст напоминания пустой."
    if len(text) > MAX_TEXT_LENGTH:
        return f"Слишком длинный текст: больше {MAX_TEXT_LENGTH} символов. Сократите, пожалуйста."
    return None


def created_text(reminder):
    """Сообщение «напоминание создано»."""
    return (
        f"✅ <b>Готово!</b> Напомню {human_when(get_due(reminder))}\n\n"
        f"📝 {escape(reminder['text'])}\n"
        f"🔢 Номер: <b>{reminder['id']}</b>"
    )


def render_list(chat_id):
    """Готовит текст списка напоминаний и inline-кнопки удаления. Возвращает (текст, клавиатура)."""
    with data_lock:
        # Берём только напоминания из этого чата: чужие показывать нельзя
        mine = [r for r in data["reminders"] if r["chat_id"] == chat_id]

    if not mine:
        return ("📭 <b>Напоминаний нет</b>\n\n"
                f"Нажмите <b>{BTN_NEW}</b> внизу, чтобы создать первое."), None

    mine.sort(key=get_due)  # сортируем по времени: ближайшие — сверху
    shown = mine[:LIST_LIMIT]

    lines = [f"📋 <b>Ваши напоминания</b> · {len(mine)}\n"]
    buttons = []
    for r in shown:
        due = get_due(r)
        text = r["text"] if len(r["text"]) <= 100 else r["text"][:100] + "…"  # длинное обрежем
        lines.append(f"🕐 <b>{capitalize(human_when(due))}</b> · №{r['id']}\n📝 {escape(text)}\n")
        buttons.append(types.InlineKeyboardButton(f"🗑 №{r['id']} · {due:%H:%M}", callback_data=f"del:{r['id']}"))

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


def remove_buttons(chat_id, message_id):
    """Убирает inline-кнопки у старого сообщения, чтобы их не нажимали повторно."""
    if not message_id:
        return
    try:
        bot.edit_message_reply_markup(chat_id, message_id, reply_markup=None)
    except ApiTelegramException:
        pass  # сообщение могло быть удалено или кнопок уже нет — ничего страшного


START_TEXT = (
    "👋 Привет, <b>{name}</b>!\n\n"
    "Я бот-напоминалка ⏰ Помогу не забыть важное.\n\n"
    f"Нажмите <b>{BTN_NEW}</b> внизу экрана — я спрошу, что и когда напомнить.\n"
    "Описание всех возможностей — в /help"
)

HELP_TEXT = (
    "❓ <b>Как пользоваться ботом</b>\n\n"
    "<b>Кнопки внизу экрана</b>\n"
    f"{BTN_NEW} — спрошу, что напомнить, а потом когда: можно выбрать кнопкой "
    "(+10 мин, +1 час, завтра утром…) или написать время\n"
    f"{BTN_LIST} — список с кнопками удаления 🗑\n"
    f"{BTN_HELP} — эта справка\n\n"
    "<b>Команды</b>\n"
    "/new — новое напоминание по шагам (как кнопка)\n"
    "/remind — создать одной строкой:\n"
    "<code>/remind 18:30 Позвонить маме</code>\n"
    "/list — все активные напоминания\n"
    "/delete — удалить по номеру:\n"
    "<code>/delete 3</code>\n"
    "/cancel — отменить создание напоминания\n"
    "/help — эта справка\n\n"
    "<b>Полезно знать</b>\n"
    f"🌍 Время считаю по поясу <b>{TIMEZONE_NAME}</b>\n"
    "📅 Если указанное время сегодня уже прошло, напомню завтра\n"
    "🔢 Номера напоминаний не меняются, когда удаляете другие\n"
    "💾 Напоминания сохраняются, даже если бота перезапустить\n\n"
    "<i>Нажмите на пример в сером прямоугольнике — он скопируется.</i>"
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

# Состояние диалога. Создание напоминания кнопками идёт в несколько шагов:
# сначала бот спрашивает текст, потом время. Между сообщениями нужно помнить,
# на каком шаге каждый чат. Для этого словарь:
#   states[chat_id] = {"step": "text", "msg_id": 55}                   — ждём текст
#   states[chat_id] = {"step": "time", "text": "...", "msg_id": 56}    — ждём время
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
        remove_buttons(chat_id, st.get("msg_id"))


@bot.message_handler(commands=["start"])
def cmd_start(message):
    """/start — приветствие и главное меню с кнопками."""
    reset_state(message.chat.id)
    name = escape(message.from_user.first_name or "друг")
    bot.send_message(message.chat.id, START_TEXT.format(name=name), reply_markup=main_keyboard())


@bot.message_handler(commands=["help"])
@bot.message_handler(func=lambda m: m.text == BTN_HELP)
def cmd_help(message):
    """/help и кнопка «Помощь» — подробное описание всех команд."""
    reset_state(message.chat.id)
    bot.send_message(message.chat.id, HELP_TEXT, reply_markup=main_keyboard())


@bot.message_handler(commands=["new"])
@bot.message_handler(func=lambda m: m.text == BTN_NEW)
def cmd_new(message):
    """/new и кнопка «Новое напоминание» — шаг 1: спрашиваем текст."""
    reset_state(message.chat.id)
    sent = bot.send_message(
        message.chat.id,
        "📝 <b>Что напомнить?</b>\n\n"
        "Напишите текст одним сообщением, например:\n<i>Позвонить маме</i>",
        reply_markup=cancel_keyboard(),
    )
    # Запоминаем: этот чат теперь на шаге «ждём текст»
    states[message.chat.id] = {"step": "text", "msg_id": sent.message_id}


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
    """/remind 18:30 Позвонить маме — создаёт напоминание одной строкой."""
    reset_state(message.chat.id)
    # split(maxsplit=2) режет текст по пробелам не больше двух раз:
    # "/remind 18:30 Позвонить маме" -> ["/remind", "18:30", "Позвонить маме"]
    # Благодаря maxsplit текст напоминания остаётся целым, со всеми пробелами.
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        bot.reply_to(message, "⚠️ Напишите время и текст, например:\n<code>/remind 18:30 Позвонить маме</code>\n\n"
                              f"Или нажмите <b>{BTN_NEW}</b> — я спрошу всё по шагам.")
        return  # return — выходим из функции, дальше не идём

    time_part, text = parts[1], parts[2].strip()

    parsed = parse_time(time_part)
    if parsed is None:
        bot.reply_to(message, f"⚠️ Не понял время «{escape(time_part)}».\n"
                              "Напишите часы и минуты через двоеточие: <code>18:30</code> или <code>9:05</code>")
        return

    error = text_error(text)
    if error:
        bot.reply_to(message, f"⚠️ {error}")
        return

    reminder = create_reminder(message.chat.id, text, next_occurrence(*parsed))  # * раскладывает (18, 30) на два аргумента
    bot.reply_to(message, created_text(reminder))


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
# 6. ДИАЛОГ СОЗДАНИЯ И INLINE-КНОПКИ
# ===========================================================================

# content_types=["text"] без других условий — ловит любой текст. Этот обработчик
# стоит последним среди message_handler, поэтому сюда попадает только то,
# что не подошло командам и кнопкам меню выше.
@bot.message_handler(content_types=["text"])
def on_text(message):
    """Ответы в диалоге создания (текст, потом время) и всё непонятное."""
    chat_id = message.chat.id
    text = message.text.strip()
    st = states.get(chat_id)

    if st is None:
        # Диалога нет — значит, бот не ждал этого сообщения
        bot.send_message(chat_id, "🤔 Не понял. Нажмите кнопку внизу экрана или отправьте /help",
                         reply_markup=main_keyboard())
        return

    if text.startswith("/"):
        bot.reply_to(message, "⚠️ Такой команды нет. Ответьте на вопрос выше или нажмите /cancel")
        return

    if st["step"] == "text":
        # Шаг 1 -> 2: получили текст, теперь спрашиваем время
        error = text_error(text)
        if error:
            bot.reply_to(message, f"⚠️ {error}")
            return
        remove_buttons(chat_id, st.get("msg_id"))  # у вопроса «Что напомнить?» кнопка «Отмена» больше не нужна
        sent = bot.send_message(
            chat_id,
            f"🕐 <b>Когда напомнить?</b>\n\n📝 {escape(text)}\n\n"
            "Выберите кнопку или напишите время, например <code>18:30</code>",
            reply_markup=time_keyboard(),
        )
        states[chat_id] = {"step": "time", "text": text, "msg_id": sent.message_id}

    elif st["step"] == "time":
        # Шаг 2: время написали текстом
        parsed = parse_time(text)
        if parsed is None:
            bot.reply_to(message, f"⚠️ Не понял время «{escape(text)}».\n"
                                  "Напишите, например, <code>18:30</code> или выберите кнопку выше.")
            return
        st = take_state(chat_id, "time")
        if st is None:
            return  # напоминание уже успели создать кнопкой
        remove_buttons(chat_id, st["msg_id"])
        reminder = create_reminder(chat_id, st["text"], next_occurrence(*parsed))
        bot.send_message(chat_id, created_text(reminder))


# Обработчики нажатий inline-кнопок. В них приходит call:
#   call.data    — строка, которую мы записали в callback_data кнопки ("del:3", "time:+10", "cancel")
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
        remove_buttons(chat_id, call.message.message_id)
        return

    due = due_from_quick(call.data.split(":", 1)[1])  # "time:+10" -> "+10"
    reminder = create_reminder(chat_id, st["text"], due)
    bot.answer_callback_query(call.id, "✅ Напоминание создано")
    edit_message(call.message, created_text(reminder))  # превращаем вопрос в подтверждение


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


# ===========================================================================
# 7. ПРОВЕРКА НАПОМИНАНИЙ ПО ВРЕМЕНИ (работает в отдельном потоке)
# ===========================================================================

def check_reminders():
    """Один проход: отправляет все напоминания, время которых уже наступило."""
    now = datetime.now(TZ)

    with data_lock:
        due_now = [r for r in data["reminders"] if get_due(r) <= now]

    # Отправляем уже без замка: отправка по сети может занять пару секунд,
    # и не нужно всё это время мешать обработчикам команд.
    for r in due_now:
        due = get_due(r)
        text = f"⏰ <b>Напоминание!</b>\n\n📝 {escape(r['text'])}"
        # Если бот был выключен и напоминание опоздало, честно скажем об этом
        if now - due > timedelta(minutes=2):
            text += f"\n\n<i>⌛ Должно было прийти {due.strftime('%d.%m в %H:%M')}, но бот был выключен</i>"

        try:
            bot.send_message(r["chat_id"], text)
            log.info("Отправил напоминание №%s в чат %s", r["id"], r["chat_id"])
        except ApiTelegramException as error:
            # Ошибку прислал сам Telegram. 403 — пользователь заблокировал бота,
            # 400 — чат не найден. Повторять бесполезно, поэтому напоминание удалим.
            if error.error_code in (400, 403):
                log.warning("Не могу отправить напоминание №%s (%s), удаляю его", r["id"], error.description)
            else:
                log.warning("Telegram вернул ошибку для №%s, попробую через минуту: %s", r["id"], error)
                continue  # continue — пропускаем удаление, напоминание останется до следующей проверки
        except Exception as error:
            # Например, пропал интернет. Не удаляем — попробуем снова через минуту.
            log.warning("Не удалось отправить №%s, попробую через минуту: %s", r["id"], error)
            continue

        with data_lock:
            # Проверяем, что напоминание ещё в списке: его могли удалить командой /delete,
            # пока мы его отправляли
            if r in data["reminders"]:
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

if __name__ == "__main__":
    # Проверяем токен: get_me() спрашивает у Telegram «кто я?».
    # Если токен неверный, Telegram ответит ошибкой 401.
    try:
        me = bot.get_me()
    except ApiTelegramException as error:
        if error.error_code == 401:
            sys.exit("Telegram не принял токен (ошибка 401). Проверьте BOT_TOKEN в файле .env.")
        raise

    # Список команд для синей кнопки «Меню» слева от поля ввода в Telegram.
    # Заменяет то, что было настроено через @BotFather /setcommands.
    try:
        bot.set_my_commands([
            types.BotCommand("start", "Главное меню"),
            types.BotCommand("new", "Новое напоминание по шагам"),
            types.BotCommand("list", "Мои напоминания"),
            types.BotCommand("remind", "Быстро: /remind 18:30 текст"),
            types.BotCommand("delete", "Удалить: /delete номер"),
            types.BotCommand("cancel", "Отменить создание"),
            types.BotCommand("help", "Описание всех команд"),
        ])
    except ApiTelegramException as error:
        log.warning("Не удалось обновить меню команд: %s", error)  # бот работает и без него

    # Запускаем проверку напоминаний во втором потоке.
    # daemon=True — поток сам завершится, когда вы остановите бота (Ctrl+C).
    threading.Thread(target=reminder_loop, daemon=True).start()

    log.info("Бот @%s запущен. Напоминаний в файле: %s. Остановить — Ctrl+C",
             me.username, len(data["reminders"]))

    # infinity_polling — бесконечный опрос Telegram. Если пропадёт интернет,
    # telebot сам переподключится, бот не упадёт.
    try:
        bot.infinity_polling()
    except KeyboardInterrupt:
        pass
    log.info("Бот остановлен")
