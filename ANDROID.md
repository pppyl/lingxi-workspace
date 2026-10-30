# Android 独立客户端

安装发布的 ARM64 APK 后打开「本地工具台」。首次使用在账号管理登录自己的手机号。此版本内置 Python 服务与静态前端，电脑关机也不影响手机上的服务。

应用使用系统 WebView、原生 HTTP/2 网络栈和 Android Keystore。账号、任务及提交记录保存在应用私有目录。卸载会清除本机数据；使用同一签名更新可保留数据。发布文件不包含开发时的数据库或登录信息。

后台任务运行时有持续通知。应用右上角菜单提供重新加载、后台运行说明和停止退出。允许通知及后台运行有助于看到任务状态；Android 的休眠、断网和厂商电池策略仍可能推迟任务。启动后会补查每日签到；不会补做过期秒杀或未知状态的付款请求。

## 构建

需要 JDK 17+、Android SDK 35、Gradle 8.10.2、Python 3.12。项目使用 AGP 8.8.2、Chaquopy 17.2.0。

设置 `JAVA_HOME`、`ANDROID_HOME`，以及指向 Gradle 可执行文件的 `LINGXI_GRADLE`，然后运行：

```powershell
python build_tools/build_android.py
```

脚本按白名单复制公开源码与前端到 `.build/android-python`，构建签名 APK 到 `release/`。不会复制 `.local`、分析文件或用户数据库。

首次构建在 `.local/android-release.keystore` 生成专用签名密钥，密码保存在同目录的 `android-release-password.txt`。这两个文件不会上传 GitHub，必须妥善备份；以后更新 APK 需要继续使用同一密钥。不要把它们随安装包分发。

本版按用户要求完成安装，手机登录、任务和用券功能由用户自行实测。
