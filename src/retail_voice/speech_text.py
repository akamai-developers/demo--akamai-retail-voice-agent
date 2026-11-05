"""Spoken form of a reply. The speech service runs Magpie with text normalization off, and on
the reference GPU host an ASR read-back heard "$119" as "as wet N". Prices, counts, clock times and dates
are therefore written out as words before synthesis: "3:15 PM" is "three fifteen P M" and
"November 2, 2026" is "November second, twenty twenty six". The text on screen keeps its
symbols."""

import re

ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen"
).split()
TENS = "zero ten twenty thirty forty fifty sixty seventy eighty ninety".split()
PRICE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?")
ORDINAL = re.compile(r"(?<![\w.,-])(\d{1,6})(?:st|nd|rd|th)(?![A-Za-z0-9])")
# A number glued to letters (2XL, 5k) is left as written rather than half converted.
NUMBER = re.compile(r"(?<![\w.,-])(\d{1,6})(?![\w.,]\d|\d|[A-Za-z])")
ORDINALS = {
    "one": "first",
    "two": "second",
    "three": "third",
    "five": "fifth",
    "eight": "eighth",
    "nine": "ninth",
    "twelve": "twelfth",
}
MONTHS = (
    "January February March April May June July August September October November December"
).split()
# A clock time with AM or PM. A trailing period stays, since it may also end the sentence.
TIME = re.compile(r"(?<![\w:.])(\d{1,2}):([0-5]\d)\s*([AaPp])\.?\s?[Mm](?![A-Za-z])")
DATE = re.compile(rf"\b({'|'.join(MONTHS)})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:(,?\s+)(\d{{4}})\b)?")
SPACES = {0x00A0: " ", 0x202F: " ", 0x2009: " ", 0x2011: "-", 0x2013: ", ", 0x2014: ", "}
LIMIT = 1200
SIZE_NAMES = {
    "XS": "extra small",
    "S": "small",
    "M": "medium",
    "L": "large",
    "XL": "extra large",
    "OS": "one size",
}
SIZE_LIST = re.compile(
    r"(?i:\bsizes?\s+)(?:XS|XL|OS|S|M|L)(?:\s*(?:,|and|or)\s*(?:XS|XL|OS|S|M|L))*\b"
)
COLOR_SIZE = re.compile(
    r"(?i:\b(?:navy|black|gray|red|green|blue|yellow|olive|tan|white|cream|brown|orange|purple|pink)\s+)(XS|XL|OS|S|M|L)\b"
)


def number_words(n):
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ("" if n % 10 == 0 else " " + ONES[n % 10])
    if n < 1000:
        rest = n % 100
        return ONES[n // 100] + " hundred" + ("" if rest == 0 else " " + number_words(rest))
    if n < 1_000_000:
        rest = n % 1000
        return (
            number_words(n // 1000) + " thousand" + ("" if rest == 0 else " " + number_words(rest))
        )
    return str(n)


def ordinal_words(n):
    """ "2nd" is spoken as "second", not "twond"."""
    parts = number_words(n).split()
    last = parts[-1]
    if last in ORDINALS:
        parts[-1] = ORDINALS[last]
    elif last.endswith("y"):
        parts[-1] = last[:-1] + "ieth"
    else:
        parts[-1] = last + "th"
    return " ".join(parts)


def year_words(year):
    """2026 is "twenty twenty six"; 2005 is "two thousand five"; 1900 is "nineteen hundred"."""
    century, rest = divmod(year, 100)
    if 2000 <= year <= 2009:
        return "two thousand" + ("" if rest == 0 else " " + ONES[rest])
    if rest == 0:
        return number_words(century) + " hundred"
    return number_words(century) + (f" oh {ONES[rest]}" if rest < 10 else " " + number_words(rest))


def _time(match):
    hour, minute = int(match.group(1)), int(match.group(2))
    minutes = (
        "" if minute == 0 else f" oh {ONES[minute]}" if minute < 10 else " " + number_words(minute)
    )
    return f"{number_words(hour)}{minutes} {match.group(3).upper()} M"


def _date(match):
    words = f"{match.group(1)} {ordinal_words(int(match.group(2)))}"
    if match.group(4):
        year = int(match.group(4))
        words += match.group(3) + (year_words(year) if 1900 <= year <= 2099 else match.group(4))
    return words


def _price(match):
    dollars = int(match.group(1).replace(",", ""))
    cents = int(match.group(2) or 0)
    words = f"{number_words(dollars)} dollar{'' if dollars == 1 else 's'}"
    if cents:
        words += f" and {number_words(cents)} cent{'' if cents == 1 else 's'}"
    return words


def expand_sizes(match: re.Match[str]):
    def replace_size(size: re.Match[str]):
        return SIZE_NAMES[size.group()]

    return re.sub(r"\b(?:XS|XL|OS|S|M|L)\b", replace_size, match.group())


def spoken(text):
    text = text.translate(SPACES)
    text = SIZE_LIST.sub(expand_sizes, text)
    text = COLOR_SIZE.sub(lambda m: m.group()[: -len(m.group(1))] + SIZE_NAMES[m.group(1)], text)
    text = PRICE.sub(_price, text)
    text = TIME.sub(_time, text)
    text = DATE.sub(_date, text)
    text = ORDINAL.sub(lambda m: ordinal_words(int(m.group(1))), text)
    text = NUMBER.sub(lambda m: number_words(int(m.group(1))), text)
    return text


def speech_chunks(text, limit=LIMIT):
    """Keep every word when normalization grows a reply beyond the private TTS input cap."""
    chunks = []
    text = text.strip()
    while len(text) > limit:
        boundaries = list(re.finditer(r"(?<=[.!?])\s+", text[: limit + 1]))
        cut = boundaries[-1].start() if boundaries else text.rfind(" ", 0, limit + 1)
        if cut <= 0:
            raise ValueError("speech_word_too_long")
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks
