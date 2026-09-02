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

安装后新建一个 Codex 任务，然后直接说：

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

仓库还通过根目录的 `plugin.json`、`skills/` 与本地 stdio `mcp.json`
兼容厂商中立的 [Agent Plugins 1.0.0](https://agent-plugins.org/) 标准；
Codex 原生客户端仍使用 `.codex-plugin/plugin.json` 和 `.mcp.json`。

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

超过十秒的任务通过实时 runner 运行，持续显示：

```text
已运行 2分15秒 · 运行中 4 · 就绪 2 · 已完成 7 · 失败 0
本次预计节约 4分31秒 · 累计节约 19分52秒
```

结束后还会核对每个原子任务的状态、返回码、超时、跳过原因、输出截断、峰值并发、本次节约和累计节约。

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
