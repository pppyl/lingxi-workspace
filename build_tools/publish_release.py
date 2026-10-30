"""Publish the reviewed source and portable archive with Git Credential Manager."""
from __future__ import annotations
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import quote,urlsplit
from urllib.request import Request,urlopen,getproxies
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from runtime_paths import VERSION
OWNER='pppyl'
NAME='lingxi-workspace'


def main():
    result=subprocess.run(['git','credential','fill'],input='protocol=https\nhost=github.com\n\n',capture_output=True,text=True,
        env={**os.environ,'GIT_TERMINAL_PROMPT':'0','GCM_INTERACTIVE':'never'},check=True,timeout=30)
    credential=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    token=credential['password']
    headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','User-Agent':'Lingxi-release-builder','X-GitHub-Api-Version':'2022-11-28'}
    def api(path,data=None,method=None):
        request=Request('https://api.github.com'+path,data=json.dumps(data).encode() if data is not None else None,
            headers={**headers,'Content-Type':'application/json'},method=method)
        with urlopen(request,timeout=60) as response:return json.load(response)
    user=api('/user')
    if user['login']!=OWNER:raise RuntimeError('The signed-in GitHub account does not match the requested owner.')
    try:repo=api(f'/repos/{OWNER}/{NAME}')
    except HTTPError as error:
        if error.code!=404:raise
        repo=api('/user/repos',{'name':NAME,'description':'本地工具台：统一账号、灵犀对话与定时发送、周三充值日。Windows 便携客户端。',
                              'private':False,'auto_init':False})
    if repo['private']:raise RuntimeError('Repository must be public for this release.')
    proxy=getproxies().get('https')
    git_env={**os.environ,**({'https_proxy':proxy,'http_proxy':proxy} if proxy else {})}
    subprocess.run(['git','push','origin','HEAD:main'],cwd=ROOT,env=git_env,check=True)
    subprocess.run(['git','push','origin','v'+VERSION],cwd=ROOT,env=git_env,check=True)
    notes=(ROOT/'RELEASE_NOTES.md').read_text('utf-8')
    try:release=api(f'/repos/{OWNER}/{NAME}/releases/tags/v{VERSION}')
    except HTTPError as error:
        if error.code!=404:raise
        release=api(f'/repos/{OWNER}/{NAME}/releases',{'tag_name':'v'+VERSION,'target_commitish':'main',
            'name':'v'+VERSION+' · 本地工具台 Android 与 Windows','body':notes,'draft':True,'prerelease':False})
    assets={asset['name']:asset for asset in api(f'/repos/{OWNER}/{NAME}/releases/{release["id"]}/assets')}
    for path in [ROOT/'release'/f'LingxiWorkspace-{VERSION}-Windows-x64.exe',ROOT/'release'/f'LingxiWorkspace-{VERSION}-Windows-x64.zip',ROOT/'release'/f'LingxiWorkspace-{VERSION}-Android-arm64.apk',ROOT/'release/SHA256SUMS.txt']:
        with path.open('rb') as stream:digest='sha256:'+hashlib.file_digest(stream,'sha256').hexdigest()
        if path.name in assets:
            asset=assets[path.name]
            if asset.get('digest')!=digest:raise RuntimeError('Existing release asset checksum differs; refusing to replace it.')
            continue
        if proxy:
            endpoint=urlsplit(proxy)
            connection=http.client.HTTPSConnection(endpoint.hostname,endpoint.port or 80,timeout=180)
            connection.set_tunnel('uploads.github.com',443)
        else:
            connection=http.client.HTTPSConnection('uploads.github.com',timeout=180)
        connection.putrequest('POST',f'/repos/{OWNER}/{NAME}/releases/{release["id"]}/assets?name='+quote(path.name))
        for key,value in headers.items():connection.putheader(key,value)
        connection.putheader('Content-Type',{'.zip':'application/zip','.exe':'application/octet-stream','.apk':'application/vnd.android.package-archive'}.get(path.suffix,'text/plain'))
        connection.putheader('Content-Length',str(path.stat().st_size));connection.endheaders()
        uploaded=0;next_report=32*1024*1024
        with path.open('rb') as stream:
            while chunk:=stream.read(1024*1024):
                connection.send(chunk);uploaded+=len(chunk)
                if uploaded>=next_report:
                    print(f'Upload {path.name}: {uploaded//1024//1024} MiB',flush=True);next_report+=32*1024*1024
        response=connection.getresponse();asset=json.loads(response.read());connection.close()
        if response.status!=201:raise RuntimeError('Release asset upload HTTP '+str(response.status))
        if asset.get('digest') and asset['digest']!=digest:raise RuntimeError('Uploaded asset checksum mismatch')
        print('Uploaded '+path.name,flush=True)
    release=api(f'/repos/{OWNER}/{NAME}/releases/{release["id"]}',{'draft':False,'make_latest':'true'},method='PATCH')
    check=api(f'/repos/{OWNER}/{NAME}/releases/tags/v{VERSION}')
    if check['draft'] or len(check['assets'])<4:raise RuntimeError('Published release verification failed')
    print(json.dumps({'repository':repo['html_url'],'release':check['html_url'],
        'assets':[{'name':asset['name'],'bytes':asset['size'],'digest':asset.get('digest')} for asset in check['assets']]},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
