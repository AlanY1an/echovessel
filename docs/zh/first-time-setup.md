# 首次启动指南

> 从 `git clone` + `uv sync --all-extras` 到在浏览器里跟第一个 persona 对话的完整流程——约十分钟。

## 谁适合看这篇

你刚发现 EchoVessel · 手头有终端和浏览器 · 想在一杯咖啡的时间内让 persona 开口说话。这页把安装 → 配置 → 首次启动 → 新手引导 → 第一条消息串起来,最后再带两个大多数人想接着做的可选路径(语音和 Discord)。

**前置条件:**

- Python 3.11 或更高(用 `python --version` 确认)
- 一台你顺手的终端
- 任意现代浏览器(Chrome / Firefox / Safari / Edge · 支持 ES2020 就行)
- 可选:环境变量里有 `OPENAI_API_KEY`,如果你想让真实 LLM 回你。想先做烟测可以跳过(见第 2 步)。

本流程假设你在自己这台机器上装。EchoVessel 是 local-first 的:所有 persona 数据默认都在 `~/.echovessel/` 下 · 无遥测 · 无上报。出站网络流量只会到你配置的 LLM endpoint 和(可选的)语音服务。

---

## 第 1 步 · 从源码安装

EchoVessel **目前还没发 PyPI**。从 GitHub clone · 用 [`uv`](https://github.com/astral-sh/uv) 同步一个本地 venv:

```bash
git clone https://github.com/AlanY1an/echovessel.git
cd echovessel
uv sync --all-extras
```

`--all-extras` 一次把所有可选栈全拉进来(sentence-transformers embedder · OpenAI + Anthropic SDK · FishAudio TTS · discord.py)。只想装用得到的:

```bash
uv sync --extra embeddings --extra llm --extra voice --extra discord
```

下面所有命令都在 repo 根目录用 `uv run …` 跑。

验证安装:

```bash
uv run echovessel --help
```

你会看到一个短小的子命令列表,包含 `run` · `stop` · `reload` · `status`。

---

## 第 2 步 · 生成配置文件

EchoVessel 读一份 TOML 文件 · 默认位置是 `~/.echovessel/config.toml`。生成它:

```bash
uv run echovessel init
```

这会把打包的配置样板写到 `~/.echovessel/config.toml`,顺便创建数据目录(如果还没有)。你会看到类似这样一行:

```
✓ wrote config to /Users/<you>/.echovessel/config.toml
```

默认配置用 `openai_compat` provider 指向 OpenAI。环境变量里已经有 `OPENAI_API_KEY` 的话,直接跳到第 3 步,一切都能用。

### 零密钥烟测路径

手头没 API key,或者你就想先验证装得上——编辑 `~/.echovessel/config.toml`,找到 `[llm]` section,把 provider 换成 `stub`:

```toml
[llm]
provider = "stub"
api_key_env = ""
```

`stub` provider 只回固定答案,永远不联网。这是最快确认 daemon 能启动 / 浏览器 UI 能加载 / 记忆数据库能被创建的路子。等你手头有真实 key 了再换回来——唯一变化的只是回复质量。

完整字段参考在 [`configuration.md`](./configuration.md)。眼下你需要了解的几个 section:

- `[persona]` · persona 的本地 id 和显示名
- `[memory]` · SQLite 数据库路径(默认是数据目录下的 `memory.db`)
- `[llm]` · provider · 模型 · 存 API key 的环境变量名
- `[channels.web]` · Web channel 的 host / port / 是否启用(默认启用)

---

## 第 3 步 · 启动 daemon

```bash
uv run echovessel run
```

### 首次启动会发生什么

**预热(仅第一次启动需要 30–60 秒):** daemon 会下载默认的 sentence-transformers 嵌入模型。约 90 MB · 一次性 · 之后缓存在数据目录下 · 后续启动接近秒开。没装 `[embeddings]` extra 的话,daemon 会回落到零嵌入,跳过下载。

**启动日志:** daemon 会按顺序打印一串 log 行——打开数据库 / 跑 schema 迁移 / 构建 LLM 客户端 / 接上 channels,最后以 `local-first disclosure:` 一行收尾,告诉你所有可能的出站网络目标。

**浏览器自动打开:** 首次安装时 daemon 会检测到 persona 的 core blocks 还是空的,Web channel 起来后会自动打开 `http://localhost:7777/`。无头环境(SSH · CI · 服务器)下 daemon 会打一行提示说打不开浏览器,让你手动访问该 URL。

### 优雅退出

结束时在跑 `uv run echovessel run` 的终端按 `Ctrl-C`。Daemon 会干净收尾:停掉后台 worker · 关 channels · 刷写挂起的写入 · 退出。硬盘上的数据原封不动。

也可以从另一个终端停:

```bash
uv run echovessel stop
```

效果等同于按 `Ctrl-C`。`run` 终端接了 `nohup` 也能用这条关掉。

---

## 第 4 步 · 新手引导 persona

浏览器第一次打开该 URL 时显示一个两路径引导向导。按你手头已有的素材多少挑一条:

- **从头写**(blank-write) — 写一段话介绍这个 persona 是谁。适合脑子里已经有个具体形象、想完全掌控每一句。
- **上传材料**(import-upload) — 贴一段个人简介 / 日记 / 某个你喜欢的 persona 的聊天记录。导入流程会跑一次,抽出事件和想法,从里面起草长期记忆。

两条路径最后都汇到同一个 **review(审核)页面**。LLM 读完你给的材料,填好一份审核表单,交给你确认。表单分两块。

**生平事实 — 15 个字段,全部可选**

LLM 会尝试抽取结构化身份信息(姓名 / 性别 / 生日 / 国籍 / 时区 / 职业 / 感情状态 等)。错的直接改,不确定的留空。其中 5 个——姓名 / 性别 / 出生年 / 职业 / 母语——每个 turn 都会进系统 prompt 里的 "who you are" 区块。其余 10 个只给系统代码用(生日提醒 / 跨文化语气调节 / ...)。

**五段 core block 散文 — persona block 是最关键的那个**

| 字段 | 写什么 |
| --- | --- |
| **Display name**(显示名) | 你在聊天界面看到的那个名字。必填。 |
| **Persona block** | persona 是谁:性格特质 / 口吻 / 价值观 / 任何跨对话都要保持一致的东西。要想第一次体验就不错,这个块最重要。 |
| **Self block** | persona 怎么看待自己,第一人称写。可选——留空也行,后面会自己长出来。 |
| **User block** | persona 对你的认知,第三人称写。可选。 |
| **Relationship block** | persona 该认识的、你身边的人——家人 / 好友 / 宠物。可选。 |
| **Mood block** | persona 现在的心情。可选。 |

最精简的首次引导就是:一个显示名 · 两三句 persona block。其他全留空,daemon 会通过后台 consolidate pass 把它们从对话里慢慢长出来。

点 **完成**,会被路由到聊天视图。

### 引导表单填的内容去哪了

审核页提交时对应一次 admin API 调用。Block 文字写进长期记忆的 core-block 层;生平事实写成 persona 行上的结构化列。两份数据每个 turn 都会被重载入并拼进 LLM prompt · 所以你在这里写的任何东西下一条消息就能看出效果。之后你可以在 admin 面板里无需重启 daemon 就改它们。

---

## 第 5 步 · 发第一条消息

在聊天视图底部的输入框里打字,按发送。你会看到:

1. 你的消息以用户气泡形式出现在底部
2. persona 气泡出现 · 内容为空
3. LLM 的 token 流式到达 · 几个字符几个字符地填进去
4. 回复结束时 persona 气泡定稿 · daemon 把用户消息和 persona 回复一起写进长期记忆

后续消息基于同一条对话历史往下走。Daemon 把短期上下文放在一个 recent-window 缓冲里 · 通过检索从分层记忆里拉更丰富的上下文 · 每一个 turn 都跑一次。

### 记忆随时间成长

当你持续跟 persona 聊下去 · 一个后台 worker 会把结束掉的对话 session 合并(consolidate)成更高层的记忆:事件("我们聊了东京那趟")· 想法("用户好像很期待樱花季")· 还有 episodic state 的情绪更新。这个流程自己有节奏,不需要你插手 · 会话结束后一两分钟就开始出现在检索结果里。完整的六层记忆如何交互看 [`memory.md`](./memory.md)。

---

## 第 6 步 · 可选 · 启用语音

语音完全可选。只想要文字就跳过这节。

语音路径用 FishAudio 做 TTS · 用 OpenAI Whisper 做 STT。你可以自带音色(克隆),也可以从 FishAudio 的公开库里随便挑一个 voice ID。

**设置:**

1. 在 [https://fish.audio](https://fish.audio) 注册拿一个 API key
2. 用环境变量 export 出来:
    ```bash
    export FISHAUDIO_API_KEY=your_key_here
    ```
3. 编辑 `~/.echovessel/config.toml`,改 `[voice]` section:
    ```toml
    [voice]
    enabled = true
    tts_provider = "fishaudio"
    fishaudio_api_key_env = "FISHAUDIO_API_KEY"
    ```
4. 重启 daemon:run 终端里 `Ctrl-C` · 然后再 `uv run echovessel run`
5. 在浏览器 admin 面板里把 "Voice enabled" toggle 打开

从此 persona 回复会带一个音频播放器 · 回复文字会用你挑的音色说出来。Toggle 是运行时开关 · 你可以对话中途把语音关回去 · 不会丢记忆也不会丢历史。

provider 选项 · 声音克隆工作流 · 完整语音配置参考都在 [`voice.md`](./voice.md)。`[voice]` 的字段列表在 [`configuration.md`](./configuration.md)。

---

## 第 7 步 · 可选 · 启用 Discord DM channel

同一个 persona 可以同时在多个 channel 说话 · 共用一份长期记忆。你在 Web channel 开的对话 · 跑到 Discord 上可以无缝续下去 · persona 什么都不会忘。这是整套设计最吃重的那条铁律:一个 persona · 一份记忆 · 多个嘴。

**速成版:**

1. 在 [https://discord.com/developers/applications](https://discord.com/developers/applications) 建一个 application · 往里加一个 bot
2. 在 bot 的 Privileged Gateway Intents 里启用 Message Content Intent
3. 复制 bot token
4. 确认你装了 Discord extra(`uv sync --all-extras` 或 `uv sync --extra discord`)。
5. export token:
    ```bash
    export ECHOVESSEL_DISCORD_TOKEN=your_token_here
    ```
6. 编辑 `~/.echovessel/config.toml`:
    ```toml
    [channels.discord]
    enabled = true
    token_env = "ECHOVESSEL_DISCORD_TOKEN"
    ```
7. 重启 daemon
8. 从任何 Discord 客户端私聊那个 bot · persona 会在同一条 DM 串里回你

完整流程——邀请 bot 进服务器 / 对特定 Discord 用户做允许名单 / 排查 gateway 连接问题——都在 [`channels.md`](./channels.md) 的 *Discord DM channel setup* 一节里。

---

## 常见问题

**"Config file not found"。**
你多半跳过了第 2 步。跑 `uv run echovessel init` 生成 `~/.echovessel/config.toml`,再试 `uv run echovessel run`。或者显式传路径:`uv run echovessel run --config /path/to/config.toml`。

**Port 7777 already in use。**
别的进程占住了 Web 默认端口。要么干掉它,要么改 config 里 `[channels.web].port` 再重启 daemon。浏览器自动打开会读配置里的端口 · 不用再改别的。

**启动日志里 LLM 401 / 403。**
配置里指向的 API key 没设或设错了。确认 `[llm].api_key_env` 里那个环境变量**在启动 daemon 的那个 shell 里**确实被 export 出来了——一个终端里 export 不会漏到另一个终端。想快速验证其他都没问题,把 `[llm].provider` 换成 `"stub"`(见第 2 步)。

**浏览器没自动打开。**
有些环境(SSH 会话 · 最小化 Linux 装机 · CI)没注册默认浏览器。Daemon 会打一行 log 说明情况然后继续正常跑。你手动打开 `http://localhost:7777/` 就行。如果你在远程机器上,先隧道端口:`ssh -L 7777:127.0.0.1:7777 your-host`。

**首次启动很慢。**
装了 `[embeddings]` extra 的话,第一次跑 daemon 会下载 ~90 MB 的 sentence-transformers 模型。之后缓存在数据目录下,后续启动跳过下载。没装 `[embeddings]` extra 时 daemon 会用内置的零嵌入 · 首次启动接近秒开。

**persona 每次都回一模一样的固定文本。**
你在用 `stub` LLM provider(第 2 步的烟测路径)。把 `[llm].provider` 改回真实 provider(`openai_compat` 或 `anthropic`)再重启。

**我改了 `config.toml` 但没生效。**
一部分 section 支持 `SIGHUP` 热重载 · 其他的要完全重启。`[llm]` section 支持热重载 · `[channels.*]` · `[persona]` · `[memory]` 这些结构性 section 都要 `Ctrl-C` + 重新 `uv run echovessel run`。完整的重载矩阵在 [`configuration.md`](./configuration.md)。

---

## 接下来看什么

按你感兴趣的方向挑一个:

- [`memory.md`](./memory.md) · 六层记忆怎么工作 · 检索和 rerank 怎么喂进 prompt · consolidate worker 如何把原始对话变成长期知识
- [`voice.md`](./voice.md) · provider 选择 · 声音克隆流程 · 语音投递决策从 persona 状态如何流到 channel 层
- [`channels.md`](./channels.md) · 怎么新增一个 transport · debounce state machine · 一 persona 多嘴的铁律 · Discord DM 的完整流程
- [`proactive.md`](./proactive.md) · 如何让 persona 在严格 gate 下主动发第一条消息
- [`import.md`](./import.md) · 把已有文本(日记条目 · 聊天记录 · 笔记)批量导入 persona 记忆 · 不用跑实时对话
- [`configuration.md`](./configuration.md) · 每一个配置字段 · 默认值 · `SIGHUP` 热重载 vs 需要重启的矩阵
- [`contributing.md`](./contributing.md) · clone 仓库 · 跑测试 · 提交 PR

Runtime 内部机制——启动序列 / turn loop / 流式 / local-first disclosure 审计——在 [`runtime.md`](./runtime.md)。
