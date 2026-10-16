"""Compile teemerchant's native library and bundle it inside the private engine."""
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
            raise ValueError(f'Missing locked crate sources for notices: {name}; run cargo fetch --locked --manifest-path native/Cargo.toml')
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
    parser.add_argument('--cargo', default='cargo', help='Cargo executable or configured toolchain wrapper')
    parser.add_argument('--cargo-sources', type=Path, default=Path(os.environ.get('CARGO_HOME', Path.home() / '.cargo')) / 'registry/src',
                        help='Cargo registry/src cache containing the native library locked crates for bundled notices')
    args = parser.parse_args()
    source = ROOT / 'native'
    subprocess.run([args.cargo, 'build', '--manifest-path', str(source / 'Cargo.toml'), '--locked', '--release',
                    '--target-dir', str(source / 'target')], cwd=ROOT, check=True)
    library = source / 'target/release/teemerchant_attestation.dll'
    digest = hashlib.sha256(library.read_bytes()).hexdigest()
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--console',
        '--name', 'teemerchant-engine', '--distpath', str(ROOT / 'build/engine'), '--workpath', str(ROOT / 'build/pyinstaller'),
        '--specpath', str(ROOT / 'build'), '--add-binary', str(library) + ';.',
        str(ROOT / 'desktop_worker.py')], cwd=ROOT, check=True)
    resources = ROOT / 'build/resources'
    # Clean only this resolved build staging directory, never merchant data.
    assert resources.resolve() == ROOT.resolve() / 'build/resources'
    if resources.exists():
        shutil.rmtree(resources)
    shutil.copytree(ROOT / 'build/engine/teemerchant-engine', resources / 'engine')
    native_notices = resources / 'licenses/native'
    native_notices.mkdir(parents=True)
    for name in ('LICENSE', 'NOTICE', 'Cargo.lock'):
        shutil.copyfile(source / name, native_notices / name)
    dependency_notices(source, args.cargo_sources, native_notices / 'dependency-notices')
    notices = resources / 'licenses'
    notices.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(sys.base_prefix) / 'LICENSE.txt', notices / 'Python-LICENSE.txt')
    pyinstaller = importlib.metadata.distribution('pyinstaller')
    license_path = next(p for p in pyinstaller.files if str(p).endswith('/licenses/COPYING.txt'))
    shutil.copyfile(pyinstaller.locate_file(license_path), notices / 'PyInstaller-COPYING.txt')
    (native_notices / 'build.json').write_text(json.dumps({'source': 'https://github.com/wangyi160/teemerchant/tree/main/native',
        'library':library.name, 'sha256':digest,
        'sources_sha256':{p.relative_to(source).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in [source/'Cargo.toml',source/'Cargo.lock',*sorted((source/'src').glob('*.rs'))]}},
        indent=2) + '\n', encoding='utf-8', newline='\n')
    assert not list(resources.rglob('teeproxy.exe'))
    print('Packaged engine with in-process merchant TEE library.', flush=True)


if __name__ == '__main__':
    main()
