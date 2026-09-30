"""
Разбор времени из обычного русского текста.

Главная функция — parse_when(). Она находит в сообщении фразу о времени,
вычисляет момент напоминания и возвращает оставшийся текст:

    «через 10 минут выключить плиту»  ->  через 10 минут, текст «Выключить плиту»
    «позвонить врачу завтра в 9»      ->  завтра в 09:00, текст «Позвонить врачу»
    «каждый день в 8:00 зарядка»      ->  ежедневно в 08:00, текст «Зарядка»

Как это устроено:
1. Фраза о времени состоит из «кирпичиков»: день («завтра», «в пятницу», «5 октября»),
   время («в 18:30», «в 7 вечера»), часть дня («вечером»), интервал («через 2 часа»),
   повтор («каждый день»). Для каждого кирпичика есть свой шаблон.
2. Шаблоны записаны регулярными выражениями — это мини-язык для поиска в тексте.
   Например, \\d{1,2}:\\d{2} значит «одна-две цифры, двоеточие, две цифры».
3. Сначала ищем кирпичики в начале сообщения, потом в конце. Что не подошло — текст напоминания.
4. Из найденных кирпичиков собираем конкретные дату и время.

Этот файл ничего не знает про Telegram — поэтому его легко проверять отдельно.
"""

import re
from datetime import date, datetime, timedelta

DEFAULT_HOUR = 9        # если день указан, а время нет («завтра к врачу») — напоминаем в 9:00
MAX_DAYS_AHEAD = 366    # дальше чем на год не планируем

# Часть дня -> час
PART_HOURS = {"утром": 9, "днем": 13, "вечером": 19, "ночью": 23}

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

# День недели -> номер (понедельник — 0, как в Python)
WEEKDAYS = {
    "понедельник": 0, "вторник": 1, "среду": 2, "четверг": 3,
    "пятницу": 4, "субботу": 5, "воскресенье": 6,
}

# Виды повторов и как их называть в сообщениях
REPEAT_LABELS = {"daily": "каждый день", "weekdays": "по будням", "weekly": "каждую неделю"}


class WhenError(Exception):
    """Время распознано, но использовать его нельзя (например, оно уже прошло).

    Текст ошибки написан для человека — бот показывает его как есть.
    """


class When:
    """Результат разбора: когда напомнить, что напомнить и нужно ли повторять."""

    def __init__(self, due, text, repeat=None):
        self.due = due          # момент напоминания (datetime)
        self.text = text        # текст напоминания без фразы о времени (может быть пустым)
        self.repeat = repeat    # None, "daily", "weekdays" или "weekly"

    def __repr__(self):
        return f"When(due={self.due:%Y-%m-%d %H:%M}, text={self.text!r}, repeat={self.repeat!r})"


# ===========================================================================
# ШАБЛОНЫ
# ===========================================================================
#
# Короткая шпаргалка по регулярным выражениям:
#   \d      — цифра              \s      — пробел
#   +       — один или больше    ?       — может быть, а может не быть
#   {1,2}   — от 1 до 2 раз      (a|b)   — a или b
#   (?:...) — просто группа      (?P<имя>...) — группа с именем, её потом можно достать
#   (?!...) — «дальше НЕ должно идти...»
#
# Перед поиском заменяем «ё» на «е», поэтому в шаблонах только «е».

I = re.IGNORECASE
END = r"(?![а-яa-z0-9])"  # конец слова: дальше не буква и не цифра

_NUM = r"\d+(?:[.,]\d+)?"  # число: 5, 1.5 или 1,5
_UNIT = (r"(?:минут[уыа]?|мин\.?|месяц(?:ев|а)?|мес\.?|м|час(?:ов|а)?|ч\.?|"
         r"дн(?:ей|я)|день|суток|сутки|дн\.?|недел[юиь]|нед\.?)")
_SEG = rf"(?:(?:{_NUM}|полтора|полторы|пару|пол)\s*)?(?:полчаса|{_UNIT}){END}"  # «2 часа», «полчаса», «час»
_SEG_RX = re.compile(rf"(?P<num>{_NUM}|полтора|полторы|пару|пол)?\s*(?P<unit>полчаса|{_UNIT}){END}", I)

REL_RX = re.compile(rf"через\s+(?P<body>{_SEG}(?:\s*(?:и\s+)?{_SEG})*)", I)   # «через 1 час 30 минут»
REL_NUM_RX = re.compile(rf"через\s+(?P<n>\d+){END}", I)                       # «через 45» — считаем минутами
REL_BARE_RX = re.compile(rf"(?P<body>{_SEG}(?:\s*(?:и\s+)?{_SEG})*)", I)      # «15 минут» без «через»

_WD = "понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье"
_MON = "|".join(MONTHS)

REPEAT_RX = re.compile(
    rf"(?:каждый\s+день|ежедневно|каждое\s+утро|каждый\s+вечер|по\s+будням|каждый\s+будний\s+день|"
    rf"каждую\s+неделю|еженедельно|кажд(?:ый|ую|ое)\s+(?:{_WD})){END}", I)

DAY_RX = re.compile(rf"(?P<w>послезавтра|завтра|сегодня){END}", I)
WEEKDAY_RX = re.compile(rf"во?\s+(?P<wd>{_WD}){END}", I)
DATE_MONTH_RX = re.compile(
    rf"(?:(?:на|к)\s+)?(?P<d>\d{{1,2}})(?:-?го)?\s+(?P<mon>{_MON})(?:\s+(?P<y>\d{{4}})(?:\s*(?:года|г\.?))?)?{END}", I)
DATE_NUM_RX = re.compile(r"(?:(?:на|к)\s+)?(?P<d>\d{1,2})\.(?P<mo>\d{1,2})(?:\.(?P<y>\d{4}|\d{2}))?(?!\d)(?![.:]\d)", I)

TIME_AMPM_RX = re.compile(
    rf"(?:(?:в|на|к)\s+)?(?P<h>\d{{1,2}})(?:[:.](?P<m>\d{{2}}))?\s*(?:час(?:ов|а)?\s+)?(?P<ampm>утра|дня|вечера|ночи){END}", I)
TIME_HM_RX = re.compile(r"(?:(?:в|на|к)\s+)?(?P<h>\d{1,2})[:.](?P<m>\d{2})(?!\d)(?![.:]\d)", I)
TIME_WORD_RX = re.compile(rf"(?:в\s+)?(?P<w>полдень|полночь){END}|в\s+(?P<one>час)(?:\s+(?P<ampm>дня|ночи))?{END}", I)
TIME_H_WORD_RX = re.compile(rf"(?:в|к)\s+(?P<h>\d{{1,2}})\s*(?:час(?:ов|а)?|ч){END}", I)
# «в 9» без минут. После числа не должно быть слов, с которыми это явно не время: «в 5 минут», «в 3 раза»
TIME_H_RX = re.compile(r"(?:в|к)\s+(?P<h>\d{1,2})(?!\d)(?![:.]\d)(?!\s*(?:мин|сек|дн|недел|раз|лет|год|мес|руб|шт|кг|км|%))", I)

PART_RX = re.compile(rf"(?P<p>утром|днем|вечером|ночью){END}", I)

# После даты вида «05.10» идёт что-то похожее на время? Тогда «05.10» — точно дата, а не 5 часов 10 минут
_AFTER_DATE_RX = re.compile(
    r"\s*,?\s*(?:(?:в|на|к)\s+)?(?:\d{1,2}(?:[:.]\d{2}|\s*(?:час|утра|дня|вечера|ночи))|утром|днем|вечером|ночью)", I)

_SKIP_RX = re.compile(r"[\s,]*")
_INTRO_RX = re.compile(r"\s*(?:напомни(?:те|ть)?(?:\s+мне)?|напоминание)(?![а-яa-z])\s*[,:—-]?\s*", I)

_SHORT_UNITS = {"м", "ч", "ч.", "мин", "мин.", "дн", "дн.", "нед", "нед.", "мес", "мес."}


# ===========================================================================
# РАЗБОР КИРПИЧИКОВ
# ===========================================================================

def _unit_minutes(unit):
    """Сколько минут в единице времени."""
    unit = unit.lower()
    if unit.startswith("мин") or unit == "м":
        return 1
    if unit.startswith("час") or unit.startswith("ч"):
        return 60
    if unit.startswith("недел") or unit.startswith("нед"):
        return 7 * 24 * 60
    if unit.startswith("мес"):
        return 30 * 24 * 60
    return 24 * 60  # день, дня, дней, сутки


def _interval_minutes(body):
    """«1 час 30 минут» -> 90. Если фраза бессмысленная — None."""
    total = 0.0
    for m in _SEG_RX.finditer(body):
        num, unit = m.group("num"), m.group("unit").lower()
        if unit == "полчаса":
            if num:
                return None
            total += 30
            continue
        if num is None:
            if unit in _SHORT_UNITS:   # «через ч» — нет, а вот «через час» — да
                return None
            value = 1.0
        else:
            num = num.lower()
            value = {"полтора": 1.5, "полторы": 1.5, "пару": 2.0, "пол": 0.5}.get(num)
            if value is None:
                value = float(num.replace(",", "."))
        total += value * _unit_minutes(unit)
    return total


def _hour_with_ampm(hour, ampm):
    """«7 вечера» -> 19, «12 ночи» -> 0, «2 дня» -> 14."""
    ampm = ampm.lower()
    if ampm == "утра":
        return 0 if hour == 12 else hour
    if ampm == "дня":
        return hour + 12 if 1 <= hour <= 6 else hour
    if ampm == "вечера":
        return hour + 12 if hour < 12 else hour
    # ночи
    if hour == 12:
        return 0
    return hour + 12 if hour in (10, 11) else hour


def _match_one(text, pos, slots, allow_bare):
    """Пробует распознать один кирпичик, начиная с позиции pos.

    Возвращает (название, значение, позиция_конца) или None.
    slots — уже найденные кирпичики: каждый вид может встретиться только один раз.
    """
    if "rel" in slots:
        return None  # «через 5 минут» самодостаточно, больше ничего не нужно

    if "repeat" not in slots:
        m = REPEAT_RX.match(text, pos)
        if m:
            phrase = " ".join(m.group(0).lower().split())
            if phrase in ("каждый день", "ежедневно"):
                value = ("daily", None, None)
            elif phrase == "каждое утро":
                value = ("daily", None, PART_HOURS["утром"])
            elif phrase == "каждый вечер":
                value = ("daily", None, PART_HOURS["вечером"])
            elif phrase in ("по будням", "каждый будний день"):
                value = ("weekdays", None, None)
            elif phrase in ("каждую неделю", "еженедельно"):
                value = ("weekly", None, None)
            else:
                value = ("weekly", WEEKDAYS[phrase.split()[-1]], None)
            return "repeat", value, m.end()

    if not slots:
        m = REL_RX.match(text, pos)
        if m:
            minutes = _interval_minutes(m.group("body"))
            if minutes is not None:
                return "rel", minutes, m.end()
        m = REL_NUM_RX.match(text, pos)
        if m:
            return "rel", float(m.group("n")), m.end()
        # Интервал без «через» проверяем раньше времени суток: «3 дня» в ответ на
        # «Когда напомнить?» — это «через 3 дня», а не «в 3 часа дня»
        if allow_bare:
            m = REL_BARE_RX.match(text, pos)
            if m:
                minutes = _interval_minutes(m.group("body"))
                if minutes is not None:
                    return "rel", minutes, m.end()

    if "day" not in slots:
        m = DATE_MONTH_RX.match(text, pos)
        if m:
            year = int(m.group("y")) if m.group("y") else None
            return "day", ("date", int(m.group("d")), MONTHS[m.group("mon").lower()], year), m.end()

        m = DATE_NUM_RX.match(text, pos)
        if m:
            d, mo = int(m.group("d")), int(m.group("mo"))
            year = int(m.group("y")) if m.group("y") else None
            if year is not None and year < 100:
                year += 2000
            # «05.10» — это 5 октября или 5 часов 10 минут? Считаем датой, если так однозначнее:
            looks_like_date = (
                year is not None                        # «05.10.2026»
                or "time" in slots                      # время уже названо раньше
                or _AFTER_DATE_RX.match(text, m.end())  # время названо сразу после: «05.10 18:30»
                or d > 23 or mo > 59                    # как время не годится: «25.12»
            )
            if looks_like_date:
                return "day", ("date", d, mo, year), m.end()

        m = DAY_RX.match(text, pos)
        if m:
            offset = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[m.group("w").lower()]
            return "day", ("offset", offset), m.end()

        m = WEEKDAY_RX.match(text, pos)
        if m:
            return "day", ("weekday", WEEKDAYS[m.group("wd").lower()]), m.end()

    if "time" not in slots:
        # значение времени: (час, минута, указано ли «утра/вечера», «слабое» ли совпадение)
        m = TIME_AMPM_RX.match(text, pos)
        if m:
            hour = _hour_with_ampm(int(m.group("h")), m.group("ampm"))
            minute = int(m.group("m") or 0)
            if hour <= 23 and minute <= 59:
                return "time", (hour, minute, True, False), m.end()

        m = TIME_HM_RX.match(text, pos)
        if m and int(m.group("h")) <= 23 and int(m.group("m")) <= 59:
            return "time", (int(m.group("h")), int(m.group("m")), False, False), m.end()

        m = TIME_WORD_RX.match(text, pos)
        if m:
            if m.group("w"):
                hour = 12 if m.group("w").lower() == "полдень" else 0
            else:  # «в час», «в час дня», «в час ночи»
                hour = 1 if (m.group("ampm") or "").lower() == "ночи" else 13
            return "time", (hour, 0, True, False), m.end()

        m = TIME_H_WORD_RX.match(text, pos)
        if m and int(m.group("h")) <= 23:
            return "time", (int(m.group("h")), 0, False, False), m.end()

        m = TIME_H_RX.match(text, pos)
        if m and int(m.group("h")) <= 23:
            # «в 9» — слабое совпадение: это может быть и не время («в 5 подъезде собрание»)
            return "time", (int(m.group("h")), 0, False, True), m.end()

    if "part" not in slots:
        m = PART_RX.match(text, pos)
        if m:
            return "part", PART_HOURS[m.group("p").lower()], m.end()

    return None


def _consume(text, pos, allow_bare, slots=None):
    """Собирает подряд идущие кирпичики, начиная с pos. Возвращает (кирпичики, позиция_конца)."""
    slots = dict(slots or {})
    while True:
        found = _match_one(text, _SKIP_RX.match(text, pos).end(), slots, allow_bare)
        if found is None:
            break
        name, value, pos = found
        slots[name] = value
    return slots, pos


# ===========================================================================
# ИЗ КИРПИЧИКОВ — В КОНКРЕТНУЮ ДАТУ
# ===========================================================================

def in_minutes(now, minutes):
    """Момент «через N минут», округлённый вверх до целой минуты (бот проверяет время раз в минуту)."""
    exact = now + timedelta(minutes=minutes)
    due = exact.replace(second=0, microsecond=0)
    if due < exact:
        due += timedelta(minutes=1)
    return due


def _check_far(due, now):
    if due - now > timedelta(days=MAX_DAYS_AHEAD):
        raise WhenError("Слишком далеко: могу напомнить не позже чем через год.")


def _resolve(slots, now):
    """Превращает кирпичики в (момент, повтор). None — если времени недостаточно (например, только «сегодня»)."""
    if "rel" in slots:
        if slots["rel"] < 1:
            raise WhenError("Слишком маленький интервал: минимум 1 минута.")
        due = in_minutes(now, slots["rel"])
        _check_far(due, now)
        return due, None

    repeat = slots.get("repeat")
    day = slots.get("day")
    part = slots.get("part")

    hm = None  # (час, минута)
    if "time" in slots:
        hour, minute, has_ampm, _weak = slots["time"]
        # «вечером в 7» — это 19:00, а не 7 утра
        if not has_ampm and part is not None and part >= 13 and 1 <= hour <= 11:
            hour += 12
        hm = (hour, minute)
    elif part is not None:
        hm = (part, 0)
    elif repeat and repeat[2] is not None:
        hm = (repeat[2], 0)  # «каждое утро» — час уже задан

    def at(day_):
        return datetime(day_.year, day_.month, day_.day, hm[0], hm[1], tzinfo=now.tzinfo)

    def make_date(year, month, day_):
        try:
            return date(year, month, day_)
        except ValueError:
            raise WhenError("Такой даты не существует. Проверьте число и месяц.") from None

    if repeat:
        mode, weekday, _hour = repeat
        if day and day[0] == "weekday":
            weekday = day[1]           # «каждую неделю в пятницу»
        if hm is None:
            hm = (DEFAULT_HOUR, 0)
        current = now.date()
        if day and day[0] == "offset":
            current += timedelta(days=day[1])
        elif day and day[0] == "date":
            current = make_date(day[3] or now.year, day[2], day[1])
        for _ in range(400):  # ищем ближайший подходящий день
            ok_weekday = weekday is None or current.weekday() == weekday
            ok_workday = mode != "weekdays" or current.weekday() < 5
            if at(current) > now and ok_weekday and ok_workday:
                return at(current), mode
            current += timedelta(days=1)
        return None

    if day is None:
        if hm is None:
            return None
        due = at(now.date())
        if due <= now:                 # «в 18:30», а сейчас уже 19:00 — значит, завтра
            due = at(now.date() + timedelta(days=1))
        return due, None

    if day[0] == "offset":
        if hm is None:
            if day[1] == 0:
                return None            # просто «сегодня» — непонятно, во сколько
            hm = (DEFAULT_HOUR, 0)
        due = at(now.date() + timedelta(days=day[1]))
        if due <= now:
            raise WhenError("Это время сегодня уже прошло. Укажите время позже или другой день.")

    elif day[0] == "weekday":
        if hm is None:
            hm = (DEFAULT_HOUR, 0)
        current = now.date()
        while current.weekday() != day[1] or at(current) <= now:
            current += timedelta(days=1)
        due = at(current)

    else:  # конкретная дата
        _kind, day_number, month, year = day
        if hm is None:
            hm = (DEFAULT_HOUR, 0)
        due = at(make_date(year or now.year, month, day_number))
        if due <= now:
            if year:
                raise WhenError("Эта дата уже прошла.")
            due = at(make_date(now.year + 1, month, day_number))  # «1 сентября» в октябре — это следующий год

    _check_far(due, now)
    return due, None


def _clean_text(text):
    """Приводит в порядок текст напоминания: убирает «напомни мне», лишние знаки, делает первую букву заглавной."""
    text = text.strip()
    intro = _INTRO_RX.match(text)
    if intro:
        text = text[intro.end():]
    text = text.strip(" \t\n,.;:—–-")
    text = re.sub(r"^(?:что|чтобы)\s+", "", text, flags=I)
    text = text.strip()
    return text[:1].upper() + text[1:]


def _word_starts(text, start):
    """Позиции начала каждого слова, начиная со start."""
    return [i for i in range(start, len(text)) if not text[i].isspace() and (i == 0 or text[i - 1].isspace())]


# ===========================================================================
# ГЛАВНАЯ ФУНКЦИЯ
# ===========================================================================

def parse_when(message, now, allow_bare=False):
    """Ищет в сообщении время напоминания.

    message    — текст от пользователя
    now        — текущий момент (datetime с часовым поясом)
    allow_bare — разрешить интервал без слова «через» («15 минут»). Включаем, когда бот
                 прямо спросил «Когда напомнить?» — там это однозначно время.

    Возвращает When(due, text, repeat) или None, если времени в сообщении нет.
    Бросает WhenError, если время названо, но не годится (уже прошло, слишком далеко, нет такой даты).
    """
    original = message.strip()
    text = original.replace("ё", "е").replace("Ё", "Е")  # длина строки не меняется — позиции совпадают с original

    intro = _INTRO_RX.match(text)
    start = intro.end() if intro else 0

    # 1) Фраза о времени в начале: «завтра в 9 к врачу»
    slots, end = _consume(text, start, allow_bare)
    rest_from, rest_to = end, len(text)
    from_start = bool(slots)

    # 2) И/или в конце: «к врачу завтра в 9», «завтра позвонить в 18:00»
    for pos in _word_starts(text, end):
        merged, tail_end = _consume(text, pos, allow_bare, slots)
        if len(merged) > len(slots) and not text[tail_end:].strip(" \t\n.,!?;"):
            slots, rest_to = merged, pos
            if not from_start:
                rest_from = 0
            break

    if not slots:
        return None

    rest = _clean_text(original[rest_from:rest_to])

    # Одинокое «в 9» в начале фразы слишком ненадёжно («в 5 подъезде собрание») — не считаем временем
    if from_start and rest and set(slots) == {"time"} and slots["time"][3]:
        return None

    resolved = _resolve(slots, now)
    if resolved is None:
        return None
    due, repeat = resolved
    return When(due, rest, repeat)


def next_repeat(due, repeat, now):
    """Следующий момент повторяющегося напоминания — строго после now."""
    step = timedelta(days=7 if repeat == "weekly" else 1)
    nxt = due + step
    while nxt <= now or (repeat == "weekdays" and nxt.weekday() >= 5):
        nxt += step
    return nxt
