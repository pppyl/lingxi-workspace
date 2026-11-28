"""Portable activity manifests and versioned, writable package storage."""
from __future__ import annotations

from dataclasses import dataclass, replace
from io import BytesIO
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import stat
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile
from zlib import error as CompressionError


FORMAT_VERSION = 1
MAX_ARCHIVE = 16 * 1024 * 1024
MAX_EXPANDED = 48 * 1024 * 1024
MAX_FILES = 512
ACTIVITY_ID = re.compile(r'[a-z][a-z0-9-]{0,63}')
FOLDER_NAME = re.compile(r'[a-z][a-z0-9_]{0,63}')
VERSION = re.compile(r'(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})')
EXCLUDED = {'__pycache__', 'tests', 'data', 'release', 'logs', 'credentials', 'secrets', 'node_modules'}
BUNDLED_ACTIVITY_IDS = frozenset(('daily-gold', 'seven-day', 'wechat-checkin'))
ASSET_SUFFIXES = {'.py', '.json', '.txt', '.md', '.html', '.css', '.js', '.mjs',
                  '.png', '.jpg', '.jpeg', '.webp', '.gif', '.ico', '.svg', '.woff', '.woff2'}


class PlatformError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ActivityManifest:
    id: str
    title: str
    home: str
    module: str
    temporary: bool
    navigation: str
    version: str
    folder: Path

    @property
    def api(self):
        return '/api/checkin-wechat' if self.id == 'wechat-checkin' else '/api/' + self.id

    @property
    def version_key(self):
        return tuple(map(int, self.version.split('.')))

    @property
    def identity(self):
        return self.folder.name, self.home, self.navigation


def read_manifest(folder, *, packaged=False):
    try:
        raw = json.loads((folder/'manifest.json').read_text('utf-8'))
    except (OSError, ValueError) as error:
        raise PlatformError('无法读取活动目录中的 manifest.json。') from error
    if not isinstance(raw, dict):
        raise PlatformError('活动清单必须是 JSON 对象。')
    platform_id, title, home, module, temporary = (raw.get(key) for key in ('id', 'title', 'home', 'module', 'temporary'))
    navigation = raw.get('navigation', 'main')
    version = raw.get('version', '0.0.0' if not packaged else '')
    schema = raw.get('format_version', FORMAT_VERSION if not packaged else None)
    if type(schema) is not int or schema != FORMAT_VERSION:
        raise PlatformError('活动包格式版本不受支持，需要 format_version 为 1。')
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise PlatformError('活动版本须使用主版本.次版本.修订号，例如 1.0.0。')
    if (not isinstance(platform_id, str) or not ACTIVITY_ID.fullmatch(platform_id)
            or not isinstance(title, str) or not 1 <= len(title.strip()) <= 80
            or not isinstance(home, str) or not re.fullmatch(r'/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*', home)
            or not FOLDER_NAME.fullmatch(folder.name) or PureWindowsPath(folder.name).is_reserved()
            or module != f'activities.{folder.name}.plugin' or type(temporary) is not bool
            or navigation not in ('main', 'checkin')):
        raise PlatformError('活动清单的标识、入口、目录或类型有误。')
    if packaged:
        expected = ('/checkin/' if navigation == 'checkin' else '/') + platform_id
        if platform_id == 'wechat-checkin':
            expected = '/checkin/wechat'
        if home != expected or not (folder/'__init__.py').is_file() or not (folder/'plugin.py').is_file():
            raise PlatformError('活动包须包含 __init__.py、plugin.py 和与活动标识对应的页面入口。')
    return ActivityManifest(platform_id, title.strip(), home, module, temporary, navigation, version, folder)


def _valid_member(parts):
    return all(part and not part.startswith('.') and not part.endswith((' ', '.'))
               and not any(char in '<>:"\\|?*' or ord(char) < 32 for char in part)
               and not PureWindowsPath(part).is_reserved() and len(part) <= 160 for part in parts)


def _portable_file(parts):
    return (not any(part.casefold() in EXCLUDED for part in parts)
            and (PurePosixPath(parts[-1]).suffix.lower() in ASSET_SUFFIXES or parts[-1] in {'LICENSE', 'NOTICE'}))


def _extract(payload, stage):
    if not payload or len(payload) > MAX_ARCHIVE:
        raise PlatformError('请选择不超过 16 MB 的活动 ZIP 包。', 413 if payload else 400)
    try:
        with ZipFile(BytesIO(payload)) as archive:
            members = archive.infolist()
            if not members or len(members) > MAX_FILES or sum(member.file_size for member in members) > MAX_EXPANDED:
                raise PlatformError('活动包须包含 1～512 个条目，解压后不超过 48 MB。')
            roots, seen = set(), {}
            for member in members:
                name = member.orig_filename.removesuffix('/')
                parts = name.split('/')
                mode = stat.S_IFMT(member.external_attr >> 16)
                is_dir = member.is_dir()
                if (not _valid_member(parts) or not FOLDER_NAME.fullmatch(parts[0])
                        or mode not in (0, stat.S_IFDIR if is_dir else stat.S_IFREG)
                        or member.flag_bits & 1 or not is_dir and (len(parts) < 2 or not _portable_file(parts[1:]))):
                    raise PlatformError('活动包包含不支持的文件路径、文件类型或加密条目。')
                roots.add(parts[0])
                for length in range(1, len(parts) + 1):
                    path = '/'.join(parts[:length])
                    key = path.casefold()
                    previous = seen.get(key)
                    leaf = length == len(parts)
                    if previous and (previous[0] != path or previous[1] or leaf and (previous[2] or not is_dir)):
                        raise PlatformError('活动包包含重复路径或文件与目录冲突。')
                    if leaf:
                        seen[key] = (path, not is_dir, True)
                    elif previous is None:
                        seen[key] = (path, False, False)
                path = stage.joinpath(*parts)
                if is_dir:
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, path.open('xb') as target:
                        shutil.copyfileobj(source, target)
            if len(roots) != 1:
                raise PlatformError('每个活动包必须只包含一个活动文件夹。')
    except (BadZipFile, NotImplementedError, RuntimeError, EOFError, CompressionError) as error:
        raise PlatformError('活动 ZIP 包损坏或使用了不支持的压缩格式。') from error
    return stage / roots.pop()


class ActivityPackages:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def installed(self):
        for activity in sorted(self.root.iterdir()) if self.root.is_dir() else ():
            if not ACTIVITY_ID.fullmatch(activity.name) or not activity.is_dir():
                continue
            for version in activity.iterdir():
                if not VERSION.fullmatch(version.name) or not version.is_dir():
                    continue
                if version.resolve() != version:
                    raise PlatformError('已安装活动目录指向了数据目录之外。')
                folders = list(version.iterdir())
                if len(folders) != 1 or folders[0].resolve() != folders[0]:
                    raise PlatformError(f'{activity.name} 的活动包目录无效。')
                manifest = read_manifest(folders[0], packaged=True)
                if (manifest.id, manifest.version) != (activity.name, version.name):
                    raise PlatformError('已安装目录与活动标识或版本不一致。')
                yield manifest

    def install(self, payload, validate):
        self.root.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix='.upload-', dir=self.root) as directory:
            stage = Path(directory)/'release'
            stage.mkdir()
            folder = _extract(payload, stage)
            manifest = read_manifest(folder, packaged=True)
            for source in folder.rglob('*.py'):
                try:
                    compile(source.read_bytes(), source.relative_to(folder).as_posix(), 'exec')
                except (SyntaxError, ValueError) as error:
                    raise PlatformError(f'活动包 Python 源码无效：{source.relative_to(folder).as_posix()}。') from error
            validate(manifest)
            target = self.root/manifest.id/manifest.version
            if target.exists():
                raise PlatformError('该活动版本已安装。更新请提高版本号。', 409)
            target.parent.mkdir(exist_ok=True)
            if target.parent.resolve() != target.parent:
                raise PlatformError('活动安装目录无效。', 409)
            stage.rename(target)
            return replace(manifest, folder=target/folder.name)

    def remove(self, platform_id):
        directory = self.root/platform_id
        if (not ACTIVITY_ID.fullmatch(platform_id) or directory.resolve() != directory
                or directory.parent != self.root):
            raise PlatformError('活动安装目录发生变化，请检查后重试。', 409)
        if directory.is_dir():
            shutil.rmtree(directory)


class ActivityPackageStorage:
    """Give new packages a data namespace separate from built-in feature state."""
    def __init__(self, database, activity_id):
        self.database = database
        self.activity_id = activity_id
        self.state_key = 'activity-plugin:' + activity_id
        self.scope_prefix = self.state_key + ':'

    def _state_key(self, activity_id):
        if activity_id != self.activity_id:
            raise PlatformError('活动包只能读写所属活动的数据。')
        return self.state_key

    def load_activity_data(self, activity_id):
        return self.database.load_activity_data(self._state_key(activity_id))

    def save_activity_data(self, activity_id, data):
        self.database.save_activity_data(self._state_key(activity_id), data)

    def delete_activity_data(self, activity_id):
        self.database.delete_activity_data(self._state_key(activity_id))

    def load_credentials(self, scope, login_id):
        return self.database.load_credentials(self.scope_prefix + scope, login_id)

    def save_credentials(self, scope, login_id, data):
        self.database.save_credentials(self.scope_prefix + scope, login_id, data)

    def delete_credentials(self, login_id=None, scope=None):
        if scope is None:
            self.database.delete_credentials(login_id=login_id, scope_prefix=self.scope_prefix)
        else:
            self.database.delete_credentials(login_id=login_id, scope=self.scope_prefix + scope)


def export_package(manifest):
    folder = manifest.folder
    files = []
    for path in folder.rglob('*'):
        parts = path.relative_to(folder).parts
        if path.is_file() and _valid_member(parts) and _portable_file(parts) and path.resolve() == path:
            files.append(path)
    if len(files) > MAX_FILES or sum(path.stat().st_size for path in files) > MAX_EXPANDED:
        raise PlatformError('活动源码或资源超出活动包大小限制。', 413)
    output = BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.write(path, path.relative_to(folder.parent).as_posix())
    if output.tell() > MAX_ARCHIVE:
        raise PlatformError('导出的活动包超过 16 MB。', 413)
    output.seek(0)
    return output


def main():
    from argparse import ArgumentParser

    parser = ArgumentParser(description='将一个活动源码目录打为可导入的 ZIP 包。')
    parser.add_argument('folder', type=Path, help='包含 manifest.json 的活动目录')
    parser.add_argument('output', type=Path, help='输出 ZIP 路径')
    args = parser.parse_args()
    try:
        manifest = read_manifest(args.folder.resolve(), packaged=True)
        args.output.write_bytes(export_package(manifest).getbuffer())
    except (PlatformError, OSError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
