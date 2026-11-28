# Third-party components

The portable package includes Python, Flask and dependencies, curl_cffi, cryptography, Waitress, pystray, Pillow, and MiniRacer. No browser or Node.js runtime is bundled. The UI opens in the user's default browser. Fixed JavaScript and WebAssembly resources execute in the embedded V8 runtime under Python control.

Component license texts are preserved in `_internal/third-party-notices`. Python license text is included with the runtime notices.

- Python: https://www.python.org/psf/license/
- PyInstaller (bootloader exception): https://pyinstaller.org/en/stable/license.html
- MiniRacer: https://github.com/bpcreech/PyMiniRacer
- Lucide navigation icons (ISC, with Feather portions under MIT): https://github.com/lucide-icons/lucide. The bundled license is in `static/licenses/lucide.txt`.
- imageio-ffmpeg (BSD-2-Clause): https://github.com/imageio/imageio-ffmpeg. Its license is collected with the Python distribution notices. The Windows FFmpeg subprocess used for sign-in media playback is a separate Gyan essentials build, licensed under GPLv3 or later. The full license is in `static/licenses/ffmpeg-gpl-3.0.txt`; its build configuration is available through `ffmpeg -version`. Upstream source and build information: https://ffmpeg.org/download.html and https://www.gyan.dev/ffmpeg/builds/. The Android client uses the system MediaPlayer instead.

The release archive checksum is supplied separately as `SHA256SUMS.txt`.

The Android build uses Chaquopy (MIT), Javet (Apache-2.0), OkHttp and Okio (Apache-2.0), Kotlin standard library (Apache-2.0), HTTPX and its dependencies, and Android-compatible Python wheels. Android's installed System WebView displays the local UI only. Business protocol and device calculations run through Python and the bundled V8 library. Android component notices are included in the APK assets under `third-party-notices` and Python distribution metadata.

- Chaquopy: https://github.com/chaquo/chaquopy/blob/master/LICENSE.txt
- Javet: https://github.com/caoccao/Javet/blob/main/LICENSE
- OkHttp: https://github.com/square/okhttp/blob/master/LICENSE.txt
- HTTPX: https://github.com/encode/httpx/blob/master/LICENSE.md
- OpenCV (Apache-2.0): https://github.com/opencv/opencv/blob/4.12.0/LICENSE. Android uses the packaged native image primitives from Python; Windows includes opencv-python and NumPy with their distribution licenses.
