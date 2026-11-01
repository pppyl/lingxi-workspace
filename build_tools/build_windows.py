"""Build the Windows x64 portable client from an explicit resource allowlist."""
from __future__ import annotations
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.request import urlopen
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from runtime_paths import VERSION


def sha256(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def fetch_json(url):
    with urlopen(url,timeout=60) as response:return json.load(response)


def download(url,path):
    if path.exists():return
    print('Downloading '+path.name,flush=True)
    with urlopen(url,timeout=120) as response,path.open('wb') as output:
        total=0;next_report=64*1024*1024
        while data:=response.read(1024*1024):
            output.write(data);total+=len(data)
            if total>=next_report:
                print(f'  {total//1024//1024} MiB',flush=True);next_report+=64*1024*1024


def main():
    cache=ROOT/'.build/runtime-cache';cache.mkdir(parents=True,exist_ok=True)
    lock_file=ROOT/'build_tools/runtime-lock.json'
    if lock_file.exists():runtime={'node':json.loads(lock_file.read_text('utf-8'))['node']}
    else:
        node=next(item for item in fetch_json('https://nodejs.org/dist/index.json') if item['version'].startswith('v24.') and item['lts'])
        node_name=f'node-{node["version"]}-win-x64.zip'
        with urlopen(f'https://nodejs.org/dist/{node["version"]}/SHASUMS256.txt',timeout=30) as response:checksums=response.read().decode()
        node_sha=next(line.split()[0] for line in checksums.splitlines() if line.endswith('  '+node_name))
        runtime={'node':{'version':node['version'],'url':f'https://nodejs.org/dist/{node["version"]}/{node_name}','sha256':node_sha}}
    extracted={}
    for name,item in runtime.items():
        archive=cache/(name+'-'+item['version']+'.zip');download(item['url'],archive)
        digest=sha256(archive)
        if item.get('sha256') and item['sha256']!=digest:raise RuntimeError(name+' download checksum mismatch')
        item['sha256']=digest
        destination=cache/(name+'-'+item['version'])
        if not destination.exists():
            with zipfile.ZipFile(archive) as source:source.extractall(destination)
        extracted[name]=next(path for path in destination.iterdir() if path.is_dir())
    lock_file.write_text(json.dumps(runtime,indent=2)+'\n','utf-8')
    from PIL import Image
    assets=ROOT/'desktop_assets';assets.mkdir(exist_ok=True)
    image=Image.open(assets/'app.png').convert('RGBA')
    image.save(assets/'app.ico',sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])
    shutil.copy2(assets/'app.ico',ROOT/'static/app.ico')
    notice_dir=ROOT/'.build/notices';notice_dir.mkdir(exist_ok=True)
    shutil.copy2(Path(sys.base_prefix)/'LICENSE.txt',notice_dir/'Python-LICENSE.txt')
    dependencies=['Flask','Werkzeug','Jinja2','MarkupSafe','itsdangerous','click','blinker','curl_cffi','cffi','certifi','cryptography','waitress','pystray','Pillow','six','PyInstaller','qrcode']
    for name in dependencies:
        dist=importlib.metadata.distribution(name)
        for entry in dist.files or []:
            if any(part.lower().startswith(('license','copying','notice')) for part in entry.parts):
                source=Path(dist.locate_file(entry))
                if source.is_file():
                    target=notice_dir/name/Path(*entry.parts[-2:]);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
    shutil.copy2(extracted['node']/'LICENSE',notice_dir/'Node-LICENSE.txt')
    command=[sys.executable,'-m','PyInstaller','--noconfirm','--onedir','--windowed','--name','LingxiWorkspace',
        '--distpath',str(ROOT/'.build/dist'),'--workpath',str(ROOT/'.build/pyinstaller'),'--specpath',str(ROOT/'.build'),
        '--icon',str(assets/'app.ico'),'--noupx','--collect-all','curl_cffi','--collect-all','cryptography',
        '--collect-data','certifi','--hidden-import','pystray._win32',
        '--exclude-module','tkinter','--exclude-module','matplotlib','--exclude-module','numpy','--exclude-module','pytest',
        '--exclude-module','orjson']
    activity_resources = ROOT/'.build/activity-resources'
    if activity_resources.exists():
        if activity_resources.resolve().parent != (ROOT/'.build').resolve():
            raise RuntimeError('Activity staging path escaped the build directory')
        shutil.rmtree(activity_resources)
    shutil.copytree(ROOT/'activities', activity_resources,
                    ignore=shutil.ignore_patterns('__pycache__', 'tests', 'android'))
    resources=[(ROOT/'static','static'),(ROOT/'templates','templates'),
        (ROOT/'app_login_public_key.json','.'),(assets,'desktop_assets'),
        (ROOT/'recharge/static','recharge/static'),(ROOT/'recharge/templates','recharge/templates'),
        (ROOT/'recharge/browser_bridge.mjs','recharge'),(ROOT/'recharge/package.json','recharge'),
        (activity_resources,'activities'),
        (ROOT/'recharge/node_modules/playwright-core','recharge/node_modules/playwright-core'),
        (notice_dir,'third-party-notices')]
    for source,dest in resources:command+=['--add-data',str(source)+':'+dest]
    command+=['--add-data',str(extracted['node']/'node.exe')+':runtime/node',str(ROOT/'desktop.py')]
    if '--single-file-only' not in sys.argv:subprocess.run(command,cwd=ROOT,check=True)
    package=ROOT/'.build/dist/LingxiWorkspace'
    shutil.copy2(ROOT/'DESKTOP.md',package/'使用说明.txt')
    shutil.copy2(ROOT/'THIRD_PARTY_NOTICES.md',package/'THIRD_PARTY_NOTICES.md')
    shutil.copy2(ROOT/'allow-lan.ps1',package/'allow-lan.ps1')
    release=ROOT/'release';release.mkdir(exist_ok=True)
    archive=release/f'LingxiWorkspace-{VERSION}-Windows-x64.zip'
    forbidden={'.local','.git','analysis','data','window-profile','browser-profile'}
    files=[path for path in package.rglob('*') if path.is_file()]
    for path in files:
        if forbidden.intersection(path.relative_to(package).parts) or path.suffix in {'.sqlite3','.log','.db'}:
            raise RuntimeError('Private runtime data in package: '+str(path.relative_to(package)))
        if path.name.lower() in {'chrome.exe','msedge.exe'}:raise RuntimeError('Browser must not be bundled in this release')
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as output:
        for path in files:output.write(path,Path('LingxiWorkspace')/path.relative_to(package))
    command[command.index('--onedir')]='--onefile'
    subprocess.run(command,cwd=ROOT,check=True)
    executable=release/f'LingxiWorkspace-{VERSION}-Windows-x64.exe'
    shutil.copy2(ROOT/'.build/dist/LingxiWorkspace.exe',executable)
    artifacts=[executable,archive]
    android=release/f'LingxiWorkspace-{VERSION}-Android-arm64.apk'
    if android.exists():artifacts.append(android)
    (release/'SHA256SUMS.txt').write_text(''.join(sha256(path)+'  '+path.name+'\n' for path in artifacts),'utf-8')
    shutil.copy2(ROOT/'RELEASE_NOTES.md',release/f'RELEASE_NOTES-{VERSION}.md')
    print(json.dumps({'archive':str(archive),'bytes':archive.stat().st_size,'files':len(files),'runtime':runtime},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
