# Codex 本地工作台

使用官方 OpenAI Codex CLI `0.167.0` 的本地网页界面。支持自定义 API 地址、Key 和模型，原地打开项目、继续会话、停止任务，以及保存包含交互记录的 delivery 快照。

## 首次使用（从 GitHub 克隆后）

仓库不包含 Codex 二进制（单文件 258MB，超过 GitHub 100MB 上限）和你的配置。克隆后执行：

```bash
python3 install_codex.py
python3 app.py
```

- `install_codex.py` 从官方 npm 包下载固定版本 `0.167.0`，核对 SHA-512 后安装到 `vendor/` 和 `bin/`，并写入版本与校验记录 `codex-install.json`（该文件会提交，二进制不会）。支持 macOS、Linux、Windows 的 x64 和 ARM64；Windows 使用无需软链接权限的 `bin/codex.cmd`，工作台直接启动 vendor 中的 `codex.exe`。
- 仓库自带的 `setting.json` 不含 Key 和个人路径。启动后在网页“连接与模型”里填写 Key、模型和交付目录即可。
- `setting.example.json` 是同结构的空白模板；`setting.json` 被改乱或误删时，可用 `cp setting.example.json setting.json` 恢复。

## 启动

运行环境：macOS、Linux 或 Windows（x64/ARM64），Python 3.9 或更新版本。基础启动不需要 Node 或第三方 Python 包；Windows 可将 `python3` 命令替换为 `py -3`，例如 `py -3 install_codex.py`、`py -3 app.py`。

跨平台图片缩小使用可选 Pillow：`python3 -m pip install "Pillow>=11.3.0,<13"`（Windows 使用 `py -3 -m pip install "Pillow>=11.3.0,<13"`）。未安装时 macOS 可回退系统 `sips`，Linux/Windows 保留原图；安装后需重启服务。

```bash
python3 app.py
```

也可以在 Finder 双击 `start.command`。默认打开浏览器并访问：

http://127.0.0.1:8765

只启动服务，不自动打开浏览器：

```bash
python3 app.py --no-open
```

指定项目、delivery 目录和端口：

```bash
python3 app.py --workspace "/绝对路径/项目" --delivery-path "/绝对路径/delivery" --port 8765
```

使用其他配置文件：

```bash
python3 app.py --settings "/绝对路径/setting.json"
```

完整参数：

```bash
python3 app.py --help
```

前台运行时按 `Ctrl+C` 停止服务。如果提示端口占用，说明可能已有工作台在运行；直接打开网页，或使用 `--port 8766` 启动另一个实例。不同实例应使用不同配置文件，避免同时写入。

## 停止服务

前台运行可以按 `Ctrl+C`。也可以在另一个终端停止默认端口 `8765` 的工作台：

```bash
python3 app.py --stop --port 8765
```

macOS 也支持 `./start.command --stop --port 8765`。有 `lsof` 的 POSIX 环境核验进程启动命令和项目目录后发送 `SIGTERM`；Windows 或缺少 `lsof` 的 Linux 核验当前工作台的项目路径、实例令牌后请求 HTTP 协作关闭。均不修改配置或自动重启，不会根据端口猜测并结束无关程序。

工作台关闭时停止自身 Codex 任务：POSIX 先发送 `SIGTERM`，3 秒后必要时强制结束；Windows 使用 `taskkill /T /F` 强制结束该任务的进程树，并非温和的 `SIGTERM` 退出。

## 本地页面操作服务器文件

左侧和顶部“远程连接”读取本机 `~/.ssh/config`，选择具体 Host 别名。继承 SSH 配置中的 User、Port、IdentityFile、ProxyJump；页面、API 和模型配置仍在本机，通过 SSH 标准输入输出直接操作最终目标服务器。

服务器需要 SSH 和 Python 3.9+（远端为 Linux/macOS），不需要安装整套工作台、运行远端网页或开放 8767 端口。连接时通过 SSH 内存加载文件与 Codex 执行器；任务私有状态保存在服务器 `~/.codex-local-studio-remote/`，断开重连可恢复历史。编码需要服务器 Codex：优先使用 PATH/用户目录已有 CLI，没有时点击“安装远端 Codex”下载并校验官方包到用户私有目录。

macOS/Linux 支持网页认证弹窗：通过系统 OpenSSH 的 askpass 机制逐次显示跳板机、目标机的密码、密钥口令或验证码提示。分别核对提示账号/主机后输入，回答只在进程内用于当前挑战，不写入配置、delivery、浏览器存储或日志；不要把密码发送到聊天。可取消连接，单次提示等待 3 分钟，整条连接最多等待 6 分钟。

使用 `BatchMode=no` 和 `StrictHostKeyChecking=ask`：首次主机指纹必须先核实，用户显式输入 yes 后由 OpenSSH 存入 known_hosts；指纹变化不会自动接受。原生 Windows 暂不支持网页 askpass，仍使用严格非交互密钥认证；需要密码可在 WSL 运行本机工作台。文件模式不需要服务器安装 Codex。

`banner exchange` / `UNKNOWN port 65535` 可能表示 ProxyJump 子链路认证或网络超时，不表示应把 SSH 端口改为 65535。尚未出现密码提示时，应检查跳板机可达性及目标 SSH 服务；新增密码窗口不等于已排除网络问题。

连接成功点击“在本页打开服务器项目”，可填写服务器绝对路径或从 home 逐层选择目录。工作目录左侧可显式切换“本机项目 / SSH 服务器项目”；标题显示目标 Host。右侧文件树来自服务器，可在线编辑 UTF-8 文本、新建目录、上传/下载单文件（上限 2MiB），保存使用内容版本校验，上传拒绝覆盖已有文件。凭据、缓存、符号链接和疑似秘密文件受到访问限制。

服务器项目打开后，可在本地输入需求，实际由服务器 Codex 运行并修改服务器文件；日志/工具调用/模型回答实时回到本地，支持续聊、摘要、停止、超时及历史恢复。使用本机配置的模型地址和 Key，Key 经 SSH 只进入远端执行器内存，不写远端配置；模型接口由服务器访问，所以不能把只能在本机访问的 localhost 模型地址当成远端可用地址。

点击“保存 delivery”可浏览服务器保存根目录，选择后立即设置 trajectory 自动保存位置；手输路径使用“设为 trajectory 自动保存目录”生效。默认是服务器项目的 `delivery/`，已有记录会补存到新选择目录，运行中也可改保存路径。每条任务实际写入 `<所选目录>/agent_runs/<run_id>/request.json`、`response.json`、`result.json`，摘要任务可附带 `context_summary.json`；不伪造用户、工具或模型记录。

任务结束后点击“创建交付快照”，生成 `<所选目录>/<时间戳>/payload/` 源码包及同层 `agent_runs/` 的真实轨迹副本、命令验证日志等。运行中 trajectory 持续保存，但源码快照需要先停止或等待完成。写入选定目录失败会明确显示错误，私有运行记录仍保留，可修复权限后重新设置路径补存。

断连/切换后旧 target_id 失效，请求不会自动回退本机，写入超时也不会自动重试。断开 SSH 时执行器停止自身任务并保存终态，重新连接恢复已保存历史；不保证意外服务器断电后的任务继续运行。同一远端用户的执行器有私有运行锁，避免双连接重复调度。网页/API 始终使用本机 loopback 的 Host/Origin/令牌保护，无远端网页监听或隧道端口。

## 查看与复制文件路径

右侧“项目文件”的文件或目录支持右键菜单：查看路径信息、复制绝对路径、复制相对路径、复制名称。路径详情支持选中文本手动复制。

顶部工作目录、文件树当前目录和文件预览标题也支持右键。文件行悬停显示完整路径。菜单支持方向键、Enter 和 Escape；若浏览器拒绝剪贴板访问，会展示并选中路径，按 `⌘C` / `Ctrl+C` 手动复制。

## 配置文件

默认配置文件为：

项目根目录下的 `setting.json`。

POSIX 文件权限为 `600`；Windows 使用目标用户目录的 ACL，请确保配置与 `.local/` 不对其他用户开放。配置不会出现在网页文件预览或 delivery 的源码包中。默认内容不含 Key 和个人路径，可以提交到 GitHub；Key 只有勾选“记住 Key”后才会写入该文件，此时请不要提交。

配置结构如下，`api_key` 的空字符串替换为接口服务发放的 Token / API Key，`model` 替换为接口支持的模型 ID：

```json
{
  "base_url": "http://39.96.222.203:3000/v1",
  "model": "",
  "remember_key": true,
  "api_key": "",
  "api_key_file": "",
  "developer_role": "system",
  "max_output_tokens": 65536,
  "model_context_window": 400000,
  "model_auto_compact_token_limit": 280000,
  "max_parallel_runs": 3,
  "task_timeout_seconds": 0,
  "stream_idle_timeout_seconds": 900,
  "max_auto_continues": 30,
  "max_context_images": 6,
  "delivery_path": ""
}
```

- `base_url`：模型接口基础地址，不要包含 Key、账号密码或 query 参数。
- `api_key`：该接口服务提供的令牌。后端自动发送 `Authorization: Bearer ...`，不要再手动添加 `Bearer` 前缀。
- `api_key_file`：可填写保存 Key 的 Python 文件绝对路径（例如 `/绝对路径/token_set.py`），直接 Key 留空时仅读取其中的 `API_KEY` 字符串常量。不会执行 Python 文件，也不会把 Key 复制到 `setting.json`。网页中清除 Key 会同时移除该来源。
- `developer_role`：当前 Claude 网关使用 `system`，通过仅回环可访问、临时令牌认证的本地代理，将 Codex 的 `developer` 输入映射为 `system`。工具输入、工具结果及 Responses SSE 原样保留。原生 OpenAI Responses 可设为 `developer` 保留消息角色；两种模式都由本地代理明确设置输出预算。
- `model`：页面中点击“获取模型”后选择，或手动填写模型 ID。
- `remember_key`：控制网页输入的 Key 是否写入文件。取消保存时仅清空 `api_key` 的值，保留字段供手动填写。如果你直接在文件中填写非空 `api_key`，启动会读取并自动启用保存，不会因为旧的 `remember_key: false` 删除已填写的 Key。

`workspace` 不需要写进配置。启动后在页面顶部输入项目绝对路径，点击“打开项目”即可设置或切换；仅对本次服务运行有效。重启后重新选择，或用 `--workspace` 启动参数直接打开指定项目。
- `delivery_path`：默认交付目录，使用绝对路径。留空时使用当前项目下的 `delivery/`。

可通过“连接与模型”页面修改配置，无需重启。手动编辑配置文件时，请先停止服务，修改后重新启动；运行中的服务不会自动重新读取文件。

当前 HTTP 地址会明文传输 Key，请仅使用可信接口。Codex 要求接口支持 `/responses` 和模型工具调用；只有 `/chat/completions` 的服务不能直接使用。页面的“检测 Responses”会发送一条短模型请求，可能产生少量费用。

## 输出上限与上下文总结

`max_output_tokens` 是单次模型回复的最大输出预算，不是上下文容量。实际编码请求默认明确发送 `65536`，避免网关使用不明的低默认值；可在设置页面调到 `256–65536`。上游仍必须支持该预算，提高配置不会突破服务端硬限制。

上下文窗口默认 `400000` Token，`model_auto_compact_token_limit` 默认 `280000`。Codex 达到阈值时使用模型生成本地会话摘要，不调用网关可能不支持的 `/responses/compact`。阈值加输出预算应小于上下文窗口的 90%，为系统指令与工具保留空间。

官方 [Claude 模型文档](https://platform.claude.com/docs/en/about-claude/models/overview) 列出的 Opus 5.5 支持 1M 上下文与 128K 最大输出，但第三方网关是否提供同等能力仍取决于其实际路由和限制。本工作台先使用 400K 上下文与 64K 输出，并预留自动总结余量；短请求已验证 65536 输出预算可被当前接口接受，不代表完成了 400K 或 1M 输入压力测试。

也可以点击“总结上下文”：基于当前线程生成只读摘要，保留目标、决策、文件修改、验证结果与未完成事项。成功后下一条需求会在新线程中携带摘要继续，不再重复发送原始长历史；原始运行记录不会删除，摘要保存在 `agent_runs/<总结任务ID>/context_summary.json`。总结会产生模型调用费用，失败时仍保留原线程。

遇到 `Incomplete response returned, reason: max_output_tokens` 时，会标记输出截断、保存失败及部分交互记录，页面提供继续或总结入口。不会自动重放有副作用的命令，也不会把截断响应标记为成功。可以提高预算、拆分需求，或先总结再继续。模型请求诊断卡会显示实际发送的输出预算及是否触发自动压缩。

## 任务窗口与多轮记录

“最近任务”按逻辑任务归组，不会每运行一轮新增窗口。连续对话、失败后继续、上下文总结和摘要续接都属于同一任务；只有点击“新建任务”才开始独立任务。

点击任务会按顺序回放所有轮次及工具结果，历史超过 200 条事件也会读尽全部分页。页面重载后恢复同一任务的最新线程或摘要。旧记录根据 `thread_id`、`resume_id` 和 `summary_id` 自动归组，不凭需求文本相似度合并不同任务；缺失关联的旧记录保持独立，不删除历史。

模型的摘要新线程与页面任务窗口是不同概念：压缩历史不会新增侧栏条目，但 `delivery/agent_runs/` 仍保留各轮独立记录，并通过 `task_id` 与 `parent_job_id` 标识归属。

## 截图与上下文超限

当前网关会把工具结果（如 Codex 查看截图）里的图片 base64 当文本计 Token：实测一张 100KB 小图在工具结果中约 9.8 万 Token，放在用户消息中只多 16 Token。几张游戏截图就可能触发 `prompt is too long: ... > 1000000`，而 Codex 按图片估算，不会提前压缩。

本地代理会在转发前把工具结果中的图片移到紧随其后的用户消息中（实测网关接受且模型能看到图片），并只保留最近 6 张，较早的替换为文字说明；较大的 base64 截图优先使用可选 Pillow 缩到长边不超过 1568 像素的 JPEG，支持 macOS/Linux/Windows；macOS 可回退自带 `sips`。只有结果更小时才采用，无可用后端或处理失败时保留原图。原会话记录不修改。请求诊断卡会显示 `incoming_bytes`、`moved_images`、`dropped_images`、`shrunk_images` 和实际转发的 `request_bytes`。

截图数量可调整，默认 `6`，范围 `0–50`，`0` 表示不发送任何图片：

- 设置页“发送给模型的截图数量”：保存到 `setting.json` 的 `max_context_images`，作为默认值。
- 输入框下方“截图 N 张”：任务运行中修改，会从当前任务的**下一次模型请求**开始生效，无需停止或重启；空闲时修改，作用于本窗口之后提交的任务。自动续跑会沿用当前值。

调高能让模型对比更多画面，但请求更大、更易超限；以写代码为主时可调到 1–2 张，或设 0。

Codex 估算上下文时把图片算得很小，截图多的会话不会因此提前压缩，本地历史可能远大于实际转发内容；代理入口限制为 512MiB，只用于内存保护。

已因此卡住的会话：重启服务后在同一任务发送“继续”即可，历史中的图片会在转发时自动处理。若仍超限，点击“总结上下文”后在新线程继续。上下文超限不会触发自动续跑。

## 未完成时自动续跑

模型回复中没有工具调用时，Codex 就会结束这一轮，即使模型刚说完“接下来做……”。为避免任务中途停下，工作台会在每轮需求后追加执行规则：持续执行直到全部完成，完成并验证后在最后一行输出 `[TASK_COMPLETE]`；确实需要用户提供信息时输出 `[NEED_USER_INPUT]`。

每轮结束后：

- 有 `[TASK_COMPLETE]`：停止，显示“模型已标记完成”。这只是模型自评，仍需你验收。
- 有 `[NEED_USER_INPUT]`：停止，等待你回复。
- 两个标记都没有：自动在同一会话发送“自动继续”，任务窗口中显示为新的一轮。
- 上游断流、超时或 5xx/429：间隔 15 秒递增后自动续跑，连续失败 3 次后停止。输出被 `max_output_tokens` 截断、达到任务时限或手动停止，都不会自动续跑。

- `max_auto_continues`：自动续跑次数上限，默认 `30`，可设 `0–200`，`0` 为关闭。达到上限后停止并提示，可发送“继续”或调高上限。每次续跑都会产生模型费用。

每次模型响应的结束状态、截断原因和输出类型会记录为 `provider.completed` 事件，便于排查停止原因。

## 多窗口并发与连接中断

可在同一页面的“最近任务”切换后台任务，也可同时打开多个浏览器标签。各标签独立保存项目、模型和 delivery 设置，不存储 Key；任务提交时冻结配置，其他窗口的设置修改不会改变正在运行的任务。停止按钮只停止当前任务，切换或新建窗口不会停止旧任务。

- `max_parallel_runs`：独立项目并发数，默认 `3`，可设 `1–8`。
- 同一任务不能同时提交两轮；同一目录或父子目录的任务顺序排队，防止并发写入冲突。要真正同时编码，应使用独立项目目录或独立工作副本。排队状态会在页面显示，排队任务可直接取消。
- `task_timeout_seconds`：总执行时限，默认 `0` 为不限时，取消旧的固定 30 分钟截止。若设非零，计时从实际启动开始，不包含排队时间。
- `stream_idle_timeout_seconds`：模型流式响应空闲超时，默认 `900` 秒，可设 `30–3600`。本地代理在等待时发送 SSE 心跳，收到完成事件即结束；完成前 EOF/重置会明确记录，不能把截断当作成功。

页面每两秒同步全部后台任务状态。前端请求超时或浏览器断网不会取消后台任务；相同提交 ID 会去重，响应丢失后可以恢复已有任务，不自动重放编码命令。服务退出会拒绝新任务并清理全部运行和排队任务。模型网关限流、硬性时限或实际网络故障仍可能导致失败，不能靠本地心跳保证永久不断线。

## 指定 delivery 路径与保存交互记录

有三种方式：

1. 在 `setting.json` 设置 `delivery_path`，然后重启服务。
2. 在“连接与模型”填写“默认 delivery 保存目录”，保存设置。
3. 点击“保存 delivery”，填写本次目标目录。勾选“保存成功后将此目录设为默认”会记住该路径；取消勾选仅影响本次保存。

路径选择优先级：本次保存对话框 > `delivery_path` 配置 > 当前项目的 `delivery/`。

每次保存会创建独立目录，不覆盖旧交付：

```text
delivery/<时间戳-标识>/
├── payload/                  项目源码快照
├── original_requirement.md   需求文本
├── requirement_register.json 需求记录
├── project_info.json
├── generation.json
├── file_manifest.json        文件大小与 SHA256 清单
├── agent_runs/               请求、脱敏回复、执行结果
├── validation_logs/          记录到的命令与退出码
├── validation.json           真实验收状态
└── package_secret_scan.json  启发式秘密扫描记录
```

交互记录始终保存到 `.local/runs/`。配置非空 `delivery_path` 后，新任务从开始到结束自动同步到 `delivery_path/agent_runs/<任务ID>/request.json`、`response.json`、`result.json`；失败或停止的任务也保存。保存设置或重新打开项目时，会把该项目已有交互记录补存到此目录。页面状态栏展示实际保存路径或写入失败原因。

例如设置 `/Users/bohanzeng/Documents/project/数元/阿里游戏数据/delivery/G001` 后，交互直接位于 `G001/agent_runs/`。源码快照仍需点击“保存 delivery”，它会另外创建 `G001/<时间戳-标识>/payload/`，不会在每次模型事件时反复复制整个项目。

任务运行中不能切换项目或保存 delivery。配置、Key、依赖安装目录、缓存、构建输出、旧 delivery 和符号链接默认不打包。超过 25MiB 的单个文件会被跳过，并在页面和清单中提示。疑似秘密会阻止保存，但启发式扫描无法证明不存在所有秘密。

保存快照不代表验收通过：未执行的测试、静态审计与质量检查保持 `NOT_RUN`，验收状态保持 `NOT_READY`，不会伪造通过结果。

## 安装与检查

已安装的官方 CLI：`bin/codex`。如需重新安装固定版本：

```bash
python3 install_codex.py
./bin/codex --version
```

安装器从 OpenAI 官方 npm 包下载 ARM64 二进制，校验 SHA512 完整性，仅安装到本项目，不修改全局 Codex。

运行本地回归测试：

```bash
python3 -W error::ResourceWarning -m unittest -v test_studio
```

测试使用模拟模型服务，不需要真实 Key；包括官方 CLI Responses 接入与续聊、任务停止、配置保存、路径保护及 delivery 快照行为。
