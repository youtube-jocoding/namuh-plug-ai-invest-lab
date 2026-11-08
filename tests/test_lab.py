import copy
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from namuh_lab.common import LabError, canonical, digest, instant, minor, strict_json
from namuh_lab.config import load
from namuh_lab.data import Dataset
from namuh_lab.demo import create
from namuh_lab.engine import execute, run, run_round, validate
from namuh_lab.providers import DemoProvider, InfrastructureError, Result
from namuh_lab.quotes import Quote, after_precision_boundary, collect, valid
from namuh_lab.report import export
from namuh_lab.storage import Store, run_lock


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = create(self.root/'inputs')
        self.cfg = load(self.path)
        self.data = Dataset(self.cfg)

    def tearDown(self):
        self.temp.cleanup()

    def store(self):
        folder = self.root/'unit'
        folder.mkdir(exist_ok=True)
        return Store(folder, self.cfg, {'test': True})


class DataTests(Fixture):
    def test_current_and_future_prices_never_enter_prompt(self):
        session = self.data.active[0]
        before = self.data.snapshot(session)
        for key in self.data.keys(session['market']):
            self.data.at_open(key, session['open'])['close'] = 987654321
            self.data.at_open(key, session['open'])['volume'] = 999999999
            self.data.at_open(key, session['open'])['open'] = 222222222
        self.assertEqual(before, self.data.snapshot(session))
        for row in before['instruments']:
            for bar in row['bars']:
                self.assertLessEqual(instant(bar['available_at']), instant(before['cutoff']))

    def test_late_published_bar_is_not_available(self):
        session = self.data.active[0]
        key = self.data.keys(session['market'])[0]
        history = self.data.history(key, session['open'])
        history[-1]['available_at'] = session['open'] + timedelta(days=1)
        snap = self.data.snapshot(session)
        target = next(x for x in snap['instruments'] if x['symbol'] == key[1])
        self.assertNotIn(history[-1]['close_time'].isoformat(), canonical(target))

    def test_missing_session_fails_before_execution(self):
        bars = Path(self.cfg['bars']).read_text().splitlines()
        Path(self.cfg['bars']).write_text('\n'.join(bars[:-1])+'\n')
        with self.assertRaisesRegex(LabError, 'Missing session'):
            Dataset(self.cfg)

    def test_naive_timestamp_and_fractional_minor_units_rejected(self):
        with self.assertRaises(LabError):
            instant('2024-01-01T09:00:00')
        with self.assertRaises(LabError):
            minor('10.001', 'USD')

    def test_duplicate_json_key_is_not_silently_overwritten(self):
        with self.assertRaises(LabError):
            strict_json('{"orders":[],"orders":[1]}')

    def test_duplicate_bar_rejected(self):
        path = Path(self.cfg['bars'])
        lines = path.read_text().splitlines()
        path.write_text('\n'.join(lines+[lines[1]])+'\n')
        with self.assertRaisesRegex(LabError, 'Duplicate'):
            Dataset(self.cfg)

    def test_split_blocks_instead_of_fake_accounting(self):
        action = self.root/'actions.csv'
        action.write_text('market,symbol,kind,effective_at,published_at,value\nKR,KR1,split,2024-02-01T00:00:00Z,2024-01-01T00:00:00Z,2\n')
        self.cfg['actions'] = str(action)
        with self.assertRaisesRegex(LabError, 'Split detected'):
            Dataset(self.cfg)


class EngineTests(Fixture):
    def test_late_response_aborts_every_participant(self):
        class Slow(DemoProvider):
            def decide(self, p, packet, cfg):
                if p['name'] == 'Cash':
                    time.sleep(1.1)
                return super().decide(p, packet, cfg)
        self.cfg['timeout_seconds'] = 1
        s = self.store()
        try:
            with self.assertRaises(LabError):
                run_round(s, self.data.active[0], self.data, self.cfg, Slow())
            self.assertEqual(s.db.execute('SELECT COUNT(*) FROM fills').fetchone()[0], 0)
            self.assertIsNone(s.db.execute("SELECT cost FROM requests WHERE name='Cash'").fetchone()[0])
        finally:
            s.db.close()

    def test_partial_fills_use_prior_volume_and_expire_once(self):
        self.cfg['liquidity_participation'] = '0.000001'
        output = self.root/'partial'
        run(self.cfg, self.data, output)
        s = Store(output)
        try:
            self.assertGreater(s.db.execute("SELECT COUNT(*) FROM orders WHERE status='partial_expired'").fetchone()[0], 0)
            self.assertEqual(s.db.execute('SELECT MAX(quantity) FROM fills').fetchone()[0], 1)
            self.assertFalse(s.audit())
        finally:
            s.db.close()

    def test_net_cash_event_updates_terminal_equity(self):
        final = max(s['close'] for s in self.data.active)+timedelta(seconds=1)
        self.data.actions = [{'key': ('KR', 'KR1'), 'at': final, 'published': final-timedelta(days=10),
                              'kind': 'cash_dividend', 'value': '100'}]
        output = self.root/'cash-event'
        run(self.cfg, self.data, output)
        s = Store(output)
        try:
            portfolio = s.portfolio('Equal')['KR']
            last = json.loads(s.db.execute("SELECT equity FROM valuations WHERE name='Equal' ORDER BY at DESC LIMIT 1").fetchone()[0])['KR']
            expected = portfolio['cash']
            for symbol, qty in portfolio['positions'].items():
                expected += qty*s.db.execute('SELECT price FROM marks WHERE market=? AND symbol=?', ('KR', symbol)).fetchone()[0]
            self.assertEqual(last, expected)
            self.assertFalse(s.audit())
        finally:
            s.db.close()

    def test_demo_reproducible_and_idempotent(self):
        first, second = self.root/'one', self.root/'two'
        run(self.cfg, self.data, first)
        run(self.cfg, self.data, second)
        a, b = export(first), export(second)
        self.assertEqual(a, b)
        self.assertFalse(a['audit_failures'])
        self.assertEqual(len(a['models']), 6)
        run(self.cfg, self.data, first, resume=True)
        self.assertEqual(a, export(first))
        with self.assertRaisesRegex(LabError, 'already exists'):
            run(self.cfg, self.data, first)

    def test_all_models_receive_same_market_snapshot(self):
        s = self.store()
        try:
            run_round(s, self.data.active[0], self.data, self.cfg, DemoProvider())
            rows = s.rows('SELECT packet FROM requests')
            hashes = {json.loads(r['packet'])['snapshot_hash'] for r in rows}
            inputs = {canonical(json.loads(r['packet'])['common']) for r in rows}
            self.assertEqual(len(rows), 6)
            self.assertEqual(len(hashes), 1)
            self.assertEqual(len(inputs), 1)
        finally:
            s.db.close()

    def test_one_credit_failure_cancels_everyones_fills(self):
        class Failure(DemoProvider):
            def decide(self, p, packet, cfg):
                if p['name'] == 'Cash':
                    return Result(status='infrastructure', error='HTTP 402', cost=0)
                return super().decide(p, packet, cfg)
        s = self.store()
        try:
            with self.assertRaisesRegex(LabError, 'Common infrastructure'):
                run_round(s, self.data.active[0], self.data, self.cfg, Failure())
            self.assertEqual(s.db.execute('SELECT COUNT(*) FROM fills').fetchone()[0], 0)
            self.assertEqual(s.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 6)
            self.assertEqual(s.db.execute('SELECT state FROM rounds').fetchone()[0], 'aborted')
        finally:
            s.db.close()

    def test_credit_preflight_fails_without_model_calls(self):
        class Failure(DemoProvider):
            calls = 0
            def preflight(self, *args):
                raise InfrastructureError('Insufficient credits')
            def decide(self, *args):
                self.calls += 1
                return Result()
        provider = Failure()
        s = self.store()
        try:
            with self.assertRaises(LabError):
                run_round(s, self.data.active[0], self.data, self.cfg, provider)
            self.assertEqual(provider.calls, 0)
            self.assertEqual(s.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 0)
        finally:
            s.db.close()

    def test_invalid_model_output_does_not_abort_other_models(self):
        class Invalid(DemoProvider):
            def decide(self, p, packet, cfg):
                if p['name'] == 'Cash':
                    return Result(content='not JSON')
                return super().decide(p, packet, cfg)
        s = self.store()
        try:
            run_round(s, self.data.active[0], self.data, self.cfg, Invalid())
            self.assertEqual(s.db.execute('SELECT state FROM rounds').fetchone()[0], 'complete')
            self.assertGreater(s.db.execute('SELECT COUNT(*) FROM fills').fetchone()[0], 0)
            self.assertEqual(s.db.execute("SELECT status FROM requests WHERE name='Cash'").fetchone()[0], 'invalid')
        finally:
            s.db.close()

    def test_price_gap_rejects_whole_batch_without_partial_posting(self):
        session = self.data.active[0]
        s = self.store()
        snap = self.data.snapshot(session)
        symbols = [x['symbol'] for x in snap['instruments'][:2]]
        decision = {'portfolio_reason': 'test', 'orders': [DemoProvider.order(x, 'BUY', 10) for x in symbols]}
        bar = self.data.at_open((session['market'], symbols[-1]), session['open'])
        bar['open'] = 999999999
        try:
            with s.db:
                s.db.execute('INSERT INTO rounds VALUES(?,?,?,?,?,?,?)', ('gap', session['market'], session['open'].isoformat(), canonical(snap), digest(snap), 'decided', None))
            execute(s, 'gap', session, self.data, {'Momentum': decision}, self.cfg)
            self.assertEqual(s.db.execute('SELECT COUNT(*) FROM fills').fetchone()[0], 0)
            self.assertEqual(s.db.execute("SELECT COUNT(*) FROM orders WHERE status='rejected'").fetchone()[0], 2)
            self.assertFalse(s.audit())
        finally:
            s.db.close()

    def test_interrupted_round_is_not_replayed(self):
        session = self.data.active[0]
        s = self.store()
        snap = self.data.snapshot(session)
        rid = digest([session['market'], session['open'].isoformat()])[:20]
        try:
            with s.db:
                s.db.execute('INSERT INTO rounds VALUES(?,?,?,?,?,?,?)', (rid, session['market'], session['open'].isoformat(), canonical(snap), digest(snap), 'inflight', None))
            s.recover()
            class Never(DemoProvider):
                def decide(self, *args):
                    raise AssertionError('Must not replay')
            run_round(s, session, self.data, self.cfg, Never())
            self.assertEqual(s.db.execute('SELECT state FROM rounds').fetchone()[0], 'aborted')
        finally:
            s.db.close()

    def test_configuration_change_blocks_resume(self):
        output = self.root/'resume'
        run(self.cfg, self.data, output)
        changed = copy.deepcopy(self.cfg)
        changed['max_positions'] = 9
        with self.assertRaisesRegex(LabError, 'Frozen'):
            run(changed, Dataset(changed), output, resume=True)

    def test_reason_and_integer_quantity_validation(self):
        s = self.store()
        try:
            snap = self.data.snapshot(self.data.active[0])
            portfolio = s.portfolio('Momentum')
            order = DemoProvider.order(snap['instruments'][0]['symbol'], 'BUY', True)
            with self.assertRaises(LabError):
                validate(canonical({'portfolio_reason': 'test', 'orders': [order]}), snap, portfolio, self.cfg)
            order['quantity'] = 1
            order['quantity_reason'] = ''
            with self.assertRaises(LabError):
                validate(canonical({'portfolio_reason': 'test', 'orders': [order]}), snap, portfolio, self.cfg)
        finally:
            s.db.close()

    def test_report_escapes_untrusted_model_text(self):
        output = self.root/'escape'
        run(self.cfg, self.data, output)
        s = Store(output)
        with s.db:
            s.db.execute('UPDATE requests SET response=?', ('<script>alert(1)</script>',))
        s.db.close()
        export(output)
        text = (output/'report.html').read_text(encoding='utf-8')
        self.assertNotIn('<script>alert', text)
        self.assertIn('&lt;script&gt;', text)

    def test_ledger_cannot_be_rewritten(self):
        s = self.store()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                s.db.execute('UPDATE ledger SET cash_delta=0')
            s.db.rollback()
        finally:
            s.db.close()

    def test_second_runner_is_locked_out(self):
        folder = self.root/'locked'
        with run_lock(folder):
            with self.assertRaises(LabError):
                with run_lock(folder):
                    pass


class QuotePrecisionTests(unittest.TestCase):
    def test_next_bucket_even_when_deadline_is_exact_second(self):
        t = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(after_precision_boundary(t), t+timedelta(seconds=1))

    def test_first_symbol_is_not_penalized_by_subsecond_deadline(self):
        class Clock:
            now = datetime(2024, 1, 1, 0, 0, 0, 840000, tzinfo=timezone.utc)
            def read(self): return self.now
            def sleep(self, seconds): self.now += timedelta(seconds=seconds)
            def fetch(self, _):
                requested = self.now
                self.sleep(.03)
                return Quote(requested.replace(microsecond=0), requested, self.now, 100, 101, 100, 100, True)
        clock = Clock()
        deadline = clock.now
        quotes = collect(['ZZZ', 'AAA'], deadline, clock.fetch, clock=clock.read, sleep=clock.sleep)
        self.assertEqual(set(quotes), {'AAA', 'ZZZ'})
        self.assertTrue(all(valid(q, deadline) for q in quotes.values()))

    def test_stale_and_future_quotes_are_rejected(self):
        t = datetime(2024, 1, 1, tzinfo=timezone.utc)
        for source in [t-timedelta(seconds=60), t+timedelta(seconds=20)]:
            self.assertFalse(valid(Quote(source, t+timedelta(seconds=1), t+timedelta(seconds=2), 100, 101, 10, 10, True), t))


if __name__ == '__main__':
    unittest.main()
