# EchoVessel 文档(中文)

> **受众**:第一次接触 EchoVessel 的开发者。本文档树保持干净、当前、聚焦于理解和使用今天的系统。

EchoVessel 是一个 local-first 的 Python daemon · 承载一个长期存在的数字 persona · 带分层记忆 / 语音 / 多 channel 支持。它不是 chatbot 框架,而是**数字存在**(digital presence)的持久化系统。

---

## 快速上手

**第一次来?** **[首次启动指南](./first-time-setup.md)** 带你从 `git clone` + `uv sync --all-extras` 到在浏览器里跟第一个 persona 对话——全程大约十分钟。

急性子速览:

```bash
git clone https://github.com/AlanY1an/echovessel.git
cd echovessel
uv sync --all-extras
uv run echovessel init         # 生成 ~/.echovessel/config.toml
uv run echovessel run          # 首次启动会自动打开 http://localhost:7777/
```

`Ctrl-C` 优雅退出 daemon。配置字段查 [`configuration.md`](./configuration.md),启动流程细节查 [`runtime.md`](./runtime.md)。

---

## 架构一览

> 🗺 **完整一页架构图**:[`architecture.html`](https://alanyian.com/projects/echovessel/docs/architecture.html) —— 浏览器打开,一页拿到模块分层、Memory L1-L6 架构、消息流程、跨 channel SSE mirror、完整 HTTP 接口清单、铁律、发布时间线。
>
> 🧠 **记忆系统最简图**:[`memory/layers.html`](https://alanyian.com/projects/echovessel/docs/memory/layers.html) —— 一张 SVG · 6 层 · 如何连接 · write / distill / read 三种模式 · 附 Stanford Generative Agents 打分公式的致敬说明。
>
> 🔄 **运行时流程 / "记忆层如何被唤醒"**:[`architecture-flow.html`](https://alanyian.com/projects/echovessel/docs/architecture-flow.html) —— 配套页 · 聚焦单次 turn 的逐步唤醒、8 列 sequence 图、真实故事 trace("我养了只猫叫小黑")、检索排序、policy 门禁、SSE 神经系统事件。

```
┌───────────────────────────────────────────────────────┐
│                    RUNTIME(daemon)                    │
│     启动 · turn loop · LLM streaming · SIGHUP         │
└───────────────────────────────────────────────────────┘
              ▲                        ▲
              │                        │
    ┌─────────┴─────────┐    ┌─────────┴─────────┐
    │     CHANNELS      │    │     PROACTIVE     │
    │   debounce · turn  │    │  policy · trigger │
    └─────────┬─────────┘    └─────────┬─────────┘
              │                        │
              └────────────┬───────────┘
                           ▼
              ┌────────────┴────────────┐
              │         MEMORY          │
              │ L1·L2·L3·L4·L5·L6 │
              │   retrieve · consolidate│
              │     observer 模式       │
              └────────────┬────────────┘
                           │
                  ┌────────┴────────┐
                  │      VOICE      │
                  │  TTS · STT · 克隆
                  └─────────────────┘

另外:IMPORT pipeline — 把外部文本离线导入进 memory
```

五个核心模块严格分层。**Runtime** 编排全局 · **Channels** 和 **Proactive** 在 Memory 和 Voice 之上 · **Memory** 和 **Voice** 在 core 之上。Import 通过独立 pipeline 最终写入 Memory。

---

## 模块文档

每个模块一篇。顺序随意——有需要的地方会交叉链接。

| 模块 | 它是什么 |
| --- | --- |
| 📖 [memory.md](./memory.md) | 分层 persona 记忆:L1 core blocks · L2 原始消息 · L3 事件 · L4 thoughts/intentions/expectations · L5 entities + aliases · L6 episodic state · 带 rerank 的 retrieve · 生命周期事件的 observer 模式 · 幂等 schema 迁移 |
| 🗣️ [voice.md](./voice.md) | 文字转语音、语音转文字、声音克隆。Provider 抽象层覆盖 FishAudio / Whisper / stub · `VoiceService.generate_voice()` facade 带本地缓存 |
| 📡 [channels.md](./channels.md) | Channel Protocol:外部传输层(web / Discord / iMessage / WeChat)如何插入 daemon。Burst 用户输入的 debounce state machine。跨 channel 统一 persona 的设计 |
| ⚡ [proactive.md](./proactive.md) | 自主消息。四条 policy gate(quiet hours · cold user · rate limit · no-in-flight-turn)。关系触发。delivery 从 `persona.voice_enabled` 继承 |
| ⚙️ [runtime.md](./runtime.md) | Daemon 主体。启动序列、带 streaming 的 turn loop、SIGHUP config reload、原子 `voice_enabled` toggle、local-first disclosure 审计 |
| 📥 [import.md](./import.md) | 通用 LLM importer。一条 pipeline 处理任何文本格式(日记 / 聊天记录 / 小说 / 简历)。LLM 驱动的 content-type 分类到 memory 的 5 类目标。强制 embed pass |

## 参考

| 文档 | 作用 |
| --- | --- |
| 🔧 [configuration.md](./configuration.md) | `config.toml` 的每个字段 · 默认值 · 合法值 · 何时改 |
| 🛠 [contributing.md](./contributing.md) | Clone · `uv sync` · 跑测试 · PR 流程 · 每次贡献都必须守住的两条铁律 |
| 🧪 [memory-testing.md](./memory-testing.md) | Memory 测试套件钉住了什么 · 按 ingest 到 retrieve 的流程逐层列清楚 · 外加 live-LLM eval 层 |

---

## 设计原则(短版)

1. **Local-first**:所有 persona 数据都在你本机。出站网络限于你选的 LLM endpoint 和(可选的)语音 provider。无 telemetry · 无 phone-home。
2. **分层架构**(CI 里 `import-linter` 强制):`runtime → channels | proactive → memory | voice → core`。下层永不 import 上层。
3. **Memory 永不按 `channel_id` 过滤。** 一个 persona 在所有 channel 上都是同一个 persona。检索、core-block 加载、recall-message 查询全部返回统一时间线;memory 读 API 里没有任何 `channel_id=` 参数,将来也不会有。
4. **LLM prompt 永不泄漏 transport 身份。** System prompt、user prompt、retrieval 拿出来的上下文块都不包含 `channel_id` 或任何传输标识 token。模型根本不知道自己在 Web / Discord / iMessage。

每条原则的深度版本在对应的模块文档里。

---

## 状态

这份文档正在积极开发中。上面列的页面多数是规划但尚未写完的。凡是本文档没覆盖到的内容,直接读 `src/echovessel/` 下的源码——每个模块都有详细的 docstring。
