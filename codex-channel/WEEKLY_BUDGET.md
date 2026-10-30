# 周额度比例与准确分账：开发状态

2026-10-30。目标是给指定朋友设置整周额度的可分享比例，并准确区分本人和朋友的使用。本人其他客户端的消耗不得扣减朋友的预算。全账号总表增量、token 比例、credits 换算均不作为替代方案。

## 当前可用版本

v0.3.0 仍按模型请求次数限制。准确周额度预算尚未完成真实验收，也未发布为可用功能。开发分支的任务归属隔离是后续分账基础；安装旧版不会自动得到百分比限制。

## 官方提供了什么

- `account/rateLimits/read` 提供账号总额度、窗口和重置时间；总表不区分使用者。周窗口可能位于 primary，需按窗口长度识别。
- `POST /backend-api/wham/usage/thread_usage/query_v2` 可按根任务及后代查询 `weekly_limit_percent`，并返回 `data_status`、`usage_source` 和 `data_as_of`。
- 已安装 CLI 0.166.0-alpha.5.1 的 `account/usage/read {threadId}` 返回估算 credits/USD/tokens，没有周占比。TUI 的任务统计通过独立后端接口读取周占比。
- 周占比来自后端已记录的任务使用量。任务响应没有独立周窗口 ID；核验前不能直接累加跨周任务的整个生命周期读数。

固定源码：[Codex task_usage.rs](https://github.com/openai/codex/blob/5c6ef3d1a6769a3ea78080792bd67a2c42affdcc/codex-rs/backend-client/src/client/task_usage.rs)、[Codex responses_metadata.rs](https://github.com/openai/codex/blob/5c6ef3d1a6769a3ea78080792bd67a2c42affdcc/codex-rs/core/src/responses_metadata.rs)、[TUI task_panel.rs](https://github.com/openai/codex/blob/5c6ef3d1a6769a3ea78080792bd67a2c42affdcc/codex-rs/tui/src/analytics/task_panel.rs)。

## 真实核验结果

已通过 CLIProxyAPI 8.0.47 的本机管理代查读取官方接口，OAuth 凭证由适配器内部使用。只取管理列表中已解码的账号 ID 摘要，并与官方 usage 的账号 ID 匹配；未读取 OAuth 文件、导出 token 或记录完整授权列表。

账号总表和任务查询均返回 HTTP 200。但 HTTP 成功不代表账单完整：新代理请求的任务周占比为 null，状态 unavailable；同账号的原生聊天对照有周占比，但状态 partial，账单时间落后读取时间约 83 分钟。该观察不能证明所有账号或所有任务都具有相同延迟，也不能证明新请求一定会在某个时限内入账。

探针使用临时回环管理配置与随机密钥，禁远程管理及控制面板，结束后停止适配器并删除临时配置。原安装配置保持管理入口禁用。这条通用管理代查仅用于本机实验；生产计量桥应固定官方目标路径、禁止重定向并只返回允许的计量字段。管理 API、账号摘要和管理密钥不得暴露给朋友。

适配器固定源码：[v8.0.47 api_tools.go](https://github.com/router-for-me/CLIProxyAPI/blob/90c447d3a8727d115aa7552ec93ce023736ed492/internal/api/handlers/management/api_tools.go)、[v8.0.47 codex_executor_request.go](https://github.com/router-for-me/CLIProxyAPI/blob/90c447d3a8727d115aa7552ec93ce023736ed492/internal/runtime/executor/codex_executor_request.go)。

## 实现与验收边界

供应端应为每个朋友的任务分配独立上游编号，并在转发前保存请求归属。客户端提交的任务号、缓存键和父任务字段不能直接用作扣账依据。同一朋友的连续请求可以关联同一任务；不同朋友即使提交相同编号也必须隔离。

后续百分比放行仅能依赖所属任务的完整、可验证、新鲜账单。partial、unavailable、null 或过期数据表示待结算，不能按零扣减，也不能改用本人消耗估算。账号切换、周重置、断流与重启必须保留待核对状态。账单异步且单次生成成本事前未知，因此还需区分“准确事后归账”和“绝不超过比例的实时硬上限”。

发布前仍需完成：真实通道任务可归因；本人并行使用不计入朋友；跨朋友隔离；账单完整性与时效；重复查询幂等；重置分期；缺读数停止；在途请求边界；Windows/macOS 与双机验证。未通过前不发布“准确周额度共享已完成”的版本。

## 按 token 与缓存校准（用户提出的后续方向）

用户进一步提出：根据输入、缓存命中、输出等 token 记录与官方额度变化，参考 API 用量推导周额度消耗。该方向可以建立账号/套餐相关的经验模型，须经过独立样本验证；当前未将估算作为精确预算放行依据。

网关现记录 `input_tokens`、`cached_input_tokens`、`uncached_input_tokens`、`output_tokens`、`reasoning_tokens`、请求与返回的模型/档位。缓存输入是输入总数的子集，推理 token 是输出总数的子集。缺明细记为未知；异常值不参与计算。请求归属与 token 原始读数单独留存，估算更新不能改写原始记录。

初始线性模型可写为：

`估算周占比（百分点） = a × 未缓存输入/1e6 + b × 缓存输入/1e6 + c × 总输出/1e6`

三个系数需按模型、推理档位、速度档位及同一账号套餐校准。API 单价仅可提供初始相对权重或合理性参考，不能直接给出订阅周额度兑换率。若验证误差表明线性模型不适合，应拒绝该模型并重新设计实验。

校准标签优先采用完整的官方任务账单，且 `data_as_of` 覆盖该任务结束时间。账号整数百分比有舍入区间；同时有未记录的本人或其他设备请求时，总表变化无法归到朋友，不能作为精确训练标签。使用总表做实验需完整记录同期所有消耗或隔离实验，并处理入账延迟；当前离线工具只接受完整的任务级标签。

训练集与验证集按任务分开，避免同一任务或同一累计账单重复进入两组。验证报告给出平均绝对误差和最大误差，单位为百分点；这些观测误差不构成未来误差上界。接口、套餐、模型或计量方式变化后需要重新验证。没有完整标签时保留采样数据，不输出伪造的系数、准确率或置信区间。

当前代码及校准工具属于开发分支，尚未接入实时预算执行。正式版仍为 v0.3.0。

## 开发分支里的离线工具

进入 `codex-channel` 目录后运行：

```sh
python -B scripts/calibrate_usage.py /absolute/path/to/samples.json
```

输入为 `{"training": [...], "validation": [...]}`。每条样本包含：

- `task_id`：完整任务的内部编号；训练与验证不得重复。
- `calibration_scope`：同一账号、套餐和额度规则时期的匿名标记；所有样本必须一致，报告不输出该值。
- `model`、`reasoning_effort`、`service_tier`：同一组合单独拟合。
- `uncached_input_tokens`、`cached_input_tokens`、`output_tokens`：任务完整生命周期内的非负整数汇总，推理已计入输出。
- `weekly_limit_percent`：官方完整任务账单的周占比，允许大于 100。
- `data_status: "available"`、`usage_source: "included_plan"`、`token_source: "upstream_reported"`。
- `completed_at`、`data_as_of`：含时区的 ISO-8601 时间；账单须覆盖任务结束。

至少 8 个训练任务、4 个独立验证任务。这个下限仅用于启动实验，不保证泛化精度。三类 token 必须有足够独立变化；完全共线、负拟合系数、缺缓存明细、混账号/套餐、混档位、部分账单、过旧账单和训练/验证任务重复都会被拒绝。该工具校验输入声明，不独立认证账单来源，数据集应由供应端可信采样和官方查询生成。

输出为每百万 token 的三个经验系数，以及独立验证集的 MAE/最大误差（百分点）。固定标注 `experimental_estimate` 和 `not_billing`；不会修改通道限制。10 项数学与输入校验测试通过；54 项网关/快速连接/归属/用量回归测试通过。合成数据测试仅验证算法与拒绝条件，不证明真实订阅预测精度。

本轮更新网关的真实短句调用成功，记录输入 314、输出 5、缓存输入 0、推理 0；官方该任务账单仍 unavailable。当前没有足够完整的真实标签，尚未产生实测系数或真实预测误差。
