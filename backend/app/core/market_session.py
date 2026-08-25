from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# NSE holidays 2024-2026 (major Indian market holidays)
# This is a subset; full list from NSE website
NSE_HOLIDAYS = {
    # 2024
    date(2024, 1, 26),   # Republic Day
    date(2024, 3, 25),   # Holi
    date(2024, 3, 29),   # Good Friday
    date(2024, 4, 11),   # Id-Ul-Fitr
    date(2024, 4, 17),   # Shri Ram Navmi
    date(2024, 4, 21),   # Shri Mahavir Jayanti
    date(2024, 5, 1),    # Maharashtra Day
    date(2024, 5, 23),   # Buddha Purnima
    date(2024, 6, 17),   # Id-Ul-Adha
    date(2024, 7, 17),   # Muharram
    date(2024, 8, 15),   # Independence Day
    date(2024, 10, 2),   # Mahatma Gandhi Jayanti
    date(2024, 11, 1),   # Diwali Laxmi Pujan
    date(2024, 11, 15),  # Prakash Gurpurab
    date(2024, 12, 25),  # Christmas
    # 2025
    date(2025, 1, 26),   # Republic Day
    date(2025, 2, 26),   # Maha Shivaratri
    date(2025, 3, 14),   # Holi
    date(2025, 3, 31),   # Id-Ul-Fitr
    date(2025, 4, 10),   # Shri Mahavir Jayanti
    date(2025, 4, 14),   # Dr. Ambedkar Jayanti
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 1),    # Maharashtra Day
    date(2025, 5, 27),   # Buddha Purnima
    date(2025, 6, 7),    # Id-Ul-Adha
    date(2025, 7, 6),    # Muharram
    date(2025, 8, 15),   # Independence Day
    date(2025, 8, 27),   # Shri Krishna Janmashtami
    date(2025, 10, 2),   # Mahatma Gandhi Jayanti
    date(2025, 10, 21),  # Diwali Laxmi Pujan
    date(2025, 11, 5),   # Prakash Gurpurab
    date(2025, 12, 25),  # Christmas
    # 2026
    date(2026, 1, 26),   # Republic Day
    date(2026, 2, 17),   # Maha Shivaratri
    date(2026, 3, 4),    # Holi
    date(2026, 3, 20),   # Id-Ul-Fitr
    date(2026, 3, 30),   # Shri Mahavir Jayanti
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 16),   # Buddha Purnima
    date(2026, 5, 27),   # Id-Ul-Adha
    date(2026, 6, 25),   # Muharram
    date(2026, 8, 15),   # Independence Day
    date(2026, 8, 16),   # Shri Krishna Janmashtami
    date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    date(2026, 11, 10),  # Diwali Laxmi Pujan
    date(2026, 11, 24),  # Prakash Gurpurab
    date(2026, 12, 25),  # Christmas
}


def now_ist() -> datetime:
    return datetime.now(IST)


def is_holiday(dt: date | None = None) -> bool:
    dt = dt or now_ist().date()
    return dt in NSE_HOLIDAYS


def is_market_hours(dt: datetime | None = None) -> bool:
    dt = dt or now_ist()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    if dt.weekday() >= 5:
        return False
    if is_holiday(dt.date()):
        return False
    market_open = dt.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = dt.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= dt <= market_close


def market_session(dt: datetime | None = None) -> str:
    dt = dt or now_ist()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    if dt.weekday() >= 5:
        return "closed"
    if is_holiday(dt.date()):
        return "closed"
    h, m = dt.hour, dt.minute
    t = h * 60 + m
    if t < 555:              # before 09:15
        return "pre_market"
    elif t == 555:           # exactly 09:15
        return "market_open"
    elif t < 915:            # 09:16 - 15:14
        return "trading"
    elif t <= 930:           # 15:15 - 15:30 (closing auction)
        return "closing"
    else:
        return "closed"
