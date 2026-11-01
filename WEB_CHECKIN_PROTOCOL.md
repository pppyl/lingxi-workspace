# 网页渠道签到协议核对

入口：`https://wx.10086.cn/qwhdhub/qwhdmark/1021122301`。

该 URL 与主签到相同，服务器会依据登录渠道选择不同业务。APP 身份在官方前端走 `mark31/markstatus`、`mark31/domark`；非 APP 身份使用 `info/prizeInfo` 的 `todayMarked`、`markedTimes`、`totalMarkTimes`，签到为 `do/mark`，空 JSON 请求。不能把主签到的日历记录复制为新渠道进度。

## 登录及隔离

微信环境进入官方微信 OAuth，需要有效微信授权；普通网页则由 `qwhdsso/login?dlwmh=true` 引导至官方微门户短信登录。两个渠道均检查服务器 `user/info.channel` 和号码绑定，严格要求各自身份。微信授权与网页短信登录并非同一种身份，不能用伪造 User-Agent 代替授权。真实微信会话的核对与接入记录见末节。

网页实际流程（与微信授权严格隔离）：

1. 从官方登录页提取当次 CSRF、reqTmp 和 SSO 回调，保持同一 Cookie 会话。
2. 用户主动点击后调用 `/website/bind/sendMsg`，手机号采用官方 AES-CBC/PKCS7、十六进制大写编码；短信发出后本地等待至少 60 秒才可重发。
3. 用户输入验证码，调用 `/website/bind/bindAccount/new`。号码和验证码分别加密，保留官方 `pwdType`、`checkCtrol` 与归因字段。
4. 成功后由 `/website/api/singleAuth` 获取 SSO 换票令牌，完成官方 `qwhdsso/redirect` 回调，查询并核实活动账号／渠道后才加密保存独立会话。

`web-checkin:1021122301` 为独立凭据范围；有效期由官方 Cookie／JWT 到期信息决定，不套用其他活动两小时。没有登录时不自行转换 APP 凭据或发送短信。验证码仅在单次内存登录操作使用，不写入账号状态、日志或凭据缓存。

## 请求与调度

每天默认对全部有效账号执行。自动时间是开始时间；新增和恢复登录后补做尚未完成的签到。周期累计达到官方 `totalMarkTimes` 时显示本周期已完成，不再发签到提交，只在后续每日检查是否进入新周期。主签到和本渠道分别记账、加锁和保存提交标记。提交前查官方进度，提交后核实；断网或响应不明不自动重放。资格限制、限流当天暂停，其他失败有界重试。页面轮询仅读本地，主动刷新使用已有会话查询。

## 分析来源和验证边界

2026-11-01 使用 Scrapling 0.5 的 Selector 提取当前公开页面资源。`build_tools/inspect_web_checkin.py` 可重现公共资源获取，不带用户凭据，也不发送验证码或签到。

- 当前官方 `index.f6a4f350.js`：SHA-256 `e30e13c54f7b9f4c433f6764f04692886ac0e1ef805f6223499eabf93dffee0d`。
- 官方 `signInMin1010092026.js`：SHA-256 `0498a5a4d1396dec1ac11f09168420467b69debdc3809568d38c8ef584391057`。
- 官方 `indexMin1010992026.js`：SHA-256 `8aedc71ef8a1e4567aa4cbbf224721225d47e2ba31c07d6c341198966dae4e33`。
- 官方 `crypto20251103.js`：SHA-256 `0a59c71f9ec53974dcdf10bbc5cb0f50c86fb3040938556fcdfa6ddf00e2ebc2`；Python AES 输出已与其纯加密函数离线对照。
- REA 当前主活动资源静态证据：`ev_1b91ef8b107baac802f48b83f3c5234a28b13536f6869af154e60658cc2fc141`，摘要 `ev_082a813a1bb003eeca13e6d1cebf88983b4a082a08c9a4702b2c8aa74cfcfa96`。静态语义覆盖有限，不将其描述为登录或签到实测。

登录响应与 CSRF 会话绑定、账号及渠道隔离、AES、Cookie 复用、提交去重、恢复调度和前端交互由离线回归验证。微门户短信分支仍须用用户授权号码实测；若官方额外要求设备校验，会显示官方拒绝结果，不能据离线测试宣称已突破验证。

## APP 转微信的定向核对

2026-11-01，经用户指定，使用尾号 1621 的现有 APP 凭据进行两条换票流程对照：正常 APP 入口换票成功，官方 `user/info` 返回号码匹配、`channel=app`；从微信入口创建的 SSO 会话调用同一 APP 换票接口返回 `FAILED`，未返回活动回调。此过程中没有发送验证码，也没有提交签到。

以上验证说明当前已知 APP 换票流程不能直接产生微信渠道身份，不据此断言所有尚未发现的官方接口均不存在。微信授权、微门户网页短信登录和 APP 登录仍须分别处理；不得修改 User-Agent 或本地渠道字段后将 APP 会话记作微信会话。真正的微信渠道接入仍需核对一次已授权的官方微信活动会话。

定向验证只保存不含凭据的结果：`analysis/wx-checkin/app-bridge-probe.json` 和 `app-baseline-probe.json`，原始 Cookie、令牌和用户完整号码未写入这些结果。

## 真实微信渠道的手机会话验证与接入

2026-11-01 用户连接并打开微信中尾号 1621 的活动。REA 手机截图证据 `ev_c75050dcd087b60dcc51d4385db44710a577ebdc61dd8a9a403b716c57cd753d` 显示对应号码、签到成功和 0.5 元话费券。实际前台为微信分身 Android 用户 999，不使用系统 current-user=0 选择凭据。

只查询该前台微信实例的 `wx.10086.cn` 活动 Cookie，在内存中解析 SQLite，不导出 Cookie 数据库或记录原始令牌。复用这一授权后，由 Python 请求官方 `user/info`、`info/commonInfo`、`info/prizeInfo`，核实号码绑定、`channel=wechat`、今日已签到、累计 1/3 次及 0.5 元话费券 `UNUSED`。本次未发送短信、未提交新的签到，也未使用抓包、代理、证书或浏览器执行。

真正的微信入口为 `/checkin/wechat`，凭据范围 `wechat-checkin:1021122301`，状态和记录存于 `wechat-checkin`。它与 APP 和微门户网页短信身份分别隔离，严格要求官方返回相应渠道；共享实现位于无活动清单的 `activities/checkin_common`，单独永久删除网页渠道不会破坏微信渠道。

当前支持用户主动点击“同步微信授权”，从已连接、已允许 root、前台已打开此活动的手机同步既有授权。平时查询和每日任务只复用加密缓存，不轮询手机、不发送短信、不反复转换 APP 身份。同步时核实的签到进度直接用于页面和调度，不重复查询或签到。会话按实际 Cookie／JWT 的官方到期信息使用；本次观测的 Cookie 期限不作为其他账号或后续授权的固定寿命。

以上为手机会话对照阶段的记录；用户随后要求取消手机同步。当前实现已改为下述纯 Python 短信登录，手机同步功能及其运行时代码已移除。

## 纯 Python 微信渠道短信登录实测

2026-11-01，按用户要求清除微信分身用户 999 中 `wx.10086.cn` 的 8 条活动 Cookie 后重新打开页面，官方出现短信登录页。仅删除该域 Cookie，未清除微信账号或其他域的数据。清除后的页面证据 `ev_07f7d31f6262bd68029f07adbac9be116f428f18f6e38fac5bd128a310804bef`，短信表单证据 `ev_97c0839a9947a70338f15260c5b00573eb8e092ef348a0cd7b7a538c49a866b1`。

用户指定尾号 7797 实测。使用全新的 Python HTTP 会话，不导入手机 Cookie：以微信入口建立原始 SSO 回调，在官方 OAuth 跳转中取回该官方短信页的 `redirect_uri`，用网页短信页完成短信验证，随后恢复微信入口的 User-Agent，执行官方 POST `singleAuth` 和 GET 原始 SSO 回调。官方返回的裸十六进制 AES 密文需要先解密再解析 JSON，不能当成异常 HTML 或普通 JSON。

换票后必须请求 `user/info` 核对真实身份，不能仅依据跳转、User-Agent 或短信验证成功断言已登录。实测官方返回号码匹配、`channel=wechat`；随后自动签到返回“签到成功，已核实该渠道今日记录”，`todayMarked=true`、`markedTimes=1`、`totalMarkTimes=3`，奖品 0.5 元话费券 `UNUSED`。验证码收到后直接提交，验证码不保存。

当前 `/checkin/wechat` 使用短信登录弹窗，打开弹窗时自动请求一次验证码，并执行 60 秒重发等待。网页和微信会话仍按各自 scope、状态和提交标记严格隔离。有效微信会话加密复用，官方明确失效后再由用户短信恢复；无须连接手机、root 或调用浏览器。单靠共享 APP Cookie 直转仍未验证成功，当前可用路径是独立短信认证。
