# 网页渠道签到协议核对

入口：`https://wx.10086.cn/qwhdhub/qwhdmark/1021122301`。

该 URL 与主签到相同，服务器会依据登录渠道选择不同业务。APP 身份在官方前端走 `mark31/markstatus`、`mark31/domark`；非 APP 身份使用 `info/prizeInfo` 的 `todayMarked`、`markedTimes`、`totalMarkTimes`，签到为 `do/mark`，空 JSON 请求。不能把主签到的日历记录复制为新渠道进度。

## 登录及隔离

微信环境进入官方微信 OAuth，需要有效微信授权；普通网页则由 `qwhdsso/login?dlwmh=true` 引导至官方微门户短信登录。本次实现网页短信分支，认证后检查服务器 `user/info.channel` 和号码绑定，拒绝把 `app` 身份当作非 APP 渠道。微信授权与网页短信登录并非同一种身份，不能用伪造 User-Agent 代替授权。

网页实际流程：

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

登录响应与 CSRF 会话绑定、账号及渠道隔离、AES、Cookie 复用、提交去重、恢复调度和前端交互由离线回归验证。真实短信登录及签到仍须用用户授权号码实测；若官方额外要求设备校验，会显示官方拒绝结果，不能据离线测试宣称已突破验证。
