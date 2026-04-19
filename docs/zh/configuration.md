# 配置(Configuration)

EchoVessel 的所有运行时状态都从一份 TOML 配置文件读入。本页是每个字段的查表:作用、合法取值、何时改。

## 文件位置和格式

Daemon 默认读取 `~/.echovessel/config.toml`。起步模板打包在安装包内 `echovessel/resources/config.toml.sample`，用 `init` 子命令一键生成工作副本:

```bash
uv run echovessel init
```

这会把 sample 写到 `~/.echovessel/config.toml`。传 `--force` 覆盖现有文件,传 `--config-path PATH` 指定其他位置。无论 source checkout 还是 wheel 安装,`init` 都通过 `importlib.resources` 读 sample,不走文件系统路径。

文件用的是标准 TOML 语法,有一条 EchoVessel 特有的约定:**以 `_env` 结尾的字段存的是环境变量名,而不是 secret 本身**。Daemon 在启动时从环境里读出实际值。这样 API key、bot token、provider 凭据就不会落在任何可能被复制粘贴或被不小心 commit 进版本控制的文件里。如果你写 `api_key_env = "OPENAI_API_KEY"`,daemon 在构造 LLM provider 时会读 `os.environ["OPENAI_API_KEY"]`。

Daemon 只在启动时 load 一次 `config.toml`。对大多数 section 的修改只有下次启动才会生效。少数 section 可以通过给运行中的 daemon 发 `SIGHUP` 做热重载——本页末尾有一张表。像切换 `persona.voice_enabled` 这类管理操作不走 TOML 路径,它们有专用 API,能原子地把相关字段写回文件并同步更新进程内状态。

## `[runtime]`

Daemon 自身的进程级设置。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `data_dir` | `~/.echovessel` | 一切都落在这里:数据库、日志、语音缓存、克隆指纹缓存。设成绝对路径时,该路径必须对运行 daemon 的用户可写。 |
| `log_level` | `"info"` | `"debug"` / `"info"` / `"warn"` / `"error"` 之一。`"debug"` 非常啰嗦,会打印每一条 LLM prompt——只在追 bug 时才用。 |
| `turn_timeout_seconds` | `120` | 串行 TurnDispatcher handler 的 per-turn 挂钟超时。超过这个时间的 handler(通常是挂掉的 `llm.stream`)会被取消,后续 channel 的消息不会被它堵住。设为 `0` 关闭(不推荐)。 |

## `[persona]`

这个 daemon 实例服务的单个 persona 的身份字段。Phase 1 每个 daemon 进程只支持一个 persona。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `id` | `"default"` | memory 表里作为主键用的短稳定标识符。只在首次启动前改——一旦数据库里有以这个 id 为 key 的行,改它就会让所有东西变成孤儿。 |
| `display_name` | `"Your Companion"` | persona 在 prompt 和 UI 里对自己的称呼。两次启动之间改这个不需要做数据迁移。 |
| `voice_id` | 未设置 | 从一次语音克隆跑出来的 reference-model id。不设就是 persona 没有语音。 |
| `voice_provider` | 未设置 | 通常用不着——provider 从 `[voice]` section 推断出来。 |
| `voice_enabled` | `false` | persona 回复是否除了文字还附带语音。这个字段**不是**通过直接改 TOML 文件来切换的;它有专用的管理 API,能原子地重写文件并同步更新运行中的 daemon。直接改文件再重启也能生效,但两条路径不应该混用。 |

## `[memory]`

Memory 模块的存储和检索旋钮。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `db_path` | `"memory.db"` | SQLite 文件路径。相对路径基于 `data_dir`。特殊值 `":memory:"` 跑在进程内内存数据库里,shutdown 后一切消失——适合测试和本地实验。 |
| `embedder` | `"all-MiniLM-L6-v2"` | sentence-transformers 模型名。Daemon 首次启动时下载(~90 MB),缓存到 `data_dir/embedder.cache/`。如果改这个,也要连带把数据库删掉——已有 embedding 是旧模型产出的,和新模型不可比。 |
| `retrieve_k` | `10` | retrieve 管道给 prompt 组装器返回的 memory 命中数。值越高 persona 上下文越多,但 token 成本也涨。 |
| `relational_bonus_weight` | `1.0` | rerank 打分器里"关系加成"项的乘数。调高能让 persona 更倾向召回涉及用户命名关系的记忆。 |
| `recent_window_size` | `20` | prompt 组装器无条件带上的最近 L2 消息数——不受 retrieve 结果影响。 |

## `[llm]`

哪个模型驱动 persona,以及怎么和它说话。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `provider` | `"openai_compat"` | `"openai_compat"` / `"anthropic"` / `"stub"` 之一。`openai_compat` 覆盖任何 OpenAI 兼容端点——实际上 OpenAI 本身 / OpenRouter / Ollama / LM Studio / vLLM / DeepSeek / Groq / Together / Fireworks / xAI / Perplexity / Moonshot / 智谱 GLM 都算。`anthropic` 用 Anthropic 原生 SDK。`stub` 返回固定回复、零网络调用——是验证干净安装最省心的方式。 |
| `api_key_env` | `"OPENAI_API_KEY"` | 存 API key 的环境变量名。对不需要认证的 provider(比如本地 Ollama)设为 `""`。 |
| `base_url` | 未设置 | 覆盖 API base URL。任何非 OpenAI 官方的 `openai_compat` provider 都必须设。 |
| `model` | 未设置 | 把所有 tier 固定到同一个模型。优先级高于 `tier_models`。 |
| `max_tokens` | `1024` | 回复长度上限。 |
| `temperature` | `0.7` | sampling 温度。 |
| `timeout_seconds` | `60` | 请求超时。 |

### `[llm.tier_models]`

EchoVessel 把 LLM 调用按语义分三档——`small` / `medium` / `large`——让你把每档映射到不同的具体模型。Extraction 和 reflection 走 `small`(跑得频繁,对模型要求低),judge 走 `medium`,persona 实时回复和 proactive 生成走 `large`。

```toml
[llm.tier_models]
small  = "gpt-4o-mini"
medium = "gpt-4o"
large  = "gpt-4o"
```

如果设了 `model`,它压过所有 tier,`tier_models` 被忽略。两者都没设的话,provider 用自己的默认(比如 Anthropic provider 默认走 `haiku` / `sonnet` / `opus`)。

### 常见 `[llm]` 配方

**零配置 OpenAI** — 在 shell 里设 `OPENAI_API_KEY`,section 保持默认。

**本地 Ollama** — 不需要 key:

```toml
[llm]
provider    = "openai_compat"
base_url    = "http://localhost:11434/v1"
api_key_env = ""

[llm.tier_models]
small  = "llama3:8b"
medium = "llama3:70b"
large  = "llama3:70b"
```

**OpenRouter** — 一个账号任意模型:

```toml
[llm]
provider    = "openai_compat"
base_url    = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"
model       = "anthropic/claude-sonnet-4"
```

**Anthropic native** — 用一方 SDK 而不是 OpenAI 线协议:

```toml
[llm]
provider    = "anthropic"
api_key_env = "ANTHROPIC_API_KEY"
```

**离线烟测** — stub provider · 零网络 · 固定回复。这是验证新安装前最安全的配法:

```toml
[llm]
provider    = "stub"
api_key_env = ""
```

## `[consolidate]`

控制从已关闭 session 里抽取 event 和 thought 的后台 worker。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `trivial_message_count` | `3` | 消息数少于这个数的 session 被跳过——材料不够抽。 |
| `trivial_token_count` | `200` | token 数低于这个的 session 也被跳过,原因同上。 |
| `reflection_hard_gate_24h` | `3` | 任何滚动 24 小时窗口里允许的最大反思(L4 thought 写入)次数。反思是系统里最贵的调用,这个 gate 防止用户突然产出大量 session 时成本失控。 |
| `worker_poll_seconds` | `5` | 多久扫一次已关闭的 session。值小反应快但更多数据库压力。 |
| `worker_max_retries` | `3` | 瞬时失败的每 session 重试次数,之后标记失败等人工处理。 |

## `[idle_scanner]`

空闲扫描器负责关闭陈旧的 open session,让 memory 可以去 consolidate 它们。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `interval_seconds` | `60` | 扫描频率。30 分钟没收到消息的 session 会在下次扫描时关掉;这个 30 分钟阈值是代码常量,不是 config 字段。 |

## `[proactive]`

自主消息引擎。完整设计见 `proactive.md`。字段名和默认值在各版本之间保持稳定,集合会随新 policy gate 落地而增长。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 主开关。false 时 scheduler 根本不构造,proactive 不跑。等你确认 daemon 不会瞎发之后再开。 |
| `tick_interval_seconds` | `60` | scheduler 多久醒一次来评估 policy 队列。 |
| `max_per_24h` | 视情况 | 粗粒度 rate-limit 上限。完整 policy gate 字段见 `proactive.md`。 |

## `[voice]`

voice 模块开关。整个 section 缺失或 `enabled = false` 时,daemon 启动时不构造 `VoiceService`,runtime 和 channel 侧任何语音路径都干净降级为纯文字。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 主开关。 |
| `tts_provider` | `"stub"` | `"stub"` / `"fishaudio"` 之一。 |
| `stt_provider` | `"stub"` | `"stub"` / `"whisper_api"` 之一。 |
| `fishaudio_api_key_env` | 未设置 | FishAudio API key 的环境变量名。 |
| `whisper_api_key_env` | 未设置 | Whisper provider 用的 OpenAI API key 环境变量——通常和 `[llm].api_key_env` 是同一个。 |

## `[channels.*]`

每个 transport 一个子 section。v0.0.1 有**两个**真实可用的 channel:Web UI(`127.0.0.1:7777/`)和 Discord DM bot。iMessage 和 WeChat section 作为占位保留,让 config 形状稳定,但 adapter 本身还没实现。

### `[channels.web]`

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否启动这个 channel。 |
| `channel_id` | `"web"` | 内部用作存储消息时 via-tag 的稳定标识符。改它通常是个错误。 |
| `host` | `"127.0.0.1"` | 监听 host。除非你明确要远程访问,否则保持在 `127.0.0.1`——daemon 没有鉴权。 |
| `port` | `7777` | 监听端口。 |
| `static_dir` | `"embedded"` | 构建好的前端在哪。`"embedded"` 用 wheel 自带的静态文件;绝对路径允许你开发时 serve 自己的 build。 |

### `[channels.discord]`

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 是否启动 Discord DM bot。 |
| `channel_id` | `"discord"` | 稳定标识符。 |
| `token_env` | `"ECHOVESSEL_DISCORD_TOKEN"` | 存 Discord bot token 的环境变量名。 |
| `debounce_ms` | `2000` | 等多久再把连续消息合并成一个 turn。 |
| `allowed_user_ids` | `[]`(空 = 不限制) | 可选的 Discord 用户 ID allowlist。 |

enable 这个 channel 后 · 把 bot token 放进 `./.env` · 重启 daemon。通过 Discord 发的消息也会流入 Web chat timeline(runtime-mirror 架构见 `channels.md`)。

### `[channels.imessage]`

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 是否启动 iMessage channel。仅 macOS;依赖外部 `imsg` CLI(`brew install steipete/tap/imsg`)。 |
| `channel_id` | `"imessage"` | 稳定标识符。 |
| `persona_apple_id` | `""`(空 = 单账号模式) | 目的地过滤字段。空 → 接收发到这台 Mac iMessage 账号的所有消息,不做过滤。设为 persona 专用 Apple ID → 只有目的地是这个 ID 的消息才进 LLM(适用于同一台 Mac 同时登录主账号和 persona 账号的场景)。 |
| `cli_path` | `"imsg"` | `imsg` 二进制的路径。默认走 `$PATH`;也接受绝对路径或 SSH 包装脚本。 |
| `db_path` | `""` | 可选的 `chat.db` 覆盖。空 → `imsg` 读 `~/Library/Messages/chat.db`。 |
| `allowed_handles` | `[]`(空 = 不限制) | 发件人 handle 白名单(E.164 电话或邮箱)。单账号模式下**强烈建议**配上。 |
| `default_service` | `"auto"` | 发送 service:`"imessage"` / `"sms"` / `"auto"`(让 `imsg` 自己选)。 |
| `region` | `"US"` | 用于把没带 `+` 的纯数字电话号码规范化成 E.164 的区域。 |
| `debounce_ms` | `2000` | 等多久再把连续消息合并成一个 turn。 |

完整安装流程(FDA / Automation 权限、单账号 vs 双账号模式、语音 MP3 附件行为)见 `channels.md` 的「iMessage 通道配置」章节。

### `[channels.wechat]`

这个 section 是占位。目前只读 `enabled` 和 `channel_id`,设 `enabled = true` 不会真的起一个 channel。真实的 adapter 在后续版本落地。

## 哪些字段热重载 · 哪些要重启

有两条路径不用重启 daemon 就能让 config 改动生效:

- **`echovessel reload`** —— 从磁盘重读 `config.toml` · 校验 · 把改动应用到对应的运行时属性。想继续用信号的脚本直接发 `SIGHUP` 也等价。
- **`PATCH /api/admin/config`** —— Web admin panel 写 `config.toml`(原子)· 然后在内部触发同一条 reload。persona toggle / LLM 调节滑块这些 UI 都走这条路径。

两条路径读的是同一张白名单(`src/echovessel/core/config_paths.py`)。下面按字段一一列出当前真实行为;每条都有 `tests/runtime/test_reload_matrix.py` 里的断言兜底。

### 热重载字段

| 字段 | reload 时发生什么 |
| --- | --- |
| `[llm].provider` · `.model` · `.api_key_env` · `.timeout_seconds` · `.temperature` · `.max_tokens` | 新 provider 被构造并换进 `ctx.llm`。in-flight 的 turn 继续用老 provider 直到跑完(引用快照)。 |
| `[memory].retrieve_k` · `.recent_window_size` · `.relational_bonus_weight` | `ctx.config.memory.*` 更新。turn handler 每次 turn 都现读这些字段 · 下一个 turn 就用新值。 |
| `[consolidate].trivial_message_count` · `.trivial_token_count` · `.reflection_hard_gate_24h` | `ctx.config.consolidate.*` 更新 **并** 镜像到活着的 `ConsolidateWorker` 实例上。worker 是启动时用旧值构造的 · reload 路径直接改它的实例属性 · 之后的 session 用新的阈值 · 不用重启。 |
| `[persona].display_name` | `ctx.config.persona.display_name` 在任何 reload 上都会更新。`ctx.persona.display_name`(turn handler 实际读的对象)**只在**通过 `PATCH /api/admin/config` 改动时才会镜像过去(admin 路径在 reload 后跑了一个额外的 side-path)。只用 `echovessel reload` 改的话 `ctx.persona` 会留旧值——改名字请走 admin API。 |

### 运行时状态 · 根本不从 `config.toml` 读

- `persona.voice_enabled` —— 走 admin API(`POST /api/admin/persona/voice-toggle`)· 改文件后 reload 不会拾起。
- `persona.voice_id` —— 同上 · 走 `POST /api/admin/voice/activate`。

### 要重启才能生效

所有其他字段。daemon 启动时把它们 load 进一次性构造的东西,进程中途不重建。常见的:

| Section | 为什么要重启 |
| --- | --- |
| `[runtime].data_dir` · `[memory].db_path` | 改这些意味着要重开数据库 · 重跑迁移 · 丢弃 in-memory 的 embedder 缓存。admin API 直接用 400 拒绝。 |
| `[memory].embedder` | 进程中途换 embedder 会让现有 vector 全部失效(模型不同 = embedding space 不同)。 |
| `[voice]` · `[proactive]` · `[idle_scanner]` | 驱动的后台 worker / scheduler 是启动时构造一次的 · 不重建。 |
| `[channels.*]` | channel 的注册和启动只发生在启动时。Admin → Channels 面板通过 `PATCH /api/admin/channels` 把改动写进 TOML 并弹出"需要重启"的提示,但这一节里没有任何字段是热重载的——切换 `enabled`、改 `allowed_handles` 或换 `persona_apple_id` 都要等下一次 `echovessel run` 才生效。 |

拿不准就重启。reload 是为少数改动频率足够高的 tunable 设计的便利——LLM 设定 · 检索旋钮 · consolidate 阈值——不是通用的重新配置通道。
