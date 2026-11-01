# 周额度比例：真实校准、独立归属与官方对账

## 0.4.2的两道门禁

“朋友周比例（估算）+ 账号30%保底”在同一邀请上建立两道检查：

- 朋友预算10%：以整周额度100%为分母，供应端可信任务编号隔离本人和各朋友，按上游报告的输入、缓存、输出token估算朋友消耗。本人其他请求不扣减朋友预算。
- 账号保留30%：读取官方604800秒周窗口，总剩余<=30%或读取异常即暂停共享。本人并行使用也会改变这个账号总表。

两道检查都通过才放行。经验范围上端触达朋友预算、请求用量未知、校准过期、官方读数失效、账号/套餐或周窗口变化都会暂停。已接受请求和官方计量延迟可能超调；10%和30%均不构成精确硬保证。旧请求次数仅作异常封顶，不换算周额度。

每个邀请保存创建时参数快照；导入新profile仅影响以后创建的邀请。保护截止最长24小时且不晚于官方周重置；重启保持原期限，续期必须明确设置新截止，仍有有效受保护邀请时拒绝改变截止。没有自动续期或自动提高预算。

## 已完成的真实校准

2026-11-01，原有12个任务全部取得available/included_plan官方标签，账单时间覆盖各任务结束。8训练、4独立验证任务，共28次原有模型调用；输入81778、缓存输入28672、输出2827，明细和结束时间完整。

经验模型为：估算周占比（百分点）= a × 未缓存输入/1e6 + b × 缓存输入/1e6 + c × 总输出/1e6。

三系数非负；标准化特征矩阵满秩3、条件数约2.485。4个验证任务的MAE约2.17e-19百分点、最大绝对误差约4.34e-19百分点，仅说明这批数据在浮点精度下吻合。验证任务级消耗约0.00190–0.00328百分点，不能据此证明大任务、未来任务或累计10%预算无误差。

本次范围限定同一账号/套餐/官方周窗口、gpt-6.1-sol、请求配置low、default档位。low是请求配置，不是返回端对推理强度的独立证明。参数标记为experimental_estimate/not_billing。账号、套餐、模型、档位或计量方式改变后须重新验证。个人profile和账号标记不进入公开包，不能将本次参数套给任意账号。

参考范围按未对账任务数累计验证集最大绝对误差；它没有置信度含义，也不是未来误差硬界。当前没有自动泛化质量门槛；启用前由供应方审查标签来源、满秩、非负、适用范围及验证误差。早期4个缺缓存/结束信息的任务未用于训练。

## 官方读数与归属

- account/rateLimits/read给账号总额度、窗口和重置时间，总表不区分使用者。按604800秒窗口识别周表。
- POST /backend-api/wham/usage/thread_usage/query_v2按根任务及后代查询weekly_limit_percent，并给data_status、usage_source、data_as_of。
- 固定CLI源码观察中的credits/USD/tokens不能直接换算订阅周额度。任务账单可能延迟；HTTP 200不代表标签完整。

供应端为每位朋友分配并持久化独立上游任务编号。客户端提交的任务号、缓存键和父任务字段不直接作为可信归属。不同朋友使用相同客户端任务号也会隔离。未知用量、断流或重启后无法确认的请求保留待核对，不按零处理。

官方替换必须满足：任务属于该通道，available、included_plan，data_as_of覆盖所有本地请求结束时间，校准范围一致。外部任务、跨朋友任务、旧快照或冲突数据被拒绝。partial/unavailable/null不替换成零。官方任务响应没有独立周窗口ID，不能累加跨周任务的全生命周期读数。

目前官方任务账单由供应方通过owner接口导入，没有后台自动轮询或外部签名认证服务。管理API和原始账号凭证留在供应端，不开放给朋友。

固定源码证据：[Codex task_usage.rs](https://github.com/openai/codex/blob/5c6ef3d1a6769a3ea78080792bd67a2c42affdcc/codex-rs/backend-client/src/client/task_usage.rs)、[responses_metadata.rs](https://github.com/openai/codex/blob/5c6ef3d1a6769a3ea78080792bd67a2c42affdcc/codex-rs/core/src/responses_metadata.rs)、[TUI task_panel.rs](https://github.com/openai/codex/blob/5c6ef3d1a6769a3ea78080792bd67a2c42affdcc/codex-rs/tui/src/analytics/task_panel.rs)、[CLIProxyAPI v8.0.47 api_tools.go](https://github.com/router-for-me/CLIProxyAPI/blob/90c447d3a8727d115aa7552ec93ce023736ed492/internal/api/handlers/management/api_tools.go)。

## 离线工具与数据要求

进入codex-channel，运行python -B scripts/calibrate_usage.py /absolute/path/to/samples.json。输入对象含training和validation两个数组，每条包含：

- task_id、同账号套餐规则期的calibration_scope，训练与验证任务不得重复。
- model、reasoning_effort、service_tier。
- 完整非负uncached_input_tokens、cached_input_tokens、output_tokens；缓存是输入子集，推理token是输出子集，不重复相加。
- weekly_limit_percent、data_status=available、usage_source=included_plan、token_source=upstream_reported。
- 含时区的completed_at和覆盖结束的data_as_of。

至少8训练、4独立验证只是实验下限。完全共线、负系数、缺明细、混范围、部分或过旧账单会被拒绝。工具校验输入声明，不能自行证明账单来源；必须由可信采样及官方查询提供数据，不得填合成系数开放真实共享。

界面分别显示朋友估算/经验范围/官方已核对部分与账号剩余/30%阈值。用量未知时显示“待核对”，不把已知小计当总量。验证详情见发布包docs/combined-budget-verification.json。Mac与异地网络仍待实机验证。
