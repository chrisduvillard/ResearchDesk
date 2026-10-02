"""Conservative, versioned parsing. Unparsed prose must never imply a sale."""
import hashlib
import json
import re
from datetime import datetime, timezone
from html import unescape
from .calendar import NY
from .strategies import analyze, VERSION, MONTH_PATTERN

PARSER_VERSION = VERSION
SOURCE_URL = "https://www.cnbc.com/dan-nathan/"
MONTHS = {m.lower(): i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
MONTH_RE = MONTH_PATTERN


class ParseError(ValueError):
    pass


def text_of(node):
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        return "".join(text_of(c) for c in node.get("children", []))
    if isinstance(node, list):
        return "".join(text_of(c) for c in node)
    return ""


def extract(html, subject=r"Dan(?: Nathan)?|He", ancillary=None):
    marker = "window.__s_data="
    if marker not in html:
        raise ParseError("CNBC embedded page data is missing; preserving the last valid disclosure")
    try:
        state, _ = json.JSONDecoder().raw_decode(html.split(marker, 1)[1].lstrip())
    except (ValueError, json.JSONDecodeError) as exc:
        raise ParseError("CNBC page data is incomplete") from exc
    found = []
    header_pattern = re.compile(r"Disclosures? as of", re.I)
    paragraph_lead = re.compile(rf"(?:{subject})\s+(?:is|has|holds|owns|does)\b", re.I)

    def visit(obj):
        if isinstance(obj, dict):
            for value in obj.values():
                visit(value)
        elif isinstance(obj, list):
            # Read the complete disclosure within its source body. Flattening all
            # page paragraphs loses this boundary and can silently drop a new
            # paragraph when CNBC changes its wording.
            for index, node in enumerate(obj):
                if not isinstance(node, dict) or (node.get("tagName") or "").lower() != "p":
                    continue
                header = unescape(text_of(node)).strip()
                if not header_pattern.search(header):
                    continue
                block = []
                for sibling in obj[index + 1:]:
                    paragraph = unescape(text_of(sibling)).strip()
                    if not paragraph:
                        continue
                    if header_pattern.search(paragraph):
                        break
                    if not paragraph_lead.match(paragraph) and not (ancillary and ancillary.fullmatch(paragraph)):
                        raise ParseError("Unrecognized disclosure paragraph; preserving the last valid positions")
                    block.append(paragraph)
                if block:
                    found.append((header, "\n".join(block)))
            for value in obj:
                visit(value)

    visit(state)
    unique = list(dict.fromkeys(found))
    if len(unique) != 1:
        raise ParseError("Disclosure section is missing or ambiguous")
    header, disclosure = unique[0]
    match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})\s*\(\s*(\d{1,2}):(\d{2})\s*(AM|PM)\s*(?:ET|EST|EDT)\s*\)", header, re.I)
    if not match:
        raise ParseError("Disclosure timestamp format needs review")
    month, day, year, hour, minute = (int(match.group(i)) for i in range(1, 6))
    if year < 100:
        year += 2000
    if not 1 <= hour <= 12:
        raise ParseError("Disclosure timestamp is invalid")
    hour = hour % 12 + (12 if match.group(6).upper() == "PM" else 0)
    try:
        as_of = datetime(year, month, day, hour, minute, tzinfo=NY).astimezone(timezone.utc)
    except ValueError as exc:
        raise ParseError("Disclosure timestamp is invalid") from exc
    return header, disclosure, as_of


def classification(side, strategy):
    result = analyze(side, strategy)
    return result["direction"], result["confidence"], result["explanation"]


def split_clauses(text):
    # Semicolons and conjunctions within explicit [legs] belong to one strategy.
    result, buffer, depth = [], [], 0
    i = 0
    while i < len(text):
        char = text[i]
        if char == "[":
            depth += 1
            if depth > 1:
                raise ParseError("Nested option leg groups are unsupported")
        elif char == "]":
            depth -= 1
            if depth < 0:
                raise ParseError("Unmatched option leg bracket")
        separator = re.match(r",\s*(?:and\s+)?|;\s*|\s+and\s+", text[i:], re.I) if depth == 0 else None
        if separator:
            result.append("".join(buffer).strip())
            buffer = []
            i += separator.end()
        else:
            buffer.append(char)
            i += 1
    if depth:
        raise ParseError("Incomplete option leg group")
    result.append("".join(buffer).strip())
    return result


def signature(side, symbol, strategy):
    return hashlib.sha256(f"{side}|{symbol}|{' '.join(strategy.lower().split())}".encode()).hexdigest()[:24]


def parse_positions(disclosure, subject=r"Dan(?: Nathan)?|He"):
    clean = re.sub(r"\s+", " ", disclosure).strip()
    if re.fullmatch(rf"(?:{subject}) (?:has no positions|holds no positions|does not hold any positions|is not long or short any securities)\.?", clean, re.I):
        return []
    positions = []
    for sentence in re.split(rf"(?<=[.])\s+(?=(?:{subject})\b)", clean):
        lead = re.match(rf"^(?:{subject})\s+is\s+(long|short)\s+(.*)$", sentence, re.I)
        if not lead:
            raise ParseError("Unrecognized disclosure sentence; automatic changes paused")
        inherited_side = lead.group(1).lower()
        rest = lead.group(2).strip().rstrip(".")
        option_list = re.match(r"^(calls?|puts?) in (.+)$", rest, re.I)
        inherited_strategy = option_list.group(1).lower() if option_list else None
        pieces = split_clauses(option_list.group(2) if option_list else rest)
        for piece in pieces:
            original = piece.strip()
            if not original:
                raise ParseError("Empty position clause")
            item = re.sub(r"^(?:is\s+)?", "", original, flags=re.I)
            side_match = re.match(r"^(long|short)\s+", item, re.I)
            if side_match:
                inherited_side = side_match.group(1).lower()
                item = item[side_match.end():]
            item = re.sub(r"^the\s+", "", item, flags=re.I)
            match = re.fullmatch(r"([A-Z][A-Z0-9.\-]{0,9})(?:\s+(.+))?", item)
            if not match:
                raise ParseError(f"Unparsed position clause: {original}")
            symbol, strategy = match.group(1), match.group(2) or inherited_strategy or "shares"
            strategy = " ".join(strategy.split())
            if re.fullmatch(r"(?:stock|shares)(?:\s+position)?", strategy, re.I):
                strategy = "shares"
            known_words = {"COVERED", "PROTECTIVE", "MARRIED", "COLLAR", "STRADDLE", "STRANGLE", "BUTTERFLY", "CONDOR", "IRON", "CALENDAR", "DIAGONAL", "RATIO", "BACKSPREAD", "BACKSPREADS", "RISK", "REVERSAL", "SYNTHETIC", "STOCK", "SHARES", "LONG", "SHORT", "BUY", "SELL", "BOUGHT", "SOLD", "OPTIONS", "OPTION", "LEGS", "AT", "FOR", "NET", "BULLISH", "BEARISH", "BROKEN", "WING", "BUYWRITE", "WRITE", "CALL", "CALLS", "PUT", "PUTS", "SPREAD", "SPREADS", "DEBIT", "CREDIT", "BULL", "BEAR", "VERTICAL", "ITM", "OTM", "ATM", "LEAPS"} | {m.upper() for m in MONTHS}
            if any(token not in known_words for token in re.findall(r"\b[A-Z]{2,}\b", re.sub(MONTH_RE, "", strategy, flags=re.I))):
                raise ParseError("Potential additional ticker in a position clause; review the entire disclosure")
            analysis = analyze(inherited_side, strategy)
            direction, confidence, explanation = analysis["direction"], analysis["confidence"], analysis["explanation"]
            expiry = re.search(MONTH_RE, strategy, re.I)
            positions.append(dict(key=signature(inherited_side, symbol, strategy), symbol=symbol, side=inherited_side, strategy=strategy, raw_text=original, direction=direction, confidence=confidence, explanation=explanation, expiry_month=MONTHS[expiry.group(1)[:3].lower()] if expiry else None, expiry_year=int(expiry.group(2)) if expiry and expiry.group(2) else None, analysis=analysis))
    keys = [p["key"] for p in positions]
    if len(keys) != len(set(keys)):
        raise ParseError("Duplicate position clauses need review")
    return sorted(positions, key=lambda p: (p["symbol"], p["key"]))


def aggregate(positions):
    if not positions:
        return "unknown"
    directions = {p["direction"] for p in positions}
    if "unknown" in directions:
        return "unknown"
    return next(iter(directions)) if len(directions) == 1 else "mixed"
