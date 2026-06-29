"""On-demand installer for the Hikvision HCNetSDK.

The HCNetSDK is proprietary and is NOT bundled with this application. To keep
the distributable free of third-party binaries, the SDK is downloaded and
installed into the per-user data directory the first time the user opts in.

The flow is fully automatic once the user confirms. Two source kinds are
supported and tried in order:

  * ``zip``        — download an archive and extract the lib/ dir from it.
  * ``github_dir`` — recursively fetch a directory of .so files via the GitHub
                     Contents API (used when no single zip is published).

No Qt dependency here so this stays unit-testable and reusable; the GUI wraps
``download_and_install`` with a progress dialog.
"""

import json
import os
import shutil
import tempfile
import zipfile
import urllib.request
from pathlib import Path

# The shared object that marks a valid SDK lib directory.
LIB_MARKER = 'libhcnetsdk.so'

# Download sources, tried in order until one succeeds.
#   - The official Hikvision link is first so we prefer the vendor, but it
#     normally returns 403 to unattended requests; we fall through gracefully.
#   - The GitHub directory of .so files is the working unattended fallback.
SOURCES = [
    {'type': 'zip',
     'url': 'https://www.hikvision.com/content/dam/hikvision/en/support/'
            'download/sdk/device-network-sdk/'
            'EN-HCNetSDKV6.1.9.4_build20220412_linux64.zip'},
    {'type': 'github_dir',
     'repo': 'manutdsnake/Hik-monitor', 'path': 'sdk/lib', 'ref': 'main'},
]

_UA = {'User-Agent': 'hikvision-monitor'}


def install_root() -> Path:
    """Base install directory (XDG-compliant; also writable inside a Flatpak
    sandbox, where HOME maps to the app's private data dir)."""
    base = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
    return Path(base) / 'hikvision-monitor' / 'sdk'


def install_lib_dir() -> Path:
    """The directory that ends up on LD_LIBRARY_PATH (contains libhcnetsdk.so
    and the HCNetSDKCom/ subdirectory)."""
    return install_root() / 'lib'


def is_installed(lib_dir=None) -> bool:
    lib_dir = Path(lib_dir) if lib_dir else install_lib_dir()
    return (lib_dir / LIB_MARKER).is_file()


# ── zip source ──────────────────────────────────────────────────────────────
def _find_lib_dir(extract_root) -> Path | None:
    """Find the directory holding libhcnetsdk.so anywhere under the extracted
    tree. Different SDK builds/mirrors nest it differently (e.g. .../lib/), so
    we search instead of assuming a fixed layout."""
    for dirpath, _dirs, files in os.walk(extract_root):
        if LIB_MARKER in files:
            return Path(dirpath)
    return None


def _http_get(url, dest_path, progress_cb=None, base=0, grand_total=0):
    """Stream a URL to a file. Reports cumulative download progress when a
    grand_total is known (``base`` = bytes already fetched before this file)."""
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        ctype = (r.headers.get('Content-Type') or '').lower()
        if 'html' in ctype:
            raise RuntimeError(f'{url} returned a web page, not a download')
        this_total = int(r.headers.get('Content-Length') or 0)
        done = 0
        with open(dest_path, 'wb') as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress_cb:
                    if grand_total:
                        progress_cb('download', (base + done) / grand_total)
                    elif this_total:
                        progress_cb('download', done / this_total)
                    else:
                        progress_cb('download', None)
    return done


def _install_from_zip(source, dest, progress_cb):
    with tempfile.TemporaryDirectory(prefix='hik-sdk-zip-') as tmp:
        zip_path = os.path.join(tmp, 'sdk.zip')
        _http_get(source['url'], zip_path, progress_cb)

        if progress_cb:
            progress_cb('extract', None)
        extract_dir = os.path.join(tmp, 'extracted')
        try:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(extract_dir)
        except zipfile.BadZipFile:
            raise RuntimeError('Downloaded file is not a valid zip archive')

        lib_src = _find_lib_dir(extract_dir)
        if lib_src is None:
            raise RuntimeError(f'Archive does not contain {LIB_MARKER}')

        if progress_cb:
            progress_cb('install', None)
        _replace_dir(lib_src, dest)


# ── github_dir source ─────────────────────────────────────────────────────────
def _gh_list(repo, path, ref):
    """Return the GitHub Contents API listing for a directory."""
    url = f'https://api.github.com/repos/{repo}/contents/{path}?ref={ref}'
    req = urllib.request.Request(url, headers={**_UA, 'Accept':
                                               'application/vnd.github+json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _gh_walk(repo, ref, path):
    """Yield (relative_path, download_url, size) for every file under a GitHub
    directory, recursing into subdirectories."""
    for entry in _gh_list(repo, path, ref):
        if entry['type'] == 'dir':
            yield from _gh_walk(repo, ref, entry['path'])
        elif entry['type'] == 'file' and entry.get('download_url'):
            yield entry['path'], entry['download_url'], entry.get('size', 0)


def _install_from_github_dir(source, dest, progress_cb):
    repo, root, ref = source['repo'], source['path'], source['ref']
    files = list(_gh_walk(repo, ref, root))
    if not any(os.path.basename(p) == LIB_MARKER for p, _u, _s in files):
        raise RuntimeError(f'{repo}/{root} does not contain {LIB_MARKER}')

    grand_total = sum(s for _p, _u, s in files) or 0
    base = 0
    with tempfile.TemporaryDirectory(prefix='hik-sdk-gh-') as tmp:
        staging = Path(tmp) / 'lib'
        for rel, url, _size in files:
            # Path relative to the source root, so HCNetSDKCom/ is preserved.
            sub = os.path.relpath(rel, root)
            out = staging / sub
            out.parent.mkdir(parents=True, exist_ok=True)
            base += _http_get(url, out, progress_cb, base=base,
                              grand_total=grand_total)

        if progress_cb:
            progress_cb('install', None)
        _replace_dir(staging, dest)


# ── shared ────────────────────────────────────────────────────────────────────
def _replace_dir(src, dest):
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)
    for so in dest.rglob('*.so*'):          # ensure libraries are loadable
        try:
            os.chmod(so, 0o755)
        except OSError:
            pass


def download_and_install(sources=None, progress_cb=None, dest=None) -> Path:
    """Install the SDK by trying each source in turn. Returns the installed lib
    directory. ``progress_cb(stage, frac)`` reports UI progress where ``stage``
    is ``'download' | 'extract' | 'install'`` and ``frac`` is 0..1 or ``None``.

    Raises RuntimeError if no source yields a valid SDK.
    """
    sources = sources if sources is not None else SOURCES
    dest = Path(dest) if dest else install_lib_dir()

    last_err = None
    for source in sources:
        try:
            if source['type'] == 'zip':
                _install_from_zip(source, dest, progress_cb)
            elif source['type'] == 'github_dir':
                _install_from_github_dir(source, dest, progress_cb)
            else:
                continue
            return dest
        except Exception as e:
            last_err = e
            continue

    raise RuntimeError(f'Could not install the Hikvision SDK: {last_err}')
