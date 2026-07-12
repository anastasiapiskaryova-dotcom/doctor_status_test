"""
Telegram-бот "Статус врача" для координатора онбординга.

Что делает:
  Пользователь отправляет боту фамилию (или часть фамилии/имени) врача из
  ежедневной сводки — бот отвечает сводным статусом: логин, прогресс по
  обоим курсам, дни без активности и то, что известно из доп. контекста
  (договор ReStaff, самозанятость, причины паузы и т.д.).

База данных: события из сводки за 09.07 + таблица "Дополнительный контекст
по врачам" — обе зашиты ниже в виде словаря DOCTORS. Если нужно обновлять
сводку каждый день, эту часть проще всего вынести в Google Sheet и подтягивать
через gspread — см. комментарий в конце файла.

Запуск:
    pip install -r requirements.txt
    export BOT_TOKEN="ваш_токен_от_BotFather"
    python main.py
"""

import asyncio
import logging
import os
import re

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message

logging.basicConfig(level=logging.INFO)

REPORT_DATE = "09.07"
TOTAL_DOCTORS = 66
COURSE_A = "Требования к описаниям КТ ОГК и ОБП"
COURSE_B = "Знакомство с платформой RadReport"

# ──────────────────────────────────────────────────────────────────────────
# БАЗА ДАННЫХ: события из сводки за 9 июля + доп. контекст по врачам.
# Ключ словаря — фамилия в нижнем регистре, по ней идёт поиск (плюс полное
# ФИО хранится отдельно для красивого вывода).
# ──────────────────────────────────────────────────────────────────────────
DOCTORS = {
    "иванова": {
        "full_name": "Иванова Анна",
        "logged_in": False,
        "courses": {
            COURSE_A: {"status": "не начат", "idle": "51д 18ч"},
            COURSE_B: {"status": "не начат", "idle": "51д 16ч"},
        },
        "context": "Попросила паузу до начала июля — ложилась на операцию.",
    },
    "петров": {
        "full_name": "Петров Пётр Сергеевич",
        "logged_in": False,
        "courses": {
            COURSE_A: {"status": "не начат", "idle": "11д 2ч"},
            COURSE_B: {"status": "не начат", "idle": "11д 2ч"},
        },
        "context": "Испытывает трудности с регистрацией на платформе ReStaff.",
    },
    "сидорова": {
        "full_name": "Сидорова Мария Александровна",
        "logged_in": False,
        "courses": {
            COURSE_A: {"status": "не начат", "idle": "12д 21ч"},
            COURSE_B: {"status": "не начат", "idle": "12д 21ч"},
        },
        "context": (
            "Не прислала данные для оформления договора с ReStaff, нет "
            "понимания, есть ли самозанятость. Доступы к онбординг-платформе "
            "выданы."
        ),
    },
    "смирнов": {
        "full_name": "Смирнов Николай",
        "logged_in": True,  # в списке "не залогинились" отсутствует
        "courses": {
            COURSE_A: {"status": "не начат", "idle": "12д 16ч"},
            COURSE_B: {"status": "не начат", "idle": "12д 16ч"},
        },
        "context": "Подписал договор на ReStaff.",
    },
    "козлов": {
        "full_name": "Козлов Дмитрий Игоревич",
        "logged_in": True,  # не значится в "не залогинились ни разу"
        "courses": {
            # Козлов не упомянут в блоках по курсу А — считаем, что курс А
            # ему не назначен, назначен только курс B (RadReport).
            COURSE_B: {"status": "не начат", "idle": "32д 21ч"},
        },
        "context": (
            "До сих пор не зарегистрировался по приглашению на ReStaff, не "
            "выходил на связь с 20 июня."
        ),
    },
    "федорова": {  # ё/е нормализуется ниже
        "full_name": "Фёдорова Елена",
        "logged_in": True,
        "courses": {
            COURSE_A: {"status": "завершён", "idle": "—"},
            COURSE_B: {"status": "завершён", "idle": "—"},
        },
        "context": (
            "Завершила оба курса за последние 24 часа (по сводке за "
            f"{REPORT_DATE}). Отдельного контекста по договору в таблице нет."
        ),
    },
    "морозова": {
        "full_name": "Морозова Ольга",
        "logged_in": True,
        "courses": {
            COURSE_B: {"status": "завершён", "idle": "—"},
        },
        "context": (
            "Завершила курс «Знакомство с платформой RadReport» за последние "
            "24 часа. Отдельного контекста по договору в таблице нет."
        ),
    },
    "волкова": {
        "full_name": "Волкова Светлана Викторовна",
        "logged_in": True,
        "courses": {
            COURSE_B: {"status": "завершён", "idle": "—"},
        },
        "context": (
            "Завершила курс «Знакомство с платформой RadReport» за последние "
            "24 часа. Отдельного контекста по договору в таблице нет."
        ),
    },
}


def normalize(text: str) -> str:
    """Приводит запрос к виду для поиска: нижний регистр, ё→е, без лишних пробелов."""
    text = text.strip().lower().replace("ё", "е")
    text = re.sub(r"\s+", " ", text)
    return text


def find_doctor(query: str):
    """Ищет врача по фамилии (или части ФИО). Возвращает (key, data) или None."""
    q = normalize(query)
    if not q:
        return None
    # точное совпадение по ключу (фамилии)
    if q in DOCTORS:
        return q, DOCTORS[q]
    # совпадение по любому слову в полном ФИО
    for key, data in DOCTORS.items():
        full_norm = normalize(data["full_name"])
        if q in full_norm or any(q == part for part in full_norm.split()):
            return key, data
    return None


def render_status(data: dict) -> str:
    lines = [f"👤 <b>{data['full_name']}</b>\n"]

    login_line = "✅ логинился(ась) в системе" if data["logged_in"] else "🔴 ни разу не логинился(ась)"
    lines.append(f"Логин: {login_line}")

    lines.append("\nКурсы:")
    for course_name, info in data["courses"].items():
        status = info["status"]
        idle = info["idle"]
        if status == "завершён":
            emoji = "✅"
            extra = "завершён"
        else:
            emoji = "🔴" if idle != "—" else "🟡"
            extra = f"{status}, без активности {idle}"
        lines.append(f"  {emoji} {course_name} — {extra}")

    lines.append(f"\n📋 Контекст: {data['context']}")
    lines.append(f"\n<i>Данные по сводке за {REPORT_DATE}, всего врачей в потоке: {TOTAL_DOCTORS}.</i>")
    return "\n".join(lines)


HELP_TEXT = (
    "Привет! Я показываю статус врача из ежедневной сводки онбординга.\n\n"
    "Просто отправь мне фамилию врача, например:\n"
    "<code>Иванова</code> или <code>Петров</code>\n\n"
    "Отвечу: логинился ли, что с курсами (сколько дней без активности), "
    "и что известно из доп. контекста (договор ReStaff и т.д.).\n\n"
    "Сейчас в базе есть данные по: Иванова Анна, Петров Пётр Сергеевич, "
    "Сидорова Мария Александровна, Смирнов Николай, Козлов Дмитрий Игоревич, "
    "Фёдорова Елена, Морозова Ольга, Волкова Светлана Викторовна."
)

dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(HELP_TEXT)


@dp.message(F.text)
async def handle_query(message: Message):
    result = find_doctor(message.text)
    if result is None:
        await message.answer(
            "Не нашла такого врача в текущей сводке.\n"
            "Проверь написание фамилии или напиши /start, чтобы увидеть список "
            "врачей, по которым у меня есть данные."
        )
        return
    _, data = result
    await message.answer(render_status(data))


async def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Не найден BOT_TOKEN. Установи переменную окружения BOT_TOKEN "
            "с токеном, полученным от @BotFather."
        )
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

# ──────────────────────────────────────────────────────────────────────────
# Как подключить реальную ежедневную сводку вместо статичного словаря:
# 1. Ведите сводку и доп. контекст в Google Sheet (два листа).
# 2. pip install gspread google-auth
# 3. Раз в день (например, по cron или через APScheduler) читайте таблицу
#    и пересобирайте словарь DOCTORS перед стартом polling — тогда бот всегда
#    будет отвечать по последней сводке без переразвёртывания кода.
# ──────────────────────────────────────────────────────────────────────────
