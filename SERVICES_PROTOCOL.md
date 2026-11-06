# APP 已订业务查询与短信确认退订

## 实现边界

`services_query.py` 注册 `GET /api/services/query?login_id=...`，使用所选共享号码的 APP 凭据，只读查询手机业务，并复用同一官方响应已明确返回的关联固话、宽带明细。返回 `groups`、`notes`、`queried_at` 和公开 `account`。

`groups[].items[]` 为 `{name, fee, status, fields, children, cancel}`；`fields[]` 为 `{name, value}`，结构化业务说明与合约以 `children` 递归保留。查询本身不发送短信或执行业务变更。普通手机业务的退订使用独立的准备、手动获取验证码和显式确认提交接口；不提供订购、主套餐撤销、权益领取或打开业务网页的操作。

所有协议调用、文本解码和计算均为 Python。APP 包中出现的 JavaScript 仅用于静态核对，不执行其初始化函数、业务逻辑或浏览器内容。已有 `AppLoginSession` 负责平台统一的请求封装、加密和签名。

## 可复查的静态来源

- 缓存包：`analysis/phone-bundles/businessQuery-1791976526.jsbundle`，1,512,708 字节。
- SHA-256：`409dd1bda2c00994f54202ea7ebcbb88153416da06ab763d975b52fa3bd97fcd`。
- `analysis/services-protocol/source-manifest.json` 保留所选完整 Metro 模块的原包行号、字节范围和单模块 SHA-256；`sources` 与 `details-sources` 存放精确原始模块。
- 请求及旧版分支边界 REA 主证据：`ev_3f534203c5015d6b4c86caee70616a7f8641db056fc3117c781f91876819cebf`，摘要 `ev_dcafc2f9a656106a1f24bcb7e8366d3ffe994ad2c79b437afdcdb5dabad5cc11`，15 个文件均成功静态解析。
- 当前主路径、月份、字段、价格与状态 REA 主证据：`ev_3cdbf84ef3e5b9f5ef44206dbaac50056559d58e39ae9b861f02bd5bafd6a0e0`，摘要 `ev_2d1387a31ddf511c871bb06b6c880e7a43022cf4ef10f1e5c1c5ad0fb83ced5d`，8 个文件均成功静态解析。
- 家庭场景开关与内置缺省配置证据：`ev_b3557d60edee335044b83d087e8226821dfab832c118eb544ea1fc52771e3747`，摘要 `ev_16784d79d7d05c266daae2bc8d262bfb17586e4f0087de8194227fff20b8e752`，2 个文件均成功静态解析。复核记录为 `analysis/services-protocol/discovery-review.json`。
- 完整会话证据导出：`analysis/services-protocol/rea-evidence.json`。

这是此前保存的官方 RN 包，不是本次从手机私有目录重新读取的文件。REA 的静态语义恢复仍包含 Metro 动态解析和控制流未知项；本实现依赖对所选完整模块的局部核对，不将静态存在等同于当前手机运行时执行。当前号码的线上可用性和服务端响应需由主任务统一只读实测。

## 官方主查询

网关 `https://client.app.coc.10086.cn`，20109 接口路径为 `/biz-orange/BN/queryBuinessService/getNewQueryBusiness`，其中 `Buiness` 是官方原始拼写。

请求体：

```json
{"cellNum":"所选共享号码","bgnMonth":"2026-11","endMonth":"2026-11","planType":"0","busiType":"01"}
```

原包第 208 行模块 `26101419159208` 构建查询参数；第 76 行月份选择器明确使用 `YYYY-MM`。`cellNum` 的合并位于第 88 行公共请求模块。前端不传入任意手机号、产品编码或业务操作参数。

| 字段 | 官方含义 |
| --- | --- |
| `planType=0/1/2` | 手机 / 固话 / 宽带 |
| `busiType=01/02/03/04` | 我的套餐 / 增值业务 / 基础功能 / 活动 |
| `bgnMonth`、`endMonth` | 本次均为北京时间当前月，格式 `YYYY-MM` |

主要响应路径：

```text
rspBody.allBusiQryList[]
  .allBusiInfoList[]        # 明确的 planType，按手机/固话/宽带各自展示
  .busiTypeInfoList[]       # 关联线路层
  .busiInfoList[]           # productType 对应分类
  .productInfoList[]        # 已订业务产品
```

第 209 行模块核对响应 `planType` 与请求一致后才接受明细。实现要求当前所请求类型的响应范围存在，避免将错误类型冒充手机业务；同一已选账号的响应若还明确给出 `planType=1/2` 的完整明细，则按固话/宽带分组直接复用，不额外请求关联类型。一次成功的空列表是“官方暂未返回已订业务”，不补业务数量或价格。

原包第 208 行 `mainRequestFirstBusiType` / `needsRefetch` 的规则是：响应 `rspBody.busiType` 为空时，首响应已覆盖分类，切换标签复用它；非空时，各标签需分别请求。实现先查询手机 `01`，只有官方明确返回非空 `busiType` 时才追加 `02/03/04`，并仅接纳对应分类，避免重复展示。

## 关联类型与普通查询次数

静态核对确认 20110 路径 `/biz-orange/BN/userGhBroadbandInfo/getUserPlanType` 的 HTTP 请求只有当前 `cellNum`、`bgnMonth`、`endMonth`，未观察到额外亲密号码类型字段；`userPlanTypeInfo.userTelphoneState/userBroadbandState=1` 才会在原生家庭场景显示关联类型。依据为第 219、220、88 行。

这个辅助查询并非普通页面无条件调用：第 208 行 `getAllBusinessInfo` 仅在 `isFamilyScene()` 为真时执行 `setPlanTypeTabs`；条件是官方远程 `RN_BusinessQuery_UserPlanTypeProvince` 包含 `loginProvince`。第 31 行完整内置缺省配置没有这个 key，第 29 行远程合并流程不为它增加缺省值，因此缺省不会启用辅助查询。

当前项目没有接入这项官方远程开关来源，普通查询按官方内置缺省不调用 20110，也不增加开关获取请求或猜测支持省份。关联固话/宽带只复用主接口已明确返回的 `planType=1/2` 明细；没有明确返回的类型不额外补查，也不宣称该账号没有关联类型。所有结果来自同一已选账号的官方响应，不从其他共享账号取数。

完整分类响应只需一次 20109；官方按分类返回时，仍保留必要的 20109 分类 `02/03/04` 查询，不删减已订业务类别。

包中还存在 `/biz-orange/BNY/contracts/qryOrderedBusiness` 与旧版 `/biz-orange/BN/queryBuinessService/getQueryBusiness`。它们属于另外的页面分支、字段模型和产品类型，不能因主接口失败就自动回退；本实现没有发出这些请求。

## 字段、价格与状态

产品名源于 `productName`，APP 将它派生为 `businessName`，不存在时才按官方业务编码回退。`busiFee` 是原价文案，`actualPayFee` 是实际消费。`fee` 保留 `busiFee` 的官方价格与单位文本；实际消费在 `fields` 单独保留。不存在、空值与 `--` 不补为 `0`。官方给出的 `0元/月` 是真实零；单独数值 `0` 也不擅自追加货币或周期。

`freeType=0` 在第 276 行卡片中只对应“减免”标签，不能推导为免费或实付 0。`majorType=0/2` 对应主套餐/家庭套餐。订购、有效期及退订时间分别保留 `orderTime`、`validDate`、`expireDate`、`unsubscribeTime` 的官方文本，包括“长期有效”和“至双方约定之日止”。

`busiInfomsg`、`businessdetailsList[].title/titleValue/businessdetails[].bubtitle/vlues/remark` 保留为说明和递归详情。第 230、286 行明确支持部分省份将 `busiInfomsg` 作为 Base64 富文本；Python 仅解码符合规范且可识别为中文/HTML 的 UTF-8 说明，再解析为纯文本，不加载脚本、图片、链接或远程资源。

`contractList.contractDetailsList[]` 的合约名、有效期及官方状态作为子项目保留。未知枚举放入 `fields`，不猜收费、有效期或活动资格；有效日期明确在未来或过去时可以分别标注待生效/已失效。

| 字段 / 条件 | 静态可确认的显示 | 来源 |
| --- | --- | --- |
| `busiState=3` | 已失效，所有分类 | 第 276 行卡片 `X`；第 212 行排序 |
| 分类 `04` 的 `busiState=2` | 即将到期 | 第 276 行 `3===M && '2'===W.busiState` |
| 非 `04` 分类的 `busiState=1` | 即将失效 | 第 276 行 `3!==M && '1'===W.busiState` |
| `unsubscribeFlag=0` | 已退订；未来主/家庭套餐的撤销场景为已撤销 | 第 276 行中文标签；第 212 行派生 `revokeSign=0` |
| `revokeSign=0/2` | 已撤销 / 撤销中 | 第 276 行中文标签 |
| 合约 `cancelState=1` | 已退订 | 第 212 行 `showCancelTag` 与合约卡片 |

`unsubscribeFlag` 与 `cancelFlag` 不能混淆。第 276 行存在明确的 `!q && '0'===W.unsubscribeFlag` → “已退订”文本；另一处 `cancelFlag=0` 只是原生退订按钮的条件。普通手机业务的退订按钮与独立提交流程见后文；合约退订和主套餐撤销不能套用普通退订接口。

来源是已订业务列表的正常条目、状态为空或 `0` 且无明确未来/过期/退订信息时显示“已订”；未识别的业务状态码仍作为官方状态码保留，`status=null`。

## 凭据与错误处理

同一账号的并发请求通过一个 Future 合并，返回结果深拷贝。不同账号使用独立凭据快照和 transport；同一查询中的分类共用 transport。普通查询直接复用有效 APP Cookie，不因程序重启、凭据修订号或普通查询而登录、换票、初始化。

只有缺少 Cookie 或明确的 `400007`、`400009`、`410000`、`420000` 才调用平台传入的恢复回调，当前只读请求总计最多一次；恢复后重读凭据并核对手机号。共享恢复回调负责平台级同账号合并。没有账号或恢复后身份变化时不发出业务请求。`228000` 等一般失败不转换身份、不发送短信。

官方 `-800`、`8053` 或明确请求频繁提示返回 `RATE_LIMITED`，不重试并停止该次剩余分类请求。首项失败返回明确错误；后续项失败保留已取得的业务并在 `notes` 提示。不会重放已经成功的分类。

只输出明确选定的业务说明、价格、时间、类型和状态字段；不输出 Cookie、token、实例凭据、券码、任意原始响应或未掩码手机号。富文本只保留可见纯文本，手机号掩码，说明中的认证参数隐藏。

## 验证边界

主任务通过普通 ADB 在当前手机的 6815 账号打开“已订业务”，观察到我的套餐、增值业务、基础功能、活动四个分类，卡片展示订购时间、有效期限、主套餐标签和说明入口；没有点击订购或退订。页面观察与上述 1621 的 Python 单号实测分别作为界面和接口依据，不声称两号业务数值一致。

`python -m unittest tests.test_services_query -v` 的合成响应测试覆盖官方参数与月份、完整四分类只发一次请求、分类型省份保留四分类必要查询、明确关联明细直接复用、原价/实付精度、减免、零/缺失、有效期和状态、Base64/HTML 纯文本、隐私字段隔离、递归说明/合约、并发合并、账号隔离、恢复一次、Cookie 回写、限流停止及路由。

主任务统一执行过一次 1621 号码只读实测：20109 返回 `000000`，首响应复用四分类共 85 条（11/60/5/9）；当时无条件调用的辅助 20110 返回 `110001` 系统繁忙，未重试。脱敏报告为 `analysis/services-protocol/live-check.json`。本次按静态官方条件移除无条件辅助调用，没有再次线上查询；报告不包含首次原始响应，不能据此宣称它还包含哪些关联类型。测试验证 Python 的映射和行为边界，本子任务没有独立进行任何线上查询、手机操作或全账号批量实测。


## 普通业务退订（Python）

离线证据：同一已保存官方 RN 包的模块 `26101419159212/213/233/234/235/236/241/246/276`；精确原包行及 SHA-256 见 `analysis/services-protocol/unsubscribe-source-manifest.json`，只读核对记录见 `analysis/services-protocol/unsubscribe-findings.md`。本轮使用普通离线文件阅读，未增加新的 REA Evidence，未向真实号码发送短信或提交退订。静态协议与合成响应测试不等于已经线上办理成功。

### 允许退订的范围

模块 276 当前主路径：当前月份、非“我的套餐”中的主/家庭套餐，`cancelFlag='0'`，`unsubscribeFlag` 非 `0/2`，`busiState!='3'`，`contractFlag!='0'`，且模块 212 的业务编码校验通过。已失效或已退订条目不提供新提交。本实现只支持与所选共享号码一致的手机普通业务，明确支持 `codeType='01'` 的 `productCode`，以及 `codeType='02'` 的 `sPCode+bizCode`，并要求有唯一 `instanceId`；不猜未知类型或缺失编码。关联固话/宽带、合约、主套餐变更/撤销及专属退订甩单分支提供原因并引导官方 APP，不能伪装为已实现。

服务端在查询过程中保存该账号原始产品记录，只给前端返回不可预测的 `cancel.action_id`。客户端仅能引用本账号的记录，传入任意 productCode、instanceId 或手机号不会改变官方请求。重复的同一业务实例共用一份 action；刷新查询更新可选 action，但不会替换已建立的短信上下文。跨月的列表 action 需要重新查询。

### 准备与影响确认

**退订会话专用线路（已修正）**：准备、发码和最终退订均使用 `https://clientaccess.10086.cn`，`x-qen=2` 及旧版 AES，签名按实际 IT URL 计算。原 RN 公共层的 `domainType='2'` 在原生 `CommonUrlApi.getBaseUrl()` 明确映射此域，`ServiceCmccUrlMap` 对 20035/20096/20057 的 SDK 映射也一致；`KoHttpHelper` 默认 `x-qen=2`，新版加密白名单不含 IT 域。此前误用默认 `client.app.coc.10086.cn + x-qen14`，导致短信请求 HTTP 403。原生 Java/smali 来源及散列保存在 `analysis/services-protocol/native-sms`、`sms-domain-static-evidence.json` 和 `sms-domain-findings.md`。

`AppLoginSession` 的网关和请求加密版本是每个实例的内部配置，不从用户请求或共享凭据 JSON 读取；退订的独立实例在准备时设为 IT/2，其后 SMS 与提交共用它。IT 已登录请求补全 `tel`、`city`、`prov`；其它模块保留原已验证默认网关、加密及公共字段，不因这次修正整体改线。

`POST /api/services/cancel/prepare`，JSON `{login_id,action_id}`。只读查询官方 `/biz-orange/BH/unifyQueryService/getUnifyQuery`：

`{cellNum:本账号手机号,bindedFlag:'2',instanceId:原始业务实例}`。

模块 212 从 `rspBody.uniQryRecList[0].prodInfo[]` 读取影响：`busiInfomsg` 含 `1/2` 的为关联优惠，含 `0/2` 的为将一同退订的绑定业务；返回 `related_services`、`bound_services`、`warnings`、原始说明纯文本和生效选项。`offLineFlag='1'` 提示停售后无法再订购。影响查询失败或格式异常时不会允许继续到短信提交。

准备请求复用有效 APP 身份，同一 action 的并发准备合并；仅缺少 Cookie 或明确认证失效时恢复一次，只重发该只读影响查询。准备后保留同一个 `AppLoginSession` 及 HTTP transport 给本次短信和提交使用；共享凭据 revision 变化不会替换它。账号退出或绑定手机号变化后不能继续。操作确认上下文最长保留 30 分钟，这只是本地操作有效期，不是另造 APP 凭据过期时间。

响应包含 `{cancel_id,name,phone_masked,effective_options,warnings,bound_services,related_services,description,sms_required:true,account}`。`effective_options` 的 value 对应官方 `busiReturnEffTime`：`0` 下月、`1` 立即、`3` 次日；官方为 `2` 时使用 `effTimeType` 的 `|` 分隔可选值。缺省时间用 `default` 并提示以短信通知为准，不能误当成次日失效。

当前界面默认采用官方返回的第一个失效选项，以普通文字展示，不提供额外的失效时间选择框。资费、状态放在业务展开详情中。退订确认不再要求勾选复核项，也不展示“请核对退订业务及影响，再获取本次退订验证码”的重复提示；用户输入验证码并点击“确认退订”即明确确认本次操作。官方关联影响、停售说明及真实错误/办理结果仍保留。

### 手动获取退订验证码

`POST /api/services/cancel/sms`，JSON `{login_id,cancel_id,effective_type}`，仅用户明确点击后发送 `/biz-orange/BH/sms/getSmsCode`：

`{cellNum,busType:'1',productName:原始businessName+绑定业务名称（英文逗号连接）,productType:原始codeType,effectiveType,productId:原始productCode或空,spid:原始sPCode或空,bizCode:原始bizCode或空}`。

官方转换：return0→effectiveType2，return1→effectiveType1，return3→effectiveType5；官方缺省时间的初始 effectiveType 为3，最终 JSON 省略 busiReturnEffTime。短信成功只看 `retCode='000000'`，不要求 rspBody。有效期5分钟、重发间隔60秒，服务端同时限制；改选生效方式后必须重新获取对应验证码。短信会话不会自动恢复身份或重发请求。发送结果不确定会明确提示，不假定验证码发送成功。

### 显式确认、成功与不确定结果

`POST /api/services/cancel/confirm`，JSON `{login_id,cancel_id,effective_type,sms_code,confirmed:true}`。服务端校验6位验证码、发码时的生效方式和当前账号；最终仅一次调用 `/biz-orange/BH/unifyCancelService/getUnifyCancel`：

`{cellNum,businessName:原始业务名,productType:codeType,productId,spid,bizCode,smsCode,busiReturnEffTime,instanceId}`。

模块 234 的 `getRiskLevel().then(...).catch(...)` 为不等待的异步风险报告，`constId/devToken` 不属于20057最终请求参数，也没有另行 accessCode/auth 交换步骤。本实现不调用浏览器或伪造风险令牌；静态证据只能证实官方未等待该报告，不能保证所有省份的服务端都不核对其他风险状态。

`000000` 返回 `status=success`，随后界面只读刷新最新业务，同时从成功响应起计时 3 秒自动关闭退订弹窗，不等待刷新完成。手动提前关闭会清除计时，避免影响下一次弹窗。`900000` 返回 `accepted` 表示已受理；`999999`、缺少结果码或提交后网络/解析失败返回 `unknown`，只提示核对官方结果，不能自动重放，也不按成功自动关闭。`312008` 允许用户纠正验证码，`312009` 要求重新获取。其他明确拒绝透传脱敏原因、认证失效或请求频繁提示，绝不转换成成功。

每个账号的同一实例共用操作锁，跨刷新 action_id 的并发确认也最多发出一份提交。成功/已受理/不确定结果按本进程的账号+实例记录，新查询不能绕过重复提交保护；不会因普通刷新或共享凭据变化清除它。官方明确的已退订状态依旧展示为已退订。`services_queries.cancellations.busy_login(login_id)` 标识发送/提交中或5分钟短信验证期，供共享身份维护入口避免挤掉会话；该方法不访问官方服务。

### 验证

`python -m unittest tests.test_services_query -v` 覆盖原14项只读协议，以及不透明参数归属、普通退订范围、官方关联影响、生效方式映射、SP业务参数、缺省时间不误填、同一发码会话随共享 revision 更新仍绑定、退出与换手机号阻断、一次只读恢复、验证码错误/不匹配/冷却/过期、显式确认、无 rspBody 成功、结果不确定不重放、跨 action 并发提交一次和 JSON 路由参数注入隔离。所有退订测试使用合成响应，无真实业务变更。


2026-11-06 修正网关后的受控线上验证：备注 0314 的“移动来电名片基础包体验版”准备接口成功，退订短信接口返回发码成功，用户明确确认已收到短信。没有输入验证码，也没有调用最终退订接口。回归测试覆盖 IT 实例路由/旧 AES/实际 URL 签名、其它模块默认线路保持、会话绑定及失败不重放等，共 122 项相关 Python 测试通过。
