"""Versioned strategy definitions and conservative, non-LLM leg extraction."""
import re
from datetime import date
from .payoff import calculate

VERSION = "2.0.2"
MONTH_PATTERN = r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)(?:\s+(20\d{2}))?\b"
DIRECTIONS = ("bullish", "bearish", "large_move", "range_bound", "conditional",
              "mixed", "unknown", "non_directional")
LABELS = dict(bullish="Bullish", bearish="Bearish", large_move="Benefits from large moves",
              range_bound="Benefits from a price range", conditional="Conditional exposure",
              mixed="Mixed exposure", unknown="Needs more detail", non_directional="No simple direction")
SOURCES = {
    "covered_call": "covered-call-buy-write", "protective_put": "protective-put-married-put",
    "collar": "collar-protective-collar", "straddle": "long-straddle",
    "strangle": "long-strangle-long-combination", "calendar": "long-call-calendar-spread-call-horizontal",
    "ratio": "short-ratio-call-spread", "butterfly": "long-call-butterfly",
    "condor": "long-call-condor",
}


def expiry_from(text):
    exact = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    if exact:
        date.fromisoformat(exact[1])  # Reject invalid exact dates, never guess.
        return exact[1], text[:exact.start()] + text[exact.end():], exact[1]
    month = re.search(MONTH_PATTERN, text, re.I)
    if month:
        return None, text[:month.start()] + text[month.end():], month[0]
    return None, text, None


def leg(kind, quantity, strike=None, expiry=None):
    return dict(kind=kind, quantity=quantity, strike=strike, expiry=expiry,
                multiplier=1 if kind == "stock" else 100)


def base():
    return dict(version=VERSION, family="unknown", title="Unrecognized structure",
                direction="unknown", confidence="uncertain",
                explanation="The wording does not establish the option legs.",
                volatility="Not established", time_effect="Not established",
                assumptions=[], missing=[], legs=[], same_expiry=False, net_cost=None,
                payoff_available=False, score_eligible=False, source=None,
                basis="One normalized strategy unit; Dan's actual position size is unknown.")


def finish(a):
    if a["family"] in SOURCES:
        a["source"] = "https://www.optionseducation.org/strategies/all-strategies/" + SOURCES[a["family"]]
    if a["legs"]:
        if any(l["kind"] != "stock" and l["strike"] is None for l in a["legs"]):
            a["missing"].append("Option strikes")
        elif a["same_expiry"] or len([l for l in a["legs"] if l["kind"] != "stock"]) <= 1:
            try:
                result = calculate(dict(legs=a["legs"], same_expiry=a["same_expiry"], net_cost=a["net_cost"]))
            except ValueError as exc:
                a["missing"].append(str(exc))
            else:
                a["payoff_available"] = True
                # A payoff establishes its expiration shape, not today's delta.
                a["direction"] = result["direction"]
    elif a["family"] not in ("unknown",):
        a["missing"].append("Option legs, strikes and relative quantities")
    if any(l["kind"] != "stock" and not l["expiry"] for l in a["legs"]):
        a["missing"].append("Exact expiration date (the day and year are not assumed)")
    if a["net_cost"] is None:
        a["missing"].append("Total entry cost for profit/loss and breakevens")
    a["missing"] = list(dict.fromkeys(a["missing"]))
    a["label"] = LABELS[a["direction"]]
    a["score_eligible"] = (a["family"] in ("shares", "call", "put", "vertical", "synthetic")
                           and a["direction"] in ("bullish", "bearish"))
    return a


def explicit_legs(strategy):
    """An unambiguous bracketed structure; reject the entire clause on partial parsing."""
    match = re.fullmatch(r"\s*(.*?)\[\s*(.*?)\s*\]\s*", strategy, re.S)
    if not match:
        return None
    prefix = match[1].strip()
    common_date, residue, expiry_label = expiry_from(prefix)
    if residue.strip().lower() not in ("", "options", "legs", "option legs"):
        raise ValueError("Explicit legs must use an options/legs prefix and an optional common expiration")
    legs, costs = [], []
    for raw in match[2].split(";"):
        side = re.match(r"\s*(long|short|buy|sell|bought|sold)\s+(\d+(?:\.\d+)?)\s+(.+?)\s*$", raw, re.I)
        if not side:
            raise ValueError("Each explicit leg needs buy/long or sell/short, a quantity, and its instrument")
        qty = float(side[2]) * (1 if side[1].lower() in ("long", "buy", "bought") else -1)
        rest = side[3]
        premium = re.search(r"\s*(?:@|at)\s*\$?(\d+(?:\.\d+)?)\s*$", rest, re.I)
        price = float(premium[1]) if premium else None
        if premium:
            rest = rest[:premium.start()]
        if re.fullmatch(r"(?:stock|shares?)", rest.strip(), re.I):
            one = leg("stock", qty)
        else:
            expiry, rest, label = expiry_from(rest)
            parsed = re.fullmatch(r"\s*\$?(\d+(?:\.\d+)?)\s*(calls?|puts?)\s*", rest, re.I)
            if not parsed:
                raise ValueError("An explicit option leg needs a numeric strike and call/put")
            one = leg("call" if parsed[2].lower().startswith("call") else "put",
                      qty, float(parsed[1]), expiry or common_date)
            one["expiry_label"] = label or expiry_label
        costs.append(None if price is None else qty * one["multiplier"] * price)
        legs.append(one)
    # A shared month does not prove a shared day. Exact dates or the template
    # prefix's exact date are required for arbitrary explicit combinations.
    options = [l for l in legs if l["kind"] != "stock"]
    expiries = {l.get("expiry_label") or l["expiry"] for l in options}
    same = len(options) <= 1 or (all(l["expiry"] for l in options) and len({l["expiry"] for l in options}) == 1)
    normalized = [{k: v for k, v in l.items() if k != "expiry_label"} for l in legs]
    if len(legs) > 16 or any(not l["quantity"] or abs(l["quantity"]) > 10000 for l in legs):
        raise ValueError("Explicit legs need nonzero quantities and at most 16 legs")
    a = base()
    a.update(family="explicit", title="Disclosed option legs", legs=normalized,
             confidence="explicit", same_expiry=same,
             direction="conditional", net_cost=sum(costs) if all(c is not None for c in costs) else None,
             explanation="The individual legs are disclosed. The payoff describes their combined value at expiration.",
             basis="Quantities from the disclosure; a quoted ratio may describe a strategy unit rather than the full holding.")
    if len(expiries) > 1:
        a["family"], a["title"] = "calendar", "Options with different expirations"
        a["explanation"] = "The legs expire at different times. The later options retain time value when the first expire."
        a["missing"].append("A valuation model and market inputs for options with different expirations")
    elif not same:
        a["missing"].append("Confirmation that every option expires on the same exact day")
    return finish(a)


def analyze(side, strategy):
    a = base()
    s = " ".join(strategy.lower().replace("–", "-").replace("×", "x").split())
    if "[" in s or "]" in s:
        result = explicit_legs(strategy)
        if result is None:
            raise ValueError("Incomplete explicit option legs")
        return result
    if s == "shares":
        a.update(family="shares", title="Long shares" if side == "long" else "Short shares",
                 direction="bullish" if side == "long" else "bearish", confidence="explicit",
                 explanation="Disclosed ownership direction.", legs=[leg("stock", 1 if side == "long" else -1)])
        return finish(a)
    expiry, remainder, expiry_label = expiry_from(s)
    if "calendar" in s or "diagonal" in s:
        remainder = re.sub(r"\b20\d{2}-\d{2}-\d{2}\b", "", remainder)
        remainder = re.sub(MONTH_PATTERN, "", remainder, flags=re.I)
    ratio = re.search(r"\b\d+(?:\.\d+)?\s*(?:x|:)\s*\d+(?:\.\d+)?\b", remainder)
    if ratio:
        remainder = remainder[:ratio.start()] + " ratio " + remainder[ratio.end():]
    # Premium shorthand is per option share; a normalized option unit uses 100.
    premium = re.search(r"\s+(?:for\s+)?(?:a\s+)?(?:net\s+)?\$?(\d+(?:\.\d+)?)\s+(debit|credit)\s*$", remainder)
    if premium:
        a["net_cost"] = float(premium[1]) * 100 * (1 if premium[2] == "debit" else -1)
        remainder = remainder[:premium.start()]
    chain = re.search(r"(?<![\w.])\$?\d+(?:\.\d+)?(?:\s*/\s*\$?\d+(?:\.\d+)?)+(?![\w.])", remainder)
    strikes = []
    if chain:
        strikes = [float(x.strip().replace("$", "")) for x in chain[0].split("/")]
        remainder = remainder[:chain.start()] + remainder[chain.end():]
    else:
        single = re.search(r"(?<![\w.])\$?\d+(?:\.\d+)?(?![\w.])", remainder)
        if single:
            strikes = [float(single[0].replace("$", ""))]
            remainder = remainder[:single.start()] + remainder[single.end():]
    if len(strikes) != len(set(strikes)) or any(k < 0 or k > 1e7 for k in strikes):
        a["missing"].append("Distinct valid strikes for a standard structure")
        return finish(a)
    allowed = set("call calls put puts spread spreads bull bullish bear bearish vertical debit credit covered protective married collar straddle strangle butterfly condor iron calendar diagonal ratio backspread backspreads risk reversal synthetic stock long short buy write buywrite itm otm atm leaps broken wing".split())
    words = set(re.findall(r"[a-z]+", remainder))
    payment = words & {"credit", "debit"}
    if premium:
        payment.add(premium[2])
    if words - allowed or re.search(r"\d", remainder):
        a["missing"].append("Recognized strategy wording or explicit option legs")
        return finish(a)
    # Recognized words are not enough: two different named structures cannot
    # be resolved by whichever branch happens to run first.
    families = [bool(words & names) for names in (
        {'calendar', 'diagonal'}, {'ratio', 'backspread', 'backspreads'},
        {'risk', 'reversal'}, {'synthetic'}, {'covered', 'buywrite', 'write'},
        {'collar'}, {'straddle'}, {'strangle'}, {'butterfly'}, {'condor'},
    )]
    if words & {'protective', 'married'} and 'collar' not in words:
        families.append(True)
    if sum(families) > 1 or {'long', 'short'} <= words:
        a['missing'].append('Conflicting named structures need explicit option legs')
        return finish(a)
    if (({"bull", "bullish"} & words) and ({"bear", "bearish"} & words)
            or len(payment) > 1
            or {"straddle", "strangle"} <= words
            or {"butterfly", "condor"} <= words):
        a["missing"].append("Conflicting strategy qualifiers need explicit option legs")
        return finish(a)
    sign = 1 if side == "long" else -1
    is_call, is_put = bool(re.search(r"\bcalls?\b", s)), bool(re.search(r"\bputs?\b", s))
    kind = "call" if is_call and not is_put else "put" if is_put and not is_call else None
    ks = sorted(strikes)
    a["confidence"] = "inferred"
    a["assumptions"] = ["Uses the conventional structure and matched ratios of the named strategy."]
    if expiry_label:
        a["assumptions"].append(f"Source expiry wording: {expiry_label}. An unspecified day or year remains unknown.")
    a["same_expiry"] = True

    def slots(count):
        if ks and len(ks) != count:
            raise ValueError(f"The named strategy requires {count} distinct strike(s)")
        return ks if ks else [None] * count

    def options(types, quantities, keys):
        return [leg(t, q, k, expiry) for t, q, k in zip(types, quantities, keys)]

    if "calendar" in words or "diagonal" in words:
        a.update(family="calendar", title="Diagonal spread" if "diagonal" in words else "Calendar spread",
                 direction="conditional", same_expiry=False,
                 explanation="Exposure depends on the strikes, expiration order and each option's remaining time value.",
                 volatility="Depends on volatility at both expirations", time_effect="Depends on which option expires first")
        a["missing"].append("Exact legs and a valuation model for different expirations")
    elif "ratio" in words or "backspread" in words or "backspreads" in words:
        a.update(family="ratio", title="Backspread" if "ratio" not in words else "Ratio spread",
                 direction="conditional", explanation="Unequal option quantities can change the direction of exposure across prices.",
                 volatility="Depends on the purchased and sold legs", time_effect="Depends on the purchased and sold legs")
        a["missing"].append("Which strikes are bought and sold, and their relative quantities")
    elif "risk" in words and "reversal" in words:
        a.update(family="risk_reversal", title="Risk reversal",
                 explanation="A call and a put on opposite sides; direction requires knowing which is bought.")
        if side == "long" and ({"bull", "bullish", "bear", "bearish"} & words):
            bullish = bool({"bull", "bullish"} & words)
            a["direction"] = "bullish" if bullish else "bearish"
            a["legs"] = options(["put", "call"], [-1, 1] if bullish else [1, -1], slots(2))
        else:
            a["direction"] = "conditional"
            a["missing"].append("Whether the call or the put is purchased")
    elif "synthetic" in words:
        a.update(family="synthetic", title="Synthetic stock",
                 explanation="A call and an opposing put at the same strike reproduce stock-like expiration exposure.")
        if "stock" not in words or (side == "short" and ({"long", "short"} & words)):
            a.update(direction="unknown", confidence="uncertain")
            a["missing"].append("Explicit synthetic-stock leg orientation")
        else:
            orientation = -1 if "short" in words else sign
            k = slots(1)[0]
            a["legs"] = options(["call", "put"], [orientation, -orientation], [k, k])
            a["direction"] = "bullish" if orientation == 1 else "bearish"
    elif "covered" in words or "buywrite" in words or {"buy", "write"} <= words:
        a.update(family="covered_call", title="Covered call",
                 explanation="Long shares with matched calls sold against them. Upside is capped; stock downside remains.",
                 direction="bullish", volatility="Rising implied volatility hurts the sold call, all else equal",
                 time_effect="Decay of the sold call generally helps, all else equal")
        if is_put or side != "long":
            a.update(direction="unknown", confidence="uncertain")
            a["missing"].append("Confirmation of shares and the covered option legs")
        else:
            a["legs"] = [leg("stock", 100), leg("call", -1, slots(1)[0], expiry)]
            a["net_cost"] = None  # An option premium alone excludes the stock cost.
    elif ("protective" in words or "married" in words) and "collar" not in words:
        a.update(family="protective_put", title="Protective put",
                 direction="bullish", explanation="Long shares with a matched put establish an expiration floor while retaining upside.",
                 volatility="Rising implied volatility helps the purchased put, all else equal",
                 time_effect="Decay of the purchased put generally hurts, all else equal")
        if side != "long" or kind != "put":
            a.update(direction="unknown", confidence="uncertain")
            a["missing"].append("Confirmation of shares and the protective option")
        else:
            a["legs"] = [leg("stock", 100), leg("put", 1, slots(1)[0], expiry)]
            a["net_cost"] = None
    elif "collar" in words:
        a.update(family="collar", title="Protective collar", direction="bullish",
                 explanation="Long shares, a purchased put and a sold call establish a floor and a ceiling at expiration.",
                 volatility="Depends on the two option legs", time_effect="Depends on the two option legs")
        if side != "long":
            a.update(direction="conditional")
            a["missing"].append("Confirmation of stock side and option orientation")
        else:
            a["legs"] = [leg("stock", 100)] + options(["put", "call"], [1, -1], slots(2))
            a["net_cost"] = None
    elif "straddle" in words or "strangle" in words:
        family = "straddle" if "straddle" in words else "strangle"
        a.update(family=family, title=("Long " if sign == 1 else "Short ") + family,
                 direction="large_move" if sign == 1 else "range_bound",
                 explanation=("Expiration value increases away from the central strike or strikes." if sign == 1 else
                              "Expiration value is highest around the central strike or between the strikes; large moves hurt."),
                 volatility="Rising implied volatility generally " + ("helps" if sign == 1 else "hurts"),
                 time_effect="Time decay generally " + ("hurts" if sign == 1 else "helps"))
        keys = slots(2) if family == "strangle" else slots(1) * 2
        a["legs"] = options(["put", "call"], [sign, sign], keys)
    elif "butterfly" in words or "condor" in words:
        family = "butterfly" if "butterfly" in words else "condor"
        a.update(family=family, title=("Iron " if "iron" in words else "") + family,
                 explanation="The payoff changes across strike regions; premiums determine which regions are profitable.")
        if "iron" in words:
            if side != "long" or not payment:
                a["direction"] = "conditional"
                a["missing"].append("Iron structures require explicit credit/debit orientation or individual legs")
            else:
                credit = "credit" in payment
                keys = slots(4) if family == "condor" else slots(3)
                if family == "butterfly":
                    keys = [keys[0], keys[1], keys[1], keys[2]]
                quantities = [1, -1, -1, 1] if credit else [-1, 1, 1, -1]
                a["legs"] = options(["put", "put", "call", "call"], quantities, keys)
                a["direction"] = "range_bound" if credit else "large_move"
        elif kind:
            keys = slots(3 if family == "butterfly" else 4)
            quantities = [sign, -2 * sign, sign] if family == "butterfly" else [sign, -sign, -sign, sign]
            a["legs"] = options([kind] * len(keys), quantities, keys)
            a["direction"] = "range_bound" if sign == 1 else "large_move"
        else:
            a["direction"] = "conditional"
            a["missing"].append("Whether the structure uses calls, puts or iron option legs")
    elif kind and ({"spread", "spreads", "vertical"} & words):
        bull = bool({'bull', 'bullish'} & words)
        bear = bool({'bear', 'bearish'} & words)
        implied = (1 if kind == 'put' else -1) if 'credit' in payment else (1 if kind == 'call' else -1)
        if len(payment) > 1 or (payment and ((bull and implied < 0) or (bear and implied > 0))):
            a = base()
            a['missing'].append('Direction and credit/debit wording disagree; explicit option legs are needed')
            return finish(a)
        a.update(family="vertical", title=("Call" if kind == "call" else "Put") + " vertical spread",
                 explanation="Assumes a conventional vertical with equal quantities and one shared expiration.",
                 volatility="Depends on strike placement and the underlying price", time_effect="Depends on strike placement and the underlying price")
        if side == "short" and (bull or bear or payment):
            a.update(direction="unknown", confidence="uncertain")
            a["missing"].append("Short position with a named spread qualifier needs explicit legs")
        else:
            if {"bull", "bullish"} & words:
                direction = 1
            elif {"bear", "bearish"} & words:
                direction = -1
            elif 'credit' in payment:
                direction = -1 if kind == "call" else 1
            else:
                direction = sign * (1 if kind == "call" else -1)
            a["direction"] = "bullish" if direction == 1 else "bearish"
            a["legs"] = options([kind, kind], [direction, -direction], slots(2))
    elif kind and words <= {kind, kind + "s", "itm", "otm", "atm", "leaps"}:
        a.update(family=kind, title=("Long " if sign == 1 else "Short ") + kind,
                 direction="bullish" if (kind == "call") == (sign == 1) else "bearish",
                 explanation="Direction of the disclosed option; other holdings and hedges are unknown.",
                 volatility="Rising implied volatility generally " + ("helps" if sign == 1 else "hurts"),
                 time_effect="Time decay generally " + ("hurts" if sign == 1 else "helps"))
        a["legs"] = [leg(kind, sign, slots(1)[0], expiry)]
    else:
        a = base()
        a["missing"].append("A recognized strategy or explicit option legs")
    # Buying only options requires a debit; selling only options requires a
    # credit. Contradictory prose must not invent a profitable long option.
    if payment and a["legs"] and all(l["kind"] != "stock" for l in a["legs"]):
        bought_only = all(l["quantity"] > 0 for l in a["legs"])
        sold_only = all(l["quantity"] < 0 for l in a["legs"])
        if (bought_only and "credit" in payment) or (sold_only and "debit" in payment):
            a = base()
            a["missing"].append("Option sides and credit/debit wording disagree; explicit option legs are needed")
    return finish(a)


def payoff_for(analysis):
    if not analysis.get("payoff_available"):
        return None
    return calculate(dict(legs=analysis["legs"], same_expiry=analysis["same_expiry"],
                          net_cost=analysis["net_cost"]))
