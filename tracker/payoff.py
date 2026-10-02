"""Deterministic expiration values for stock and options on one underlying."""
import math
from datetime import date

MAX_LEGS = 16


def number(value, name, low=-1e9, high=1e9):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} is outside the supported range")
    return float(value)


def validate_model(model):
    if not isinstance(model, dict):
        raise ValueError("Provide an object containing option legs")
    if set(model) - {"legs", "same_expiry", "net_cost", "range_min", "range_max"}:
        raise ValueError("Unrecognized payoff input")
    raw = model.get("legs")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_LEGS:
        raise ValueError(f"Provide between 1 and {MAX_LEGS} legs")
    same_expiry = model.get("same_expiry", False)
    if not isinstance(same_expiry, bool):
        raise ValueError("Same expiration must be true or false")
    legs = []
    for i, leg in enumerate(raw, 1):
        if not isinstance(leg, dict) or set(leg) - {"kind", "quantity", "strike", "expiry", "multiplier"}:
            raise ValueError(f"Leg {i}: unrecognized fields")
        kind = leg.get("kind")
        if kind not in ("stock", "call", "put"):
            raise ValueError(f"Leg {i}: choose stock, call or put")
        qty = number(leg.get("quantity"), f"Leg {i} quantity", -10000, 10000)
        if not qty:
            raise ValueError(f"Leg {i}: quantity cannot be zero")
        multiplier = number(leg.get("multiplier", 1 if kind == "stock" else 100),
                            f"Leg {i} multiplier", 0.0001, 100000)
        if kind == "stock" and multiplier != 1:
            raise ValueError("Stock quantities are shares; their multiplier must be 1")
        strike = number(leg.get("strike"), f"Leg {i} strike", 0, 1e7) if kind != "stock" else None
        expiry = leg.get("expiry")
        if expiry:
            if not isinstance(expiry, str) or len(expiry) != 10:
                raise ValueError(f"Leg {i}: expiration must be YYYY-MM-DD")
            try:
                date.fromisoformat(expiry)
            except ValueError as exc:
                raise ValueError(f"Leg {i}: invalid expiration date") from exc
        if kind == "stock" and (leg.get("strike") is not None or expiry):
            raise ValueError("Stock legs have no strike or expiration")
        legs.append(dict(kind=kind, quantity=qty, strike=strike,
                         expiry=expiry or None, multiplier=multiplier))
    options = [l for l in legs if l["kind"] != "stock"]
    known = {l["expiry"] for l in options if l["expiry"]}
    if len(known) > 1:
        raise ValueError("Different expirations require a valuation model; a shared-expiration payoff is unavailable")
    if len(options) > 1 and (not known or any(not l["expiry"] for l in options)) and not same_expiry:
        raise ValueError("Confirm a shared expiration, or provide the same exact date for every option")
    cost = model.get("net_cost")
    if cost is not None:
        cost = number(cost, "Net entry cost")
    lower = number(model.get("range_min", 0), "Chart minimum", 0, 1e8)
    default_upper = max([l["strike"] for l in options] + [100]) * 1.5
    upper = number(model.get("range_max", default_upper), "Chart maximum", 0, 1e8)
    if upper <= lower:
        raise ValueError("Chart maximum must be greater than the minimum")
    return dict(legs=legs, same_expiry=same_expiry, net_cost=cost,
                range_min=lower, range_max=upper)


def value_at(legs, price):
    total = 0
    for leg in legs:
        intrinsic = price if leg["kind"] == "stock" else max(
            price - leg["strike"] if leg["kind"] == "call" else leg["strike"] - price, 0)
        total += leg["quantity"] * leg["multiplier"] * intrinsic
    return total


def slope_at(legs, price):
    return sum(l["quantity"] * l["multiplier"] * (
        1 if l["kind"] == "stock" or l["kind"] == "call" and price > l["strike"]
        else -1 if l["kind"] == "put" and price < l["strike"] else 0) for l in legs)


def calculate(model):
    m = validate_model(model)
    legs, cost = m["legs"], m["net_cost"]
    strikes = sorted({0.0} | {l["strike"] for l in legs if l["kind"] != "stock"})
    offset = cost if cost is not None else 0
    regions, roots, flats = [], [], []
    for i, start in enumerate(strikes):
        end = strikes[i + 1] if i + 1 < len(strikes) else None
        probe = (start + end) / 2 if end is not None else start + 1
        slope = slope_at(legs, probe)
        y = value_at(legs, start) - offset
        trend = "rising" if slope > 1e-8 else "falling" if slope < -1e-8 else "flat"
        if regions and math.isclose(regions[-1]["slope"], slope, abs_tol=1e-8):
            regions[-1]["to"] = end
        else:
            regions.append(dict(start=start, to=end, slope=slope, trend=trend))
        if cost is not None:
            if abs(slope) < 1e-8 and abs(y) < 1e-7:
                if flats and flats[-1]["to"] == start:
                    flats[-1]["to"] = end
                else:
                    flats.append(dict(start=start, to=end))
            elif abs(slope) >= 1e-8:
                root = start - y / slope
                if root >= start - 1e-7 and (end is None or root <= end + 1e-7):
                    roots.append(round(max(root, 0), 8))
    roots = sorted(set(r for r in roots if not any(
        r >= f["start"] - 1e-7 and (f["to"] is None or r <= f["to"] + 1e-7) for f in flats)))
    signs = [r["trend"] for r in regions if r["trend"] != "flat"]
    signs = [s for i, s in enumerate(signs) if i == 0 or s != signs[i - 1]]
    direction = ("bullish" if signs == ["rising"] else "bearish" if signs == ["falling"]
                 else "large_move" if signs == ["falling", "rising"]
                 else "range_bound" if signs == ["rising", "falling"]
                 else "conditional" if signs else "non_directional")
    values = [value_at(legs, x) - offset for x in strikes]
    tail = regions[-1]["slope"]
    lower, upper = m["range_min"], m["range_max"]
    xs = {lower + (upper - lower) * i / 160 for i in range(161)}
    xs.update(x for x in strikes + roots if lower <= x <= upper)
    points = [dict(price=round(x, 8), value=round(value_at(legs, x) - offset, 8))
              for x in sorted(xs)]
    return dict(
        mode="profit_loss" if cost is not None else "terminal_value",
        unit="USD per entered strategy unit", model=m, direction=direction,
        points=points, regions=regions, breakevens=roots, breakeven_ranges=flats,
        minimum=None if tail < -1e-8 else min(values),
        maximum=None if tail > 1e-8 else max(values),
        minimum_unbounded=tail < -1e-8, maximum_unbounded=tail > 1e-8,
        explanation=("Entry cost included; positive cost means a debit and negative cost a credit."
                     if cost is not None else
                     "Entry cost is unknown. This shows terminal value, not profit or loss."),
        assumptions=[
            "All legs are on the same underlying and remain in place through the shared expiration." if any(l["kind"] != "stock" for l in legs) else "Stock value at the chosen share price; shares have no expiration.",
            "Option quantities are contracts; the default multiplier is 100. Stock quantities are shares.",
            "Early exercise, interim cash flows, dividends, fees and financing are excluded.",
            "Chart limits affect the drawing only; extrema and breakevens use the full nonnegative price range.",
        ],
    )
