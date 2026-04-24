# 贡献指南(Contributing)

EchoVessel 是一个小而有主见的代码库。本页告诉你怎么搭开发环境、有哪些架构规则、一个好的 PR 长什么样。

## 开发环境

你需要 Python 3.11 或更新版本,以及 [`uv`](https://github.com/astral-sh/uv)。其他一切都通过 `uv sync` 管。

```bash
git clone <repo-url> echovessel
cd echovessel
uv sync --extra dev
```

这会装上 runtime 依赖加开发工具链:`pytest`、`pytest-asyncio`、`ruff`、`import-linter`。可选 extras(`embeddings` / `llm` / `voice`)只在你动那些子系统时才需要:

```bash
uv sync --extra dev --extra embeddings --extra llm --extra voice
```

用完整检查套件验证安装:

```bash
uv run pytest tests/ -q            # 全量测试 · 应该全绿
uv run ruff check src/ tests/      # lint
uv run lint-imports                # 架构契约 · 必须保持绿
```

新 clone 的仓库这三条里有任何一条失败,那就是 bug · 请开 issue。

## 项目布局

```
EchoVessel/
├── src/echovessel/           库和 daemon 本体
│   ├── core/                 共享类型、枚举、工具
│   ├── memory/               L1-L6 persona 记忆
│   ├── voice/                TTS / STT / 语音克隆
│   ├── channels/             Channel Protocol + 具体 channel
│   │   └── web/frontend/     React 19 + Vite + TypeScript UI 源码(编入 `web/static/`)
│   ├── proactive/            自主消息引擎
│   ├── prompts/              extraction / reflection / interaction 的 system prompt
│   ├── import_/              通用 LLM importer pipeline
│   └── runtime/              daemon 本体:启动、turn loop、LLM provider、CLI
├── tests/                    测试套件,布局和 src/ 对称
│   ├── integration/          跨模块 composition smoke 测试
│   └── eval/                 persona 质量 eval harness
├── docs/                     你现在在读的这份文档
├── src/echovessel/resources/  打包资源(config.toml.sample 供 `echovessel init` 使用)
├── pyproject.toml            依赖、分层契约、lint 配置
└── README.md
```

`src/echovessel/` 下的每个子系统在 `tests/` 下都有对应的测试目录。跨模块 composition 测试在 `tests/integration/`。`tests/eval/` 下的 eval harness 用一套 golden question 跑 memory 模块并报告指标——是检验 memory 改动没有回退检索质量最快的办法。

## 分层架构

EchoVessel 有 5 个核心模块,严格分层:

```
runtime
   │
   ▼
channels    proactive
   │             │
   └──────┬──────┘
          ▼
     memory     voice
          │       │
          └───┬───┘
              ▼
            core
```

一个层可以 import 它正下方的层。不可以 import 它正上方的层,也不可以 import 同层 sibling。具体:

- `runtime` 可以 import 它下面的一切。
- `channels` 和 `proactive` 可以 import `memory` / `voice` / `core`,但**互相不可以 import**,也**不可以 import `runtime`**。
- `memory` 和 `voice` 可以 import `core`,但**互相不可以 import**,也**不可以 import 上层**。
- `core` 不从 EchoVessel 里 import 任何东西。

这个契约在 lint 时由 `import-linter` 强制,配置在 `pyproject.toml` 里。破坏分层的 PR 无论测试过不过都 fail CI。加一个新模块意味着决定它在这个阶梯上的位置,并在 `import-linter` 配置里声明。

还有一个小工具模块 `import_/`,和 `memory`、`voice` 一起在 layer 2(因为它在导入时写 memory)。它遵循相同的 sibling 规则。

## 两条铁律

两条规则承载整个系统,由明确的 guard test 强制。

### Memory retrieval 永不按 channel 过滤

Memory 模块的 `retrieve()` 函数、它的 core-block 加载器、它的 recall-message 查询,全部只接受 persona 和 user 参数,**永远**不接受 transport 标识符。不存在 `retrieve(..., channel_id="web")` 这样的重载,也不会有。一个 persona 跨所有 channel 是同一条连续的身份;允许 retrieval 按 transport 分片会悄悄把这一个 persona 变成一堆按 channel 拆分的 bot。

Guard test 在 `tests/runtime/test_memory_facade.py::test_no_channel_id_kwarg_in_reads`。它 AST 扫描 memory facade,发现任何读路径提到 `channel_id=` 就失败。如果你要加新的 memory 读 API,这个 AST walk 会自动检查它。

### LLM prompt 永不泄漏 transport 身份

发给 LLM 的任何 prompt 里都不应该出现 `channel_id` 字符串或任何 transport 身份 token。persona 不知道自己此刻在 Web、Discord 还是 iMessage 上说话——每一个可能泄漏这个信息的设计决定都被刻意绕开了。

Guard test 在 `tests/runtime/test_f10_no_channel_in_prompt.py`。它从 fixture 渲染真实 prompt,grep 其中的禁止字符串。加新 prompt slot 时,请扩展这个测试去覆盖它。

两条铁律存在,是因为违反它们是静默且累积的。一次按 channel 过滤的 retrieval、或一条泄漏 `channel_id` 的 prompt,当下不会立刻弄坏任何东西——它只是悄悄拿走了系统其他部分依赖的保证。Guard test 的意义就是把"静默出错"变成"CI 里大声出错"。

## 测试约定

测试布局和源码布局对称。模块专属测试放在 `tests/<module>/`。跨模块 integration 测试放在 `tests/integration/`。persona 质量 eval harness 放在 `tests/eval/`。

加新功能时:

1. **单元测试放在对应模块的测试目录。** 新 memory 函数测试在 `tests/memory/`。新 voice provider 测试在 `tests/voice/`。
2. **跨模块接线测试放在 `tests/integration/`。** 如果你的改动影响两个模块怎么交互,加一个通过真实入口同时演练两者的测试。
3. **Memory 检索改动应当用 eval harness 验证。** 跑 `uv run python -m tests.eval.run_baseline`,确认四个质量指标仍然达标。
4. **测试里优先用 stub provider。** 代码库自带 `StubProvider`(LLM)、`StubVoiceProvider`(TTS/STT)、stub channel——用它们,你的测试就不依赖网络调用或 API key。

每个 PR 必须保持 `pytest tests/` 全绿、`ruff check` 干净、两条 `import-linter` 契约满足。

## Commit message

仓库强制一小组规则,其他是推荐。

**每个 commit 必须:**

- **标题说 what,正文说 why。** diff 本身已经说 how。标题自洽时正文可省;读者会问"为什么?"时就写一段。
- **Imperative 语气。** `add X` / `fix Y`,不是 `added X` / `fixes Y`。
- **一次只做一件事。** 不要"修 bug + 顺手重构 + 动了无关文档"混成一个 commit,拆开写。
- **三绿才提交。** `uv run pytest`、`uv run ruff check src/ tests/`、`uv run lint-imports` 本地全过。
- **标题 ≤ 72 字符,** 让 `git log --oneline` 读着清爽。

**推荐(软约束):**

单一范畴的改动优先用 Conventional Commits 前缀:

| 前缀 | 用于 |
| --- | --- |
| `feat:` | 用户可见的新能力 |
| `fix:` | bug 修复 |
| `docs:` | 只动文档 |
| `refactor:` | 内部重构,不改行为 |
| `test:` | 只动测试 |
| `chore:` | 构建 / 依赖 / 工具链 |
| `perf:` | 性能优化 |

加 scope 更清楚(模块名或文件):`fix(memory): ...`、`docs(README): ...`、`refactor(runtime): ...`。

跨模块 / 里程碑类型的大改不强求前缀,用 `·` 分段的自由格式即可——例如 `Wave A · admin UI truth-layer landing`。

**反模式:**

- 含糊标题:`updates`、`misc fixes`、`WIP` 留在 `main`。
- `fix X and refactor Y`——两件事,两个 commit。
- 只说 what 不说 why(当读者半年后会疑惑时)。

**刻意不引入:**

不装 commit-msg hook、不做 CI 强制、不要 DCO sign-off、不要 issue trailer。审核者是 reviewer(和未来的你)。

## 提交 PR

好 PR 的样子:

- **只做一件事。** 无关的清理留给单独的 PR。如果你不得不说"顺便我还…",那就拆。
- **更新测试。** 每个行为变化都至少有一个本应在修复前捕获 bug 的测试。只让已有测试绿而不加新测试,是黄色信号。
- **保持 `lint-imports` 绿。** 如果你在模块之间加了新依赖,分层契约必须仍然通过。如果通不过,重新想依赖方向。
- **不碰两条铁律。** 不加接受 `channel_id` 的 memory 读 API。不加泄漏 transport 身份的 prompt 内容。Guard test 会捕获明显的情况,code review 会捕获细微的。
- **Commit message 要解释 why。** 标题说 what,正文说 why。diff 本身已经说 how。

## 跑 eval harness

Eval harness 用四个指标衡量 memory 质量:Factual Recall F1、Emotional Peak Retention、Over-recall False Positive Rate、Deletion Compliance。每个都有项目认为可以发布的阈值。

```bash
uv run python -m tests.eval.run_baseline
```

Harness 默认用固定的 stub LLM,结果跨次运行确定。它打印一张指标值表以及相对阈值的 pass/fail。如果你的改动让某个指标掉到阈值以下,那就是回退。

完整的指标定义和解读说明在 `tests/eval/` 下的 eval harness 源码里。

## 在哪儿问问题

开 issue 之前:

1. 先读 `docs/en/` 或 `docs/zh/` 里相关的模块文档。
2. 看模块源码——每个文件都有详细的 docstring。
3. 搜已有的 issue。

开 issue 时,描述你看到的行为、你期望的行为、以及复现需要的最小配置。贴一份 `uv run python -m echovessel run` 的启动 log 几乎总是对的。
