import argparse
import json
import sys
import urllib.request
from pathlib import Path
from .common import LabError, decimal
from .config import load
from .data import Dataset
from .demo import create
from .engine import run
from .report import export
from .storage import Store, run_lock


def existing(path):
    if not (Path(path)/'experiment.sqlite').is_file():
        raise LabError('Run database does not exist')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Auditable historical investment experiments. Never submits broker orders.')
    commands = parser.add_subparsers(dest='command', required=True)
    demo = commands.add_parser('demo', help='Generate synthetic inputs and run six deterministic strategies, offline')
    demo.add_argument('--out', default='runs/demo')
    validate = commands.add_parser('validate', help='Validate all inputs before any model calls')
    validate.add_argument('--config', required=True)
    execute = commands.add_parser('run', help='Run or resume an experiment')
    execute.add_argument('--config', required=True)
    execute.add_argument('--out', required=True)
    execute.add_argument('--resume', action='store_true')
    execute.add_argument('--allow-paid', action='store_true', help='Explicitly enable OpenRouter API spending')
    for name in ('report', 'audit'):
        cmd = commands.add_parser(name)
        cmd.add_argument('--out', required=True)
    catalog = commands.add_parser('models', help='Read current OpenRouter catalog; no inference')
    catalog.add_argument('--query', default='')
    resolve = commands.add_parser('resolve-cost', help='Append an audited manual cost from the provider billing record')
    resolve.add_argument('--out', required=True)
    resolve.add_argument('--round', required=True)
    resolve.add_argument('--participant', required=True)
    resolve.add_argument('--usd', required=True)
    resolve.add_argument('--note', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'demo':
            root = Path(args.out)
            if root.exists():
                raise LabError('Demo output already exists; choose a fresh directory')
            path = create(root/'inputs')
            cfg = load(path)
            run(cfg, Dataset(cfg), root/'run')
            export(root/'run')
            print(f'Synthetic demo complete: {root / "run" / "report.html"}')
        elif args.command in ('validate', 'run'):
            cfg = load(args.config)
            dataset = Dataset(cfg)
            print(f'Validated {len(dataset.active)} sessions; dataset hash {dataset.hash}')
            if args.command == 'run':
                if cfg['participants'][0]['kind'] == 'openrouter' and not args.allow_paid:
                    raise LabError('Paid model calls are disabled; explicitly pass --allow-paid')
                try:
                    run(cfg, dataset, args.out, resume=args.resume)
                finally:
                    if (Path(args.out)/'experiment.sqlite').exists():
                        export(args.out)
                print(f'Report: {Path(args.out)/"report.html"}')
        elif args.command == 'report':
            existing(args.out)
            export(args.out)
            print(f'Report: {Path(args.out)/"report.html"}')
        elif args.command == 'audit':
            existing(args.out)
            store = Store(args.out)
            try:
                store.db.execute('BEGIN')
                failures = store.audit()
                print(json.dumps({'passed': not failures, 'failures': failures}))
                return int(bool(failures))
            finally:
                store.db.close()
        elif args.command == 'resolve-cost':
            existing(args.out)
            value = decimal(args.usd)
            if value < 0 or len(args.note.strip()) < 10:
                raise LabError('Nonnegative cost and a meaningful billing evidence note are required')
            with run_lock(args.out):
                store = Store(args.out)
                try:
                    row = store.db.execute('SELECT cost FROM requests WHERE round_id=? AND name=?',
                                           (args.round,args.participant)).fetchone()
                    if not row or row['cost'] is not None:
                        raise LabError('Only a request with unknown cost may be resolved')
                    with store.db:
                        store.db.execute('INSERT INTO cost_resolutions VALUES(?,?,?,?)',
                            (args.round,args.participant,float(value),args.note))
                finally:
                    store.db.close()
            print('Cost resolution appended. Original response and status preserved.')
        elif args.command == 'models':
            with urllib.request.urlopen('https://openrouter.ai/api/v1/models', timeout=20) as response:
                data = json.load(response)
            for model in data['data']:
                if args.query.casefold() in model['id'].casefold():
                    print(model['id'])
        return 0
    except (LabError, ValueError, OSError, KeyError) as exc:
        # No traceback, HTTP response or secret-bearing headers are printed.
        print(f'Experiment stopped: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
