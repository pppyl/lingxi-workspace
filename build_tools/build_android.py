"""Stage only public app resources, then build a signed standalone arm64 APK."""
from pathlib import Path
import os
import sys
import shutil
import subprocess
import secrets
import hashlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime_paths import VERSION
from build_tools.activity_resources import stage_activities


def main():
    staging = ROOT / '.build/android-python'
    if staging.exists():
        if staging.resolve().parent != (ROOT/'.build').resolve():
            raise RuntimeError('Android staging path escaped the build directory')
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    for name in ('app.py', 'activity_packages.py', 'runtime_paths.py'):
        shutil.copy2(ROOT/name, staging/name)
    packages = ('features', 'features/information', 'features/mobile_rights', 'features/member_benefits',
                'features/lingxi', 'features/star_day', 'features/checkin', 'features/monthly_coupon',
                'features/net_age', 'features/coupons', 'workspace', 'workspace/accounts', 'workspace/clients')
    for package in packages:
        (staging/package).mkdir(parents=True, exist_ok=True)
        for source in (ROOT/package).glob('*.py'):
            if source.name not in ('campus_status.py', 'desktop_browser.py', 'windows_runtime.py'):
                shutil.copy2(source, staging/package/source.name)
    for asset in ('workspace/accounts/app_login_public_key.json', 'features/mobile_rights/gateway.json'):
        shutil.copy2(ROOT/asset, staging/asset)
    for directory in ('static', 'templates', 'recharge/static', 'recharge/templates'):
        shutil.copytree(ROOT / directory, staging / directory, dirs_exist_ok=True)
    runtime = staging / 'recharge/member_benefits_runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    for name in ('dfp_runtime.js', 'wheel_runtime.js'):
        shutil.copy2(ROOT / 'recharge/member_benefits_runtime' / name, runtime / name)
    shutil.copytree(ROOT / 'recharge/member_benefits_runtime/assets', runtime / 'assets')
    stage_activities(ROOT, staging/'activities')
    for name in ('__init__', 'app', 'accounts', 'cmcc', 'scheduler', 'checkin', 'device'):
        shutil.copy2(ROOT / 'recharge' / (name + '.py'), staging / 'recharge' / (name + '.py'))
    for path in staging.rglob('*'):
        if path.is_file() and (path.suffix in ('.sqlite3', '.db', '.log', '.keystore') or path.name.startswith('.')):
            raise RuntimeError('Private runtime data found in Android staging')
    if '--stage-only' in sys.argv:
        print(str(staging))
        return 0
    icon = ROOT / 'android/app/src/main/res/mipmap-xxxhdpi/ic_launcher.png'
    icon.parent.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    Image.open(ROOT / 'build_tools/assets/app.png').resize((192, 192)).save(icon)
    sdk = Path(os.environ.get('ANDROID_HOME', str(ROOT / '.build/android-sdk')))
    java = Path(os.environ.get('JAVA_HOME', str(ROOT / 'tools/android-static/jdk-21.0.13.0.2+1')))
    gradle = Path(os.environ.get('LINGXI_GRADLE', str(ROOT / '.build/android-tools/gradle/gradle-8.10.2/bin/gradle.bat')))
    store = ROOT / '.local/android-release.keystore'
    password_file = ROOT / '.local/android-release-password.txt'
    store.parent.mkdir(exist_ok=True)
    if not password_file.exists(): password_file.write_text(secrets.token_urlsafe(32), 'utf-8')
    password = password_file.read_text('utf-8').strip()
    env = dict(os.environ, JAVA_HOME=str(java), ANDROID_HOME=str(sdk), LINGXI_KEYSTORE=str(store),
               LINGXI_KEYSTORE_PASSWORD=password, LINGXI_BUILD_PYTHON=sys.executable,
               GRADLE_USER_HOME=str(ROOT / '.build/gradle-home'))
    if not store.exists():
        subprocess.run([str(java / 'bin/keytool.exe'), '-genkeypair', '-keystore', str(store),
                        '-storepass:env', 'LINGXI_KEYSTORE_PASSWORD', '-keypass:env', 'LINGXI_KEYSTORE_PASSWORD',
                        '-alias', 'lingxi', '-keyalg', 'RSA', '-keysize', '3072', '-validity', '10000',
                        '-dname', 'CN=LingxiWorkspace, O=pppyl'], env=env, check=True)
    with (ROOT / 'analysis/android-build.log').open('w', encoding='utf-8') as log:
        result = subprocess.run([str(gradle), '--no-daemon', '--console=plain', 'assembleRelease'],
                                cwd=ROOT / 'android', env=env, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        print('Build failed; see analysis/android-build.log'); return result.returncode
    target = ROOT / 'release' / f'LingxiWorkspace-{VERSION}-Android-arm64.apk'
    target.parent.mkdir(exist_ok=True)
    shutil.copy2(ROOT / 'android/app/build/outputs/apk/release/app-release.apk', target)
    print(str(target))
    print('SHA256', hashlib.file_digest(target.open('rb'), 'sha256').hexdigest())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
