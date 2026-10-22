"""Build a self-contained portable HTTP service on Windows or Linux."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile
from licenses import dependency_notices

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--cargo',default='cargo');parser.add_argument('--go',default='go')
    parser.add_argument('--skip-native',action='store_true');parser.add_argument('--skip-egress',action='store_true')
    parser.add_argument('--cargo-sources',type=Path)
    args=parser.parse_args()
    if not args.skip_native:
        subprocess.run([args.cargo,'build','--release','--locked','--manifest-path',str(ROOT/'native/Cargo.toml')],cwd=ROOT,check=True)
    name='teemerchant_attestation.dll' if os.name=='nt' else 'libteemerchant_attestation.so'
    library=ROOT/'native/target/release'/name
    helper_name='teemerchant-egress.exe' if os.name=='nt' else 'teemerchant-egress'
    helper=ROOT/helper_name
    if not args.skip_egress:
        subprocess.run([args.go,'build','-trimpath','-mod=readonly','-o',str(helper),'.'],cwd=ROOT/'egress',check=True)
        vendor=ROOT/'build/go-vendor'
        subprocess.run([args.go,'mod','vendor','-o',str(vendor)],cwd=ROOT/'egress',check=True)
        target=ROOT/'build/egress-notices';target.mkdir(parents=True,exist_ok=True)
        for file in vendor.rglob('*'):
            if file.is_file() and (file.name.lower().startswith(('license','copying','notice','authors','patents')) or file.name=='modules.txt'):
                destination=target/'vendor'/file.relative_to(vendor);destination.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(file,destination)
        goroot=Path(subprocess.check_output([args.go,'env','GOROOT'],text=True).strip())
        shutil.copyfile(goroot/'LICENSE',target/'Go-LICENSE')
        for item in ('go.mod','go.sum'):shutil.copyfile(ROOT/'egress'/item,target/item)
    if not library.is_file() or not helper.is_file():raise RuntimeError('Native library and VLESS helper are required')
    windows=['--hide-console','hide-early','--icon',str(ROOT/'static/teemerchant.ico')] if os.name=='nt' else []
    subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir','--console',*windows,
        '--name','teemerchant','--distpath',str(ROOT/'build/service'),'--workpath',str(ROOT/'build/service-work'),
        '--specpath',str(ROOT/'build'),'--add-binary',str(library)+os.pathsep+'.',
        '--add-data',str(ROOT/'static')+os.pathsep+'static',str(ROOT/'service.py')],cwd=ROOT,check=True)
    result=ROOT/'build/service/teemerchant'
    shutil.copyfile(helper,result/helper_name)
    if os.name!='nt':os.chmod(result/helper_name,0o755)
    for item in ('LICENSE','NOTICE','README.md','service.example.json'):
        shutil.copyfile(ROOT/item,result/item)
    shutil.copytree(ROOT/'docs',result/'docs',dirs_exist_ok=True)
    notices=result/'licenses/native';notices.mkdir(parents=True,exist_ok=True)
    for item in ('LICENSE','NOTICE','Cargo.lock'):shutil.copyfile(ROOT/'native'/item,notices/item)
    registry=args.cargo_sources or Path(os.environ.get('CARGO_HOME',Path.home()/'.cargo'))/'registry/src'
    dependency_notices(ROOT/'native',registry,notices/'dependency-notices')
    egress_notices=ROOT/'build/egress-notices'
    if egress_notices.exists():shutil.copytree(egress_notices,result/'licenses/egress',dirs_exist_ok=True)
    else:
        # Source builders retain the exact Go module graph and generate vendor
        # notices alongside the helper before distributing the binary.
        raise RuntimeError('Generate build/egress-notices for the locked VLESS dependencies before packaging')
    for distribution in ('cryptography','httpx','httpcore','certifi','socksio','cffi','anyio','idna','h11','pycparser','pyinstaller'):
        d=importlib.metadata.distribution(distribution)
        destination=result/'licenses/python'/distribution;destination.mkdir(parents=True,exist_ok=True)
        for file in d.files or []:
            if Path(file).name.lower().startswith(('license','copying','notice','authors')):
                shutil.copyfile(d.locate_file(file),destination/Path(file).name)
    python_license=Path(sys.base_prefix)/'LICENSE.txt'
    if python_license.exists():shutil.copyfile(python_license,result/'licenses/Python-LICENSE.txt')
    else:
        for candidate in (Path('/usr/share/doc/python3.11/copyright'),Path('/usr/share/doc/python3.12/copyright')):
            if candidate.exists():shutil.copyfile(candidate,result/'licenses/Python-LICENSE.txt');break
    version=json.loads((ROOT/'package.json').read_text())['version']
    system='windows' if os.name=='nt' else 'linux'
    release=ROOT/'release';release.mkdir(exist_ok=True)
    files=[p for p in result.rglob('*') if p.is_file()]
    if any(p.name in ('initial-password.txt','http-auth.json','.vault-key') or 'data' in p.relative_to(result).parts or p.name.endswith('.dpapi') for p in files):raise RuntimeError('Merchant data in release')
    if os.name=='nt':
        artifact=release/f'teemerchant-{version}-{system}-x64.zip'
        with zipfile.ZipFile(artifact,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
            for file in files:archive.write(file,Path('teemerchant')/file.relative_to(result))
    else:
        artifact=release/f'teemerchant-{version}-{system}-x64.tar.gz'
        with tarfile.open(artifact,'w:gz') as archive:archive.add(result,arcname='teemerchant')
    digest=hashlib.sha256(artifact.read_bytes()).hexdigest()
    (release/(artifact.name+'.sha256')).write_text(digest+'  '+artifact.name+'\n',encoding='ascii')
    print(json.dumps({'artifact':str(artifact),'sha256':digest,'bytes':artifact.stat().st_size}),flush=True)

if __name__=='__main__':main()
