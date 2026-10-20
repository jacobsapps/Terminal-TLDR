"""Copy only upstream dependency notices into the distributable app."""
from importlib.metadata import distribution
from pathlib import Path
import sys
import sysconfig
import urllib.request

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
for package, filename in [('protobuf', 'Protobuf'), ('websockets', 'Websockets'), ('pyinstaller', 'PyInstaller')]:
    dist = distribution(package)
    for relative in dist.files:
        if relative.name in ('LICENSE', 'COPYING.txt') and '.dist-info' in str(relative):
            (out / (filename + '.txt')).write_bytes(dist.locate_file(relative).read_bytes())
            break
    else:
        raise SystemExit('Missing licence: ' + package)
python_license = Path(sysconfig.get_path('stdlib')) / 'LICENSE.txt'
if not python_license.exists():
    raise SystemExit('Missing Python licence')
(out / 'Python.txt').write_bytes(python_license.read_bytes())
# Pin to the API package's upstream release rather than mutable main.
url = 'https://raw.githubusercontent.com/gnachman/iTerm2/v3.6.9/LICENSE'
with urllib.request.urlopen(url, timeout=20) as response:
    (out / 'iTerm2.txt').write_bytes(response.read())
