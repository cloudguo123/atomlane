# AtomLane

**只并行已证明安全的任务。**

**一套通用安全内核，按平台原生执行，按工作负载定制加速。**

AtomLane 是面向 AI 编程代理的跨平台并行编译器与运行时。共享的类型化内核负责
证明依赖关系并守住任务语义；适配层再根据具体工作负载和执行域，定制发现方式、
进程约束与资源预算。macOS 为 Stable；原生 Windows 是边界明确、严格拒绝式的
Preview。

[![CI](https://github.com/cloudguo123/atomlane/actions/workflows/ci.yml/badge.svg)](https://github.com/cloudguo123/atomlane/actions/workflows/ci.yml)
[![CodeQL](https://github.com/cloudguo123/atomlane/actions/workflows/github-code-scanning/codeql/badge.svg)](https://github.com/cloudguo123/atomlane/actions/workflows/github-code-scanning/codeql)
[![五分钟基准](https://github.com/cloudguo123/atomlane/actions/workflows/long-benchmark.yml/badge.svg)](https://github.com/cloudguo123/atomlane/actions/workflows/long-benchmark.yml)
[![可视化报告](https://img.shields.io/badge/可视化报告-在线-65e6b4.svg)](https://cloudguo123.github.io/atomlane/)
[![许可：MPL-2.0](https://img.shields.io/badge/许可-MPL--2.0-blue.svg)](LICENSE)

[English](README.md) · [在线报告](https://cloudguo123.github.io/atomlane/) · [反馈首次运行](https://github.com/cloudguo123/atomlane/issues/new?template=first-run.yml) · [提交实测结果](https://github.com/cloudguo123/atomlane/issues/new?template=benchmark.yml)

**采用 MPL-2.0 开源许可，个人、科研、教育及商业使用均免费。** 当前社区版本
无需 AtomLane 账户或付费。未来可能提供替代商业授权或独立许可的扩展能力，但
不会改变已经按照 MPL-2.0 发布代码的许可。[查看许可说明](LICENSING.md)

![AtomLane 受控五分钟并行基准测试](assets/growth/social-preview.svg)

## 两条命令安装

```bash
codex plugin marketplace add cloudguo123/atomlane
codex plugin add atomlane@atomlane
```

安装或升级后要新建一个 Codex 任务。Codex 会在 **Hooks** 中列出 AtomLane
自带的任务评估 Hook；它第一次运行前，需要用户检查并信任这份确切定义。信任后，
每次提交任务都会显示三类只读预检结果之一：直接执行路径、到执行边界再检查、可能
适合并行但必须先做安全计划。这个 Hook 只读取当前提交的提示词，不扫描项目、不执行
命令、不拦截任务，也不把“看起来能并行”冒充成安全证明；真正的判断仍由 Skill 与
原子计划根据真实入口、副作用、依赖、平台和资源完成。详见
[Hooks 与实时指示器说明](docs/HOOKS.zh-CN.md)。

之后可以直接说：

```text
使用 $accelerate-local-work 扫描这个项目，把确认安全的工作并行执行。
全过程实时显示进度，并报告本次和累计节约时间。
```

遇到长时间运行的 Python 程序，可以说：

```text
使用 $optimize-python-parallelism 分析 scripts/job.py。分析阶段不要运行、导入
或修改目标代码；列出证明义务，并给出绑定源码哈希的改造预览。
```

要求：macOS Stable 或限定范围的原生 Windows Preview、支持插件和 MCP 的 Codex、
以及能通过 `PATH` 上的 `python3` 命令启动的 Python 3.10+（`python3 --version`
必须成功）；当前的 [Python Install Manager](https://docs.python.org/3/using/windows.html#python-install-manager)
包含这个 Windows 兼容别名。Ruby 只用于 macOS 上的 Compose YAML 分析；Node.js 20+ 只用于
重新构建浏览器指示器。Windows 发布证据目前来自 `windows-2025` CI 镜像，
不等同于已证明 Windows 11 Desktop UI 集成。Windows 用户请先阅读
[Windows Preview 说明](docs/WINDOWS_PREVIEW.md)。
pytest 原生 worker 路径还要求所选运行环境已经安装 pytest 与 pytest-xdist；
AtomLane 永远不会自动安装这些依赖。0.16 版的发布门禁明确覆盖 `macos-14`、
`windows-2025`、CPython 3.10–3.13、pytest 8.4.2 与 pytest-xdist 3.8.0；
其他依赖版本和主机镜像不属于本版已验证的发布承诺。

可安装包采用 Codex 原生结构：`.codex-plugin/plugin.json`、`.mcp.json`、
`skills/` 与 `hooks/hooks.json` 会作为一个整体发布；根目录 `mcp.json` 仍可作为
厂商中立的本地 stdio 配置单独使用。本版有意不再附带根目录 Agent Plugins 清单，
因为当前 Codex 会把它识别成另一种包格式，并在它存在时跳过插件内置的生命周期
Hook。

## 一套内核，三层定制

这里的“通用”不是声称所有任务、所有平台都能并行，而是所有被接纳的任务都要
通过同一套类型化安全契约。“定制”则意味着系统会按照真实平台和工作负载，改变
执行路径、隔离方式和并发预算。

| 层次 | 通用契约 | 针对性适配 |
| --- | --- | --- |
| 安全内核 | Atom IR、不可变计划哈希、副作用/冲突检查、授权边界、实时进度与节约时间记账 | 不支持的语义严格拒绝，不做近似翻译 |
| 平台 | 支持的原生执行域共用规划器与调度器 | macOS 使用 POSIX 进程组和 Apple 芯片探测/后端；原生 Windows Preview 使用 NT 路径规则、Job Object、UTF-8 管道、可选 ConPTY 和完整 PowerShell 文件原子 |
| 工作负载 | 独立性、顺序、产物与资源都通过同一证明门槛 | 构建/测试优先委托原生并发；Docker 按 daemon/VM 预算；科研保留正式计时围栏；Python 区分 CPU、阻塞 I/O、原生内核、已有线程池和未知副作用 |

WSL、原生 Windows、macOS 和 Docker 是不同执行域。AtomLane 会针对当前执行域
重新编译计划，不会把一台主机上的证明或资源预算冒充成另一执行域的证据。

| 能力 | macOS Stable | 原生 Windows Preview |
| --- | --- | --- |
| 共享内核 | Atom IR、哈希、副作用/冲突检查、调度器、实时进度、节约时间账本 | 使用同一内核和证明规则 |
| 自动入口 | 支持的 shell、package、Make、Compose、测试与构建前端 | 精确 argv 与声明完整的 `.ps1`；暂不自动拆解 shell/package/Make/Compose/`.cmd`/`.bat` |
| 进程边界 | POSIX session/process group | 分阶段 kill-on-close Job Object，覆盖 supervisor 与正常继承的目标进程树 |
| 终端/输出 | 有界管道与 live runner | 独立 UTF-8 管道或仅输出 ConPTY；ConPTY stdin 会被拒绝 |
| 发布证据 | macOS 14 CI 与保留的五分钟实测 | Windows Server 2025 CI 与独立五分钟实测；不等于 Windows 11 Desktop UI 证明 |

## 它解决什么问题

普通的“并行执行”经常只是拆分命令文本，容易改写 `&&` / `||` 的控制流，争用 `.next`、JUnit、数据库、Docker 卷或 Git 状态，叠加内部线程池，并且执行过程中只看到空白等待。

AtomLane 先把任务编译成带类型的 Atom IR，再判断哪些原子任务可以同时运行。未知副作用、多写入者、过期源快照、不支持的服务生命周期和被篡改的计划都会拒绝执行，而不是猜测。

## 典型场景

| 项目场景 | 优化目标 | 平台路径 | 关键安全边界 |
| --- | --- | --- | --- |
| Web / TypeScript | 质量门禁、包图、浏览器矩阵 | macOS 自动前端；Windows 使用显式 argv/PowerShell 原子 | 保留成功条件；隔离 `.next`、coverage、JUnit 和缓存 |
| Docker / Compose | 多镜像构建、健康检查 DAG、测试矩阵 | macOS Compose 前端；Windows Preview 可提供 Linux daemon 资源建议，但不做原生 Compose 拆解 | 约束 VM CPU/内存、端口、卷、就绪事件和迁移 |
| 科研 / 论文 | 数据准备、验证、出图、文稿构建 | macOS 前端；Windows 使用显式阶段原子 | 推断数据依赖，保护正式计时和来源证据 |
| 原生构建 / 测试 | Make、编译器、测试运行器 | 使用平台支持的前端或精确原生 argv | 优先委托原生并发，统一预算内外层 worker |
| 媒体 / 数据 / ML 批处理 | 多输入并行、确定性合并 | 两个原生执行域都支持隔离的精确 argv；Apple 专用后端在非 macOS 上只给建议 | 要求输出隔离、资源有界、合并语义明确 |
| 长时间 Python 程序 | 有序 CPU 映射、阻塞读取、原生内核、子进程批次 | 静态顾问支持两端；CPU 预览显式使用可移植 `spawn` | 不导入、不执行目标；未知副作用、共享状态、过期哈希和不安全 spawn 路径一律阻断 |

内置场景目录已覆盖 50 多类软件工程、科研、容器、媒体、机器学习、发布、数据库以及底层 CPU/GPU/I/O 优化目标。

## pytest 原生 worker 池

例如面对 100 个相互解耦的 pytest case，AtomLane 会把收集、fixture、case 调度与
worker 生命周期交给一个 pytest-xdist 原生 worker 池，而不是粗暴拆成 100 个互不
相关的子进程。`test_suite_plan` 是这条路径的便捷前端；它返回的仍是交给
`atomic_exec` 的同一份不可变 `compiled_plan` 与 `plan_hash`。

AtomLane 负责外层安全契约：精确 runner argv、完整副作用声明、配置与源码快照、
每次运行独立的临时目录与绑定计划哈希的报告路径、内外层统一 CPU/内存预算、
超时进程约束、实时进度，以及最终测试与节约统计。选用的 xdist 分发策略也绑定
在计划哈希中；独立 case 默认使用 `worksteal`，只有 fixture 或共享资源需要亲和
分组时，才改用 `loadfile`、`loadscope` 或 `loadgroup`。

这条边界是明确且可核验的：

- 规划阶段不会偷偷运行 `pytest --collect-only`，也不会导入或执行项目测试；执行前
  必须由调用方完整声明测试副作用，并设置 `independence_declared=true`，多 worker
  计划才具备执行资格。
- `runner_argv` 必须是精确的 Python 模块调用，例如 `[python, -m, pytest]`（也支持
  带版本号的等价形式）；直接使用 `pytest` 或 `py.test` 控制台脚本会被拒绝。
  AtomLane 会绑定并重验所选解释器的内容哈希，强制清空 `PYTHONPATH`、
  `PYTHONHOME` 与 `PYTHONOPTIMIZE`，并拒绝项目目录或配置 `pythonpath` 中可能
  遮蔽可信 `pytest`/`xdist` 模块的文件。清空 `PYTHONOPTIMIZE` 可防止宿主环境的
  `-O` 语义悄悄移除测试辅助代码中的普通断言。所选 Python 环境仍由调用方信任，
  且必须预先安装这些包。
- AtomLane 会解析并快照项目内实际生效的 pytest 配置，用 `-c` 固定它；合法的配置
  `addopts` 与 `PYTEST_ADDOPTS` 会原样保留，其精确 token 也会进入选择指纹。
  pytest 8.4 如果只把不含 `[tool.pytest.ini_options]` 的普通 `pyproject.toml` 当作
  rootdir fallback，AtomLane 会将这类选择单独绑定为 `fallback_pyproject`，运行时也只
  在它仍然不含 pytest 配置时接受。冲突的 worker/输出控制、非执行模式以及
  xdist/cache-provider 插件覆盖会严格拒绝。位置
  selector 与配置中的 `testpaths`/`pythonpath` 必须在编译时已经存在于
  `project_path` 内、使用不经过符号链接的直接路径，并会在执行前重验；显式
  `snapshot_paths` 也遵循同一规则。AtomLane 还会注入并绑定
  `--confcutdir=project_path`，防止 pytest 执行项目边界之外父目录中的 `conftest.py`。
  发现有歧义时应显式传入 `config_path`；
  Python 3.10 解析 `pyproject.toml` 还要求环境中可导入 `tomli`。未知的第三方
  pytest 参数如果需要取值，应写成 `--option=value`，避免参数值被误判为位置
  selector。
- AtomLane 会显式加载自己控制的 xdist 插件并注入 worker、分发策略、临时目录和
  JUnit 参数，因此禁用 pytest 插件自动加载时仍可运行。共享 cacheprovider 会被
  禁用，依赖缓存的选择参数会被拒绝。JUnit 与 base-temp 路径不得和源码快照、所选
  配置、runner 可执行文件或彼此重叠；显式 JUnit 路径还必须位于所有 collection
  目录之外，也不得进入同一计划中其他 suite 的 collection 目录；不传时使用唯一的
  系统临时路径。已存在的报告必须是非链接、仅一个硬链接的普通文件，其父目录身份
  会在持有输出租约时再次验证。重叠检查采用大小写折叠并进行 Unicode 规范化的保守
  路径身份，同时核对物理祖先/文件身份，不能用 macOS firmlink、挂载别名或 Windows
  路径别名绕过。AtomLane 不会自动安装 pytest-xdist；
  单 worker 基线与多 worker 路径都需要运行环境预先安装它。在 Windows 上，显式
  报告路径还会拒绝尾空格/尾点、备用数据流、设备名和扩展设备命名空间等会折叠为
  同一 Win32 对象的歧义写法。
- JUnit 与 base-temp 路径会在最后预检到报告解析完成之间持有排序后的、
  非阻塞跨进程租约。并发复用同一路径会立即失败，不允许一次运行读取另一次的证据；
  应重新编译以生成新路径，或显式提供不同的 `junit_path`。每个输出会同时锁定规范
  路径、物理父目录加文件名以及已存在目标的身份，并在持锁后重验。在 Windows 上，
  租约根由当前进程访问令牌对应的用户配置目录构造，不受可变用户目录环境变量影响。
- `worker_count=auto` 同时受主机资源预算和调用方提供的 case 数提示约束；这个提示
  不会被当作独立性证明。worker 数量是受限的容量决策，不是 CPU affinity。worker 进程由 pytest-xdist
  与操作系统调度，AtomLane 不把 worker 固定到某个物理核或性能核。
- `native_workers_configured` 只是“已配置 worker 数”的证据，
  `outer_peak_concurrency` 才是 AtomLane 观测到的外层峰值。系统不会把配置值冒充
  实测值；没有兼容的运行时观测时，`native_workers_observed` 以及原生池并行效率
  都保持不可用。
- 要得到实测对比，先对同一选择执行 `worker_count=1`，再把返回的、仅限当前服务
  会话的 `serial_baseline_evidence` 传给多 worker 运行。每个 suite 必须设置
  `baseline_source_closure_declared=true`，并通过 `snapshot_paths` 覆盖所有与语义
  相关的已选测试、源码、helper、项目内插件与 `conftest`；实际生效的 pytest 配置由
  AtomLane 另行绑定并快照。AtomLane 会对 selector、配置路径和 `conftest` 做有界
  静态覆盖检查；审计到的 collection 树内只要存在符号链接或重解析点，该次运行就
  不具备签发串行基线的资格，因为链接目标可能在词法选择不变时被替换。动态导入与
  动态加载插件的闭包仍只能由调用方声明。因此该证明记录
  的是“调用方声明的源码闭包”，并不证明完整语义闭包。原生 pytest 池拒绝裸的
  `serial_baseline_seconds` 数值。
  `project_path` 之外已安装的 pytest/xdist 及插件不做内容见证；调用方必须保证
  串行与并行两次执行之间该受信环境未变。
- 并行运行仍需产生新的、非空、全部通过且声明计数一致的 JUnit，且 testcase 身份
  要与会话见证的串行基线一致。没有兼容见证时，只有该 JUnit 中完整且与运行容量相符
  的 testcase 耗时才能形成明确标注的单次估算。估算只进入独立的“累计估算”分账，
  绝不会进入主“累计已入账”；两类证据都没有时，本次节约显示“待建立基线”。

节约账本 v2 会保留证据来源。`time_saved_seconds` 继续提供兼容的“本次最佳有效比较”，
`measured_time_saved_seconds` 与 `estimated_time_saved_seconds` 明确区分实测和估算；
`ledger_credit_eligible`、`ledger_credit_recorded` 与
`credited_time_saved_seconds` 区分“具备入账资格”和“确实写入成功”。主
`cumulative_saved_seconds` 只包含新版实测入账与从旧版保留的
`legacy_unclassified`，估算则单列为 `cumulative_estimated_saved_seconds`。已有账本
若损坏或不可读会封闭失败，不会被静默清零覆盖。

## Python 程序级并行改造顾问

`$optimize-python-parallelism` 会在执行任务之前增加一条程序级分析通道。
`python_parallel_advisor` MCP 工具只对项目内、大小受限的 UTF-8 源码做 AST
分析；不会导入模块、运行目标代码、安装依赖或修改文件。

首版只为非常窄、可证明的形态生成改造预览：同模块 `worker(item)` 的有序
列表推导、直接返回列表推导，以及 `append` 循环。系统会沿本地调用图传播
副作用，检查循环控制和输出顺序；纯 Python CPU 任务还必须证明可移植的
`__main__` 导入路径，并显式使用 `spawn` 上下文。最终分类为：

- `reviewable_rewrite`：纯 CPU 候选，附带已通过语法检查的统一 diff；
- `advisory_only`：I/O 或受外部约束的工作，需要进一步人工设计；
- `prefer_native`：应优先向量化或使用原生库释放 GIL 的并发；
- `already_parallel`：已有并发，应统一 worker 预算，避免嵌套超卖；
- `blocked`：存在未证明风险，继续串行。

改造预览绑定精确源码 SHA-256，永远不会自动应用。即使提供了串行热点耗时，
收益也只标注为“实测串行 + 建模并行”，不能冒充基准结果。真正采用前仍需做
串并行差分测试、显式 `spawn` 确定性测试、异常/顺序/产物核对、内存测量和
重复性能验证。详见 [Python Candidate IR 与证明门槛](skills/optimize-python-parallelism/references/python-program-ir.md)。

## 真正实时显示

![20 秒实时执行演示，显示运行中、就绪、完成、失败和预计节约时间](assets/growth/demo.gif)

超过十秒的任务通过实时 runner 运行，持续显示生命周期计数、已用时间和
当前已经成立的对比；pytest 原生池在新 JUnit 或兼容基线证据产生前，
节约时间会明确显示为待确认：

```text
已运行 2分15秒 · 运行中 4 · 就绪 2 · 已完成 7 · 失败 0
当前预计节约 4分31秒
```

结束后会正式确认本次节约和更新后的累计节约，同时核对每个原子任务的
状态、返回码、超时、跳过原因、输出截断和峰值并发。

Windows 的实时界面在运行期间显示任务生命周期计数与节约时间；捕获的任务
stdout/stderr 在任务完成后随结果返回。普通任务通过独立字节管道并发排空；
需要终端输出语义的任务可选择 ConPTY，此时两路输出合并为一条 VT 流。父进程先按
PID 把等待中的 supervisor 加入 Job Object，再发送启动记录，由 supervisor
创建目标进程。这是分阶段监管，不是原子创建目标进程。Job 的 CPU 与内存预算
包括 supervisor 和正常继承的目标进程树；WSL、Docker、WMI、服务、计划任务或
其他 broker 创建的工作明确不属于该 Job 边界。

## Windows Preview 契约

Preview 与 macOS 共用 Atom IR、不可变计划哈希、副作用检查、调度器、实时进度
和节约时间账本；平台适配层增加 Windows 原生 CPU/内存/电源探测、NT 路径冲突
规则、分阶段 Job Object 监管、可选 ConPTY 与保守的 `pwsh` 文件前端。

- 原生 Windows、WSL 和 Docker Linux VM 是三个独立执行域；计划不能跨域执行或重放。
- 支持精确 argv 任务和已声明的 `.ps1` 文件。PowerShell 文件整体视为一个不透明原子，必须完整声明副作用。
- 原生 Windows Preview 对 POSIX shell、package script、Make、Compose、`.cmd`、`.bat` 的自动拆解一律拒绝；POSIX 工作流应放在 WSL 中运行，或改为显式原子任务。
- Job 范围的 CPU 比例和内存限制覆盖 supervisor 与正常继承的目标进程树，
  内存下限为 128 MiB。管道模式下，`max_processes` 是整个 Job 的精确活动
  成员总上限（2–4096），supervisor 存活时占用其中一个槽位；它不是目标树的
  额外配额。ConPTY 与 `max_processes` 的组合会在启动目标代码前拒绝，但 CPU
  和内存限制仍可使用。broker 外部工作不受这些限制。Windows 进程池建议
  不会超过 61 个 worker。
- 本 Preview 的 ConPTY 只承诺输出端终端语义。由于尚未实现并验证终端输入与 EOF
  语义，显式 ConPTY `stdin` 会在目标创建前拒绝；有界 stdin 请使用管道模式。

完整边界和排障说明见 [Windows Preview](docs/WINDOWS_PREVIEW.md)。

## 保留的 macOS 五分钟以上公开基准

保留的 macOS 公开实测通过真实并行执行器运行四个隔离的低负载任务，每个任务
都至少运行五分钟。原生 Windows Preview 证据在在线报告中单独展示：

| 指标 | 结果 |
| --- | ---: |
| 并行实际用时 | **5分10秒** |
| 串行等效用时 | **20分40秒** |
| 本次节约 | **15分30秒** |
| 观测加速比 | **4.00×** |
| 并行效率 | **100.0%** |

串行等效时间是四个独立任务实测时长之和，并没有为了展示数字再串行重复执行 20 分钟。这个受控测试验证了调度、实时显示和统计开销，不代表所有真实项目都能获得 4 倍加速。请查看[完整可视化报告](https://cloudguo123.github.io/atomlane/)、[原始 JSON](https://cloudguo123.github.io/atomlane/benchmark-results.json)和[基准规范](BENCHMARKING.md)。

## 核心安全契约

```text
atomic_task_plan
  → 完整且不可变的 compiled_plan + plan_hash
  → atomic_exec 原样执行同一个对象和哈希
```

插件不会把编译结果再翻译成手写并发波次。成功、失败、顺序、数据、流、就绪、健康、完成和清理依赖保持不同类型；文件、副作用、容量资源、生命周期和源快照都属于执行契约。

## 隐私与权限

- 项目和可选 trace 扫描都在本地、有明确边界。
- Python 建议只做静态分析；不会导入或执行目标模块，改造预览也不会改写文件。
- trace 只返回聚合路由信号，不返回提示词、推理、命令正文或工具输出。
- 并行只改变时间，不扩大权限；不会凭空授权远程操作、破坏性清理或重试。
- 运行结果不会自动上传；分享必须由用户明确触发并可先审阅。

## 一起完善

请先在一个真实任务上试用，再提交脱敏后的实测结果。即使没有加速、而是被安全规则拦住，也很有价值——它能告诉我们下一条需要补齐的语义规则。

[反馈首次运行](https://github.com/cloudguo123/atomlane/issues/new?template=first-run.yml) · [提交实测](https://github.com/cloudguo123/atomlane/issues/new?template=benchmark.yml) · [参与讨论](https://github.com/cloudguo123/atomlane/discussions) · [查看路线图](ROADMAP.md) · [品牌规范](BRAND.md) · [贡献代码](CONTRIBUTING.md)

[MPL-2.0 许可](LICENSE) · [许可说明](LICENSING.md) · [商标政策](TRADEMARKS.md) · [隐私说明](PRIVACY.md) · [使用条款](TERMS.md)
