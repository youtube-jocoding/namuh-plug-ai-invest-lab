"""Inspect only distributable source files; never print matched secret values."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP = {'.git', '.venv', 'venv', '__pycache__', 'runs', 'private', 'data', 'build', 'dist'}
PATTERNS = {
    'private key': re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
    'provider key': re.compile(r'\bsk-(?:or-v1-|proj-)[A-Za-z0-9_-]{16,}'),
    'AWS access key': re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'personal mailbox': re.compile(r'\b[A-Za-z0-9._%+-]+@(?:gmail|naver|hotmail|outlook)\.[A-Za-z]{2,}\b', re.I),
    'personal home path': re.compile(r'(?:[A-Z]:[/\\]Users[/\\][^/\\\s]+|/home/[a-z][^/\s]+)', re.I),
    'public IPv4 literal': re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b'),
}


def files():
    for path in sorted(ROOT.rglob('*')):
        relative = path.relative_to(ROOT)
        if any(part in SKIP or part.endswith('.egg-info') for part in relative.parts):
            continue
        if path.is_symlink():
            raise RuntimeError('Do not distribute symlinks')
        if path.is_file():
            yield path


def check():
    errors = []
    paths = list(files())
    for path in paths:
        relative = path.relative_to(ROOT)
        if (path.name.startswith('.env') and path.name != '.env.example') or path.suffix in {'.pem','.key','.sqlite','.db','.p12'}:
            errors.append(f'{relative}: private/runtime file')
            continue
        try:
            text = path.read_text(encoding='utf-8-sig')
        except UnicodeError:
            errors.append(f'{relative}: unexpected binary')
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                errors.append(f'{relative}: possible {label}')
    return paths, errors


if __name__ == '__main__':
    paths, errors = check()
    if errors:
        print('\n'.join(errors))
        raise SystemExit(1)
    print(f'Public-source scan passed: {len(paths)} files. Manual review still required for publication.')
