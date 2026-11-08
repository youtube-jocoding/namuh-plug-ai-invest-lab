import json
import re
from pathlib import Path
from .common import LabError, decimal, integer, instant, minor


def load(path):
    path = Path(path).resolve()
    cfg = json.loads(path.read_text(encoding='utf-8'))
    allowed = {'version', 'start', 'end', 'bars', 'sessions', 'actions', 'data_declaration',
               'markets', 'participants', 'warmup_bars', 'max_positions', 'max_orders',
               'decision_lead_seconds', 'timeout_seconds', 'max_output_tokens',
               'liquidity_participation', 'max_api_budget_usd', 'credit_reserve_usd', 'max_prompt_bytes'}
    if set(cfg) - allowed or cfg.get('version') != 1:
        raise LabError('Unknown configuration field or unsupported version')
    if instant(cfg['start']) >= instant(cfg['end']):
        raise LabError('start must precede end (end is exclusive)')
    for key, default in [('warmup_bars', 20), ('max_positions', 10), ('max_orders', 20),
                         ('decision_lead_seconds', 60), ('timeout_seconds', 180),
                         ('max_output_tokens', 4096), ('max_prompt_bytes', 200000)]:
        cfg.setdefault(key, default)
        integer(cfg[key], 1)
    cfg.setdefault('liquidity_participation', '0.001')
    if not 0 < decimal(cfg['liquidity_participation']) <= 1:
        raise LabError('liquidity_participation must be in (0, 1]')
    for key in ('max_api_budget_usd', 'credit_reserve_usd'):
        cfg.setdefault(key, '10')
        if decimal(cfg[key]) <= 0:
            raise LabError(f'{key} must be positive')
    if cfg.get('data_declaration') not in ('unadjusted_actions_supplied', 'unadjusted_no_actions_confirmed', 'synthetic'):
        raise LabError('Declare unadjusted data and corporate actions explicitly')
    markets = cfg['markets']
    if not markets:
        raise LabError('At least one market required')
    for name, market in markets.items():
        if set(market) != {'currency', 'initial_cash', 'commission_bps', 'sell_tax_bps', 'slippage_bps'}:
            raise LabError('Unknown or missing market configuration field')
        if not re.fullmatch(r'[A-Z0-9_-]{1,12}', name) or market['currency'] not in ('KRW', 'USD'):
            raise LabError('Invalid market/currency')
        if minor(market['initial_cash'], market['currency']) <= 0:
            raise LabError('Initial cash must be positive')
        for field in ('commission_bps', 'sell_tax_bps', 'slippage_bps'):
            if not 0 <= decimal(market[field]) < 10000:
                raise LabError('Cost basis points must be in [0, 10000)')
    participants = cfg['participants']
    if not 2 <= len(participants) <= 20:
        raise LabError('Use 2 to 20 participants')
    names = [p['name'] for p in participants]
    if len(set(names)) != len(names) or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', n) for n in names):
        raise LabError('Participant names must be unique safe identifiers')
    strategies = {'momentum', 'contrarian', 'defensive', 'equal', 'rotate', 'cash'}
    for p in participants:
        if set(p) - {'name', 'kind', 'strategy', 'model', 'provider', 'provider_name', 'parameters'}:
            raise LabError('Unknown participant configuration field; never put credentials in config')
        if p.get('kind') == 'demo':
            if p.get('strategy') not in strategies:
                raise LabError('Unknown deterministic demo strategy')
        elif p.get('kind') == 'openrouter':
            if not all(p.get(k) and 'REPLACE' not in p[k] for k in ('model', 'provider', 'provider_name')):
                raise LabError('Set exact model ID, provider slug, and response provider name')
            if set(p.get('parameters', {})) - {'temperature', 'reasoning'}:
                raise LabError('Only temperature/reasoning overrides are allowed')
        else:
            raise LabError('Unknown participant kind')
    if len({p['kind'] for p in participants}) != 1:
        raise LabError('Do not mix demonstration strategies and AI models in a ranked run')
    for key in ('bars', 'sessions', 'actions'):
        if cfg.get(key):
            cfg[key] = str((path.parent / cfg[key]).resolve())
    if cfg['data_declaration'] == 'unadjusted_actions_supplied' and not cfg.get('actions'):
        raise LabError('Actions file required by the data declaration')
    return cfg
