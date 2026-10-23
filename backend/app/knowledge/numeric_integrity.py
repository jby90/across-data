"""Conservative numeric/unit checks; no substring matches or inferred currencies."""

import re
import unicodedata
from decimal import Decimal, InvalidOperation

NUMBER = r"\d+(?:,\d{3})*(?:\.\d+)?"
NUMERIC = re.compile(rf"(?<!\d){NUMBER}(?:\s*%)?(?!\d)")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٪٫٬", "01234567890123456789%.,"))
    return re.sub(rf"百分之\s*({NUMBER})", r"\1%", text)


def canonical(token: str) -> tuple[str, bool]:
    percent = token.strip().endswith("%")
    value = token.replace(",", "").replace("%", "").strip()
    try:
        value = format(Decimal(value).normalize(), "f")
    except InvalidOperation:
        pass
    return value, percent


def tokens(text: str) -> set[tuple[str, bool]]:
    return {canonical(match.group()) for match in NUMERIC.finditer(normalize(text))}


# Bare '$' remains ambiguous: it must stay '$', not silently become USD/EUR/SGD.
CURRENCIES = {
    "SGD": r"S\$|SGD|新加坡元|新元",
    "USD": r"US\$|USD|美元|美金",
    "EUR": r"€|EUR|euros?|欧元",
    "GBP": r"£|GBP|英镑",
    "EGP": r"EGP|埃及镑",
    "TND": r"TND|突尼斯第纳尔",
    "MAD": r"MAD|摩洛哥迪拉姆",
    "LBP": r"LBP|黎巴嫩镑",
    "DOLLAR_UNSPECIFIED": r"(?<![A-Za-z])\$|dollars?",
}


def amounts(text: str) -> set[tuple[str, str]]:
    text = normalize(text)
    found = set()
    for currency, marker in CURRENCIES.items():
        # Explicit codes/symbols before or immediately after the same numeric amount.
        pattern = re.compile(
            rf"(?:{marker})\s*({NUMBER})(?!\d)|"
            rf"(?<!\d)({NUMBER})\s*(?:{marker})",
            re.IGNORECASE,
        )
        for match in pattern.finditer(text):
            found.add((currency, canonical(match.group(1) or match.group(2))[0]))
    return found


def missing_amounts(original: str, translated: str) -> list[str]:
    return [
        f"{currency} {value}" for currency, value in sorted(amounts(original) - amounts(translated))
    ]


MONTHS = {
    word: index
    for index, group in enumerate(
        (
            "january janvier يناير جانفي",
            "february février فيفري فبراير",
            "march mars مارس",
            "april avril أبريل افريل",
            "may mai مايو ماي",
            "june juin يونيو جوان",
            "july juillet يوليو جويلية",
            "august août أغسطس اوت",
            "september septembre سبتمبر",
            "october octobre أكتوبر اكتوبر",
            "november novembre نوفمبر",
            "december décembre ديسمبر",
        ),
        1,
    )
    for word in group.split()
}


def dates(text: str) -> set[tuple[int, int, int]]:
    text = normalize(text).lower()
    found = set()
    for year, month, day in re.findall(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", text):
        found.add((int(year), int(month), int(day)))
    for year, month, day in re.findall(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)", text):
        found.add((int(year), int(month), int(day)))
    month_words = "|".join(sorted(MONTHS, key=len, reverse=True))
    for day, month, year in re.findall(
        rf"(?<!\d)(\d{{1,2}})\s+({month_words})\s+(\d{{4}})(?!\d)", text
    ):
        found.add((int(year), MONTHS[month], int(day)))
    for month, day, year in re.findall(rf"({month_words})\s+(\d{{1,2}}),?\s+(\d{{4}})(?!\d)", text):
        found.add((int(year), MONTHS[month], int(day)))
    return found
