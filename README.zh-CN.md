# AtomLane

**只并行已证明安全的任务。**

AtomLane 是面向 AI 编程代理的安全并行优化系统：既能找出可审查的 Python
并行改造候选，也能让 Codex 在不破坏任务语义的前提下，更快完成 Mac 上的
构建、测试、Docker 和科研流水线。

[![CI](https://github.com/cloudguo123/atomlane/actions/workflows/ci.yml/badge.svg)](https://github.com/cloudguo123/atomlane/actions/workflows/ci.yml)
[![CodeQL](https://github.com/cloudguo123/atomlane/actions/workflows/github-code-scanning/codeql/badge.svg)](https://github.com/cloudguo123/atomlane/actions/workflows/github-code-scanning/codeql)
[![五分钟基准](https://github.com/cloudguo123/atomlane/actions/workflows/long-benchmark.yml/badge.svg)](https://github.com/cloudguo123/atomlane/actions/workflows/long-benchmark.yml)
[![可视化报告](https://img.shields.io/badge/可视化报告-在线-65e6b4.svg)](https://cloudguo123.github.io/atomlane/)

[English](README.md) · [在线报告](https://cloudguo123.github.io/atomlane/) · [反馈首次运行](https://github.com/cloudguo123/atomlane/issues/new?template=first-run.yml) · [提交实测结果](https://github.com/cloudguo123/atomlane/issues/new?template=benchmark.yml)

![AtomLane：受控基准测试，串行等效 20 分 41 秒，并行实际 5 分 10 秒，节约 15 分 31 秒](assets/growth/social-preview.svg)

## 两条命令安装

```bash
codex plugin marketplace add cloudguo123/atomlane
codex plugin add mac-parallel-accelerator@mac-parallel-accelerator
```

品牌迁移期间继续保留 `mac-parallel-accelerator` 这个技术插件 ID，因此旧安装、
命令和链接仍然可用。

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

要求：macOS、支持插件和 MCP 的 Codex、Python 3.10+。只有分析 Compose YAML 时需要 Ruby；只有重新构建浏览器指示器时需要 Node.js 20+。

仓库还通过根目录的 `plugin.json`、`skills/` 与本地 stdio `mcp.json`
兼容厂商中立的 [Agent Plugins 1.0.0](https://agent-plugins.org/) 标准；
Codex 原生客户端仍使用 `.codex-plugin/plugin.json` 和 `.mcp.json`。

## 它解决什么问题

普通的“并行执行”经常只是拆分命令文本，容易改写 `&&` / `||` 的控制流，争用 `.next`、JUnit、数据库、Docker 卷或 Git 状态，叠加内部线程池，并且执行过程中只看到空白等待。

AtomLane 先把任务编译成带类型的 Atom IR，再判断哪些原子任务可以同时运行。未知副作用、多写入者、过期源快照、不支持的服务生命周期和被篡改的计划都会拒绝执行，而不是猜测。

## 典型场景

| 项目场景 | 优化目标 | 关键安全边界 |
| --- | --- | --- |
| Web / TypeScript | 质量门禁、包图、浏览器矩阵 | 保留成功条件；隔离 `.next`、coverage、JUnit 和缓存 |
| Docker / Compose | 多镜像构建、健康检查 DAG、测试矩阵 | 约束 VM CPU/内存、端口、卷、就绪事件和迁移 |
| 科研 / 论文 | 数据准备、验证、出图、文稿构建 | 推断数据依赖，保护正式计时和来源证据 |
| 原生构建 / 测试 | Make、编译器、测试运行器 | 优先委托原生并发，统一预算内外层 worker |
| 媒体 / 数据 / ML 批处理 | 多输入并行、确定性合并 | 要求输出隔离、资源有界、合并语义明确 |
| 长时间 Python 程序 | 有序 CPU 映射、阻塞读取、原生内核、子进程批次 | 不导入、不执行目标；未知副作用、共享状态、过期哈希和不安全 spawn 路径一律阻断 |

内置场景目录已覆盖 50 多类软件工程、科研、容器、媒体、机器学习、发布、数据库以及底层 CPU/GPU/I/O 优化目标。

## Python 程序级并行改造顾问

`$optimize-python-parallelism` 会在执行任务之前增加一条程序级分析通道。
`python_parallel_advisor` MCP 工具只对项目内、大小受限的 UTF-8 源码做 AST
分析；不会导入模块、运行目标代码、安装依赖或修改文件。

首版只为非常窄、可证明的形态生成改造预览：同模块 `worker(item)` 的有序
列表推导、直接返回列表推导，以及 `append` 循环。系统会沿本地调用图传播
副作用，检查循环控制和输出顺序；纯 Python CPU 任务还必须证明 macOS
`spawn` 所需的 `__main__` 入口安全。最终分类为：

- `reviewable_rewrite`：纯 CPU 候选，附带已通过语法检查的统一 diff；
- `advisory_only`：I/O 或受外部约束的工作，需要进一步人工设计；
- `prefer_native`：应优先向量化或使用原生库释放 GIL 的并发；
- `already_parallel`：已有并发，应统一 worker 预算，避免嵌套超卖；
- `blocked`：存在未证明风险，继续串行。

改造预览绑定精确源码 SHA-256，永远不会自动应用。即使提供了串行热点耗时，
收益也只标注为“实测串行 + 建模并行”，不能冒充基准结果。真正采用前仍需做
串并行差分测试、macOS `spawn` 确定性测试、异常/顺序/产物核对、内存测量和
重复性能验证。详见 [Python Candidate IR 与证明门槛](skills/optimize-python-parallelism/references/python-program-ir.md)。

## 真正实时显示

![20 秒实时执行演示，显示运行中、就绪、完成、失败和预计节约时间](assets/growth/demo.gif)

超过十秒的任务通过 PTY runner 运行，持续显示：

```text
已运行 2分15秒 · 运行中 4 · 就绪 2 · 已完成 7 · 失败 0
本次预计节约 4分31秒 · 累计节约 19分52秒
```

结束后还会核对每个原子任务的状态、返回码、超时、跳过原因、输出截断、峰值并发、本次节约和累计节约。

## 五分钟以上公开基准

保留的公开实测通过真实并行执行器运行四个隔离的低负载任务，每个任务都超过五分钟：

| 指标 | 结果 |
| --- | ---: |
| 并行实际用时 | **5分10秒** |
| 串行等效用时 | **20分41秒** |
| 本次节约 | **15分31秒** |
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

[MIT 许可](LICENSE) · [隐私说明](PRIVACY.md) · [使用条款](TERMS.md)
