# EchoVessel

[![CI](https://github.com/AlanY1an/echovessel/actions/workflows/ci.yml/badge.svg)](https://github.com/AlanY1an/echovessel/actions/workflows/ci.yml)

> 🌐 **其他语言版本:** [English](./README.md)

*有些人留下回忆。
有些人留下一副声音。
有些人以碎片的形式继续和我们在一起:一种语调、一种节奏、一种永远不会彻底消散的说话方式。*

**EchoVessel** 是一个开源引擎,用来构建**能够记得、能够回应、能够演化、能够跨时间持续在场**的数字 persona。

它是为想要创作角色、陪伴者、虚构人格、个人回响,或有明确同意的数字化身而设计的,主要关注:

- 身份与风格
- 长期记忆
- 关系演化
- 语音交互
- Local-first 的隐私

EchoVessel 不是一个通用 chatbot · 它是**为"在场"而造的容器**(a vessel for presence)。

---

## 快速开始(v0.0.1)

v0.0.1 是早期 alpha 版本。它发布一个 local-first daemon · 基于完整 5 模块栈(memory / voice / channels / proactive / runtime)· 含可用的 Web channel(对话 + 人格 block 编辑 + 语音开关 + 首次 onboarding)和可用的 Discord DM channel。**部分 admin 面板在本版本是 placeholder**——具体延后到 v0.0.2+ 的项目见 [`CHANGELOG.md`](./CHANGELOG.md) 的 **Known Limitations** 一节。已在 macOS 和 Linux 上测试;Windows 暂不支持。

### 从源码安装

EchoVessel 要求 Python **3.11+**。**目前还没发 PyPI**,从源码跑:

```bash
git clone https://github.com/AlanY1an/echovessel.git
cd echovessel
uv sync --all-extras
```

`--all-extras` 一次装全。只装用得到的:

```bash
uv sync --extra embeddings --extra llm --extra voice --extra discord
```

- `embeddings` — 本地 sentence-transformers embedder
- `llm` — OpenAI / Anthropic SDK
- `voice` — FishAudio TTS SDK
- `discord` — `discord.py` · Discord DM channel

下面所有命令都在 repo 根目录用 `uv run …` 跑。

### 首次启动

EchoVessel 从 `~/.echovessel/config.toml` 读配置 · 从**当前工作目录**的 `./.env` 读 API 密钥。一条命令同时生成这两个:

```bash
uv run echovessel init
```

`init` 把 `~/.echovessel/config.toml` 和一个注释全关掉的 `.env` 写到**当前目录**(权限 0600)。daemon 启动时自动加载 `./.env`,所以 `.env` 要留在你启动 daemon 的那个目录(通常是项目根)。按需取消注释填值:

```
OPENAI_API_KEY=sk-...
FISH_AUDIO_KEY=...              # 可选 · FishAudio TTS
ECHOVESSEL_DISCORD_TOKEN=...    # 可选 · Discord bot token
```

编辑 `~/.echovessel/config.toml` 选一个 LLM provider——任何 OpenAI 兼容端点都可以零配置跑(设置 `OPENAI_API_KEY`),或切到 `anthropic` + `ANTHROPIC_API_KEY`,或用 `ollama`(本地 · 无需 key)。所有选项见 sample 文件。

**不带任何 API key 做烟测**:在 config 里设 `[llm].provider = "stub"`,daemon 会以固定 stub 回复启动——这是验证新安装最省心的方式。

### 启动 Daemon

```bash
uv run echovessel run
```

首次启动会下载 sentence-transformers embedder(~90MB · 一次性)· 之后启动瞬时完成。

干净启动时预期的 log:
```
schema migration: created table core_block_appends
importer facade: built
memory observer: registered
EchoVessel runtime started | ...
local-first disclosure: outbound = only <llm endpoint>; embedder runs locally; no telemetry
```

数据住在 `~/.echovessel/memory.db`(SQLite + sqlite-vec)· 日志在 `~/.echovessel/logs/`。

### Web Channel

Daemon 直接在 `http://127.0.0.1:7777/` 托管 React UI(host / port 在 `config.toml` 的 `[channels.web]` 下可配置)。浏览器打开就能用——不需要 `npm`,也不需要额外的 dev server。

如果你想从源码重新构建前端(仅 contributor 需要),源码在 `src/echovessel/channels/web/frontend/`:

```bash
cd src/echovessel/channels/web/frontend
npm install
npm run build
```

hatch build hook 会把产物复制到 `src/echovessel/channels/web/static/` · 这份 bundle 跟着 wheel 一起发布。

### Discord Channel

EchoVessel 可以通过 Discord DM 和你对话——文本回复 + 启用语音后的原生 OGG Opus 语音消息。

1. 在 <https://discord.com/developers/applications> 创建 application + bot · 在 **Bot → Privileged Gateway Intents** 里打开 **MESSAGE CONTENT INTENT**。
2. 把 bot token 放进 `.env`:
   ```
   ECHOVESSEL_DISCORD_TOKEN=...
   ```
3. 编辑 `~/.echovessel/config.toml`:
   ```toml
   [channels.discord]
   enabled = true
   token_env = "ECHOVESSEL_DISCORD_TOKEN"
   debounce_ms = 2000
   # allowed_user_ids = [123456789012345678]   # 可选 allowlist
   ```
4. 把 bot 邀请进你的账号(OAuth2 URL generator → `bot` scope + DM 权限),然后给 bot 发 DM。消息会按 debounce(默认 2s)聚合成一个 turn 再派发。
5. 当 `[persona].voice_enabled = true` **且** `ffmpeg` 在 PATH 上时,Discord 会收到原生语音消息气泡——channel 会把 FishAudio 的 MP3 现场转成 OGG Opus。安装方式:`brew install ffmpeg`(macOS)或 `apt install ffmpeg`(Debian / Ubuntu)。没有 ffmpeg 时 Discord channel 会退化为纯文本。
6. 你通过 Discord DM 发的消息也会实时出现在 `http://127.0.0.1:7777/` 的 Web 聊天页 · 带 `📱 Discord` 角标。历史 Discord 消息在 Web 挂载时通过 `/api/chat/history` 拉回来。两个 channel 共享同一份 persona 记忆(铁律 D4)。

### 语音

EchoVessel 的 TTS 用 [FishAudio](https://fish.audio)。把 `FISH_AUDIO_KEY` 放进 `.env`,在 `config.toml` 的 `[persona]` 里选一个 `voice_id`,再设 `[persona].voice_enabled = true`,语音就会和文本一起发出。Discord 走原生语音气泡的路径额外需要 `ffmpeg`(MP3 → OGG Opus 转码)。

### 跑测试

```bash
uv run pytest tests/ -q                # 916 测试 · 覆盖 memory / runtime / voice / proactive / channels / import / integration
uv run ruff check src/ tests/          # lint
uv run lint-imports                    # 分层架构契约
```

### 项目布局

```
src/echovessel/
├── core/            — 共享类型、枚举、工具
├── memory/          — L1-L4 记忆 · SQLite + sqlite-vec · observer + 迁移
├── voice/           — TTS + STT + 语音克隆(FishAudio + Whisper + stub)
├── proactive/       — 自主消息 · policy gate · delivery
├── channels/        — Channel Protocol + 各 channel 适配器(web + discord)
│   ├── web/         — FastAPI routes + SSE + 内嵌 React bundle
│   │   ├── frontend/ — React 19 + Vite + TS 源码(contributor)
│   │   └── static/  — daemon 托管的构建产物
│   └── discord/     — discord.py bot · DM 接入 · OGG Opus 语音
├── import_/         — 通用 LLM importer pipeline(文本 → 记忆)
├── prompts/         — extraction / reflection / interaction 的 system prompt
├── resources/       — 内置的 config.toml.sample
└── runtime/         — daemon · turn dispatcher · LLM provider · CLI
```

### 当前状态(v0.0.1)

- ✅ **Daemon**:完整 boot · 所有启动接线都在 log 里可验证 · 916 测试通过(3 skipped)
- ✅ **跨 channel 统一 timeline**:Web 聊天页实时流入任何 channel 的 turn 事件(今天 Web + Discord · iMessage 就绪)· 消息带 `📱 Discord` / `💬 iMessage` 来源角标。新 `/api/chat/history` 端点在挂载时跨 channel 回填最近 50 条消息。
- ✅ **Memory**:L1–L4 分层 · 幂等 schema 迁移 · observer hook · 4/4 MVP eval 指标达标(Over-recall FP Rate 0.08 ≤ 目标 0.15)
- ✅ **Voice**:FishAudio TTS + stub TTS provider · `VoiceService.generate_voice()` facade · per-persona `voice_id` · 本地 MP3 磁盘缓存
- ✅ **Proactive**:policy 引擎 · 四条 gate(含 `no_in_flight_turn`)· delivery 从 `persona.voice_enabled` 继承
- ✅ **Runtime**:流式 turn loop(IncomingTurn + text delta)· 原子 persona voice toggle · `SIGHUP` 热重载 · memory observer 接线
- ✅ **Web channel**(本版本生产路径):FastAPI + SSE 流式 · 内嵌 React 19 bundle · onboarding 流程 · 流式 token 对话 · admin → 人格 core block 编辑 · admin → 语音开关
- 🚧 **Web channel**(本版本 placeholder):admin → 发生过的事 / 长期印象 / 声音克隆 / 配置 等 tab 只渲染骨架,无后端联动;Onboarding 的"上传材料"路径是 coming-soon 占位屏
- ✅ **Discord channel**:DM 接入 + debounce · 文本回复 · 原生 OGG Opus 语音消息(需 ffmpeg)
- ✅ **Import pipeline**(仅库,未挂路由):通用 LLM importer · 五类 content type 分类 · `self_block` 侧路径 · 强制 embed pass —— *本版本未暴露 HTTP 路由,Web SPA / CLI 都还无法触发真实 import*
- ⚠️ **平台**:macOS 和 Linux 已测试;Windows 暂不支持
- 🔜 **v0.0.2 目标**:接 `/api/admin/import/*` 路由 + Web 导入向导 · admin events / thoughts 列表视图 · Web chat 订阅 mood / session-boundary SSE 事件

---

## 这是什么

EchoVessel 是一个 local-first 的**数字 persona 引擎**。

它让用户从结构化设定和源素材出发,定义或蒸馏出一个 persona,然后把这个 persona 放进一套带记忆、带语音、带关系行为的长期交互系统里运行。

目标不是生成一次性的回复 · 目标是创造一个**感觉上是连续的** persona。

## 支持的 persona 来源

EchoVessel 的适用场景是:

- 虚构角色
- 原创角色
- 自我 persona
- 有明确同意的数字化身
- 纪念性、创作性、研究性的重建

EchoVessel **不是**用来在对外沟通里冒充真实他人的工具。

## 核心理念

### 1. Persona 定义

每个 persona 可以由以下维度塑造:

- 姓名
- 身份
- 年龄
- 背景
- 性格
- 价值观
- 关系角色
- 说话风格

### 2. 风格蒸馏

一个 persona 的交互风格可以从以下材料里学来:

- 聊天记录
- 小说
- 剧本
- 对白台词
- 混合源素材

目标不是肤浅的复制 · 目标是**连贯的行为风格**。

### 3. 记忆系统

EchoVessel 把记忆当作一等系统:

- 事实性记忆
- 偏好记忆
- 情绪模式
- 事件时间线
- 关系记忆

难的问题不只是**存储**记忆,而是决定:

- 哪些应该被记住
- 应该怎样被表达
- 什么时候应该影响行为

### 4. 关系演化

EchoVessel 不依赖可见的"好感度条"。

Persona 的演化通过内部的**关系状态**表现,这种状态外化为:

- 语气变化
- 称呼变化
- 不同级别的主动性
- 更深的上下文召回
- 适应性的安抚与支持模式

### 5. 交互层

规划中的交互模式包括:

- 文字对话
- 语音消息
- 主动消息
- 问候与 check-in
- 群聊在场
- 多 persona 交互
- AI 生成的照片分享

### 6. 语音层

语音是项目的核心部分,不是可选加成。

系统被设计为支持 persona 的语音输出和语音消息往来,尽可能使用 local-first 或自托管的语音 pipeline。

## 设计原则

- 默认 local-first
- 隐私重要
- 记忆就是护城河
- 语音是身份的一部分
- 关系应当通过行为演化,而不是暴露的分数
- Persona 应当感觉是持续的,不是无状态的

## 早期 MVP 方向

EchoVessel 第一个真正可用的版本应该聚焦于:

- 一个 persona
- 文字对话
- 语音消息
- 长期记忆
- 关系状态
- 主动消息
- 一个简单的 Web 界面
- 一个外部 channel 适配器

## 长期方向

EchoVessel 可能演化的方向:

- Persona 市集
- 可导入/导出的 persona 包
- 基于插件的适配器和行为
- 多 persona 社交空间
- 世界模拟和叙事场景
- 跨消息 channel 的自托管部署

## 为什么开源

EchoVessel 应当保持开放、可审视、可修改、可私有。

这个项目建立在一个信念上:数字在场、记忆系统、亲密计算工具不应该只属于封闭的商业平台。

## 当前状态

EchoVessel **v0.0.1** 是早期 alpha 版本——核心 5 模块栈已就位,daemon 完整可启动,Web 和 Discord channel 的对话路径都能跑。本版本若干 admin 面板还是 placeholder;详见 [`CHANGELOG.md`](./CHANGELOG.md) 的 **Known Limitations** 和本 README 上方的 **当前状态(v0.0.1)** 明细。

- ✅ 5 模块架构(memory / voice / channels / proactive / runtime)已实现并测试
- ✅ CLI daemon 完整 boot 且可运行
- ✅ 916 测试通过(3 skipped)· 分层 import 契约被强制 · 4/4 MVP eval 指标达标
- ✅ Web channel:daemon 在 7777 端口托管内嵌的 React bundle(对话 + onboarding + 人格 block 编辑 + 语音开关)
- ✅ Discord DM channel:文本 + 原生 OGG Opus 语音消息(需 ffmpeg)
- 🚧 Import 流程尚未接入 daemon · admin 的发生过的事 / 长期印象 / 声音克隆 / 配置 tab 是 placeholder · 计划 v0.0.2
- ⚠️ 仅 macOS / Linux —— Windows 暂不支持
- 🔜 iMessage / WeChat channel · persona 自选语音投递 · multi-persona 计划在后续版本

## 名字

**EchoVessel** 的意思是:把一个回响承载得足够久,直到它变成在场。
