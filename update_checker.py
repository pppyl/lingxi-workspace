"""Cached GitHub Release checks; never replace running programs or user data."""
from __future__ import annotations

from copy import deepcopy
import json
import math
import os
import re
from threading import Event, RLock, Thread
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from flask import jsonify
from runtime_paths import FROZEN, VERSION

REPOSITORY = 'https://github.com/pppyl/lingxi-workspace'
RELEASES_URL = REPOSITORY + '/releases'
API_URL = 'https://api.github.com/repos/pppyl/lingxi-workspace/releases/latest'
CHECK_INTERVAL = 6 * 3600
MANUAL_INTERVAL = 60


def version_numbers(value):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:\+[a-zA-Z0-9.-]+)?', value)
    return tuple(int(part) for part in match.groups()) if match else None


def fetch_release(etag):
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'LingxiWorkspace/'+VERSION,
               'X-GitHub-Api-Version': '2022-11-28'}
    if etag:
        headers['If-None-Match'] = etag
    try:
        with urlopen(Request(API_URL, headers=headers), timeout=10) as response:
            return json.load(response), response.headers.get('ETag', '')
    except HTTPError as error:
        if error.code == 304:
            return False, etag
        if error.code == 404:
            return None, ''
        raise


class UpdateChecker:
    def __init__(self, storage=None, *, current_version=VERSION, platform=None, fetcher=None, now=None):
        self.storage, self.current_version = storage, current_version
        self.platform = platform or ('android' if os.environ.get('LINGXI_ANDROID') == '1' else 'windows' if FROZEN else 'source')
        self.fetcher, self.now = fetcher or fetch_release, now or time.time
        self.lock, self.quit, self.thread = RLock(), Event(), None
        self.checking = False
        self.data = (storage.load_update_check() if storage is not None else None) or {
            'release': None, 'etag': '', 'checked_at': 0, 'last_attempt_at': 0, 'next_check_at': 0, 'error': '',
        }

    def _save(self):
        if self.storage is not None:
            self.storage.save_update_check(self.data)

    def status(self):
        with self.lock:
            release = deepcopy(self.data['release'])
            available = bool(release and version_numbers(release['version']) > version_numbers(self.current_version))
            recommended = None
            if release:
                recommended = next((asset for asset in release['assets'] if asset['platform'] == self.platform), None)
                if self.platform == 'source':
                    recommended = {'platform': 'source', 'name': '源代码 ZIP', 'url': release['source_url']}
            return {'current_version': self.current_version, 'platform': self.platform, 'available': available,
                    'checking': self.checking, 'checked_at': self.data['checked_at'],
                    'next_check_at': self.data['next_check_at'], 'error': self.data['error'],
                    'retry_after': max(0, math.ceil(self.data['last_attempt_at'] + MANUAL_INTERVAL - self.now())),
                    'release': release, 'recommended': recommended, 'releases_url': RELEASES_URL}

    def request_check(self, *, force=False):
        with self.lock:
            now = self.now()
            if self.checking or (not force and now < self.data['next_check_at']) or (
                    self.data['last_attempt_at'] and now < self.data['last_attempt_at'] + MANUAL_INTERVAL):
                return self.status()
            self.checking = True
            self.data['last_attempt_at'] = now
            self._save()
            Thread(target=self._check, daemon=True, name='github-release-check').start()
            return self.status()

    def _check(self):
        try:
            payload, etag = self.fetcher(self.data['etag'])
            release = self.data['release']
            if payload is None:
                release = None
            elif payload is not False:
                if not isinstance(payload, dict):
                    raise ValueError('发布信息格式无效')
                tag = payload.get('tag_name', '')
                if not isinstance(tag, str) or version_numbers(tag) is None or payload.get('draft') or payload.get('prerelease'):
                    raise ValueError('未取得有效的正式版本')
                raw_assets = payload.get('assets', [])
                if not isinstance(raw_assets, list) or any(not isinstance(item, dict) for item in raw_assets):
                    raise ValueError('发布附件格式无效')
                version = tag.removeprefix('v').split('+')[0]
                root = REPOSITORY + '/releases/download/' + quote(tag, safe='') + '/'
                names = {f'LingxiWorkspace-{version}-Windows-x64.exe': 'windows',
                         f'LingxiWorkspace-{version}-Windows-x64.zip': 'windows-zip',
                         f'LingxiWorkspace-{version}-Android-arm64.apk': 'android'}
                assets = [{'name': item['name'], 'platform': names[item['name']], 'url': root+quote(item['name'], safe=''),
                           'size': item.get('size', 0)} for item in raw_assets if item.get('name') in names]
                release = {'version': version, 'tag': tag, 'title': str(payload.get('name') or tag)[:200],
                           'notes': str(payload.get('body') or '')[:20000], 'published_at': payload.get('published_at'),
                           'url': REPOSITORY+'/releases/tag/'+quote(tag, safe=''), 'assets': assets,
                           'source_url': REPOSITORY+'/archive/refs/tags/'+quote(tag, safe='')+'.zip'}
            with self.lock:
                self.data.update(release=release, etag=etag, checked_at=self.now(),
                                 next_check_at=self.now()+CHECK_INTERVAL, error='')
        except (URLError, TimeoutError, OSError, ValueError) as error:
            delay = 15 * 60
            message = '暂时无法连接 GitHub，请检查网络后重试。'
            if isinstance(error, HTTPError) and error.code in (403, 429):
                message = 'GitHub 检查次数已达限制，稍后自动重试。'
                reset = error.headers.get('X-RateLimit-Reset', '')
                retry = error.headers.get('Retry-After', '')
                if reset.isdigit():
                    delay = max(delay, int(reset)-int(self.now()))
                if retry.isdigit():
                    delay = max(delay, int(retry))
            elif isinstance(error, ValueError):
                message = 'GitHub 发布信息暂时不可用，请稍后重试。'
            with self.lock:
                self.data.update(error=message, next_check_at=self.now()+delay)
        finally:
            with self.lock:
                self.checking = False
                self._save()

    def start(self):
        with self.lock:
            if self.thread is not None:
                return
            self.thread = Thread(target=self._loop, daemon=True, name='version-updates')
            self.thread.start()

    def _loop(self):
        while not self.quit.is_set():
            self.request_check()
            self.quit.wait(60)

    def shutdown(self):
        self.quit.set()
        if self.thread is not None:
            self.thread.join(timeout=1)


def register_updates(app, storage):
    checker = UpdateChecker(storage)
    app.extensions['update_checker'] = checker

    @app.get('/api/updates')
    def updates_status():
        return jsonify(checker.status())

    @app.post('/api/updates/check')
    def updates_check():
        return jsonify(checker.request_check(force=True))

    return checker
