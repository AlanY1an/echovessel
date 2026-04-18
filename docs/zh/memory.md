# 记忆（Memory）

> 分层的 persona 记忆。L1 core block 永远进 prompt，L2 原始消息作为 ground truth，L3 抽取出的事件，L4 蒸馏出的想法。一个 persona 在它说话的每一个 channel 上都是同一份持续身份。

记忆是 EchoVessel 的核心资产。其他所有东西——runtime、channels、voice、proactive——存在的理由都是喂养它、查询它、或把它记住的内容呈现出来。一个数字 persona 的持久性取决于它背后的记忆，而这个模块就是那份持久性住的地方。

---

## Overview

记忆给一个 persona 一份稳定的自我感，以及对它所接触之人的稳定印象。它之所以分层，是因为这四层分别回答四个不同的问题："此刻我是谁"（L1）、"对方究竟说了什么原话"（L2）、"那次对话里发生了什么"（L3）、以及"跨很多次对话之后，我对这个人持有什么看法"（L4）。每一层都有自己的写入路径、自己的检索角色、自己的遗忘语义。把它们塌缩成一个统一 store 要么把 prompt 撑爆，要么丧失做 reflection 的能力。

模块的设计刻意和其他层解耦。它不知道 channel 是什么，不知道 LLM provider 是什么，不知道 runtime 是怎么 stream 回复的。上层把 embedding 函数、extraction 函数、reflection 函数当作纯 callable 注入进来；记忆负责存储、打分和生命周期。这条纪律由 `pyproject.toml` 里的分层契约强制执行——memory 只能依赖 `echovessel.core`，别无其他。

系统其他部分在记忆之上给出的承诺很简单：一个 persona 就是一份持续身份。当用户下午在 Web 上跟它聊、晚上又在 Discord 上跟它聊时，记忆是同一份池子。检索从不按消息是从哪个 channel 来的来过滤。这一条规矩塑造了下面大多数的设计决策。

---

## Core Concepts

**L1 core blocks** — 短小、稳定的文本段，无条件注入每次 prompt。`core_blocks` 表里住着五个 label：`persona`、`self`、`mood`、`user`、`relationship`。前三个跨用户共享（persona 是一个角色，它的自我形象和情绪不会因用户不同而 fork）。后两个按用户分份，key 是 `(persona_id, user_id)`。每个 block 上限 5000 字符，并且在 `core_block_appends` 里有一份 append-only 审计日志。

**生平事实**(Biographic facts)— 跟 core blocks 并排住在 `personas` 行本身的十五个结构化身份列(`full_name`、`gender`、`birth_date`、`nationality`、`native_language`、`timezone`、`occupation`、`relationship_status`、…)。这些字段之所以跟散文 block 分开放,是因为代码想知道"她是什么时区"或"她是哪年生的"时可以直接查列,不用重新解析 persona block 里那段散文。十五个全部可空 — onboarding 时 LLM 抽取能填什么就填什么,用户在 review 页面校对剩下的。其中五个(姓名 / 性别 / 出生年 / 职业 / 母语)会在每个 turn 渲进系统 prompt 的 "# Who you are" 段;另外十个只供系统代码使用。

**L2 raw messages** — 每一条用户消息和 persona 回复都原样写进 `recall_messages`。这是档案级的 ground truth。表里每行带一个 `channel_id`，这样前端可以渲染"via Web"或"via Discord"的小标签，但进 prompt 的查询从不在它上面过滤。L2 用 FTS5 建了索引作为关键字兜底，但**不参与**主检索 pipeline；它是所有其他路径失败时系统能永远回落的那一层。

**L3 events** — 从一个关闭的 session 里抽取出的事实。以 `type='event'` 的 `ConceptNode` 行存储：一段自然语言描述、一个 `-10..+10` 的 `emotional_impact`、emotion 和 relational 标签、一份存在 sqlite-vec 伴随表里的 embedding，以及一个指回 `source_session_id` 的溯源指针。Events 是 episodic 记忆的主要单位——"那次用户告诉我 Mochi 做了手术的对话"。

**L4 thoughts** — 从很多 events 里蒸馏出来的更长程的观察。和 L3 同一张表，用 `type='thought'` 区分。每条 thought 带一条 `filling` 证据链（通过 `concept_node_filling`），记录它是从哪些 events 生成的，这样当用户删掉源头 events 时可以选择把 thought 保留为孤儿。Thoughts 是由 reflection pass 写入的，不是靠单个 session 的 extraction 产生。

**Consolidate** — session 关闭时跑的 pipeline。它把 session 的 L2 消息一次性读进来，调用注入进来的 extraction 函数生成零条或多条 L3 events，给每条 event 算 embedding，按需触发一次 reflection pass 产生 L4 thoughts，然后把 session 标记为 `CLOSED`。入口是 `src/echovessel/memory/consolidate.py` 里的 `consolidate_session`。

**Retrieve** — persona 说话前跑的 pipeline。它把所有 L1 core block 加载进来，让 storage backend 在 `concept_nodes` 上跑一次向量检索，用四项因子对候选做 rerank，用一个最低 relevance floor 抑制正交匹配，再按需给每个命中扩展附近的 L2 消息。如果向量索引返回的命中数不够，L2 上的 FTS fallback 会补刀。入口是 `src/echovessel/memory/retrieve.py` 里的 `retrieve`。

**Observer** — 一份住在 `src/echovessel/memory/observers.py` 的 Protocol，上层实现它之后就能对记忆写入做出反应。记忆从不 import runtime 或 channels；相反，runtime 在启动时注册一个 `MemoryEventObserver`，记忆在每次成功 commit 之后往里面触发 hook。observer 抛出的异常被捕获并 log，绝不会回滚到记忆写入里。

**幂等迁移** — 模块升级已有的 `memory.db` 时不用 Alembic。`ensure_schema_up_to_date` 会检查 `sqlite_master` 和 `PRAGMA table_info`，只有目标状态缺失时才执行 `ADD COLUMN` / `CREATE TABLE IF NOT EXISTS`。在全新数据库上跑是 no-op；在旧数据库上跑一次就能把它带到当前形状。

---

## Architecture

记忆坐落在五模块栈的下层。Runtime 负责编排。Channels 和 Proactive 住在 memory 和 voice 之上。Memory 和 Voice 直接坐在共享的 `echovessel.core` 类型之上。memory 里没有任何代码 import 更上层，`pyproject.toml` 的 import-linter 契约强制这一点。

```
runtime
   |
   +-- channels    proactive
   |      \        /
   |       +------+
   |       |
   +----> memory      voice
              \       /
               core
```

这个模块里跑着两条数据路径。

### 写路径

```
channel / runtime
      |
      v
ingest_message(persona, user, channel, role, content, turn_id)
      |
      v
get_or_create_open_session()  --+  (可能入队 "new session started")
      |                         |
      v                         |
write RecallMessage to L2       |
      |                         |
      v                         |
update session counters         |
      |                         |
      v                         |
check_length_trigger            |
      |                         |
      v                         |
db.commit()                     |
      |                         |
      v                         |
drain_and_fire_pending_lifecycle_events()  <--+
      |
      v
observer.on_message_ingested(msg)   (per-call hook)
```

每次写入都是先 commit、再触发任何 hook。`sessions.py` 里的生命周期队列把 "new session" / "session closed" 事件做了批处理，这样一次 commit 可以在一次 drain 里 dispatch 多个 hook。Per-write hook 走 `ingest_message`、`bulk_create_events`、`append_to_core_block` 上的显式 `observer=` 参数；生命周期 hook 走模块级别的 `_observers` 注册表——这个注册表由 `register_observer(...)` 调一次即可。

当一个 session 跨过 `SESSION_MAX_MESSAGES` 或 `SESSION_MAX_TOKENS` 时，它被标记为正在关闭，下一次 `ingest_message` 调用会在同一 channel 里打开一个新的 session。用户对此毫无感知——这个切分只是一个内部的 extraction 边界。Idle session（超过 30 分钟没新消息）和来自 runtime 的生命周期信号（daemon 关闭、persona 切换）关闭 session 的方式是一样的。

Session 关闭会流入 `consolidate_session`，跑一次 extraction pass、可能一次 reflection pass，然后把 `session.status` 翻成 `CLOSED` 再触发 `on_session_closed`。无论 session 有多少轮对话，extraction 对注入进来的 LLM 只调用一次；用户一段 burst 可能产生多条 L2 行，但只会产生一次 extraction 调用。

### 读路径

```
runtime 问："memory 对 <query> 怎么说？"
      |
      v
retrieve(db, backend, persona, user, query, embed_fn)
      |
      +-- load_core_blocks()  -> 所有 L1 block 进结果
      |
      v
backend.vector_search(embed_fn(query), types=('event','thought'))
      |
      v
load ConceptNode rows where deleted_at IS NULL
      |
      v
score each = 0.5*recency + 3*relevance + 2*impact + 1*relational_bonus
      |
      v
drop rows where relevance < min_relevance (默认 0.4)
      |
      v
按 total 排序，保留 top_k
      |
      v
每条命中的 access_count += 1，commit
      |
      v
对每条 event 命中可选扩展 +/- N 条 L2 邻居消息
      |
      v
如果原始 vector 命中数 < fallback_threshold：
    在 L2 上跑 FTS 搜索
      |
      v
返回 RetrievalResult(core_blocks, memories, context_messages, fts_fallback)
```

四项 rerank 因子各有各的作用。Recency 是基于时间的指数衰减，半衰期 14 天，这样老但仍相关的记忆不会凭空消失。Relevance 直接来自向量 backend 的距离，被映射到 `[0, 1]`。Impact 是 `|emotional_impact| / 10`，这样在 relevance 平手时，peak event 会压过平平无奇的 event。relational bonus 是一个小幅度的平坦加分（`0.5`），任何带 relational tag 的节点都能拿到——这些 tag 是 `identity-bearing`、`unresolved`、`vulnerability`、`turning-point`、`correction`、`commitment`——这样身份级的事实在平手时被优先召回。

`min_relevance` floor 是承重墙。没有它，严格正交的向量命中会停在 relevance `0.5`，impact 权重就会悄无声息地把高强度 event 推到完全无关的 query 下。默认的 `0.4` 低到足够保住那些只有部分重叠的候选，同时高到足以拒绝真正的陌生人。想恢复旧行为的调用方可以传 `min_relevance=0.0`。

### 一个 persona 跨越所有 channel

记忆检索**从不**按 `channel_id` 过滤。不在向量搜索里过滤。不在 FTS fallback 里过滤。不在 session 上下文扩展里过滤。不在 core-block 加载里过滤。一个在群聊里的真人依然记得他经历过的每一次私聊；记忆也应该是同样的。至于某条被想起的事实是否适合在当前 channel 里被带出来，那是更上层的事，不是检索的事。

channel 身份在记忆内部只在一个地方有意义：session 是按 `(persona_id, user_id, channel_id)` 创建的，这样一个 channel 的 idle timer 和 max-length 触发器不会关掉另一个 channel 的活跃 session。一旦一个 session 的 L3 events 被抽取出来，这些 events 就加入统一的记忆池，被检索时被当作完全 channel-agnostic。

### Session 生命周期

```
get_or_create_open_session()      -- OPEN
       |
       v
ingest_message() x N              -- OPEN (counter 在累加)
       |
       v
idle > 30min OR 长度触发 OR 生命周期信号
       |
       v
consolidate_session()             -- extract + reflect 之后 CLOSED
       |
       +-- A. trivial？跳过 extraction
       +-- B. extract_fn(messages) -> L3 events    [写入 extracted_events=True]
       +-- C. 任一 event 的 |impact| >= 8 -> SHOCK reflection
       +-- D. 距上次 reflection > 24h -> TIMER reflection
       +-- E. reflect_fn(recent events) -> L4 thoughts (硬闸门: 每 24h 最多 3 次)
       +-- F. 标记 CLOSED
       |
       v
on_session_closed 通过生命周期队列触发
```

每一步都是在下一步开始前先 commit，observer 的 dispatch 严格位于把 `session.status` 改掉的那次 commit 之后。一次 consolidation 如果中途崩了，数据库仍然处于可恢复状态：session 停留在 `CLOSING`，下次启动时 catch-up pass 会把它捡回来，而一个从未真正关闭过的 session 绝不会触发生命周期 hook。

### 重试安全

B 阶段把抽取出来的 L3 events **与新的 `extracted_events=True` 标志位放在同一个事务里 commit**。如果 E 阶段（reflection）随后抛异常——瞬时 LLM 错误、超时、甚至 `SIGTERM`——worker 会从头重试 `consolidate_session`。函数顶端的 guard 读取 `extracted_events`，**直接跳过 B 阶段**：已持久化的 events 从数据库加载出来，喂给 SHOCK/TIMER 判断，reflection 对着它们跑。每个 session 最多调用一次抽取 LLM，无论反思失败多少次。

这个不变量在两个方向都成立：

- `extracted=True` 蕴含 `extracted_events=True`（F 阶段只有在 B 阶段的 flag 已 commit 后才会运行）
- `extracted_events=True` **不**蕴含 `extracted=True`——这正是中间断点的意义所在

处于 `extracted_events=True, status=CLOSING` 状态的 session 会被 worker 安全地重试；被推到 `FAILED` 状态的 session（`consolidate_worker._mark_failed` 的兜底分支）是终态，不会自动重试，需要管理员介入才能重置。

### Schema 迁移

`ensure_schema_up_to_date(engine)` 在 daemon 启动时于 `create_all_tables(engine)` 之前被调用。它走一条硬编码的 "add column if not exists" 和 "create table if not exists" 步骤列表，每一步都被 `PRAGMA table_info` 或 `sqlite_master` 查询守住。每个新列要么 nullable 要么有 SQL 默认值，所以旧的行不需要回填。迁移器不支持重命名、删列、类型变更——这些被推迟给未来的 migration framework。失败是致命的：一份半迁移的 schema 宁可在启动时 fail-fast，也不要在后续写入时悄悄炸掉。

### Observer 契约

Observer 是 fire-and-forget 的 post-commit 通知。Protocol 住在 `observers.py`：

```
MemoryEventObserver
  on_message_ingested(msg)        per-call，通过 observer= 参数
  on_event_created(event)         per-call，通过 observer= 参数
  on_thought_created(thought)     per-call，通过 observer= 参数
  on_core_block_appended(append)  per-call，通过 observer= 参数
  on_new_session_started(...)     生命周期，通过 _observers 注册表
  on_session_closed(...)          生命周期，通过 _observers 注册表
  on_mood_updated(...)            生命周期，通过 _observers 注册表
```

所有方法都是同步 `def`（不是 `async def`）。Hook 抛出的异常在记忆边界被捕获，通过模块 logger 记成 log；触发这个 hook 的那次记忆写入在此时**已经 commit**，绝不会被回滚。只实现了部分 hook 的消费者依赖结构化 subtyping——`NullObserver` 作为一个 no-op 基类提供给继承使用。

生命周期事件流经 `sessions.py` 里一个很小的队列。修改 `session.status` 的那条代码路径把一个待决事件入队，提交完成的调用方在 `db.commit()` 返回之后立刻 drain 这个队列。这让一次 commit 能在一轮里 dispatch 好几个生命周期 hook，而不需要每个函数都知道哪个 hook 该触发。

---

## How to Extend

三种常见的扩展方式，每种都给出一份最小可运行的草稿。真的去跑之前，请把它们接到真实的 persona 和真实的数据库上。

### 1. 注册一个自定义 observer

实现 Protocol（或继承 `NullObserver`）然后在启动时注册实例。Hook 在记忆模块所在的线程里、紧接着产生它的 commit 之后触发。

```python
from echovessel.memory import (
    MemoryEventObserver,
    NullObserver,
    ConceptNode,
    register_observer,
)


class EventLogger(NullObserver):
    """玩具 observer：落到一条新 L3 event 就 log 一下。"""

    def __init__(self) -> None:
        self.count = 0

    def on_event_created(self, event: ConceptNode) -> None:
        self.count += 1
        print(
            f"[event #{self.count}] {event.description!r} "
            f"impact={event.emotional_impact} "
            f"tags={event.relational_tags}"
        )

    def on_session_closed(
        self, session_id: str, persona_id: str, user_id: str
    ) -> None:
        print(f"[session closed] {session_id} for {persona_id}/{user_id}")


logger = EventLogger()
register_observer(logger)  # 注册后生命周期 hook 自动触发
# Per-write hook（on_event_created 等）只在调用方把
# observer=logger 传进 consolidate_session / bulk_create_events 时才会触发。
```

生命周期 hook（`on_new_session_started`、`on_session_closed`、`on_mood_updated`）在 observer 注册之后就自动触发。Per-write hook（`on_event_created`、`on_thought_created`、`on_message_ingested`、`on_core_block_appended`）只在调用方显式把 `observer=...` 传进对应写 API 时才触发。结构化 subtyping 意味着你只需要实现你在乎的那些 hook。

### 2. 加一个新的 retrieve scorer

rerank 权重以模块常量形式住在 `retrieve.py` 里。抬高一个权重只是一行 patch，但更干净的扩展是包一层 scorer，这样默认行为完全不动、你的偏好是 opt-in 的。

```python
from datetime import datetime
from echovessel.memory import retrieve as m_retrieve
from echovessel.memory.retrieve import ScoredMemory, RetrievalResult


def retrieve_with_access_boost(
    db, backend, persona_id, user_id, query, embed_fn, *, top_k=10
) -> RetrievalResult:
    """等同于 memory.retrieve.retrieve，但对被频繁访问的节点额外加分。"""

    result = m_retrieve.retrieve(
        db,
        backend,
        persona_id,
        user_id,
        query,
        embed_fn,
        top_k=top_k * 2,            # 超额抓取，给我们的 rerank 留余地
        min_relevance=0.4,          # 保留正交 floor
    )

    boosted: list[ScoredMemory] = []
    for sm in result.memories:
        # 对 access_count 做简单的 log bonus；你可以随意调或替换
        import math
        bonus = 0.25 * math.log1p(sm.node.access_count)
        sm.total += bonus
        boosted.append(sm)

    boosted.sort(key=lambda s: -s.total)
    result.memories = boosted[:top_k]
    return result
```

`min_relevance` 过滤器在 rerank 之前跑，所以你加的任何自定义权重只会在已经通过了 floor 的候选之间竞争。如果你的 scorer 需要把 relevance 低但 impact 高的记忆顶出来（比如要在用户拐弯提起一段创伤时把它召回），请在调用处直接降低 `min_relevance`，**不要**在 scorer 里绕过它——这个 floor 存在的理由正是防止 tie-break 的小聪明把正交的 peak event 漏进 prompt。

### 3. 加一个新的 L3 event 抽取规则

`bulk_create_events` 是 import 侧用于 event 的写入原语。用它对一个刚关闭的 session 跑你自己的启发式后处理，在你的 pattern 命中时插一条额外的 L3 行。注意：没有 embedding 的 bulk-written event 对向量检索**不可见**，所以 embed pass 是强制的，不是可选的。

```python
from echovessel.memory import (
    EventInput,
    bulk_create_events,
    ConsolidateResult,  # consolidate_session 的返回
)
from echovessel.memory.models import RecallMessage
from sqlmodel import select


def detect_apology_and_write_event(
    db, backend, embed_fn, result: ConsolidateResult
) -> None:
    """如果用户在这个 session 里道过歉，就多写一条 L3 event。"""

    session = result.session
    msgs = db.exec(
        select(RecallMessage).where(RecallMessage.session_id == session.id)
    ).all()

    apology_lines = [m for m in msgs if "sorry" in m.content.lower()]
    if not apology_lines:
        return

    inputs = [
        EventInput(
            persona_id=session.persona_id,
            user_id=session.user_id,
            description=f"User apologized: {apology_lines[0].content}",
            emotional_impact=-3,
            emotion_tags=("regret",),
            relational_tags=("vulnerability",),
            imported_from=f"rule:apology:{session.id}",
        )
    ]
    event_ids = bulk_create_events(db, events=inputs)

    # 强制 embed pass —— 没有这一步，这条新 event 永远不会出现在
    # retrieve() 的向量检索结果里。
    for eid, ev_input in zip(event_ids, inputs):
        backend.insert_vector(eid, embed_fn(ev_input.description))
```

`bulk_create_events` 会设置 `imported_from`，并刻意把 `source_session_id` 留成 `NULL`——schema 的 CHECK 约束禁止两者同时非空。用一个稳定的、规则专属的前缀（这里是 `rule:apology:`）作为 `imported_from` 的值，这样 `count_events_by_imported_from` 就能回答"这条规则在这个 session 上是不是已经跑过了？"，让规则保持幂等。

同样的模式也适用于 L4：调 `bulk_create_thoughts` 传一个 `ThoughtInput` 列表，然后在它能被检索之前给每条 thought 算 embedding。证据链（soul chain）住在 `concept_node_filling` 里，由 consolidate pass 写入，而不是 bulk 原语——如果你的自定义规则产生的 thought 需要引用具体的 events，请在同一个 transaction 里自己把 filling 行插进去。

---

## 另见

- [`configuration.md`](./configuration.md) — 与记忆相关的配置字段和 tunables
- [`runtime.md`](./runtime.md) — 启动序列，记忆是怎么被接进 daemon 的
- [`channels.md`](./channels.md) — 产生记忆里那些 `turn_id` 的 debounce / turn 层
- [`import.md`](./import.md) — 通过 `import_content` 写进记忆的离线导入 pipeline
