import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from .common import LabError, canonical, minor

SCHEMA = '''
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rounds(id TEXT PRIMARY KEY,market TEXT NOT NULL,at TEXT NOT NULL,
 snapshot TEXT NOT NULL,snapshot_hash TEXT NOT NULL,state TEXT NOT NULL,error TEXT);
CREATE TABLE IF NOT EXISTS requests(round_id TEXT,name TEXT,packet TEXT NOT NULL,status TEXT NOT NULL,
 response TEXT,error TEXT,cost REAL,elapsed REAL,PRIMARY KEY(round_id,name));
CREATE TABLE IF NOT EXISTS accounts(name TEXT,market TEXT,cash INTEGER NOT NULL CHECK(cash>=0),PRIMARY KEY(name,market));
CREATE TABLE IF NOT EXISTS positions(name TEXT,market TEXT,symbol TEXT,quantity INTEGER NOT NULL CHECK(quantity>=0),PRIMARY KEY(name,market,symbol));
CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY,event TEXT,name TEXT,market TEXT,symbol TEXT,
 cash_delta INTEGER NOT NULL,quantity_delta INTEGER NOT NULL,UNIQUE(event,name,market,symbol));
CREATE TABLE IF NOT EXISTS orders(round_id TEXT,name TEXT,symbol TEXT,side TEXT,quantity INTEGER,
 reason TEXT,status TEXT,rejection TEXT,PRIMARY KEY(round_id,name,symbol));
CREATE TABLE IF NOT EXISTS fills(round_id TEXT,name TEXT,market TEXT,symbol TEXT,side TEXT,
 quantity INTEGER,price INTEGER,fee INTEGER,tax INTEGER,PRIMARY KEY(round_id,name,symbol));
CREATE TABLE IF NOT EXISTS marks(market TEXT,symbol TEXT,price INTEGER,at TEXT,PRIMARY KEY(market,symbol));
CREATE TABLE IF NOT EXISTS valuations(at TEXT,name TEXT,equity TEXT,cash TEXT,PRIMARY KEY(at,name));
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS cost_resolutions(round_id TEXT,name TEXT,cost REAL NOT NULL,note TEXT NOT NULL,
 PRIMARY KEY(round_id,name),FOREIGN KEY(round_id,name) REFERENCES requests(round_id,name));
CREATE TRIGGER IF NOT EXISTS no_ledger_update BEFORE UPDATE ON ledger BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
CREATE TRIGGER IF NOT EXISTS no_ledger_delete BEFORE DELETE ON ledger BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
CREATE TRIGGER IF NOT EXISTS no_fill_update BEFORE UPDATE ON fills BEGIN SELECT RAISE(ABORT,'immutable fills'); END;
CREATE TRIGGER IF NOT EXISTS no_fill_delete BEFORE DELETE ON fills BEGIN SELECT RAISE(ABORT,'immutable fills'); END;
CREATE TRIGGER IF NOT EXISTS no_packet_update BEFORE UPDATE OF packet ON requests BEGIN SELECT RAISE(ABORT,'immutable prompt'); END;
CREATE TRIGGER IF NOT EXISTS no_snapshot_update BEFORE UPDATE OF snapshot,snapshot_hash ON rounds BEGIN SELECT RAISE(ABORT,'immutable snapshot'); END;
'''


@contextmanager
def run_lock(directory):
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    file = open(path / '.run.lock', 'a+b')
    file.seek(0, 2)
    if file.tell() == 0:
        file.write(b'0')
        file.flush()
    file.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        file.close()
        raise LabError('Another process owns this run directory') from exc
    try:
        yield
    finally:
        file.close()  # OS releases the lock after a crash, too.


class Store:
    def __init__(self, directory, cfg=None, manifest=None):
        self.directory = Path(directory)
        self.db = sqlite3.connect(self.directory / 'experiment.sqlite', timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if manifest is not None:
            existing = self.get('manifest')
            if existing and existing != manifest:
                self.db.close()
                raise LabError('Frozen code/config/data hashes differ: create a new run directory')
            if not existing:
                with self.db:
                    self.set('manifest', manifest)
                    self.set('config', cfg)
                    self.set('state', 'running')
                    for p in cfg['participants']:
                        for market, spec in cfg['markets'].items():
                            cash = minor(spec['initial_cash'], spec['currency'])
                            self.db.execute('INSERT INTO accounts VALUES(?,?,?)', (p['name'], market, cash))
                            self.db.execute('INSERT INTO ledger(event,name,market,symbol,cash_delta,quantity_delta) VALUES(?,?,?,?,?,?)',
                                            ('initial', p['name'], market, '', cash, 0))

    def get(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, canonical(value)))

    def rows(self, query, args=()):
        return [dict(row) for row in self.db.execute(query, args)]

    def portfolio(self, name):
        result = {r['market']: {'cash': r['cash'], 'positions': {}} for r in
                  self.rows('SELECT * FROM accounts WHERE name=?', (name,))}
        for row in self.rows('SELECT * FROM positions WHERE name=? AND quantity>0', (name,)):
            result[row['market']]['positions'][row['symbol']] = row['quantity']
        return result

    def post(self, event, name, market, symbol, cash_delta, quantity_delta):
        self.db.execute('INSERT INTO ledger(event,name,market,symbol,cash_delta,quantity_delta) VALUES(?,?,?,?,?,?)',
                        (event, name, market, symbol, cash_delta, quantity_delta))
        self.db.execute('UPDATE accounts SET cash=cash+? WHERE name=? AND market=?', (cash_delta, name, market))
        if symbol:
            exists = self.db.execute('SELECT 1 FROM positions WHERE name=? AND market=? AND symbol=?',
                                     (name, market, symbol)).fetchone()
            if exists:
                self.db.execute('UPDATE positions SET quantity=quantity+? WHERE name=? AND market=? AND symbol=?',
                                (quantity_delta, name, market, symbol))
            else:
                self.db.execute('INSERT INTO positions VALUES(?,?,?,?)', (name, market, symbol, quantity_delta))

    def recover(self):
        # A paid request may have completed just before a crash. Never replay it.
        with self.db:
            pending = self.rows("SELECT id FROM rounds WHERE state IN ('inflight','decided')")
            for row in pending:
                self.db.execute("UPDATE rounds SET state='aborted',error='Interrupted; no automatic replay' WHERE id=?", (row['id'],))
                self.db.execute("UPDATE requests SET status='interrupted',error='Interrupted' WHERE round_id=? AND status='pending'", (row['id'],))

    def value(self, at, cfg):
        for p in cfg['participants']:
            name = p['name']
            portfolio = self.portfolio(name)
            equity = {}
            for market, account in portfolio.items():
                total = account['cash']
                for symbol, quantity in account['positions'].items():
                    mark = self.db.execute('SELECT price FROM marks WHERE market=? AND symbol=?', (market, symbol)).fetchone()
                    if not mark:
                        raise LabError('Missing valuation mark')
                    total += quantity * mark[0]
                equity[market] = total
            self.db.execute('INSERT OR REPLACE INTO valuations VALUES(?,?,?,?)',
                            (at, name, canonical(equity), canonical({m: a['cash'] for m, a in portfolio.items()})))

    def audit(self):
        failures = []
        for row in self.rows('SELECT * FROM accounts'):
            actual = self.db.execute('SELECT SUM(cash_delta) FROM ledger WHERE name=? AND market=?', (row['name'], row['market'])).fetchone()[0]
            if actual != row['cash']:
                failures.append('Cash ledger mismatch')
        for row in self.rows('SELECT * FROM positions'):
            actual = self.db.execute('SELECT SUM(quantity_delta) FROM ledger WHERE name=? AND market=? AND symbol=?',
                                     (row['name'], row['market'], row['symbol'])).fetchone()[0]
            if actual != row['quantity']:
                failures.append('Position ledger mismatch')
        for row in self.rows('SELECT snapshot,snapshot_hash FROM rounds'):
            from .common import digest
            if digest(json.loads(row['snapshot'])) != row['snapshot_hash']:
                failures.append('Snapshot hash mismatch')
        bad = self.db.execute("SELECT COUNT(*) FROM fills f JOIN rounds r ON f.round_id=r.id WHERE r.state!='complete'").fetchone()[0]
        if bad:
            failures.append('Fill in an aborted/incomplete round')
        if self.db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            failures.append('SQLite integrity check failed')
        return failures
