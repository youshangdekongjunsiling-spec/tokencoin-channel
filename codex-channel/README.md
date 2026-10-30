# TokenCoin 双向通道 Demo 0.2.0

**图形面板与安装入口：[INSTALL.md](INSTALL.md)。** 同一个插件管理“提供我的额度”和“使用朋友的额度”；支持官方 MCP 侧栏入口，另附本机浏览器启动器。

以下保留命令行工作流及 v0.1.4 的历史验证记录。

**现在已验证：Windows 真实 Codex CLI 通过通道读取文件、看到 3 个测试失败、修复 calculator.py 一行代码，再运行 3 个测试全部通过；测试文件未改。** 工作目录内使用 MXC workspace-write 沙箱，CLI 退出码为 0，共 5 轮模型请求。CLI 用时 23.359 秒，其中 5 轮模型响应流生命周期合计 19.437 秒（含上游等待和生成，不是纯网络往返）；适配器冷启动另用 3.25 秒。macOS 实机和朋友双机仍待验证。证据见 `development-verification.json`；前一轮文本任务证据保留在 `cli-real-verification.json`。

本指南先让朋友用你的供应节点，再交换角色。两个人使用同一个安装包，Windows 与 macOS 可以互联。供应方负责保持电脑、网络和服务在线；消费者在自己的电脑启动 Codex CLI。供应方能看到经过通道的请求上下文，所以首次任务只用下方不含私密数据的例子。

```text
朋友的 Codex CLI
    ↓ 朋友持有的限时通道邀请
你的私网 IP:8781（TokenCoin 网关）
    ↓ 仅在你的电脑内部
127.0.0.1:18317（CLIProxyAPI 适配器）
    ↓ 你的账号授权
真实模型 → 沿原路返回朋友的 CLI
```

插件提供操作引导，Python 程序负责通道。它启动独立 CLI，不切换当前桌面对话的模型。API 与订阅共用网关协议；本轮验证的是本人订阅路径。跨人使用前，供应方需确认上游允许的使用范围。以下网络与邀请操作也可先用 `mock` 演练。

## 1. 双方准备与安装插件

双方各自解压整个 ZIP，保留 `demo/.agents/plugins/marketplace.json` 和 `demo/codex-channel` 的相对位置。macOS 的 `.agents` 是隐藏目录，终端仍可正常使用，不要只复制可见的子文件。

需要 Python 3.10+ 和 Codex CLI。先在终端确认：

| Windows PowerShell | macOS Terminal |
| --- | --- |
| `python --version` | `python3 --version` |
| `codex --version` | `codex --version` |

Windows 若只有 `py -3` 可用，后文全部 `python` 可替换为 `py -3`。没有 CLI 时按 [Codex 官方安装页](https://learn.chatgpt.com/docs/cli)安装；已有 Node/npm 的环境也可使用 `npm install -g @openai/codex`。本机验收版本为 `0.166.0-alpha.5.1`；其他版本需确认支持下面的 `plugin` 命令。

进入解压后的 **demo 根目录**，两平台执行相同命令：

```sh
codex plugin marketplace add .
codex plugin add tokencoin-channel@tokencoin-demo
```

成功后新开 Codex 会话，输入“使用 TokenCoin 双人通道插件，按 README 带我建立通道”。也可运行 `codex` 后进入 `/plugins`，选择 `tokencoin-demo` 中的 `tokencoin-channel` 安装。当前机器的上述命令已在独立 CODEX_HOME 内验证；尚未替你更改正在使用的全局插件配置。[官方本地市场说明](https://developers.openai.com/plugins/build/plugins)

后续命令在 `demo/codex-channel` 目录执行。插件安装后加载的是缓存副本；使用新包时重新注册新包的 demo 路径并执行 `plugin add`，然后新开会话。代码也可直接运行，无需等待插件界面。

## 2. 每个终端先设置自己的路径

这些路径存放自己的节点状态、朋友邀请和隔离 CLI 配置，放在个人私有目录。它们与下载包分开，升级包时不会丢失授权状态。

Windows PowerShell：

```powershell
cd "<解压后的 demo\codex-channel 绝对路径>"
$tcState = Join-Path $env:USERPROFILE '.tokencoin'
New-Item -ItemType Directory -Force -Path $tcState | Out-Null
```

macOS Terminal：

```sh
cd "<解压后的 demo/codex-channel 绝对路径>"
TC_STATE="$HOME/Library/Application Support/Tokencoin"
mkdir -p "$TC_STATE"
chmod 700 "$TC_STATE"
```

把尖括号中的路径替换为实际目录，不保留尖括号。每新开一个终端重新设置路径变量。不要把状态目录放进 iCloud、OneDrive、共享盘或代码仓库。

## 3. 谁提供额度，谁在自己的电脑登录

你本人已完成此步，无需重复登录。朋友只有在准备向你提供额度时，才需在朋友的机器上执行这一节。单纯作为消费者不需要你的账号登录文件。

| Windows | macOS |
| --- | --- |
| `python -B scripts/setup_adapter.py login` | `python3 -B scripts/setup_adapter.py login` |

首次会下载固定版本 CLIProxyAPI 8.0.47 并校验 SHA256，然后打开官方授权页面。本人完成授权，保持终端开启直到完成。不将密码、token、回调 URL 或授权文件发送给另一方。Windows 也可双击 `login-codex.cmd`；Mac 可用终端命令，避免 `.command` 双击权限问题。

登录保存在自己的独立目录。接下来 `run_supplier.py` 会自动启动适配器、在进程内使用其入口 key。无需手动设置 key 环境变量，也无需另开 `start-adapter`。若你之前单独开过适配器窗口，先 Ctrl+C 停止；供应脚本会拒绝已占用的 18317，避免接错服务。

## 4. 先让两台电脑的私网互通

同一可信局域网可使用各自的 `192.168.x.x` / `10.x.x.x` 地址。异地试用可使用已有加密 VPN；没有时，可按 [Tailscale 设备共享说明](https://tailscale.com/docs/features/sharing)操作：双方各自登录自己的 Tailscale 账号；供应方在管理页 Machines 找到自己的电脑，选择 Share，将共享邀请私下交给对方接受。先共享你的电脑给朋友；反向使用时朋友再共享其电脑给你。双方记录 Tailscale 显示的 IPv4 地址。

后面的例子使用 `100.80.10.11` 代表你的地址、`100.80.10.22` 代表朋友的地址。**这两个是假地址，必须替换。** 使用数字 IPv4，不用电脑名。朋友连接网关的 **8781**；适配器 **18317 只保留本机访问**。不配置路由器公网端口转发，也不把教学网站当作模型网关。

`--allow-private-http` 表示你明确允许这条受信私网连接使用 HTTP。异地流量依赖 VPN 加密。供应方的 VPN 访问规则与系统防火墙应只允许朋友的设备访问 TCP 8781；不要关闭整个防火墙。如果两台机器互相共享，要分别允许两个方向。普通未加密或不可信局域网应改用你管理的 HTTPS 入口，不直接用此示例传递邀请凭证。

## 5. 你启动供应节点：让朋友用你的额度

在你的电脑第一个终端运行，并保持窗口开启。这里的 host 是**你自己的私网 IP**。

Windows：

```powershell
python -B scripts/run_supplier.py --home "$tcState/supplier" --name Alice --model gpt-6.1-sol --host 100.80.10.11 --allow-private-http
```

macOS：

```sh
python3 -B scripts/run_supplier.py --home "$TC_STATE/supplier" --name Alice --model gpt-6.1-sol --host 100.80.10.11 --allow-private-http
```

看到 `"ready": true` 与 `http://你的IP:8781` 即表示服务已就绪，此步骤不发起推理。程序只绑定你指定的私网地址。`Alice` 是供应者显示名，可自行改名；同一个状态目录再次启动时须保持相同名称和模型。

朋友可先检查网络：Windows 使用 `curl.exe http://100.80.10.11:8781/health`，Mac 使用 `curl http://100.80.10.11:8781/health`，应返回 `{"ready":true}`。这只证明端口可达，不消耗推理额度。连不通时先修正 VPN 设备共享、实际 IP 和 TCP 8781 的访问规则。

## 6. 你发出一份短期邀请

保持供应窗口运行，在你自己的第二个终端执行第 2 节路径设置，再执行：

Windows：

```powershell
python -B scripts/channel.py invite --home "$tcState/supplier" --url http://100.80.10.11:8781 --out "$tcState/alice-to-bob.json" --label Bob --requests 5 --minutes 30 --allow-private-http
```

macOS：

```sh
python3 -B scripts/channel.py invite --home "$TC_STATE/supplier" --url http://100.80.10.11:8781 --out "$TC_STATE/alice-to-bob.json" --label Bob --requests 5 --minutes 30 --allow-private-http
```

这个例子允许 30 分钟内最多 5 次模型请求尝试。一个 Codex 任务可能包含多次模型请求，5 次请求不等于 5 个任务，也不等于金额限额。记下输出里的 `channel_id`，撤销时用它。

通过双方选择的私密文件传输方式，把 `alice-to-bob.json` 交给朋友，保存在朋友自己的私有状态目录。它是一张可在限制内重复使用的通行证，不是一次性连接码；不要展示文件正文、贴进聊天或发到教学网站。文件不包含你的原始账号授权。

## 7. 朋友接收并运行第一次 Codex 任务

朋友在自己的终端完成第 2 节路径设置，并把收到的文件放到 `$tcState/alice-to-bob.json` 或 `$TC_STATE/alice-to-bob.json`，然后：

Windows：

```powershell
python -B scripts/channel.py join --home "$tcState/consumer" --peer alice --invite "$tcState/alice-to-bob.json" --allow-private-http
python -B scripts/channel.py status --home "$tcState/consumer" --peer alice
New-Item -ItemType Directory -Force -Path "$tcState/exercise" | Out-Null
python -B scripts/channel.py codex --home "$tcState/consumer" --peer alice --workdir "$tcState/exercise" --effort low --prompt 'Without tools or files: a Python sum function initializes n=1 and adds 2,3,5. State the current result, the corrected result, and the fix in one short sentence.'
```

macOS：

```sh
python3 -B scripts/channel.py join --home "$TC_STATE/consumer" --peer alice --invite "$TC_STATE/alice-to-bob.json" --allow-private-http
python3 -B scripts/channel.py status --home "$TC_STATE/consumer" --peer alice
mkdir -p "$TC_STATE/exercise"
python3 -B scripts/channel.py codex --home "$TC_STATE/consumer" --peer alice --workdir "$TC_STATE/exercise" --effort low --prompt 'Without tools or files: a Python sum function initializes n=1 and adds 2,3,5. State the current result, the corrected result, and the fix in one short sentence.'
```

预期答案包含：当前结果 **11**，修正后 **10**，初值应设为 **0**。CLI 开头应显示 `Supplier: Alice | backend: subscription-adapter`。你同时检查供应方账本：

| Windows | macOS |
| --- | --- |
| `python -B scripts/channel.py status --home "$tcState/supplier"` | `python3 -B scripts/channel.py status --home "$TC_STATE/supplier"` |

出现 `completed_upstream` 和上游报告用量，才说明该通道确实完成了请求。模型自称是谁或文本返回成功都不能单独证明路由。CLI 输入会包含自身指令上下文；本机验收的这个小任务也报告了约 1.57 万输入 token，不能按用户提示的字数估算实际用量。

启动器默认只读，并隔离其他插件、MCP 和用户配置。用户需要修复代码时，显式使用 `--sandbox workspace-write` 将工作目录内的写入交给 CLI；Windows 使用 MXC 沙箱，仍受管理策略约束。下方独立练习已在本机验证，不改变已有项目权限。

## 7a. 双机试用：让朋友的 Codex 读文件、修 bug、跑测试

供应方保持 `run_supplier.py` 运行。按第 6 节另发一份 `--requests 8 --minutes 30` 的短期邀请，文件名使用 `alice-to-bob-dev.json`；消费者按第 7 节以新的 `--peer alice-dev` 加入。避免沿用只剩少量次数的旧邀请。这个练习的本机验收用了 5 次模型请求。

消费者在自己的电脑准备一个全新的测试目录，已有目录会被拒绝，不会覆盖：

Windows：

```powershell
python -B scripts/prepare_exercise.py --out "$tcState/bugfix-exercise"
python -B scripts/channel.py codex --home "$tcState/consumer" --peer alice-dev --workdir "$tcState/bugfix-exercise" --sandbox workspace-write --effort low --prompt 'Work only in this exercise directory. Read calculator.py and test_calculator.py, run python -B -m unittest -v, fix only calculator.py, then run tests again. Keep tests unchanged. Batch related local operations when possible. Report initial failures and final passing count. Stop if policy rejects a command; do not bypass it.'
```

macOS：

```sh
python3 -B scripts/prepare_exercise.py --out "$TC_STATE/bugfix-exercise"
python3 -B scripts/channel.py codex --home "$TC_STATE/consumer" --peer alice-dev --workdir "$TC_STATE/bugfix-exercise" --sandbox workspace-write --effort low --prompt 'Work only in this exercise directory. Read calculator.py and test_calculator.py, run python3 -B -m unittest -v, fix only calculator.py, then run tests again. Keep tests unchanged. Batch related local operations when possible. Report initial failures and final passing count. Stop if policy rejects a command; do not bypass it.'
```

应看到本地读文件、第一次 3 项测试失败、仅修复 `result = 1` 为 `result = 0`、第二次 3 项通过。消费者再自行运行一次测试核对，供应者查看账本确认这几次请求均为 `completed_upstream`。两台机器分别记录 CLI 版本、结果与耗时；Windows 本机通过不代替 Mac 或双机验收。

速度：首次启动和完整任务耗时不同。供应窗口保持运行可避免每个任务都重启适配器。模型每次等待工具结果后通常还要再响应一轮；当前测得单轮约 3.1–4.8 秒。合并相关工具操作可以减少往返机会，实际耗时仍需按双方网络和模型测量。本次总输入 token 含每轮上下文，缓存命中与实际扣费未单独核验。

## 8. 交换角色：你使用朋友的额度

朋友在朋友自己的机器完成第 3 节登录后，按第 5–6 节启动和邀请，替换这几项：

| 项目 | 你供应给朋友 | 朋友供应给你 |
| --- | --- | --- |
| 供应方名字 | Alice | Bob |
| 供应机器 IP | 你的实际私网 IP | 朋友的实际私网 IP |
| 供应状态目录 | 你机器上的 supplier | 朋友机器上的 supplier |
| 邀请文件 | alice-to-bob.json | bob-to-alice.json |
| 消费者加入时的 peer | alice | bob |

朋友把 `bob-to-alice.json` 私下交给你。你在自己的机器执行第 7 节，把邀请名改为 `bob-to-alice.json`、`--peer alice` 改为 `--peer bob`。两个人的 `consumer` 可同时保存 alice 和 bob 这两个不同的 peer；两个方向的额度、到期时间和撤销独立。

每次发任务都明确选择 `--peer`。保持提供额度那一侧的供应窗口运行；消费方无需启动自己的供应服务。双方想同时提供额度，就各开一个供应窗口。

## 9. 到期、撤销与停止

供应方运行下面的命令，`CHANNEL_ID` 换成自己创建邀请时输出的 ID：

| Windows | macOS |
| --- | --- |
| `python -B scripts/channel.py revoke --home "$tcState/supplier" --channel CHANNEL_ID` | `python3 -B scripts/channel.py revoke --home "$TC_STATE/supplier" --channel CHANNEL_ID` |

撤销或到期后，新的请求被拒绝。已接受的请求可能继续结束；中断后用量可能不确定，程序不自动重试。供应窗口按 Ctrl+C 会关闭网关和它启动的适配器。下次使用同一名称、模型和目录重新启动即可；登录授权仍保留。

续发邀请时使用新的文件名，例如 `alice-to-bob-02.json`，避免覆盖秘密文件。朋友重新加入时使用新的 peer 名，例如 `alice02`；当前版本不会静默覆盖已有 peer。旧邀请先撤销。

## 常见卡点

| 现象 | 先检查 |
| --- | --- |
| 找不到 python/codex | 按第 1 节确认命令可用，安装后重开终端 |
| 本地市场找不到插件 | 注册的是包含 `.agents` 的 demo 根目录；完整解压隐藏目录 |
| 18317 被占用 | 停止之前单独启动的适配器，再运行供应脚本；不要盲目结束未知进程 |
| 供应目录设置不匹配 | 用原名称/模型启动；想换模型就使用新 supplier 目录 |
| health 连接失败 | 供应窗口、VPN、实际 IP、设备共享与 TCP 8781 的访问规则 |
| HTTP 401 | 邀请是否过期、撤销；重新发邀请并换一个 peer 名 |
| HTTP 429 | 请求次数已用完，或同一通道已有请求在运行 |
| HTTP 400/403 或模型不可用 | 供应侧账号与模型兼容性；目录列出模型不等于账号一定能调用 |
| 有回答但本地工具失败 | 文本链路与工具权限分开检查；当前不宣称本地工具已通过 |

## 合成演示、API 入口与开发验证

不想消耗真实额度时，运行 `python -B scripts/try_demo.py`（Mac 换 python3）。加 `--codex` 可演示 Codex 与合成上游；旧合成测试在只读/未配置 Windows 沙箱时曾被策略拒绝；当前真实开发流程已在明确授权的 MXC 工作区沙箱下通过。

独立 API key 模式仍支持：用 `channel.py init --backend api --upstream https://api.openai.com/v1 --key-env MY_MODEL_API_KEY --model MODEL --home PRIVATE_DIR --name NAME` 初始化，再 `channel.py serve --home PRIVATE_DIR`。供应者自行配置进程环境中的 key；本项目尚未创建 API 项目或验证 API 计费路径。

自动测试：`python -B tests/test_channel.py` 和 `python -B tests/test_supplier.py`。测试仅用合成数据，真实验收证据另存。请求次数与 token 报告均不构成金额上限；签名、账号权限、模型目录和真实用量也分别需要相应证据。
