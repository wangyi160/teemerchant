"""Build the self-contained Python engine and stage the pinned TEE verifier."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import importlib.metadata
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def dependency_notices(source, registry, target):
    """Include notices for all locked crates, a superset of compiled features."""
    roots = list(registry.glob('*'))
    entries = []
    for package in tomllib.loads((source / 'Cargo.lock').read_text(encoding='utf-8'))['package']:
        if not package.get('source', '').startswith('registry+'):
            continue
        name = package['name'] + '-' + package['version']
        directory = next((root / name for root in roots if (root / name).is_dir()), None)
        if directory is None:
            raise ValueError(f'Missing locked crate sources for notices: {name}; run cargo fetch in teeproxy')
        metadata = tomllib.loads((directory / 'Cargo.toml').read_text(encoding='utf-8'))['package']
        included = []
        for file in directory.rglob('*'):
            if file.is_file() and file.name.lower().startswith(('license', 'copying', 'copyright', 'notice')):
                relative = file.relative_to(directory)
                destination = target / name / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, destination)
                included.append(relative.as_posix())
        entries.append({'package':name, 'license':metadata.get('license'), 'repository':metadata.get('repository'), 'files':included})
    target.mkdir(parents=True, exist_ok=True)
    (target / 'manifest.json').write_text(json.dumps(entries, indent=2) + '\n', encoding='utf-8', newline='\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--teeproxy', type=Path, required=True, help='Reviewed teeproxy.exe with merchant-transport support')
    parser.add_argument('--teeproxy-source', type=Path, required=True, help='Matching teeproxy source tree for notices')
    parser.add_argument('--teeproxy-sha256', required=True, help='Expected SHA-256 from the pinned teeproxy release')
    parser.add_argument('--cargo-sources', type=Path, default=Path(os.environ.get('CARGO_HOME', Path.home() / '.cargo')) / 'registry/src',
                        help='Cargo registry/src cache containing the locked teeproxy crates for bundled notices')
    args = parser.parse_args()
    executable = args.teeproxy.resolve(strict=True)
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    if digest != args.teeproxy_sha256.lower():
        raise ValueError('TEE verifier digest does not match the reviewed release')
    source = args.teeproxy_source.resolve(strict=True)
    for name in ('LICENSE', 'NOTICE', 'Cargo.lock'):
        if not (source / name).is_file():
            raise ValueError('Missing teeproxy source notices or dependency lock')
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--console',
        '--name', 'teemerchant-engine', '--distpath', str(ROOT / 'build/engine'), '--workpath', str(ROOT / 'build/pyinstaller'),
        '--specpath', str(ROOT / 'build'), str(ROOT / 'desktop_worker.py')], cwd=ROOT, check=True)
    resources = ROOT / 'build/resources'
    shutil.copytree(ROOT / 'build/engine/teemerchant-engine', resources / 'engine', dirs_exist_ok=True)
    transport = resources / 'transport'
    transport.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(executable, transport / 'teeproxy.exe')
    for name in ('LICENSE', 'NOTICE', 'Cargo.lock'):
        shutil.copyfile(source / name, transport / name)
    dependency_notices(source, args.cargo_sources, transport / 'dependency-notices')
    notices = resources / 'licenses'
    notices.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(sys.base_prefix) / 'LICENSE.txt', notices / 'Python-LICENSE.txt')
    pyinstaller = importlib.metadata.distribution('pyinstaller')
    license_path = next(p for p in pyinstaller.files if str(p).endswith('/licenses/COPYING.txt'))
    shutil.copyfile(pyinstaller.locate_file(license_path), notices / 'PyInstaller-COPYING.txt')
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    (transport / 'build.json').write_text(json.dumps({'source': 'https://github.com/wangyi160/teeproxy',
        'revision': revision, 'sha256': digest}, indent=2) + '\n', encoding='utf-8', newline='\n')
    print('Packaged engine and SHA-256-verified TEE transport.', flush=True)


if __name__ == '__main__':
    main()
