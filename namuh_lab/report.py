import csv
import html
import json
from pathlib import Path
from .common import canonical, minor
from .storage import Store

LIMITATIONS = [
    'Historical replay is not a forward test. A current language model may remember future events.',
    'Synthetic demo strategies are deterministic programs, not AI models or investment recommendations.',
    'Daily next-open fills are assumptions, not real broker fills or an order-book simulation.',
    'Fixed, user-supplied universes can have survivorship/sector bias. Validate point-in-time membership.',
    'No news, filings or browsing are included. No broker credentials or real orders are supported.',
    'Corporate splits are blocked, not silently approximated. Dividend events mean net cash payment while held.',
    'Returns average normalized market sleeves equally; no FX transfers, cash interest, borrowing or shorts.',
    'Drawdown is sampled at stored valuations, not a continuous intraday maximum drawdown.',
    'Common infrastructure failures cancel an entire round before fills. They do not restore lost opportunities.',
]


def summarize(store):
    cfg = store.get('config')
    initial = {m: minor(s['initial_cash'], s['currency']) for m, s in cfg['markets'].items()}
    models = []
    for participant in cfg['participants']:
        name = participant['name']
        valuations = store.rows('SELECT * FROM valuations WHERE name=? ORDER BY at', (name,))
        peak, mdd, series = 1, 0, []
        for row in valuations:
            equity = json.loads(row['equity'])
            normalized = sum(equity[m]/v for m, v in initial.items()) / len(initial)
            peak = max(peak, normalized)
            mdd = max(mdd, 1-normalized/peak)
            series.append({'at': row['at'], 'return_pct': (normalized-1)*100})
        last = json.loads(valuations[-1]['equity']) if valuations else initial
        requests = store.rows('SELECT status,error,cost FROM requests WHERE name=?', (name,))
        fills = store.rows('SELECT * FROM fills WHERE name=?', (name,))
        models.append({'name': name, 'return_pct': sum(last[m]/v-1 for m, v in initial.items())/len(initial)*100,
                       'market_returns': {m: (last[m]/v-1)*100 for m, v in initial.items()},
                       'sampled_mdd_pct': mdd*100, 'fills': len(fills),
                       'requests': len(requests), 'valid_outputs': sum(r['status']=='ok' for r in requests),
                       'reported_api_cost_usd': sum(r['cost'] or 0 for r in requests),
                       'unreported_api_costs': sum(r['cost'] is None for r in requests),
                       'series': series, 'portfolio': store.portfolio(name)})
    models.sort(key=lambda m: (-m['return_pct'], m['name']))
    for model in models:
        model['rank'] = 1 + sum(other['return_pct'] > model['return_pct']+1e-10 for other in models)
    rounds = store.rows('SELECT id,market,at,state,error,snapshot_hash FROM rounds ORDER BY at')
    explanations = store.rows('''SELECT r.market,r.at,q.name,q.status,q.error,q.response
                               FROM requests q JOIN rounds r ON r.id=q.round_id ORDER BY r.at,q.name''')
    return {'state': store.get('state'), 'manifest': store.get('manifest'), 'synthetic': cfg['data_declaration']=='synthetic',
            'models': models, 'rounds': rounds, 'explanations': explanations,
            'audit_failures': store.audit(), 'limitations': LIMITATIONS}


def export(directory):
    store = Store(directory)
    try:
        store.db.execute('BEGIN')
        data = summarize(store)
        root = Path(directory)
        (root/'report.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        with (root/'trades.csv').open('w', newline='', encoding='utf-8-sig') as file:
            rows = store.rows('SELECT * FROM fills ORDER BY round_id,name,symbol')
            columns = ['round_id', 'name', 'market', 'symbol', 'side', 'quantity', 'price', 'fee', 'tax']
            writer = csv.DictWriter(file, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: "'"+v if isinstance(v, str) and v[:1] in '=+-@' else v for k,v in row.items()})
        esc = lambda value: html.escape(str(value), quote=True)
        table = ''.join(f'<tr><td>{m["rank"]}</td><th>{esc(m["name"])}</th><td>{m["return_pct"]:+.2f}%</td>'
                        f'<td>{m["sampled_mdd_pct"]:.2f}%</td><td>{m["fills"]}</td>'
                        f'<td>{m["valid_outputs"]}/{m["requests"]}</td><td>${m["reported_api_cost_usd"]:.4f}</td></tr>' for m in data['models'])
        max_points = max((len(m['series']) for m in data['models']), default=1)
        values = [p['return_pct'] for m in data['models'] for p in m['series']] or [0]
        low, high = min(-1, min(values)), max(1, max(values))
        colors = ['#72e1bc', '#72b7ff', '#ffbc8d', '#bd9dff', '#ff9bab', '#a4d1dc']
        lines = []
        for i, model in enumerate(data['models']):
            points = ' '.join(f'{20+n*960/max(1,max_points-1):.1f},{250-(p["return_pct"]-low)/(high-low)*230:.1f}'
                              for n,p in enumerate(model['series']))
            lines.append(f'<polyline aria-label="{esc(model["name"])}" points="{points}" stroke="{colors[i%6]}" fill="none" stroke-width="2"/>')
        details = ''.join(f'<details><summary>{esc(x["at"])} · {esc(x["market"])} · {esc(x["name"])} · {esc(x["status"])}</summary>'
                          f'<pre>{esc(x["response"] or x["error"] or "No response")}</pre></details>' for x in data['explanations'])
        warnings = ''.join('<li>'+esc(x)+'</li>' for x in data['limitations'])
        title = 'Synthetic demo — NOT AI performance' if data['synthetic'] else 'Historical AI experiment — provisional results'
        page = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NAMUH Plug AI Invest Lab report</title><style>
:root{{font:16px/1.7 system-ui;color:#e7eff6;background:#0b111a}}body{{max-width:1120px;margin:40px auto;padding:0 24px}}
h1{{font-size:clamp(24px,4vw,44px)}}h2{{margin-top:40px}}a{{color:#8ad9be}}.note{{border-left:3px solid #d9b55e;background:#211d15;padding:16px}}
.scroll{{overflow:auto}}table{{border-collapse:collapse;width:100%;text-align:right}}td,th{{padding:14px;border-bottom:1px solid #2d3a4b;white-space:nowrap}}
th{{text-align:left}}details{{border-bottom:1px solid #2d3a4b;padding:12px 0}}summary{{cursor:pointer}}pre{{white-space:pre-wrap;word-break:break-word;font:13px/1.7 monospace;color:#b9cbdf}}
svg{{width:100%;height:auto;border:1px solid #2d3a4b}}.legend{{display:flex;flex-wrap:wrap;gap:20px}}.meta{{font:12px monospace;overflow-wrap:anywhere;color:#99abc3}}
</style><p>NAMUH Plug AI Invest Lab · 후원: NH투자증권 나무플러그</p><h1>{title}</h1><p>State: <b>{esc(data['state'])}</b>. Equal-weighted market sleeve returns, net of configured costs.</p>
<p class="note">The leaderboard includes operational failures. Read the limitations and raw decisions before drawing conclusions. No real orders were placed.</p>
<div class="scroll"><table><thead><tr><th>Rank</th><th>Participant</th><th>Return</th><th>Sampled drawdown</th><th>Fills</th><th>Valid / requests</th><th>Reported API cost</th></tr></thead><tbody>{table}</tbody></table></div>
<h2>Return path</h2><p>Range: {low:.2f}% to {high:.2f}%; stored valuation events, not intraday prices.</p>
<svg viewBox="0 0 1000 280" role="img" aria-label="Return paths">{''.join(lines)}</svg><div class="legend">{''.join(f'<span style="color:{colors[i%6]}">{esc(m["name"])}</span>' for i,m in enumerate(data['models']))}</div>
<h2>Downloads and audit</h2><p><a href="report.json">Report JSON</a> · <a href="trades.csv">Trades CSV</a></p><pre>{esc(canonical(data['audit_failures']) if data['audit_failures'] else 'Ledger and snapshot audit: PASS')}</pre>
<p class="meta">Frozen manifest: {esc(canonical(data['manifest']))}</p>
<h2>Limitations</h2><ul>{warnings}</ul><h2>Original decisions and reasons</h2>{details}</html>'''
        (root/'report.html').write_text(page, encoding='utf-8')
        return data
    finally:
        store.db.close()
