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


def main():
    staging = ROOT / '.build/android-python'
    if staging.exists():
        if staging.resolve().parent != (ROOT/'.build').resolve():
            raise RuntimeError('Android staging path escaped the build directory')
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    modules = ('app app_login android_bridge android_runtime android_activity_bridge http_transport '
               'checkin_client checkin_flash checkin_scheduler coupon_client coupon_use coupon_captcha coupon_cashier_h5 coupon_alipay alipay_h5 '
               'lingxi_client lingxi_scheduler login_storage credential_cache network_access monthly_coupon_auth monthly_coupon_client '
               'monthly_coupon_scheduler star_day_client star_day_auth star_day_scheduler star_day_sms star_day_crypto '
               'platform_manager runtime_paths session_maintenance unified_logins').split()
    for name in modules:
        source = ROOT / (name + '.py')
        if source.exists(): shutil.copy2(source, staging / source.name)
    shutil.copy2(ROOT / 'app_login_public_key.json', staging / 'app_login_public_key.json')
    for directory in ('static', 'templates', 'recharge/static', 'recharge/templates'):
        shutil.copytree(ROOT / directory, staging / directory, dirs_exist_ok=True)
    shutil.copytree(ROOT/'activities', staging/'activities', ignore=shutil.ignore_patterns('__pycache__', 'tests', 'android'))
    for name in ('__init__', 'app', 'accounts', 'cmcc', 'scheduler'):
        shutil.copy2(ROOT / 'recharge' / (name + '.py'), staging / 'recharge' / (name + '.py'))
    for path in staging.rglob('*'):
        if path.is_file() and (path.suffix in ('.sqlite3', '.db', '.log', '.keystore') or path.name.startswith('.')):
            raise RuntimeError('Private runtime data found in Android staging')
    icon = ROOT / 'android/app/src/main/res/mipmap-xxxhdpi/ic_launcher.png'
    icon.parent.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    Image.open(ROOT / 'desktop_assets/app.png').resize((192, 192)).save(icon)
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
