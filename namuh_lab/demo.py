"""Synthetic, deterministic fixtures; not real prices or AI performance."""
import csv
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path


def create(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError('Demo input directory must be empty')
    days = []
    day = datetime(2024, 1, 1, tzinfo=timezone.utc)
    while len(days) < 30:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    sessions, bars = [], []
    for market, hour, minute, currency in [('KR', 0, 0, 'KRW'), ('US', 14, 30, 'USD')]:
        for index, day in enumerate(days):
            opening = day.replace(hour=hour, minute=minute)
            closing = opening + timedelta(hours=6, minutes=30)
            sessions.append({'market': market, 'open_time': opening.isoformat(), 'close_time': closing.isoformat()})
            for stock in range(4):
                # Integer minor units, with trends and gaps; never random or live.
                scale = 10000 if market == 'KR' else 5000
                price = scale + stock*1000 + index*(40-stock*20) + ((index+stock)%5-2)*100
                close = price + ((index+stock)%3-1)*70
                unit = 1 if currency == 'KRW' else 100
                fmt = lambda p: str(Decimal(p) / unit)
                bars.append({'market': market, 'symbol': f'{market}{stock+1}',
                    'name': f'Synthetic {market} Company {stock+1}',
                    'sector': ['Technology', 'Healthcare', 'Utilities', 'Industry'][stock],
                    'open_time': opening.isoformat(), 'close_time': closing.isoformat(),
                    'available_at': closing.isoformat(), 'open': fmt(price),
                    'high': fmt(max(price, close)+100), 'low': fmt(min(price, close)-100),
                    'close': fmt(close), 'volume': 1000000, 'tradable_at_open': 'true'})
    for name, rows in [('sessions.csv', sessions), ('bars.csv', bars)]:
        with (directory/name).open('w', encoding='utf-8', newline='') as file:
            writer = csv.DictWriter(file, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    cfg = {'version': 1, 'start': days[20].isoformat(), 'end': (days[-1]+timedelta(days=1)).isoformat(),
           'bars': 'bars.csv', 'sessions': 'sessions.csv', 'data_declaration': 'synthetic',
           'markets': {m: {'currency': c, 'initial_cash': cash, 'commission_bps': '1',
                           'sell_tax_bps': '0', 'slippage_bps': '2'}
                       for m, c, cash in [('KR', 'KRW', '10000000'), ('US', 'USD', '10000')]},
           'participants': [{'name': name, 'kind': 'demo', 'strategy': strategy} for name, strategy in
                            [('Momentum', 'momentum'), ('Contrarian', 'contrarian'), ('Defensive', 'defensive'),
                             ('Equal', 'equal'), ('Rotation', 'rotate'), ('Cash', 'cash')]]}
    (directory/'config.json').write_text(json.dumps(cfg, indent=2)+'\n', encoding='utf-8')
    return directory/'config.json'
