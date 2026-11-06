# 账单查询协议

2026-11-06 核对已连接手机上的中国移动“我的 → 账单查询”。手机实际版本为 12.0.9，进入 React Native `phoneBills` 页面，无抓包、代理、证书、Frida 或 WebView 注入。

## 证据与限制

- 手机入口截图 REA Evidence：`ev_8dde3dd0a58bea7cde82672b14337b9ecb423058c9fce32fd4ef06573f7a7e35`。
- 手机已安装包版本 REA Evidence：`ev_4828731bd850678a5a9a6ecde23dc5192c4217b70e1f87a2919310725063622e`。
- 已保存的官方 `phoneBills-1786347646.jsbundle` SHA-256：`29bfa00029538b5b6e7a572fcea3ba81c1600be1278e35b9a2f84228164d875e`；REA 静态分析 Evidence：`ev_8fe7b62ca82dd610e1852bd188078f16a48c09b38c5c4233a91a1256f4871ddc`。该资源来自此前读取的 APP 缓存，本次无 root，未声称重新读取当前私有缓存或观测原生网络请求。
- 本次普通 Python HTTP 实测：使用共享账号尾号 0314 的已有凭据，当前月与历史月账单、余额查询均返回 `000000`。未发送短信、领取奖品、创建支付或修改套餐。账单原始响应不写入日志或源码，页面仅显示查询所需字段，说明中的完整手机号脱敏。

## 请求

复用 `AppLoginSession.from_credentials` 及现有 APP 加密请求，不重新初始化或换票。

- `/biz-orange/BH/newBillsQry/getNewBills`：`cellNum` 为选中账号，`bgnMonth`、`endMonth` 均为 `YYYY-MM`，例如 `2026-11`。
- `/biz-orange/BN/realFeeQuery/getRealFee`：`cellNum` 为同一账号，查询实时余额。余额失败保留已经成功取得的账单，提示余额未更新。

原生 monthSelector 默认显示当月加此前十二个月；页面沿用此窗口。官方可能按省份配置更短的查询范围，未返回账单时显示暂无数据，不以其他月份替代，不虚构金额。

## 主要字段

| 官方字段 | 页面含义 |
| --- | --- |
| `historyBillInfo[].inBill` | 月份，必须匹配所选月份 |
| `billCycleStartDate` / `billCycleEndDate` | 实际账单周期 |
| `totalBill` | 优惠前金额 |
| `reductionBill` | 费用减免，摘要取抵扣绝对值，明细保留官方负号 |
| `realConsumeBill` | 本号码消费 |
| `realPayBill` | 实际应付；不等同于当前欠费 |
| `billMaterialRes` | 一级费用与 `thirdBillMaterialInfo` 分项明细 |
| `billEntriesOPValue` / `billEntriesSaveValue` / `billEntriesValue` | 一级原价／减免／实际消费 |
| `thirdItemsValue` / `thirdItemsSaveValue` / `thirdItemsRealValue` | 分项原价／减免／实际消费 |
| `billPayInfoMsgRes` | 单位代付、他人代付、专款、活动返费、其他返费 |
| `billSaveInfoRes` | 官方节省金额及分项 |
| `billInfoAllRes` | 官方合账账户消费和应付 |
| `curFee` / `curFeeTotal` / `oweFee` / `realBalanceFee` | 账户余额／总余额／欠费／实际余额 |

金额使用 Decimal，不在前端重新推算。没有返回的数据为 `null`，前端显示“—”；官方返回零则显示 `0.00`。

## 查询边界

无定时任务或官方轮询。并发同账号同月份读请求合并，后续用户主动查询重新读取官方数据。普通查询不因 APP 凭据版本更新而被拒绝。官方明确登录失效时最多恢复一次 APP 会话，恢复合并交由既有共享账号锁；不自动发送短信。限流立即返回“尝试更换服务ip再试”，不重试。`400003` 等请求错误不当作账号过期，不触发重新登录。功能开关及远程访问密码沿用平台统一控制。

同一次账单查询中的账单和余额请求复用一个 Python HTTP 连接池，查询结束后关闭；不同账号、不同查询不共享 Cookie 容器。`AppLoginSession.transport` 是调用方管理的可选传输，未传入时仍按原方式创建和关闭连接。协议加密、参数计算、请求和响应处理均在 Python 中完成，不调用 Node.js、浏览器或 WebView。
