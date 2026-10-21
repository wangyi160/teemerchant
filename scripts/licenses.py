import json
import shutil
import tomllib

def dependency_notices(source, registry, target):
    """Include notices for all locked crates, a superset of compiled features."""
    roots = [registry, *registry.glob('*')]
    entries = []
    for package in tomllib.loads((source / 'Cargo.lock').read_text(encoding='utf-8'))['package']:
        if not package.get('source', '').startswith('registry+'):
            continue
        name = package['name'] + '-' + package['version']
        directory = next((root / name for root in roots if (root / name).is_dir()), None)
        if directory is None and (registry / package['name'] / 'Cargo.toml').is_file():
            candidate=registry/package['name']
            if tomllib.loads((candidate/'Cargo.toml').read_text(encoding='utf-8'))['package']['version']==package['version']:
                directory=candidate
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
