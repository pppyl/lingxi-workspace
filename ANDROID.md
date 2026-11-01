# Android 独立客户端 v1.5.0

安装 `LingxiWorkspace-1.5.0-Android-arm64.apk` 后打开「本地工具台」。适用 Android 7.0+ ARM64 手机。首次使用在账号管理登录自己的手机号。此版本内置 Python 服务与静态前端，电脑关机也不影响手机上的服务。

应用使用系统 WebView、原生 HTTP/2 网络栈和 Android Keystore。账号、任务及提交记录保存在应用私有目录。卸载会清除本机数据；使用同一签名更新可保留数据。发布文件不包含开发时的数据库或登录信息。

启动时及每 6 小时检查 GitHub 正式版本，发现新版后显示更新提示和安卓 APK 下载入口；「平台管理 → 版本更新」可手动检查及查看更新说明。下载新版后直接覆盖安装，不必卸载。页面刷新和程序重启复用检查缓存，不会反复请求 GitHub；网络失败会显示原因。

手机服务仅在应用内的回环地址运行，并由每次启动的原生会话保护。电脑服务新增的 IPv4／IPv6、域名和远程访问密码功能，不会将此 APK 的手机服务自动开放到外网。

后台任务运行时有持续通知。应用右上角菜单提供重新加载、后台运行说明和停止退出。允许通知及后台运行有助于看到任务状态；Android 的休眠、断网和厂商电池策略仍可能推迟任务。启动后会补查每日签到；不会补做过期秒杀或未知状态的付款请求。

## 构建

需要 JDK 17+、Android SDK 35、Gradle 8.10.2、Python 3.12。项目使用 AGP 8.8.2、Chaquopy 17.2.0。

设置 `JAVA_HOME`、`ANDROID_HOME`，以及指向 Gradle 可执行文件的 `LINGXI_GRADLE`，然后运行：

```powershell
python build_tools/build_android.py
```

脚本按白名单复制公开源码与前端到 `.build/android-python`，构建签名 APK 到 `release/`。不会复制 `.local`、分析文件或用户数据库。

首次构建在 `.local/android-release.keystore` 生成专用签名密钥，密码保存在同目录的 `android-release-password.txt`。这两个文件不会上传 GitHub，必须妥善备份；以后更新 APK 需要继续使用同一密钥。不要把它们随安装包分发。

更新前请保留原签名。安装包提供 SHA-256 校验值；签名不一致时不能覆盖安装，应先核对下载的文件。
