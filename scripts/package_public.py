"""Build a source-only zip after privacy checks; excludes all runtime output."""
import hashlib
import sys
import zipfile
from pathlib import Path
from check_public import ROOT, check

paths, errors = check()
if errors:
    raise SystemExit('\n'.join(errors))
destination = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT.parent/'namuh-plug-ai-invest-lab-source.zip'
if destination.exists():
    raise SystemExit('Archive already exists; choose a new output path')
if destination.is_relative_to(ROOT):
    raise SystemExit('Keep the archive outside the source tree')
with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
    for path in paths:
        archive.write(path, 'namuh-plug-ai-invest-lab/'+path.relative_to(ROOT).as_posix())
print(f'Created {destination.name}: {len(paths)} public files')
print('SHA256 '+hashlib.sha256(destination.read_bytes()).hexdigest())
