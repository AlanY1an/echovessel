# Runtime

> 守护进程本身。一个 Python 进程,负责启动 memory、voice、proactive 和所有 channel,驱动 turn loop,流式推 LLM token,一直运行到你把它停下来。

Runtime 是 EchoVessel 里**唯一**能同时 import 其他所有模块的那一层。Memory、voice、proactive、channels 各自住在自己的盒子里,互相看不见对方。Runtime 在它们之上做胶水:构造对象、接线 callable、拥有 event loop、作为一切代码实际执行的那个单进程。

这篇文档写给想从零理解这个守护进程的开发者——`echovessel run` 到底做了什么、一个 turn 从头到尾是怎么流过来的、在哪里插一个新的 LLM provider、启动步骤或信号处理函数。

---

## Overview

`echovessel run` 会启动一个长期运行的 asyncio 进程并阻塞你的 shell。这个进程就是整个守护进程——没有 worker 进程、没有 fork、没有配套的辅助服务。进程内部只有一个 event loop;loop 上住着一个 `Runtime` 实例,它持有 memory engine、LLM provider、channel registry、voice service、proactive scheduler 和几个后台任务的引用。Ctrl+C(或 `echovessel stop`)翻转 shutdown event,loop 依次收尾。`stop` / `reload` 优先走 daemon 的本地控制面(见下面 **控制面** 一节)· 仅在 plane 不通或 pidfile 是老格式时退化到信号(`SIGTERM` / `SIGHUP`)。

Launcher 暴露四条子命令:`echovessel run` 在前台启动守护进程,`echovessel stop` 向 pidfile 发 `SIGTERM` 让守护进程优雅停机,`echovessel reload` 发 `SIGHUP` 来在不中断 in-flight turn 的情况下热替换 LLM provider,`echovessel status` 读 pidfile 并告诉你守护进程是否还活着。四条命令读同一份配置文件(默认 `~/.echovessel/config.toml`);pidfile 住在配置里的 `data_dir` 下,所以用不同配置跑的多个守护进程不会互相抢同一个 pidfile。

为什么什么都塞进一个进程?因为 EchoVessel 把所有东西存在一个 SQLite 数据库里(配合 sqlite-vec),而 SQLite 同一时间只有一个写入者。单一 asyncio loop 给了我们零成本的串行化——consolidate、turn handler、idle scanner、proactive scheduler 全部共享同一个 loop,彼此不会 race。开第二个进程就得管写锁、跨 worker 的崩溃恢复协调、跨进程的 LLM 客户端池。对一个大部分时间挂机等一个真人打字的本地优先 persona 守护进程来说,这些代价都不值。由此派生出的分层规则同样简单:runtime 可以 import channels、proactive、memory、voice、core,而这些模块**永远**不 import runtime。CI 里的 import-linter 契约强制这一条。

---

## Core Concepts

**Runtime** — 顶层守护对象(`src/echovessel/runtime/app.py::Runtime`)。它有一个类方法 `Runtime.build(config_path)`:加载配置、打开数据库、迁移 schema、保证 persona 行存在、构建 LLM provider、构造 voice service 和 `RuntimeContext`,然后返回一个未启动的实例。随后 `await rt.start()` 启动后台任务并返回;`await rt.wait_until_shutdown()` 阻塞在 shutdown event 上;`await rt.stop()` 按反序拆掉一切。

**RuntimeContext** — 所有任务共享状态的那个 dataclass(`Runtime.ctx`)。它持有解析后的 config、`config_path`(热重载需要知道从哪里重读)、解析好的 `data_dir` 和 `db_path`、打开的 SQLModel engine、`SQLiteBackend`、`embed_fn` callable、`LLMProvider`、`ChannelRegistry`、`shutdown_event`,以及一个 `RuntimePersonaContext` 用于运行时可变字段(`voice_enabled`)。守护进程里每个任务都从同一个 `RuntimeContext` 读状态,没有全局变量。

**Turn loop** — 把 channel 进来的一组消息转化成 persona 回复的串行 pipeline。Channel 的 `incoming()` yield 一个 `IncomingTurn`;turn dispatcher 把它推进一个单消费者队列;handler 一次拉一条,调 `assemble_turn(turn, llm, on_token, on_turn_done)`;这个函数把每条 user 消息写进 memory、跑 retrieve、组 prompt、流式跑 LLM、把回复写进 memory、然后把回复返还给 handler 让它调 `channel.send(...)`。一个 handler task、一个队列、一次处理一个 turn。

**流式 token 回调** — `on_token(message_id, delta)`。Channel 把这个 callable 传进 `assemble_turn`;LLM 每 emit 一个 text delta,这个函数就被调一次,channel 可以把 delta 推到它自己的传输通道上(web channel 的情况就是一帧 SSE)。这个回调只接收**文本 delta**——没有结构化 JSON、没有 tone 提示、没有 delivery 元数据。如果一次 push 失败(客户端断了、socket 关了),失败被 log 下来,流继续;回复照样会落进 memory,客户端重连之后依然能看到。

**on_turn_done 回调** — `on_turn_done(turn_id)`。Channel 借此知道"runtime 对这个 turn 已经做完了,你可以清 in-flight 状态,去考虑要不要刷下一个 debounce 过的 turn"。Runtime 保证每个 turn 恰好调用一次,在 `assemble_turn` 的 `finally` 块里,无论这个 turn 是成功、是 LLM 瞬时错误、是 LLM 永久错误,还是 memory 写入失败。回调里抛的异常会被吞掉——channels 被预期在这里零抛,坏 channel 不能污染 runtime 的 turn pipeline。

**LLM tier** — 调用点声明的语义标签。Runtime 恰好持有一个 `LLMProvider` 实例,每次调用都传一个 `tier=LLMTier.SMALL | MEDIUM | LARGE`。Provider 内部把 tier 映射到具体 model 名。映射解析的优先级固定如下:如果配置里 pin 了一个 `llm.model`,所有 tier 都返回那一个模型("一个模型跑到底");否则如果配置里设了 `[llm.tier_models]`,就用每档的映射;再否则 provider 回退到内置默认值(Anthropic:Haiku / Sonnet / Opus;OpenAI 官方:`gpt-4o-mini` / `gpt-4o`)。EchoVessel 的调用点 tier 是固定的:extraction 用 SMALL,reflection 用 SMALL,未来的 judge 用 MEDIUM,interaction 和 proactive 永远用 LARGE,因为用户正盯着屏幕。

**Local-first 披露** — 启动结尾那一行日志,列出守护进程将要联系的**每一个**对外地址。包含数据目录、解析后的数据库路径、persona id、LLM provider 名称、LARGE tier 解析到的 model、provider 将要打的 base URL(比如 `https://api.anthropic.com` 或者本地 Ollama 的 URL)、启用的 channel 列表、embedder 名称。紧跟一行用口语再说一次外发 URL。任何审计者跑 `tail -f logs/runtime-*.log | head -2` 立刻就能看到流量去向。

**SIGHUP reload** — 给守护进程发 `SIGHUP`(或者跑 `echovessel reload`),runtime 会重读 `config.toml`、验证它、如果 `[llm]` 节有变动就重建 LLM provider、原子替换 `ctx.llm`。In-flight turn 不受影响,因为 turn handler 在每个 turn 开头把 `llm = self.ctx.llm` 捕获成一个局部变量——Python 的引用语义白送我们零成本的 liveness,不需要任何锁或版本号。结构性小节(`[memory]`、`[channels.*]`、`[persona].id`)无法重载;要改它们必须 `echovessel stop && echovessel run` 完整重启。

---

## Architecture

### 启动序列

`Runtime.build(config_path)` 之后紧跟 `await rt.start()`,按顺序执行以下步骤。每一步都定义了失败模式;除非特别标注为致命,否则失败只会 log warning,守护进程带着被影响的子系统以降级状态继续启动。

加载并验证配置。`load_config(path)` 用 `tomllib` 解析 TOML,然后跑 `runtime/config.py` 里的 Pydantic v2 schema。文件缺失、小节格式错、`api_key_env` 指向的环境变量不存在——任何一个都会让守护进程在任何 I/O 之前直接退出。密钥**永远不**写进 TOML,只写持有密钥的环境变量的名字。

创建数据目录及其子目录。`data_dir`(默认 `~/.echovessel`)如果不存在就创建,同时创建 `logs/` 和 `embedder.cache/`。数据目录绝不是 site-packages 安装位置;`pip upgrade` 不能抹掉用户的 persona。

打开 SQLite engine。`create_engine(db_path)` 以 WAL 模式打开数据库并加载 `sqlite-vec` 扩展。这里失败是致命的。

跑幂等 schema 迁移。`ensure_schema_up_to_date(engine)` 检查当前 schema 并为任何缺失的部分跑 `ADD COLUMN` / `CREATE TABLE IF NOT EXISTS`。新库上它是 no-op;旧库上它把 schema 推进到当前形状。迁移失败是致命的——半迁移的数据库在插入时会爆,不如启动时直接爆掉更干脆。

创建剩下的表。`create_all_tables(engine)` 跑 SQLModel metadata create。在已经最新的库上调用是安全的。

写入 persona 和 user 行。守护进程保证一条 `id=config.persona.id` 的 `Persona` 行和一条 `id='self'` 的 `User` 行存在。MVP 是单 persona 单 user——两行都是首次启动时 write-once。

追赶陈旧 session。`catch_up_stale_sessions(db, now=...)` 扫 `sessions` 表找 `status='open'` 但 `last_message_at` 早于 idle 阈值的行,把它们标为 `closing` 并 commit。这一步发生在 consolidate worker 启动之前,让初始队列看到上次崩溃留下的每个孤儿。

构建 LLM provider。`build_llm_provider(config.llm)` 根据 `config.llm.provider` 分派,实例化 `AnthropicProvider`、`OpenAICompatibleProvider` 或 `StubProvider` 中的一个。构造过程**绝不**联网——它只缓存 API key 并构造 tier → model 映射。Provider 作为唯一的共享实例挂到 `ctx.llm`。

如果 `[voice].enabled` 则构建 voice service。启用 voice 时,`build_voice_service(VoiceServiceConfig(...))` 构造一个 `VoiceService` 并挂到 `ctx.voice_service`。Voice 失败是非致命的——TTS provider 联不上,守护进程 log 一条 warning、带着 `voice_service = None` 启动,channels 和 proactive 优雅降级回文本。

如果 `[proactive].enabled` 则构建 proactive scheduler。`_build_proactive_scheduler` 组装一个 `MemoryFacade`、一个 `ProactiveChannelRegistry` 适配器、一个 proactive prompt callable,以及一个每次属性访问都现读 `voice_enabled` 的 `PersonaView`。Scheduler 此时还没启动;引用保存在 `Runtime._proactive_scheduler` 上,等后面再调它自己的 start。

构建 importer facade。`ImporterFacade` 持有 LLM provider、voice service,以及一个只读的 `MemoryFacade` 引用。它在未来的 web 管理路由和 import pipeline 之间做中介,这样 channels 和 import 永远不需要互相 import。

向 channel registry 注册 channel。传进 `Runtime.start(channels=[...])` 的任何 channel 实例都会被加到 registry,以 `channel_id` 为 key。

启动所有 channel。`await registry.start_all()` 并发跑每个 channel 的 `start()`。启动失败的 channel log error 并保持未注册;守护进程继续启动,让其他子系统保持可用。

构造并注册 runtime memory observer。`RuntimeMemoryObserver(registry, loop)` 被创建出来,传进 memory 模块的 `register_observer(...)`。从这一刻起,memory 每次 commit 一个 session close、新 session 开始或 mood 更新,observer 就把事件扇出到 registry 里每个暴露了 `push_sse()` 能力的 channel。

从配置填充 `ctx.persona.voice_enabled`。`[persona].voice_enabled` 的 bool 被拷进可变的 `RuntimePersonaContext`,这样 interaction 和 proactive 在 turn 时读到的是同一个内存值。

把 turn dispatcher、consolidate worker、idle scanner 作为后台任务用 `asyncio.create_task` 启动。这三个任务活到守护进程的终点,每一拍都检查 `shutdown_event.is_set()`。Proactive scheduler 也在这一步用 `await scheduler.start()` 启动;它 spawn 自己的内部 task,所以 runtime 不需要直接持有 task handle。

注册信号处理。`loop.add_signal_handler(SIGINT / SIGTERM)` 翻转 shutdown event;`SIGHUP` 把 `Runtime.reload()` 调度为一个 task。Windows 上这一步是 no-op,带一条 warning。

打印 local-first 披露行。一行总结日志列出这个进程将要联系的每一个对外地址。这一行永远是启动最后 emit 的东西,审计者跑 `echovessel run | head -2` 第一眼就能看到。

### Turn loop 细节

```
channel.incoming()   ┐
channel.incoming()   ├── ChannelRegistry.all_incoming()
channel.incoming()   ┘        │
                              ▼
                    ┌──────────────────┐
                    │ TurnDispatcher   │
                    │  asyncio.Queue   │  (一个队列,一个消费者)
                    └───────┬──────────┘
                            ▼
                    Runtime._handle_turn(envelope)
                            │
                            │ 规范化 IncomingMessage → IncomingTurn
                            │ llm = self.ctx.llm      (本地快照)
                            │ on_token      = getattr(channel, "on_token", None)
                            │ on_turn_done  = getattr(channel, "on_turn_done", None)
                            │ channel.in_flight_turn_id = turn.turn_id
                            ▼
                    assemble_turn(turn_ctx, turn, llm,
                                  on_token=..., on_turn_done=...)
                            │
                            │  1. 逐条 ingest user 消息  → memory (turn_id)
                            │  2. 加载 L1 core blocks
                            │  3. 检索 L3+L4 记忆
                            │  4. 加载 L2 近窗
                            │  5. 组 system + user prompt
                            │  6. async for token in llm.stream(...):
                            │        accumulated.append(token)
                            │        await on_token(msg_id, token)
                            │  7. ingest persona 回复 → memory (同一 turn_id)
                            │  finally:
                            │        await on_turn_done(turn.turn_id)
                            ▼
                    AssembledTurn(reply=..., system_prompt=..., ...)
                            │
                            ▼
                    await channel.send(external_ref, reply)
```

这个流程里有几个细节值得记住。LLM 引用在 `_handle_turn` 开头被捕获为**本地快照**。一个热重载替换 `self.ctx.llm` 时,in-flight turn 不会受影响——老 provider 对象会活到 turn 的局部变量出作用域为止。没有锁、没有 epoch 计数器;Python 的引用语义免费搞定这件事。

每一次 handler 调用都被 `asyncio.wait_for(..., timeout=runtime.turn_timeout_seconds)` 包住。挂掉的 `llm.stream`——最常见的触发方式是 provider 代理错配到一个能 TCP 连上但永远不回应的地址——会被取消,dispatcher 直接跳到下一个 envelope。默认是 120 秒;在 `config.toml` 里设为 0 可关闭(**不推荐**——一次挂住的 handler 会静默阻塞所有 channel 后续的每一条消息)。超时发生时,事件以 `error` 级别 log 出来并带上 channel id,任何已经写入 L2 的部分 token 保留,`on_turn_done` 仍然在 `finally` 里触发,channel 的 debounce 状态机能正常回卷。

Persona 的回复**先写进 memory,再让 channel 发出去**。如果写入失败,send 被拒绝——守护进程宁可不发一句 persona 不记得自己说过的话。如果 send 失败但写入成功,回复仍然在 L2,客户端下次重连就能看到。这个顺序规则是 turn loop 里最重要的不变式。

`on_turn_done` 永远恰好被调一次,位置是 `assemble_turn` 底部的 `finally` 块。Turn 成功时、LLM 瞬时错误只留下部分 token 时、LLM 永久错误一个 token 都没出来时、memory ingest 失败时——channel 永远被通知。没有这条不变式,一个 channel 的 debounce 状态机会永远挂在那里等一个早就结束的 turn。

### Memory observer 接线

Memory 的 lifecycle hook(`on_session_closed`、`on_new_session_started`、`on_mood_updated`)在 `MemoryEventObserver` Protocol 里被定义为**同步**方法——memory 没法 import asyncio,因为它的写入路径是同步的,跑在 SQLite 单写入者的锁里面。Runtime 的 observer 实现也是同步的,所以它的方法立即返回;把事件广播到 channel 这个真正的工作被通过 `asyncio.run_coroutine_threadsafe(self._broadcast(...), self._loop)` 调度到 runtime 的 event loop 上。

效果是一个干净的分离:memory 在一次成功 commit 之后触发一个 sync hook,hook 在微秒级返回,async 广播在 loop 上并发跑,遍历 channel registry 并对任何暴露了 `push_sse` 能力的 channel 调 `await channel.push_sse(event, payload)`。单 channel 的 push 失败会被捕获并 log;一个坏 channel 不会污染另一个 channel 的广播,memory 的写入无论 observer 做什么都已经 commit 了。如果 loop 不可用(observer 在关机过程中被触发),coroutine 被干净地关掉并 log 一条 warning——一次丢失的广播没有什么可做的,因为 memory 状态早就落盘了。

### `voice_enabled` toggle

`voice_enabled` 是 persona 级的主开关。`True` 时,被动回复和 proactive nudge 以中性语音片段交付;`False` 时,一切保持文本。它需要一个 runtime API 因为它在运行时可变——管理 UI 翻它不需要重启守护进程——并且翻之后必须持久化回 `config.toml`,以便下次启动记得。

`Runtime.update_persona_voice_enabled(enabled)` 用四个严格步骤实现这次翻转。第一,输入被校验为真正的 `bool`,避免一个误传的整数损坏 TOML 文件。第二,`_atomic_write_config_field` 用 `tomllib` 读当前文件,修改解析后的 dict,用 `tomli_w` 序列化到同目录下的一个 tempfile,fsync tempfile,然后跑 `os.replace` 让 rename 在 POSIX 上原子。第三——**只在**磁盘写入成功之后——`ctx.persona.voice_enabled` 被原地改写;如果写入抛了,内存状态保持不变,这样 config 和 ctx 永不分歧。第四,一条 `chat.settings.updated` SSE 事件广播给每个暴露了 `push_sse` 的 channel,单 channel 失败被 log 但被吞掉。

Interaction 在构造外发回复的那一刻读 `ctx.persona.voice_enabled`;proactive 通过一个 `RuntimeContextPersonaView` 适配器读同一个字段,适配器的 property 每次访问都现读 `ctx.persona`。不加锁、不缓存——Python 里 bool 的读在字节码层是原子的,跨 tick 边界的短暂 race 可以接受。

### LLM tier 体系

Runtime 里每个调用点声明自己想要的 tier,provider 在调用时把 tier 映射到 model。LLMProvider 契约(`runtime/llm/base.py`)是一个微小的 `Protocol`,只有三个方法:`model_for(tier)` 用于 log 和审计,`complete(system, user, *, tier, ...)` 用于单次 completion,`stream(system, user, *, tier, ...)` 用于逐 token 流式。每个调用签名把 tier 作为关键字参数传进来,默认 `MEDIUM`。

调用点的 tier 分配固定在代码里,不在配置里,因为它们反映的是架构意图而不是用户偏好。Extraction 和 reflection 是 SMALL——它们跑在关闭的 session 上,批量跑便宜的调用,用户也没在等。Reflection 理论上能从更强的模型里受益,但 MVP 阶段 Haiku 级的输出够用了;不同意的用户可以在 `[llm.tier_models]` 里把 SMALL 拉高而不用动代码。Judge(eval harness,未来)是 MEDIUM——严格评估要一致性,不要最贵的模型。Interaction 和 proactive 是 LARGE——用户正盯着屏幕,更好的模型能带来显著更好的回复。

`runtime/llm/` 里的三个具体 provider 加起来覆盖 15+ 个真实端点。`AnthropicProvider` 用原生 `anthropic` SDK,对着 Claude。`OpenAICompatibleProvider` 用原生 `openai` SDK,`base_url` 可配,这意味着它覆盖 OpenAI 官方、OpenRouter、Ollama、LM Studio、llama.cpp server、vLLM、DeepSeek、Together、Groq、xAI,以及任何实现了 OpenAI 兼容 REST 的 provider。`StubProvider` 返回预置文本,用于测试和 dry run。

### 控制面(control plane)

Runtime 跑一个**独立的 HTTP 控制面**,用来承载 operator 级别的 daemon 生命周期操作——`/health` · `/shutdown` · `/reload`。这个 plane 住在 `runtime/control.py` 里,绑 `127.0.0.1:0`(kernel 分配一个随机空闲端口),用独立的 uvicorn 实例,和 Web channel 完全正交。信任模型三根支柱:绑定 host 是模块级常量(**永不**暴露成配置项)· Host header middleware 拒绝任何不是 `localhost` / `127.0.0.1` 的请求(返回 403 · 防御 DNS-rebinding 攻击把浏览器骗去 POST 这个端口)· 不输出任何 CORS header 所以浏览器的 same-origin policy 兜底。分配到的端口会写进 pidfile 的 JSON:`{"pid": 12345, "control_port": 54321, "version": 1}`。

CLI 直接消费这个 plane。`echovessel stop` 读 pidfile · POST `/shutdown` · 打印 `stopped (via control plane, pid=…)`;`echovessel reload` POST `/reload` · 打印 `reloaded (via control plane): llm` 或 `no changes detected`;`echovessel status` 探 `/health` 同时报 plane 可达性和 PID。每个 CLI 都会在 pidfile 是 v1(老装机的纯整数格式)或 plane 不通时退化到 Unix 信号——这种情况 stop 照样发 `SIGTERM` · reload 照样发 `SIGHUP` · 输出带上 `(control plane unreachable)` 的尾巴让 operator 知道走的是慢路径。

控制面跟 Web channel 的可用性**无关**。即使 `[channels.web].enabled = false` · plane 也在 · operator 仍能干净关机。反过来说 · Web channel 灾难性崩溃也不拉 plane 一起下水——这是这套架构真正买到的分离特性。

### Reload

`Runtime.reload()` 从磁盘重读 `config.toml` · 校验 · 把改动传播到跑着的组件。三条入口:HTTP `POST /reload`(现代路径)· `SIGHUP`(legacy 兼容)· admin `PATCH /api/admin/config` 在写完合并 TOML 后也会调 `reload()`。三条路径共享同一把 `asyncio.Lock`,所以两个同时 in-flight 的 reload 请求不会在 `ctx.config` 换手中间打架。

真正热生效的东西:

- **`[llm].*`** —— `build_llm_provider` 造新 provider · 原子换进 `ctx.llm`。in-flight turn 不受影响 · 因为它们的本地 `llm` 变量在 turn 开始时捕获了快照;老 provider 一直活到最后一个 in-flight turn 结束 · 然后 Python GC 掉。
- **`[memory].retrieve_k` / `.recent_window_size` / `.relational_bonus_weight`** —— turn handler 每个 turn 都现读 `ctx.config.memory.*` · 下一个 turn 就用新值 · worker 完全不用重启。
- **`[consolidate].trivial_message_count` / `.trivial_token_count` / `.reflection_hard_gate_24h`** —— 镜像到活着的 `ConsolidateWorker` 实例上(worker 在启动时用旧值构造 · 单改 `ctx.config` 不会生效 · reload 路径直接改它的实例属性)。

`HOT_RELOADABLE_CONFIG_PATHS` 白名单里其他字段都遵循同一条规则:`ctx.config` 整个换掉 · 消费者现读。结构性 section ——`[runtime].data_dir` · `[memory].db_path` · `[memory].embedder` · `[voice]` · `[proactive]` · `[channels.*]` —— 要完整重启;完整的字段矩阵见 `configuration.md`。`[persona].voice_enabled` 有自己专用 API(`update_persona_voice_enabled`),reload 不碰。`[persona].display_name` 在纯 reload 上只更新 `ctx.config` · **不**更新 `ctx.persona`;admin PATCH 路径有一个额外的 side-step 把改动镜像进 `ctx.persona` 让可见层生效。

Reload 返回实际改变的组件名列表(比如 `["llm", "consolidate.trivial_message_count"]`),CLI 渲染出来 · admin API 把它暴露给 UI。空 list 意味着"白名单里没有一条跟当前活值不同"。

### 为什么 LLM prompt 里从不出现传输层标识

`assemble_turn` 喂给 LLM 的 system prompt 和 user prompt 包含**零**条关于 turn 从哪个 channel 进来的信息。任何字段都不含 channel id。渲染路径里任何地方都没有 "web" / "discord" / "imessage" 字面量。System prompt 底部硬编码的 style 指令明确禁止 persona 提到任何传输名、线程名或界面名,哪怕用户拿它开玩笑也不行。Memory retrieve 也不接受 `channel_id` 过滤——L1 core blocks、L3+L4 检索记忆、L2 近窗,全部是**不带过滤**加载的。

原因是 EchoVessel 承诺 persona 跨所有 channel 是一个连续的整体。Persona 一旦说出"我昨天在 Discord 见过你",幻觉就破了,因为从 persona 的视角用户从没"在 Discord 上"——他们只是在说话。Channel id 只住在两个地方:memory schema,作为每行的 provenance 存着,前端可以用它渲染"via Web"小标签;以及 ingest 路径,runtime 把它原样传给 `memory.ingest_message(...)`,这是最后一处合法使用。这两处都不喂给 prompt。

---

## How to Extend

### 1. 加一个新的 LLM provider

在 `src/echovessel/runtime/llm/` 下新建一个文件实现 `LLMProvider` Protocol,在 `runtime/llm/factory.py::build_llm_provider` 里注册它,并把字面量加到 `runtime/config.py::LLMSection.provider` 的 `Literal[...]` 里。一个 provider 的最小骨架:

```python
# src/echovessel/runtime/llm/my_provider.py
from __future__ import annotations

from collections.abc import AsyncIterator

from echovessel.runtime.llm.base import LLMProvider, LLMTier
from echovessel.runtime.llm.errors import LLMPermanentError, LLMTransientError


class MyProvider(LLMProvider):
    """新 LLM provider 的最小骨架。"""

    provider_name = "my_provider"

    _DEFAULT_TIERS = {
        LLMTier.SMALL: "my-small-model",
        LLMTier.MEDIUM: "my-medium-model",
        LLMTier.LARGE: "my-large-model",
    }

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None = None,
        pinned_model: str | None = None,
        tier_models: dict[str, str] | None = None,
        default_max_tokens: int = 1024,
        default_temperature: float = 0.7,
        default_timeout: float = 60.0,
    ) -> None:
        # 构造函数**绝不**联网,只缓存配置。
        self._api_key = api_key
        self.base_url = base_url or "https://api.example.com/v1"
        self._pinned = pinned_model
        self._tier_models = tier_models or {}
        self._max_tokens = default_max_tokens
        self._temperature = default_temperature
        self._timeout = default_timeout

    def model_for(self, tier: LLMTier) -> str:
        if self._pinned:
            return self._pinned
        if tier.value in self._tier_models:
            return self._tier_models[tier.value]
        return self._DEFAULT_TIERS[tier]

    async def complete(
        self,
        system: str,
        user: str,
        *,
        tier: LLMTier = LLMTier.MEDIUM,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        timeout: float | None = None,
    ) -> str:
        try:
            # ... call self._client.complete(...) ...
            return "response text"
        except TimeoutError as e:
            raise LLMTransientError(str(e)) from e
        except ValueError as e:
            raise LLMPermanentError(str(e)) from e

    async def stream(
        self,
        system: str,
        user: str,
        *,
        tier: LLMTier = LLMTier.MEDIUM,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        timeout: float | None = None,
    ) -> AsyncIterator[str]:
        # 不能真流式的 provider 可以退化为 complete()
        # 再一次 yield 整段文本。
        text = await self.complete(
            system, user,
            tier=tier, max_tokens=max_tokens,
            temperature=temperature, timeout=timeout,
        )
        yield text
```

在 `build_llm_provider` 里注册:

```python
if provider == "my_provider":
    from echovessel.runtime.llm.my_provider import MyProvider
    return MyProvider(
        api_key=api_key,
        base_url=cfg.base_url,
        pinned_model=cfg.model,
        tier_models=cfg.tier_models or None,
        default_max_tokens=cfg.max_tokens,
        default_temperature=cfg.temperature,
        default_timeout=float(cfg.timeout_seconds),
    )
```

骨架必须遵守三条规则。构造函数不能触网——用户 API key 错了应该在第一次 `complete` 时才发现,不在启动时。瞬时错误和永久错误必须是两个不同的异常类型,因为 consolidate worker 只对瞬时错误重试。Tier → model 解析必须遵循优先级 `pinned > tier_models > 默认`;用户依赖 `llm.model = "x"` 表达"用 x 跑一切"。

### 2. 加一个新的启动步骤

启动逻辑被分成 `Runtime.build`(同步构造)和 `Runtime.start`(异步启动)两段。纯对象构造归 `build`;任何 spawn 后台 task 或 await 一个调用的事情归 `start`。

如果新步骤产出一个其他子系统需要的长生命周期对象(新后台服务、新适配器层),就给 `RuntimeContext` 加个字段、在 `build` 里构造、在 `start` 里把它注册进 channel registry 或者 spawn 它的 task。如果新步骤需要在 channels 启动**之前**跑(因为 channels 依赖它),放在 `registry.start_all()` 前面;如果需要在 channels 启动**之后**跑(因为它把事件扇进 channels),放在 `registry.start_all()` 后面——memory observer 注册就是模板。

```python
# in Runtime.build(...)
my_service = MyService(cfg=config.my_section, engine=engine)
ctx = RuntimeContext(
    ...,
    my_service=my_service,     # RuntimeContext 上的新字段
)

# in Runtime.start(...)
# 例 A:需要 channels 先活过来
await self.ctx.registry.start_all()
try:
    self.ctx.my_service.attach(self.ctx.registry)
except Exception as e:
    log.warning("my_service.attach failed: %s", e)

# 例 B:需要自己的后台 task
self._tasks.append(
    asyncio.create_task(self.ctx.my_service.run(), name="my_service")
)
```

两条规则。非关键子系统的失败要优雅降级——log warning、把引用置 None、让守护进程照常启动。只有 schema 迁移和数据库打开是致命的,因为半开的守护进程会写坏数据。如果新服务需要干净停机,在 `Runtime.stop` 里加一块对应的收尾代码,这样 `shutdown_event` 的传播仍然起作用。

### 3. 处理一个新信号

信号处理函数在 `_register_signal_handlers` 里通过 `loop.add_signal_handler` 注册。处理函数**不能**做真正的工作——翻一个标志或调度一个 task,然后立即返回。在信号处理里阻塞会让 loop 死锁。

```python
# in Runtime._register_signal_handlers
import signal

def _dump_state() -> None:
    """SIGUSR1:把 runtime 状态 dump 到日志,用于调试。"""
    log.info("runtime state dump: channels=%s, in_flight=%s",
             self.ctx.registry.channel_ids(),
             self.ctx.registry.any_channel_in_flight())

try:
    loop.add_signal_handler(signal.SIGUSR1, _dump_state)
except NotImplementedError:
    # Windows:不支持信号处理,静默跳过。
    pass
```

如果新信号需要优雅停机语义,照 SIGINT/SIGTERM 的模板来:处理函数翻 `self.ctx.shutdown_event`,后台任务的下一拍都会看到这个事件。`Runtime.stop` 顶部已经在等这个事件,然后 cancel 后台任务、停 proactive scheduler、拆掉 channel registry——如果新信号应该触发 drain,直接 set shutdown event 就行。如果处理函数需要不停机地重建某样东西(SIGHUP 是模板),用 `asyncio.create_task(self.my_reload())` 调度一个异步方法,把 reload 逻辑写在那个 coroutine 里,这样 I/O 和锁都是异步安全的。

---

## See also

- [`memory.md`](./memory.md) — runtime 通过 `ingest_message` / `retrieve` / `load_core_blocks` 喂送和读取的 ground truth 存储
- [`channels.md`](./channels.md) — yield `IncomingTurn`、消费 `on_token` / `on_turn_done` 回调的传输层
- [`voice.md`](./voice.md) — `VoiceService`,在启动第 9 步构建,在回复时通过 `ctx.persona.voice_enabled` 读取
- [`proactive.md`](./proactive.md) — 在启动第 10 步构建、与 turn dispatcher 一起启动的调度器
- [`configuration.md`](./configuration.md) — `config.toml` 里的每一个字段以及它们如何映射到 `RuntimeContext`
- `echovessel init` — 从打包的 sample 创建 `~/.echovessel/config.toml`
