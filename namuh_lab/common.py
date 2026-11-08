import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path


class LabError(Exception):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def code_hash():
    root = Path(__file__).parent
    return digest({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob('*.py'))})


def instant(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError('timezone required')
        return result.astimezone(timezone.utc)
    except ValueError as exc:
        raise LabError('Invalid timestamp: use ISO 8601 with an explicit timezone') from exc


def stamp(value):
    return value.astimezone(timezone.utc).isoformat()


def decimal(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError()
        return result
    except (InvalidOperation, ValueError) as exc:
        raise LabError('Expected a finite decimal number') from exc


def integer(value, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise LabError(f'Expected integer >= {minimum}')
    return value


def minor(value, currency):
    scaled = decimal(value) * {'USD': 100, 'KRW': 1}[currency]
    if scaled != scaled.to_integral_value():
        raise LabError(f'Price precision exceeds {currency} minor units; normalize your data explicitly')
    return int(scaled)


def cost(gross, bps):
    return int((Decimal(gross) * decimal(bps) / 10000).to_integral_value(rounding=ROUND_CEILING))


def trade_price(price, side, bps):
    factor = 1 + decimal(bps) / 10000 * (1 if side == 'BUY' else -1)
    rounding = ROUND_CEILING if side == 'BUY' else ROUND_FLOOR
    return int((price * factor).to_integral_value(rounding=rounding))


def strict_json(text):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise LabError('Duplicate JSON key')
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(LabError('Non-finite JSON number')))
    except (ValueError, TypeError) as exc:
        raise LabError('Invalid JSON response') from exc
