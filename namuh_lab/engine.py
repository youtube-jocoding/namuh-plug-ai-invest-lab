import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import timedelta
from decimal import Decimal
from .common import LabError, canonical, code_hash, cost, decimal, digest, integer, instant, minor, stamp, strict_json, trade_price
from .providers import DemoProvider, InfrastructureError, OpenRouter, Result, SYSTEM
from .storage import Store, run_lock


def validate(content, snapshot, portfolio, cfg):
    decision = strict_json(content)
    if not isinstance(decision, dict) or set(decision) != {'portfolio_reason', 'orders'}:
        raise LabError('Decision must have exactly portfolio_reason and orders')
    if not isinstance(decision['portfolio_reason'], str) or not decision['portfolio_reason'].strip():
        raise LabError('An explanation is required even for HOLD')
    orders = decision['orders']
    if not isinstance(orders, list) or len(orders) > cfg['max_orders']:
        raise LabError('Invalid order list length')
    known = {row['symbol']: row for row in snapshot['instruments']}
    seen = set()
    simulated = copy.deepcopy(portfolio)
    market = snapshot['market']
    for order in orders:
        fields = {'symbol', 'side', 'quantity', 'selection_reason', 'timing_reason', 'quantity_reason'}
        if not isinstance(order, dict) or set(order) != fields:
            raise LabError('Order fields must exactly match the public schema')
        symbol, side = order['symbol'], order['side']
        if not isinstance(symbol, str) or symbol not in known or symbol in seen or side not in ('BUY', 'SELL'):
            raise LabError('Unknown/duplicate symbol or invalid side')
        integer(order['quantity'], 1)
        seen.add(symbol)
        if any(not isinstance(order[k], str) or not order[k].strip() for k in fields if k.endswith('_reason')):
            raise LabError('Selection, timing and quantity explanations are required')
    # Validate the entire proposed portfolio at known closes; execution rechecks gaps.
    spec = cfg['markets'][market]
    for order in sorted(orders, key=lambda o: (o['side'] != 'SELL', o['symbol'])):
        symbol, qty, side = order['symbol'], order['quantity'], order['side']
        account = simulated[market]
        held = account['positions'].get(symbol, 0)
        gross = known[symbol]['last_close'] * qty
        if side == 'SELL':
            if qty > held:
                raise LabError('Cannot sell more than held')
            account['positions'][symbol] = held - qty
            account['cash'] += gross - cost(gross, spec['commission_bps']) - cost(gross, spec['sell_tax_bps'])
        else:
            account['cash'] -= gross + cost(gross, spec['commission_bps'])
            account['positions'][symbol] = held + qty
        if account['cash'] < 0:
            raise LabError('Insufficient cash at decision prices')
    if sum(q > 0 for a in simulated.values() for q in a['positions'].values()) > cfg['max_positions']:
        raise LabError('Maximum position count exceeded')
    return decision


def execute(store, round_id, session, dataset, decisions, cfg):
    market = session['market']
    spec = cfg['markets'][market]
    snapshot = json.loads(store.db.execute('SELECT snapshot FROM rounds WHERE id=?', (round_id,)).fetchone()[0])
    infos = {row['symbol']: row for row in snapshot['instruments']}
    with store.db:
        for name, decision in decisions.items():
            if decision is None:
                continue
            portfolio = copy.deepcopy(store.portfolio(name))
            account = portfolio[market]
            plan, failure = [], None
            for order in sorted(decision['orders'], key=lambda o: (o['side'] != 'SELL', o['symbol'])):
                symbol, requested, side = order['symbol'], order['quantity'], order['side']
                bar = dataset.at_open((market, symbol), session['open'])
                cap = int(decimal(infos[symbol]['median_volume']) * decimal(cfg['liquidity_participation']))
                qty = min(requested, cap) if bar['tradable'] else 0
                if qty == 0:
                    plan.append((order, 0, 0, 0, 0))
                    continue
                price = trade_price(bar['open'], side, spec['slippage_bps'])
                if price <= 0:
                    raise LabError('Costs/slippage produce a nonpositive execution price')
                gross = qty * price
                fee = cost(gross, spec['commission_bps'])
                tax = cost(gross, spec['sell_tax_bps']) if side == 'SELL' else 0
                held = account['positions'].get(symbol, 0)
                if side == 'BUY':
                    if gross + fee > account['cash']:
                        failure = 'Opening gap exceeds cash; whole participant batch rejected'
                        break
                    account['cash'] -= gross + fee
                    account['positions'][symbol] = held + qty
                else:
                    if qty > held:
                        failure = 'Execution quantity exceeds holdings'
                        break
                    account['cash'] += gross - fee - tax
                    account['positions'][symbol] = held - qty
                if sum(q > 0 for a in portfolio.values() for q in a['positions'].values()) > cfg['max_positions']:
                    failure = 'Unfilled/partial sale leaves position limit occupied'
                    break
                plan.append((order, qty, price, fee, tax))
            if failure:
                for order in decision['orders']:
                    store.db.execute('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?)',
                        (round_id, name, order['symbol'], order['side'], order['quantity'], canonical(order), 'rejected', failure))
                continue
            for order, qty, price, fee, tax in plan:
                symbol, side = order['symbol'], order['side']
                state = 'expired' if not qty else 'filled' if qty == order['quantity'] else 'partial_expired'
                reason = 'No opening liquidity/halted' if not qty else 'Remainder expired' if state == 'partial_expired' else None
                store.db.execute('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?)',
                    (round_id, name, symbol, side, order['quantity'], canonical(order), state, reason))
                if qty:
                    store.db.execute('INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?)',
                        (round_id, name, market, symbol, side, qty, price, fee, tax))
                    cash = -qty * price - fee if side == 'BUY' else qty * price - fee - tax
                    store.post(round_id, name, market, symbol, cash, qty if side == 'BUY' else -qty)
        store.db.execute("UPDATE rounds SET state='complete' WHERE id=?", (round_id,))


def run_round(store, session, dataset, cfg, provider):
    round_id = digest([session['market'], stamp(session['open'])])[:20]
    if store.db.execute('SELECT 1 FROM rounds WHERE id=?', (round_id,)).fetchone():
        return
    snapshot = dataset.snapshot(session)
    shared_hash = digest(snapshot)
    packets = {}
    for participant in cfg['participants']:
        name = participant['name']
        history = store.rows('''SELECT response,status,error FROM requests WHERE name=?
            ORDER BY rowid DESC LIMIT 4''', (name,))
        # Only own outputs are memory; raw future execution prices never enter.
        packet = {'common': snapshot, 'snapshot_hash': shared_hash,
                  'portfolio': store.portfolio(name), 'own_history': history,
                  'rules': {'max_positions': cfg['max_positions'], 'max_orders': cfg['max_orders'],
                            'costs': cfg['markets'][session['market']]}}
        if len(canonical(packet).encode()) > cfg['max_prompt_bytes']:
            raise LabError('Common prompt budget exceeded before model calls; shorten dataset/window')
        packets[name] = packet
    with store.db:
        store.db.execute('INSERT INTO rounds VALUES(?,?,?,?,?,?,?)',
                         (round_id, session['market'], stamp(session['open']), canonical(snapshot), shared_hash, 'inflight', None))
    try:
        unknown = store.db.execute('''SELECT COUNT(*) FROM requests r LEFT JOIN cost_resolutions c
          ON r.round_id=c.round_id AND r.name=c.name WHERE r.cost IS NULL AND c.cost IS NULL''').fetchone()[0]
        if unknown:
            raise InfrastructureError('Unresolved API cost: use resolve-cost before resuming')
        spent = store.db.execute('''SELECT COALESCE(SUM(COALESCE(r.cost,c.cost)),0) FROM requests r
          LEFT JOIN cost_resolutions c ON r.round_id=c.round_id AND r.name=c.name''').fetchone()[0]
        provider.preflight(cfg['participants'], packets, cfg, spent)
    except InfrastructureError as exc:
        with store.db:
            store.db.execute("UPDATE rounds SET state='aborted',error=? WHERE id=?", (str(exc), round_id))
            store.set('state', 'blocked')
        raise LabError(str(exc)) from None
    with store.db:
        for name, packet in packets.items():
            store.db.execute('INSERT INTO requests(round_id,name,packet,status) VALUES(?,?,?,?)',
                             (round_id, name, canonical(packet), 'pending'))
    started = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=len(cfg['participants']))
    jobs = {pool.submit(provider.decide, p, packets[p['name']], cfg): p['name'] for p in cfg['participants']}
    done, pending = wait(jobs, timeout=cfg['timeout_seconds'])
    results = {}
    for job, name in jobs.items():
        if job in pending:
            results[name] = Result(status='infrastructure', error='Common response deadline exceeded', cost=None)
        else:
            try:
                results[name] = job.result()
            except Exception:
                results[name] = Result(status='infrastructure', error='Participant adapter failed', cost=None)
    # The built-in HTTP adapter is bounded. Do not start a new round while a
    # potentially billed late request is still executing.
    pool.shutdown(wait=True, cancel_futures=True)
    decisions = {}
    for name, result in results.items():
        decision = None
        if result.cost is None:
            result.status = 'infrastructure'
            result.error = result.error or 'Missing cost; common round blocked for billing review'
        if result.status == 'ok':
            try:
                decision = validate(result.content, snapshot, packets[name]['portfolio'], cfg)
            except LabError as exc:
                result.status, result.error = 'invalid', str(exc)
        decisions[name] = decision
        with store.db:
            store.db.execute('UPDATE requests SET status=?,response=?,error=?,cost=?,elapsed=? WHERE round_id=? AND name=?',
                (result.status, result.content, result.error or None, result.cost, result.elapsed, round_id, name))
    if any(r.status == 'infrastructure' for r in results.values()):
        with store.db:
            store.db.execute("UPDATE rounds SET state='aborted',error='Common infrastructure failure; no participant fills' WHERE id=?", (round_id,))
            store.set('state', 'blocked')
        raise LabError('Common infrastructure failure; round aborted, inspect costs before resume')
    with store.db:
        store.db.execute("UPDATE rounds SET state='decided' WHERE id=?", (round_id,))
    execute(store, round_id, session, dataset, decisions, cfg)


def run(cfg, dataset, directory, provider=None, resume=False):
    safe_cfg = copy.deepcopy(cfg)
    for key in ('bars', 'sessions', 'actions'):
        if safe_cfg.get(key):
            safe_cfg[key] = key + '.csv'  # Machine-specific paths are not provenance.
    manifest = {'version': 1, 'code_hash': code_hash(), 'config_hash': digest(safe_cfg),
                'data_hash': dataset.hash, 'prompt_hash': digest(SYSTEM)}
    with run_lock(directory):
        from pathlib import Path
        exists = (Path(directory) / 'experiment.sqlite').exists()
        if exists and not resume:
            raise LabError('Output already exists; use --resume or a NEW directory')
        if not exists and resume:
            raise LabError('Cannot resume a nonexistent run')
        store = Store(directory, safe_cfg, manifest)
        try:
            if store.get('state') == 'complete':
                return
            store.recover()
            provider = provider or (DemoProvider() if cfg['participants'][0]['kind'] == 'demo' else OpenRouter())
            events = []
            for session in dataset.active:
                available = max(dataset.at_open(k, session['open'])['available_at'] for k in dataset.keys(session['market']))
                events.extend([(session['open'], 1, 'open', session), (available, 2, 'close', session)])
            for action in dataset.actions:
                if instant(cfg['start']) <= action['at'] < instant(cfg['end']):
                    events.append((action['at'], 0, 'action', action))
            with store.db:
                store.set('state', 'running')
                # Known starting marks, even when markets open at different times.
                first = min(s['open'] for s in dataset.active) - timedelta(seconds=cfg['decision_lead_seconds'])
                for key in dataset.bars:
                    history = dataset.history(key, first)
                    if not history:
                        raise LabError('Each instrument needs an initial available mark')
                    store.db.execute('INSERT OR IGNORE INTO marks VALUES(?,?,?,?)', (*key, history[-1]['close'], stamp(first)))
                if not store.db.execute('SELECT 1 FROM valuations').fetchone():
                    store.value(stamp(first), cfg)
            for at, _, kind, payload in sorted(events, key=lambda e: (e[0], e[1], str(e[3]))):
                identity = payload['market'] if kind != 'action' else [payload['key'], payload['kind'], payload['value']]
                event_id = digest([stamp(at), kind, identity])[:24]
                if store.db.execute('SELECT 1 FROM events WHERE id=?', (event_id,)).fetchone():
                    continue
                if kind == 'open':
                    run_round(store, payload, dataset, cfg, provider)
                with store.db:
                    if kind == 'close':
                        for key in dataset.keys(payload['market']):
                            bar = dataset.at_open(key, payload['open'])
                            store.db.execute('INSERT OR REPLACE INTO marks VALUES(?,?,?,?)', (*key, bar['close'], stamp(at)))
                        store.value(stamp(at), cfg)
                    elif kind == 'action':
                        market, symbol = payload['key']
                        amount = minor(payload['value'], cfg['markets'][market]['currency'])
                        for p in cfg['participants']:
                            held = store.portfolio(p['name'])[market]['positions'].get(symbol, 0)
                            store.post(event_id, p['name'], market, symbol, amount * held, 0)
                        store.value(stamp(at), cfg)
                    store.db.execute('INSERT INTO events VALUES(?)', (event_id,))
            failures = store.audit()
            if failures:
                raise LabError('; '.join(failures))
            with store.db:
                store.set('state', 'complete')
        except BaseException:
            with store.db:
                store.set('state', 'blocked')
            raise
        finally:
            store.db.close()
