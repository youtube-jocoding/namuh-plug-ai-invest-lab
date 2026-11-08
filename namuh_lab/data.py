import csv
import statistics
from datetime import timedelta
from .common import LabError, digest, instant, minor, stamp, decimal


def csv_rows(path):
    with open(path, newline='', encoding='utf-8-sig') as file:
        return list(csv.DictReader(file))


class Dataset:
    def __init__(self, cfg):
        self.cfg = cfg
        self.sessions = []
        seen = set()
        for row in csv_rows(cfg['sessions']):
            market = row['market']
            opened, closed = instant(row['open_time']), instant(row['close_time'])
            if market not in cfg['markets'] or opened >= closed or (market, opened) in seen:
                raise LabError('Invalid or duplicate trading session')
            seen.add((market, opened))
            self.sessions.append({'market': market, 'open': opened, 'close': closed})
        self.sessions.sort(key=lambda s: s['open'])
        for market in cfg['markets']:
            rows = [s for s in self.sessions if s['market'] == market]
            if any(a['close'] >= b['open'] for a, b in zip(rows, rows[1:])):
                raise LabError('Overlapping sessions within a market')
        session_map = {(s['market'], s['open']): s for s in self.sessions}
        self.bars = {}
        self.instruments = {}
        for row in csv_rows(cfg['bars']):
            market, symbol = row['market'], row['symbol']
            if market not in cfg['markets'] or not symbol or len(symbol) > 40:
                raise LabError('Invalid bar instrument')
            opened, closed, available = (instant(row[k]) for k in ('open_time', 'close_time', 'available_at'))
            session = session_map.get((market, opened))
            if not session or session['close'] != closed or available < closed:
                raise LabError('Bar/session mismatch or bar available before close')
            currency = cfg['markets'][market]['currency']
            prices = {key: minor(row[key], currency) for key in ('open', 'high', 'low', 'close')}
            if not 0 < prices['low'] <= min(prices['open'], prices['close']) <= max(prices['open'], prices['close']) <= prices['high']:
                raise LabError('Invalid positive OHLC range')
            volume = decimal(row['volume'])
            if volume < 0 or volume != volume.to_integral_value():
                raise LabError('Volume must be a nonnegative integer')
            tradable = row.get('tradable_at_open', 'true').lower()
            if tradable not in ('true', 'false'):
                raise LabError('tradable_at_open must be true/false')
            key = (market, symbol)
            info = {'name': row.get('name') or symbol, 'sector': row.get('sector') or 'Unspecified'}
            if key in self.instruments and self.instruments[key] != info:
                raise LabError('Instrument metadata changed within the input; supply consistent labels')
            self.instruments[key] = info
            bars = self.bars.setdefault(key, [])
            if any(b['open_time'] == opened for b in bars):
                raise LabError('Duplicate instrument/session bar')
            bars.append({**prices, 'volume': int(volume), 'open_time': opened,
                         'close_time': closed, 'available_at': available, 'tradable': tradable == 'true'})
        for bars in self.bars.values():
            bars.sort(key=lambda b: b['open_time'])
        self.active = [s for s in self.sessions if instant(cfg['start']) <= s['open'] < instant(cfg['end'])]
        if not self.active:
            raise LabError('No sessions in the requested range')
        for left, right in zip(self.active, self.active[1:]):
            if left['close'] > right['open'] - timedelta(seconds=cfg['decision_lead_seconds']):
                raise LabError('Overlapping market/decision sessions need a synchronized multi-market engine')
        # Strict coverage fails before ANY paid request; no silent forward-filling.
        for session in self.active:
            keys = self.keys(session['market'])
            if not keys:
                raise LabError('No instruments for active market')
            for key in keys:
                current = self.at_open(key, session['open'])
                if current['available_at'] > instant(cfg['end']):
                    raise LabError('End cuts off a session or its closing-data availability')
                cutoff = session['open'] - timedelta(seconds=cfg['decision_lead_seconds'])
                if len(self.history(key, cutoff)) < cfg['warmup_bars']:
                    raise LabError('Insufficient point-in-time warmup for every instrument')
        self.actions = []
        if cfg.get('actions'):
            for row in csv_rows(cfg['actions']):
                key = (row['market'], row['symbol'])
                at, published = instant(row['effective_at']), instant(row['published_at'])
                if key not in self.bars or published > at:
                    raise LabError('Action instrument unknown or announcement after effective time')
                if row['kind'] not in ('split', 'cash_dividend'):
                    raise LabError('Supported actions: split, cash_dividend')
                value = decimal(row['value'])
                if value <= 0:
                    raise LabError('Action value must be positive')
                if row['kind'] == 'cash_dividend':
                    minor(row['value'], cfg['markets'][key[0]]['currency'])
                self.actions.append({'key': key, 'at': at, 'published': published,
                                     'kind': row['kind'], 'value': str(value)})
        action_keys = [(a['key'], a['at'], a['kind']) for a in self.actions]
        if len(set(action_keys)) != len(action_keys):
            raise LabError('Duplicate corporate action')
        # Splits in warm-up also invalidate raw price-based indicators. Fail closed
        # rather than silently mixing pre/post-split scales.
        if any(a['kind'] == 'split' for a in self.actions):
            raise LabError('Split detected: this version requires a separately audited split-aware dataset/engine')
        self.hash = digest({'sessions': [{k: stamp(v) if k in ('open', 'close') else v for k, v in s.items()} for s in self.sessions],
                            'bars': {str(k): [self.serial(b) for b in v] for k, v in sorted(self.bars.items())},
                            'actions': [{**a, 'at': stamp(a['at']), 'published': stamp(a['published'])} for a in self.actions]})

    @staticmethod
    def serial(bar):
        return {key: stamp(value) if key.endswith('_time') or key == 'available_at' else value for key, value in bar.items()}

    def keys(self, market):
        return sorted(k for k in self.bars if k[0] == market)

    def history(self, key, cutoff):
        return [b for b in self.bars[key] if b['available_at'] <= cutoff and b['close_time'] <= cutoff]

    def at_open(self, key, at):
        bar = next((b for b in self.bars[key] if b['open_time'] == at), None)
        if bar is None:
            raise LabError('Missing session bar: supply a complete dataset; do not forward-fill')
        return bar

    def snapshot(self, session):
        cutoff = session['open'] - timedelta(seconds=self.cfg['decision_lead_seconds'])
        instruments = []
        for key in self.keys(session['market']):
            bars = self.history(key, cutoff)[-self.cfg['warmup_bars']:]
            instruments.append({'symbol': key[1], **self.instruments[key],
                                'bars': [self.serial(b) for b in bars],
                                'last_close': bars[-1]['close'],
                                'mean_close': sum(b['close'] for b in bars) // len(bars),
                                'median_volume': int(statistics.median(b['volume'] for b in bars))})
        return {'market': session['market'], 'cutoff': stamp(cutoff),
                'currency': self.cfg['markets'][session['market']]['currency'],
                'price_units': 'integer minor currency units', 'instruments': instruments,
                'news_and_filings': 'not included; no point-in-time archive supplied'}
