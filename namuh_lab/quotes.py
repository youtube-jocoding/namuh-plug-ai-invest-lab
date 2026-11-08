"""Reference forward-test quote barrier. Not a live feed or brokerage adapter.

All participants reuse the returned symbol->quote map. Never fetch per model.
"""
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from .common import LabError, instant


@dataclass(frozen=True)
class Quote:
    source_at: datetime
    requested_at: datetime
    received_at: datetime
    bid: int
    ask: int
    bid_size: int
    ask_size: int
    regular: bool


def after_precision_boundary(deadline, resolution_seconds=1):
    """Strictly next timestamp bucket: even exact-second deadlines wait a bucket."""
    if resolution_seconds != 1:
        raise LabError('This adapter supports explicitly declared one-second timestamps only')
    deadline = instant(deadline)
    return deadline.replace(microsecond=0) + timedelta(seconds=1)


def valid(quote, deadline, max_age_seconds=30):
    times = [quote.source_at, quote.requested_at, quote.received_at]
    if any(t.tzinfo is None for t in times):
        return False
    if not quote.regular or not 0 < quote.bid <= quote.ask:
        return False
    if any(type(x) is not int or x < 0 for x in (quote.bid_size, quote.ask_size)):
        return False
    return (quote.requested_at >= after_precision_boundary(deadline)
            and quote.source_at >= after_precision_boundary(deadline)
            and quote.requested_at <= quote.received_at
            and 0 <= (quote.received_at-quote.source_at).total_seconds() <= max_age_seconds)


def collect(symbols, deadline, fetch, timeout_seconds=10, clock=None, sleep=time.sleep):
    """fetch(symbol) must itself have a bounded network timeout and return Quote.

Stop the common batch when any symbol remains unavailable; no partial-model fills.
No symbol is invalidated just because it was first in the collection order.
"""
    clock = clock or (lambda: datetime.now(timezone.utc))
    boundary = after_precision_boundary(deadline)
    while clock() < boundary:
        sleep(min(.1, (boundary-clock()).total_seconds()))
    end = clock() + timedelta(seconds=timeout_seconds)
    pending, result = set(symbols), {}
    while pending and clock() < end:
        for symbol in sorted(pending):
            quote = fetch(symbol)
            if valid(quote, deadline):
                result[symbol] = quote
                pending.remove(symbol)
            if clock() >= end:
                break
        if pending:
            sleep(.05)
    if pending:
        raise LabError('Common quote batch timed out; do not fill any participant')
    return result
