# Channels

## 概述

> 🧭 **命名说明。** EchoVessel 的 **channel**(代码和本文档里都用这个词)本质上是 API-infra 术语里的 **有状态 message gateway**。每条 channel 负责在一条外部传输的协议(Discord WebSocket、Web UI 的 HTTP POST + SSE、未来 iMessage 的 webhook 等)和 daemon 内部统一的 `IncomingTurn` / `OutgoingMessage` 格式之间做双向翻译,同时持有翻译所需的连接级状态(debounce 计时器、in-flight turn 追踪、per-user DM 路由)。"Channel"是偏 chat UX 味道的叫法——我们保留它,是因为终端用户在 Discord / Slack / iMessage 里看到的就是"频道"这个概念。如果你更习惯用 "gateway" 这个思维模型,两者在本模块里指同一样东西;代码里会继续用 "channel"。

**Channel 是"哑"的 I/O 适配器。** 一个 channel 独占一条外部传输——Web UI、Discord、iMessage、WeChat——它的唯一职责是把文字在那条传输和 daemon 其余部分之间来回搬运。Channel 不调 LLM、不读记忆、不决定 persona 说什么、也不保存 persona 状态。所有"思考"发生在上一层：`runtime` + `memory` + prompt 组装器。如果一个 channel 实现开始想缓存 mood block 或跑一次 retrieval 查询,这个设计就错了。

**一个 persona,多张嘴。** Channel 系统根部的架构承诺是:不管 persona 在多少条传输上说话,它都是一个连续的身份。同一个用户早上在 Web UI 上和 persona 聊天、晚上切到 Discord 接着聊,persona 会原封不动地记得早上的对话——不是因为每条 channel 各自带一份记忆,而是因为记忆只有一份存储,而且**永远不按传输分片**。记忆检索不接收任何 `channel_id` 过滤参数,将来也不会接收。这是整个模块最吃重的一条铁律:一旦被打破,"同一个 persona"的幻觉立刻崩塌,每条 channel 就会退化成互相独立的 bot。

**Debounce 问题属于这一层。** 真实的人打字是 burst 式的:四秒钟内连发三行、停一下、再多打一行。如果 channel 把每一行都当作一个独立 turn 送给 runtime,persona 就会在用户还没打完的时候插话。解法是**在 channel 层**把一串 burst 合并成一个 `IncomingTurn`,而不是在 runtime 层做——只有 channel 自己知道传输层的原生节奏(Discord 的 typing indicator、iMessage 的 read-receipt 节拍、Web UI 的 input 事件),也只有它持有稳定的 per-user 计时器。Runtime 保持简单:一次消费一个 turn。所有"再等等看用户是不是还在打字"的逻辑都住在每条 channel 内部,藏在一个小的状态机里,见"架构"一节。

---

## 核心概念

**`Channel` Protocol** —— 每一条传输都要实现的 Python `Protocol`。它定义了 runtime 会调的那组小接口:`start()`、`stop()`、`incoming()`、`send()`、`on_turn_done()`,再加一个身份属性 `channel_id`。一个类只要满足这个形状就是 channel;没有基类、没有插件注册表、没有装饰器。权威定义住在 `src/echovessel/channels/base.py`。

**`IncomingTurn`** —— 一组已经 debounce 过的 `IncomingMessage`,共享一个 `turn_id`,代表"用户一口气说出来的一串话"。Channel 从它的 `incoming()` 异步迭代器里 emit `IncomingTurn`;runtime 把每一个 yield 出来的 turn 当作恰好一次 LLM 调用单元。哪怕只有一行消息也会被包成长度为 1 的 `IncomingTurn`——不存在第二条代码路径来处理退化情况。

**`IncomingMessage`** —— 一个 turn 里面的单条原始用户消息。携带 `channel_id`、`user_id`、`content`、`received_at`、一个可选的传输层原生 `external_ref`,以及一个回指外层 `IncomingTurn` 的 `turn_id`。Memory 把这些当作 L2 recall 日志的叶子单元逐条持久化。

**`OutgoingMessage`** —— runtime 交给 `channel.send()` 的东西。只装一条哑 I/O 适配器所需要的东西:`content`、可选的 `in_reply_to_turn_id`、一个区分普通 `"reply"` 和自主 `"proactive"` 推送的 `kind`,以及一个 `delivery` 字段(当前代码库里是 `"text"` 或 `"voice_neutral"`),告诉 channel 应该怎么把消息物理递送出去。Persona 状态、mood、retrieval 结果都**不在**这里——它们早就被消费掉,变成了 `content`。

**`in_flight_turn_id`** —— channel 侧的状态。它保存着"runtime 当前正在处理的那个 turn_id",或者 `None` 表示 runtime 空闲。这一个字段就是 channel 的 debounce 状态机和 runtime 的 turn 循环之间**唯一的**协作协议。为 `None` 时,新来的用户输入进 current_turn 并跑 debounce 计时器;不为 `None` 时,新来的用户输入进 next_turn 并等待。Runtime 从另一端调 `on_turn_done(turn_id)` 来把它清掉。

**`current_turn` / `next_turn`** —— 组成 debounce 状态机的两个缓冲区。`current_turn` 是 channel 正在为**下一次** flush 累积的 burst;`next_turn` 是当 runtime 还在处理前一个 turn 时新来的消息的缓冲区。双缓冲方案既保证 LLM 调用期间用户消息不会被丢弃,又保持 runtime 依赖的"一次只处理一个 turn"的约束。

**`on_turn_done(turn_id)`** —— runtime 到 channel 的回调,在 LLM 处理完一个 turn 之后触发。它告诉 channel:`in_flight_turn_id` 可以清掉了,可以把 `next_turn` 缓冲区提拔上来了。这个回调最重要的一条规则:如果 `next_turn` 非空,channel **不能**立刻 flush——它必须把 next_turn 挪进 current_turn,然后**启动一次正常的 debounce 计时器**。为什么,见"架构"一节。

---

## 架构

### Channel 在栈里的位置

EchoVessel 是五层模块,import 方向严格单向。Channels 住在第三层,在 memory/voice/core 之上、在 runtime 之下:

```
   ┌─────────────────────────────────────────────┐
   │                   runtime                   │   第 4 层
   │   daemon、turn dispatch、LLM、observers      │
   └─────────────────┬───────────────────────────┘
                     │ import
          ┌──────────┴──────────┐
          ▼                     ▼
   ┌────────────┐        ┌──────────────┐        第 3 层
   │  channels  │        │  proactive   │
   │  I/O、debounce       │  policy、触发
   └─────┬──────┘        └──────┬───────┘
         │ import               │ import
         └──────────┬───────────┘
                    ▼
          ┌──────────────────┐
          │      memory      │                   第 2 层
          │   L1 L2 L3 L4    │
          └────────┬─────────┘
                   │
          ┌────────┴─────────┐
          │       voice      │                   第 2 层
          │   TTS / STT      │
          └────────┬─────────┘
                   ▼
          ┌──────────────────┐
          │       core       │                   第 1 层
          │  types、enums    │
          └──────────────────┘
```

一个 channel **可以** import `echovessel.core`(类型)、`echovessel.memory`(历史显示所需的只读查询 API,再加通过 runtime 走的 `ingest_message`)、以及 `echovessel.voice`(用来物化语音回复)。一个 channel **不可以** import `echovessel.runtime`——依赖方向是反的,runtime 去 import channels。一个 channel 也不可以 import 另一个 channel:web channel 不 import Discord channel,每条 channel 都独立站在共享的 Protocol 后面。分层由 CI 的 `import-linter` 强制。

### 两条铁律—— channel 层为它们而存在

整套设计的根上坐着两条铁律。两条都用"channel 不能做什么"来表述,因为 channel 层恰好是这种冲动最强烈的边界。

**Memory 检索永远不接受一个 channel 过滤参数。** Memory 模块的 `retrieve()` 函数、core-block 加载器、以及 recall-message 查询,全都只接收 persona 和 user,永远不接收任何传输标识。不存在 `retrieve(..., channel_id="web")` 的重载,将来也不会有。如果你在写一条 channel 并且发现自己想"就把这条 channel 的历史秀给 persona 看",停下:persona 只有一份历史,整个架构的重点就是它**不知道**某条记忆是从哪条传输里来的。`channel_id` 唯一能到达的地方是 L2 recall-message 行上作为 `via-` 标签,用来给 UI 渲染——不是用来检索的。

**LLM prompt 里永远不出现传输身份 token。** System prompt、user turn、retrieval 出来的上下文块——全都不能包含 `"web"`、`"discord"`、`"imessage"`、`"wechat"` 或任何其他传输名字。LLM 根本不知道消息是从哪里来的。Runtime 的 prompt 组装器里硬编码了一段 Style 指令,告诉模型要谈话题和情绪,永远不要提介质,哪怕用户自己拿介质开玩笑。Channel 被禁止在往上传的 envelope 里塞任何"channel 上下文"字段,因为这样的字段迟早会漏进 prompt。

这两条铁律合在一起就是 channel 层作为独立模块存在的全部理由。没有它们,channel 就只是一个个各自调 LLM 的薄壳,persona 会分裂成 per-传输的克隆。

### Debounce 状态机

一条 channel 有四个状态,被三种事件驱动:新来的用户消息、debounce 计时器到期、runtime 发来的 `on_turn_done`。

```
          ┌──────────────────────────────────────┐
          │                                      │
          │            ┌──────────┐              │
          │            │   idle   │◀─────────────┘
          │            └────┬─────┘   on_turn_done
          │                 │          (next_turn 为空)
          │  新消息,        │
          │  in_flight=None │
          │                 ▼
          │         ┌──────────────┐
          │         │  collecting  │◀──┐
          │         │ (计时器在跑) │   │ 新消息(重置计时器)
          │         └──────┬───────┘   │
          │                │           │
          │   计时器        │           │
          │   到期          │           │
          │                ▼           │
          │         ┌──────────────┐   │
          │         │   in_flight  │   │
          │         │  turn 已派发 │   │
          │         └──────┬───────┘   │
          │                │           │
          │  新消息         │           │
          │  (LLM 期间)    │           │
          │                ▼           │
          │         ┌──────────────┐   │
          │         │ queued_next  │   │
          │         │   已缓冲     │   │
          │         └──────┬───────┘   │
          │                │           │
          │  on_turn_done  │           │
          │  (提拔并       │           │
          │   起计时器)    │           │
          │                ▼           │
          │         ┌──────────────┐   │
          │         │  collecting  │───┘
          │         └──────────────┘
          │
          └──── stop() ─────────────────▶  stopped
```

用规则写出来:

1. **收到新的用户消息。**
   - 如果 `in_flight_turn_id is None`——runtime 空闲。把消息 append 到 `current_turn`,取消正在跑的 debounce 计时器(如果有),重启一个新的(默认 2000 ms,每条 channel 独立,从 config 读)。
   - 如果 `in_flight_turn_id is not None`——runtime 忙着。把消息 append 到 `next_turn`。**不要为 `next_turn` 启动计时器。** 计时器要等 `on_turn_done` 触发之后才启动。

2. **debounce 计时器到期。** 生成一个新 `turn_id`,把累积的 `current_turn` 封装成一个 `IncomingTurn`,推到 `incoming()` yield 的外发队列上,设 `in_flight_turn_id = <新 turn_id>`,清空 `current_turn` 缓冲区。Channel 在这里**不**调 memory 也**不**调 LLM;那是下游 runtime 从 `incoming()` 拉到这个 turn 以后的事。

3. **runtime 调 `on_turn_done(turn_id)`。** 清 `in_flight_turn_id = None`。然后:
   - 如果 `next_turn` 为空,什么也不做。Channel 现在空闲;下一条用户消息重新进入规则 1。
   - 如果 `next_turn` 非空,提拔它:把它的内容挪进 `current_turn`,清空 `next_turn`,然后**启动一次正常的 debounce 计时器**。之后的流程回到规则 1:如果用户继续打字,计时器被重置;如果用户停下来,计时器到期,规则 2 跑起来。

最容易写错的一个细节是:当 `on_turn_done` 把 `next_turn` 提拔为 `current_turn` 时,channel **不能**立刻 flush。它必须启动一次正常的 debounce 窗口。原因是:在前一次 LLM 调用期间连发三条的用户非常可能还在打字。立刻 flush 被提拔的缓冲区会让人感觉"她又插话了"。跑完整个 debounce 窗口最坏情况下多出一个 debounce 间隔的尾延迟——这是用户自己产生的节奏延迟,可以接受。实现必须保持这个行为。

两条硬上限会提前打断 debounce:`MAX_MESSAGES_PER_TURN = 50` 和 `MAX_CHARS_PER_TURN = 20000`。任一上限被触发,channel 立即 flush,不等计时器。这两条存在是为了保护 LLM 上下文窗口,也为了防止失控的生产者把单个 turn 撑爆。

Debounce 状态只被一个协程(channel 内部的 ingest 循环)修改。Runtime 来的 `on_turn_done` 回调跑在另一个协程里,所以 channel 实现要负责把它 marshal 到 ingest 循环上——通常做法是往一个内部 `asyncio.Queue` 上塞一个事件,让 ingest 循环去消费。Protocol 不规定机制,只规定"串行修改"这一不变量。

### 端到端数据流

一次 turn 的端到端流程,从用户在 Web UI 里敲字到 persona 回复:

```
1. 用户在 Web UI 里打字
        │
        ▼
2. channel.incoming_raw_message(...)  (channel 内部)
        │   append 到 current_turn
        │   重置 debounce 计时器
        ▼
3. debounce 计时器到期
        │   生成 turn_id
        │   把 IncomingTurn 推到内部队列
        │   设 in_flight_turn_id
        ▼
4. channel.incoming() yield IncomingTurn
        │
        ▼
5. TurnDispatcher 把 turn 放到它的串行队列上
        │
        ▼
6. runtime._handle_turn(turn)
        │   调 assemble_turn(ctx, turn, llm_provider)
        │     - 对每条叶子消息调 ingest_message(...)(写 L2)
        │     - retrieve(...)    (没有 channel_id 参数)
        │     - 组装 prompt      (文本里没有传输名)
        │     - llm.complete(...)  (流式 token)
        │     - 对回复再调 ingest_message(role=ASSISTANT, ...)
        ▼
7. runtime 调 channel.send(OutgoingMessage(
        content=reply_text,
        in_reply_to_turn_id=turn.turn_id,
        kind="reply",
        delivery="text" | "voice_neutral",
     ))
        │
        ▼
8. runtime 调 channel.on_turn_done(turn.turn_id)
        │   channel 清 in_flight_turn_id
        │   如果 next_turn 非空:
        │       提拔为 current_turn 并启动 debounce 计时器
        ▼
9. 回到第 1 步
```

Runtime 的 `ChannelRegistry` 管理生命周期(`start_all` / `stop_all`),并通过 `registry.all_incoming()` 把每条已注册 channel 的 `incoming()` 合并成一条异步流。`TurnDispatcher` 从这条合并流里读,并喂给一个**单线**的串行 handler——所以哪怕同时活着四条 channel,也永远只有一个 turn 在处理。这是故意的:这是让 memory 写入和 LLM 调用可以放心假设"没有并发 mutation"的那条保障。

### Web channel

Web channel 已完整端到端接通。`src/echovessel/channels/web/` 下的 FastAPI `WebChannel` 暴露 `POST /api/chat/send`、通过 `GET /api/chat/events` 用 SSE 推 `chat.message.*` 事件、实现上面描述的 debounce 状态机、并通过 Channel Protocol 和 runtime 对话。`src/echovessel/channels/web/frontend/` 下的 React + Vite + TypeScript 前端通过 `npm run build` 编译进 `channels/web/static/`,daemon 在 `http://127.0.0.1:7777/` 直接托管——终端用户不需要 Node.js。想 hack UI 的贡献者可以 `npm run dev` 连着一个活 daemon(dev server 代理到 FastAPI 进程)。

### 跨 channel 统一 timeline(runtime 级 SSE broadcaster)

EchoVessel 的架构承诺是"一个 persona 跨所有 channel"。Memory 层一直在存储层保证这一点(iron rule D4:retrieval 永不按 `channel_id` 过滤)。从 2026-04-16 起 **live view** 也统一了:

- `SSEBroadcaster` 由 runtime 持有,不属于任何单个 channel。
- 每个 channel 的 turn 事件(`chat.message.user_appended` / `chat.message.typing_started` / `chat.message.done` / `chat.message.voice_ready`)都经 broadcaster mirror · 带 `source_channel_id` 字段标记 turn 来源。`chat.message.typing_started` 在每次 turn LLM stream 开始前推一次——浏览器在收到 `chat.message.done` 前渲染一个"正在输入..."气泡。
- Web chat timeline 订阅统一 SSE 流 · Discord DM 实时出现在 Web 页面 · 时间戳旁带 `📱 Discord` 角标。iMessage 和未来 channel 继承这套行为 —— **无需前端修改**,只要 channel 实现 Channel Protocol 且 turn 走 `runtime.assemble_turn()`。
- 配对的 `GET /api/chat/history?limit=50&before=<turn_id>` 端点返回跨 channel 历史(仍然 D4-不过滤)· 新浏览器 session 会把过去 Discord 对话和 Web 对话一起回填。
- Turn 串行化仍然成立:`TurnDispatcher` 在整个进程里一次只处理一个 turn · "一个 persona 一个脑子" 不变。并发 Web + Discord 请求只是排队。

publish 路径有失败隔离:如果 broadcaster 抛异常 · 只打 `log.warning`,原 channel 的 `send()` 照常完成。反过来,如果 `send()` 抛异常,`chat.message.done` **不会** mirror —— 一个失败的 Discord DM 不会告诉 Web tab turn 成功了。

---

## 如何扩展

### 1. 写一条最小的 channel 适配器

一条 channel 就是任何满足 `Channel` Protocol 的类。没有基类可继承,也没有插件注册表要调。骨架长这样:

```python
# src/echovessel/channels/myxform/channel.py
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from echovessel.runtime.interaction import IncomingMessage, IncomingTurn


class MyTransformChannel:
    # --- 身份 -----------------------------------------------------------
    channel_id: str = "myxform"
    display_name: str = "My Transform"

    def __init__(self, *, config: dict) -> None:
        self._config = config
        self._outbox: asyncio.Queue[IncomingTurn] = asyncio.Queue()
        self._stopped = asyncio.Event()
        # Debounce 状态 —— 见示例 2。
        self._current_turn: list[IncomingMessage] = []
        self._next_turn: list[IncomingMessage] = []
        self._timer: asyncio.Task | None = None
        self._in_flight_turn_id: str | None = None
        self._debounce_ms: int = config.get("turn_debounce_ms", 2000)

    # --- 生命周期 -------------------------------------------------------
    async def start(self) -> None:
        # 打开 socket、bind 端口、连接外部服务。
        # 幂等:对一条已经 READY 的 channel 再调 start() 是 no-op。
        pass

    async def stop(self) -> None:
        # 优雅关停:flush 外发缓冲、关 socket。
        # stop() 返回之后,incoming() 必须在下一次 pull 时耗尽。
        self._stopped.set()
        if self._timer is not None:
            self._timer.cancel()
        await self._outbox.put(None)  # sentinel

    # --- 入站 -----------------------------------------------------------
    async def incoming(self) -> AsyncIterator[IncomingTurn]:
        while not self._stopped.is_set():
            item = await self._outbox.get()
            if item is None:
                return
            yield item

    async def on_turn_done(self, turn_id: str) -> None:
        # Runtime 处理完 turn。幂等;绝不 raise。
        self._in_flight_turn_id = None
        if self._next_turn:
            self._current_turn = self._next_turn
            self._next_turn = []
            self._start_debounce_timer()

    # --- 出站 -----------------------------------------------------------
    async def send(self, message) -> None:
        # 递送 persona 回复。message.delivery 告诉你是直接推文本
        # 还是先调 VoiceService。语音路径见示例 3。
        if message.delivery == "text":
            await self._push_text(message.content)
        elif message.delivery == "voice_neutral":
            await self._push_voice(message.content)

    # --- 外部 user id 映射 ----------------------------------------------
    def map_external_user(self, external_id: str) -> str:
        # MVP:单用户契约。每条 channel 都返回 "self",直到
        # 多用户支持在 runtime/config 里落地。
        return "self"
```

更完整的示例可以看 `tests/` 下的 channel stub——它们用一个假的传输层把 Protocol 端到端打通,是开新适配器时最容易 copy-paste 的起点。Runtime 用到的合并点在 `src/echovessel/runtime/channel_registry.py`;只要你的类暴露 `channel_id`、`start`、`stop`、`incoming`、`send`,以及(可选)`on_turn_done`,registry 就会接受它。

### 2. 实现 debounce 状态机

两个缓冲区、一个计时器、一个 `in_flight_turn_id` 标志,以及 `on_turn_done` 上的"提拔后走正常 debounce"规则。核心住在 channel 类的两个方法里:

```python
import uuid


class MyTransformChannel:
    # ... 生命周期方法省略 ...

    async def _on_raw_user_message(self, content: str, *, user_id: str) -> None:
        """由 channel 内部传输 ingest 循环为外部服务交来的每一条原始
        用户消息调用。这是**唯一**会修改 debounce 状态的地方。"""

        msg = IncomingMessage(
            channel_id=self.channel_id,
            user_id=user_id,
            content=content,
            received_at=datetime.now(timezone.utc),
        )

        if self._in_flight_turn_id is None:
            # Runtime 空闲:append 到 current_turn 并(重)启动计时器。
            self._current_turn.append(msg)
            self._start_debounce_timer()
        else:
            # Runtime 忙着:缓冲到 next_turn,不启动计时器。
            self._next_turn.append(msg)

        # 硬上限:如果当前 burst 超出任一上限,立即 flush。
        # 只在我们刚刚 grow 的缓冲区属于 current_turn 时跑。
        if self._in_flight_turn_id is None:
            if (
                len(self._current_turn) >= 50
                or sum(len(m.content) for m in self._current_turn) >= 20000
            ):
                await self._flush_current_turn()

    def _start_debounce_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = asyncio.create_task(self._debounce_and_flush())

    async def _debounce_and_flush(self) -> None:
        try:
            await asyncio.sleep(self._debounce_ms / 1000.0)
        except asyncio.CancelledError:
            return
        await self._flush_current_turn()

    async def _flush_current_turn(self) -> None:
        if not self._current_turn:
            return
        turn_id = str(uuid.uuid4())
        # 给每条叶子消息回填 turn_id。
        stamped = [
            IncomingMessage(
                channel_id=m.channel_id,
                user_id=m.user_id,
                content=m.content,
                received_at=m.received_at,
                external_ref=m.external_ref,
                turn_id=turn_id,
            )
            for m in self._current_turn
        ]
        turn = IncomingTurn(
            turn_id=turn_id,
            channel_id=self.channel_id,
            user_id=stamped[0].user_id,
            messages=stamped,
            received_at=datetime.now(timezone.utc),
        )
        self._current_turn = []
        self._in_flight_turn_id = turn_id
        await self._outbox.put(turn)
```

移植到真实传输时要盯两件事:

- **`on_turn_done` 的提拔必须走正常 debounce 窗口,永远不立即 flush。** 生命周期一节里的提拔路径必须调 `_start_debounce_timer()`。因为"下一个缓冲区已经满了"就跳过计时器,会把这整个模块存在的理由——"插话 bug"——重新带回来。
- **串行修改。** 只允许一个协程碰 `_current_turn`、`_next_turn`、`_timer`、`_in_flight_turn_id`。如果你的传输回调跑在另一个 loop 或线程里,用 `asyncio.Queue` 或 `loop.call_soon_threadsafe` 把它 marshal 到 ingest 循环上,然后在 ingest 循环里做真正的状态更新。

### 3. 暴露一个 `push_sse` 能力

有些传输——Web channel 是最明显的例子——天然就说 server-sent events。Runtime 的 observer 层想把生命周期事件(记忆写入、consolidation tick、语音生成进度)推送给任何能把它们转发给活 UI 的 channel,但不想把这个能力强加给每一条 channel。Discord 没有 SSE;iMessage 没有 SSE。

Pattern 是一个可选方法,用 `getattr` 检测:

```python
from typing import Any


class MyTransformChannel:
    # ... Protocol 方法省略 ...

    async def push_sse(self, event: str, payload: dict[str, Any]) -> None:
        """可选能力。能把事件流到活 UI 的 channel 实现它;
        不能的 channel 干脆不定义。Runtime 的 observer 代码这样
        检测支持:

            push = getattr(channel, "push_sse", None)
            if push is not None:
                await push(event_name, payload)
        """
        # 向每一个连着的 SSE 订阅者 fan-out。
        for subscriber in list(self._sse_subscribers):
            try:
                await subscriber.send(event, payload)
            except Exception:
                self._sse_subscribers.discard(subscriber)
```

Runtime 的 observer 接线(在 `src/echovessel/runtime/` 里)用 `getattr` 检测 `push_sse`,对不定义它的 channel 静默跳过。这样既让 Channel Protocol 保持最小,又让 Web channel 能点亮一个活色生香的实时 UI。**不要**把 `push_sse` 加进核心 Protocol——可选性本身就是重点。

---

Channel Protocol 的权威源头看 `src/echovessel/channels/base.py`。Registry 和 dispatch 管道看 `src/echovessel/runtime/channel_registry.py` 和 `src/echovessel/runtime/turn_dispatcher.py`。Turn pipeline 本身——memory 检索和 LLM 组装在那里强制执行"不泄漏传输"的规则——住在 `src/echovessel/runtime/interaction.py`。

---

## Discord 私信 channel 配置

EchoVessel 可选内置一条 Discord channel,让 persona 接收来自允许名单上的 Discord 用户的私信(DM)。同一个 persona、同一份记忆、同一种心情——和 Web 的唯一区别只是"传输路径"不同。一个 persona 在 Discord 上被私信时仍然记得 Web UI 上开始的对话,反过来也一样,因为 memory 检索从不按 channel 过滤。

当前 scope **只有私信**。服务器内频道、slash commands、语音消息附件都不在 v1 范围内。

### 1. 创建一个 Discord 机器人应用

1. 去 [Discord Developer Portal](https://discord.com/developers/applications),点 **New Application**。随便取个名字——显示名会出现在 DM 里。
2. 左侧栏打开 **Bot**,点 **Add Bot**,接受警告。
3. 向下滚到 **Privileged Gateway Intents**,打开 **Message Content Intent**。不开的话你的 bot 收到的 DM 正文是空的。
4. 仍在 Bot 页面上,点 **Reset Token**(如果已经签发了就点 **Copy**),把字符串保存好——Discord 只会显示一次。

### 2. 安装可选依赖

Discord channel 是一个可选 extra,只用 Web UI 的用户不需要装 `discord.py`:

```bash
uv sync --extra discord
```

### 3. 用环境变量暴露 token

EchoVessel 永远**不**把 token 写进 `config.toml`。用环境变量 export,然后 config 里只引用变量名:

```bash
export ECHOVESSEL_DISCORD_TOKEN='your-bot-token-here'
```

把同一行加进你的 shell 配置(`~/.zshrc`、`~/.bashrc` 或类似),这样重启后还在。

### 4. 在 `config.toml` 里启用 channel

打开你的 `config.toml`(通常在 `~/.echovessel/config.toml`),加上:

```toml
[channels.discord]
enabled = true
channel_id = "discord"
token_env = "ECHOVESSEL_DISCORD_TOKEN"
# 可选但强烈推荐:只允许指定 Discord 用户 ID 给 bot 发私信。
# 省略此项或留空列表 = 任何 bot 能被找到的 Discord 用户都能发。
allowed_user_ids = [123456789012345678]
# 合并窗口毫秒数。和 Web channel 默认保持一致。
debounce_ms = 2000
```

`allowed_user_ids` 接受 Discord 用户 **snowflake**——在 Developer Mode 开启后右键用户看到的 17–19 位数字 ID。不设这个白名单,任何能找到你 bot 的 Discord 用户都能发私信。

### 5. 把 bot 拉到一个你们都在的地方

Discord 规则:只有和 bot **共享至少一个服务器** 的用户才能私信它。在 Developer Portal 做一个 OAuth2 邀请链接(Scopes: `bot`,Permissions: 只要 **Read Messages / Send Messages** 就够了),打开它把 bot 加进一个你已经在的服务器。服务器内具体频道的权限不用改——bot 只要"在那儿"就行。

### 6. 启动 EchoVessel,发一条私信

正常启动 daemon。启动成功后应该能看到日志 `Discord bot connected as YourBot#1234`。在服务器成员列表里点 bot,发一条私信过去——persona 会在同一个 DM 线程上回复,带着和 Web UI 完全一样的记忆和心情。

### 故障排查

- **"Discord bot rejected DM from non-allowlisted user"** —— 你的用户 ID 不在 `allowed_user_ids` 里。开 Developer Mode 后右键自己 → Copy User ID,加进列表。
- **DM 正文是空的** —— Bot 页面的 Message Content Intent 没开。去 Developer Portal 打开它,重启 daemon。
- **Bot 上线但从不回** —— 确认你和 bot 共享至少一个服务器。没被邀请过的 bot 收不到 DM。
- **启动时 "Improper token"** —— `ECHOVESSEL_DISCORD_TOKEN` 没设,或者复制时带了前后空格。健康的 token 大约 70 个字符。
