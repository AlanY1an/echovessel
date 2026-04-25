# 记忆（Memory）

> 分层的 persona 记忆。L1 core block 永远进 prompt，L2 原始消息作为 ground truth，L3 抽取出的事件，L4 蒸馏出的想法。一个 persona 在它说话的每一个 channel 上都是同一份持续身份。

记忆是 EchoVessel 的核心资产。其他所有东西——runtime、channels、voice、proactive——存在的理由都是喂养它、查询它、或把它记住的内容呈现出来。一个数字 persona 的持久性取决于它背后的记忆，而这个模块就是那份持久性住的地方。

---

## Overview

记忆给一个 persona 一份稳定的自我感，以及对它所接触之人的稳定印象。它之所以分层，是因为这六层分别回答六个不同的问题："此刻我是谁"（L1）、"对方究竟说了什么原话"（L2）、"那次对话里发生了什么"（L3）、"跨很多次对话之后我对这个人持有什么看法、做过什么承诺、在预期什么"（L4）、"我认识的这些人物/地点/组织分别叫什么、又有哪些别名"（L5）、"此刻我自己是什么心情"（L6）。每一层都有自己的写入路径、自己的检索角色、自己的遗忘语义。把它们塌缩成一个统一 store 要么把 prompt 撑爆，要么丧失做 reflection 的能力。

模块的设计刻意和其他层解耦。它不知道 channel 是什么，不知道 LLM provider 是什么，不知道 runtime 是怎么 stream 回复的。上层把 embedding 函数、extraction 函数、reflection 函数当作纯 callable 注入进来；记忆负责存储、打分和生命周期。这条纪律由 `pyproject.toml` 里的分层契约强制执行——memory 只能依赖 `echovessel.core`，别无其他。

系统其他部分在记忆之上给出的承诺很简单：一个 persona 就是一份持续身份。当用户下午在 Web 上跟它聊、晚上又在 Discord 上跟它聊时，记忆是同一份池子。检索从不按消息是从哪个 channel 来的来过滤。这一条规矩塑造了下面大多数的设计决策。

---

## Core Concepts

**L1 core blocks** — 短小、稳定的文本段，无条件注入每次 prompt。`core_blocks` 表里住着三个 label：`persona`、`user`、`style`。`persona` / `style` 跨用户共享（persona 是一个角色，它的画像和 owner 钦定的风格指令不因用户不同而 fork）。`user` 按用户分份，key 是 `(persona_id, user_id)`。每个 block 上限 5000 字符，并且在 `core_block_appends` 里有一份 append-only 审计日志。情绪状态不在 L1——当下心境归 L6 episodic state 管。

**L1 是"人写的身份层"——永远不被代码自动改写。** 三个写入入口全是 human-driven：onboarding bootstrap、admin UI（`POST /api/admin/persona` 与 `POST /api/admin/persona/style`）、import pipeline 的 `bootstrap_from_material`。`slow_tick` / `consolidate` / extraction 内部的代码路径**不允许**写 `core_blocks`——这条不变律由 `tests/memory/test_slow_cycle.py` 的 "L1-never-auto-update invariant" 测试钉死。Persona 的 reflection 自动生长进 L4 thought（`subject='persona'`），不进 L1；persona 对第三方人/地/组织的描述自动生长进 L5 `entities.description`，也不进 L1。

**生平事实**(Biographic facts)— 跟 core blocks 并排住在 `personas` 行本身的十五个结构化身份列(`full_name`、`gender`、`birth_date`、`nationality`、`native_language`、`timezone`、`occupation`、`relationship_status`、…)。这些字段之所以跟散文 block 分开放,是因为代码想知道"她是什么时区"或"她是哪年生的"时可以直接查列,不用重新解析 persona block 里那段散文。十五个全部可空 — onboarding 时 LLM 抽取能填什么就填什么,用户在 review 页面校对剩下的。其中五个(姓名 / 性别 / 出生年 / 职业 / 母语)会在每个 turn 渲进系统 prompt 的 "# Who you are" 段;另外十个只供系统代码使用。

**L2 raw messages** — 每一条用户消息和 persona 回复都原样写进 `recall_messages`。这是档案级的 ground truth。表里每行带一个 `channel_id`，这样前端可以渲染"via Web"或"via Discord"的小标签，但进 prompt 的查询从不在它上面过滤。L2 用 FTS5 建了索引作为关键字兜底，但**不参与**主检索 pipeline；它是所有其他路径失败时系统能永远回落的那一层。

**L3 events** — 从一个关闭的 session 里抽取出的事实。以 `type='event'` 的 `ConceptNode` 行存储：一段自然语言描述、一个 `-10..+10` 的 `emotional_impact`、emotion 和 relational 标签、一份存在 sqlite-vec 伴随表里的 embedding，以及一个指回 `source_session_id` 的溯源指针。Events 是 episodic 记忆的主要单位——"那次用户告诉我 Mochi 做了手术的对话"。

**L4 thoughts** — 从很多 events 里蒸馏出来的更长程的观察。和 L3 同一张表，用 `type='thought'` 区分。每条 thought 带一条 `filling` 证据链（通过 `concept_node_filling`），记录它是从哪些 events 生成的，这样当用户删掉源头 events 时可以选择把 thought 保留为孤儿。Thoughts 由两条路径写入：consolidate 内部的 SHOCK / TIMER reflection（fast loop），以及 session 之间跑的 slow_tick reflection phase（slow loop · forward-looking 推理）。slow_tick 还会在合适时机产 `type='expectation'` 子类节点（"她下周可能会更新 grad school 进度"），这些 expectation 在 fast loop 里被 embedding 相似度匹配 user 的下一条消息，命中标 `fulfilled`，超期标 `expired`。

每条 thought 带一个 `subject` 列：`subject='user'` 是 persona 对 *user* 的判断（"她最近压力比较大"），`subject='persona'` 是 persona 对 *自己* 的反思（"我最近回得太短了"）。`subject='persona'` 是 slow_tick G phase 自动产出的——这是 persona 自我画像随时间累积的唯一物理路径（L1 的 `persona` block 永远不被代码触碰）。prompt 渲染时按 recency 取近 5 条挂在 user prompt 的 `# How you see yourself lately` 段，由 `retrieve.force_load_persona_thoughts(top_n=5)` 直接旁路 query embedding。

**L5 entities** — 第三方人物 / 地点 / 组织 / 宠物的 canonical 身份。三张表：`entities`（canonical name + kind + 三态 merge_status + 一段 `description` prose 列）· `entity_aliases`（多对一 alias → entity）· `concept_node_entities`（L3 event ↔ L5 entity 的多对多 junction）。Extraction 抽到新人名时建 entity，已知 entity 出现新别名时只 append 别名行。检索时 query 文本里的任何 alias 命中都会让该 entity 关联的所有 ConceptNode 一并进候选并加分，这是跨语种 / 跨别名召回的工程基础——光靠向量距离没法把 "Scott" 和 "黄逸扬" 拉到一起。三层 dedup（alias 精确匹配 → embedding 0.65/0.85 阈值 → uncertain 时 persona 自然问 user）写在 `memory/entities.py`。

`entities.description` 是 entity 的散文画像列：由 slow_tick 在 entity 的 `linked_events_count` 越过阈值（默认 ≥ 3）时合成写入；也可以由 owner 通过 `PATCH /api/admin/memory/entities/{id}` 显式覆盖（写 `owner_override=true` 后 slow_tick 不再覆盖）。两条写路径都走同一个 `update_entity_description(db, *, entity_id, description, source)` 原语，`source` 取值 `'slow_tick'` 或 `'owner'`。命中 alias anchor 的 entity 会在 system prompt 里渲一段 `# About {canonical_name}`——一行一个 entity，cardinality 与 entity 行严格 1:1。

**L6 episodic state** — persona 当下情绪状态的单行 snapshot，住在 `personas.episodic_state` 这一列 JSON 里：`{mood, energy, last_user_signal, updated_at}`。session 关闭时由 extraction LLM 输出的 `session_mood_signal` 字段顺便写入（不调额外 LLM 也不开新 round-trip），所以 L6 的更新成本是 0。`assemble_turn` 入口会做 12h decay 检查——超过 12h 没更新就把 mood 拍回 `neutral`，避免一段长安静期之后用陈旧情绪打开下一轮。渲染成 system prompt 的 `# How you feel right now` 段；mood 仍是 `neutral` 时这一段被略过，prompt 保持简短。

**Consolidate** — session 关闭时跑的 pipeline。它把 session 的 L2 消息一次性读进来，调用注入进来的 extraction 函数生成零条或多条 L3 events，给每条 event 算 embedding，按需触发一次 reflection pass 产生 L4 thoughts，然后把 session 标记为 `CLOSED`。入口是 `src/echovessel/memory/consolidate/core.py` 里的 `consolidate_session`。

**Retrieve** — persona 说话前跑的 pipeline。它把所有 L1 core block 加载进来，让 storage backend 在 `concept_nodes` 上跑一次向量检索，用四项因子对候选做 rerank，用一个最低 relevance floor 抑制正交匹配，再按需给每个命中扩展附近的 L2 消息。如果向量索引返回的命中数不够，L2 上的 FTS fallback 会补刀。入口是 `src/echovessel/memory/retrieve/core.py` 里的 `retrieve`。

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

每次写入都是先 commit、再触发任何 hook。`sessions.py` 里的生命周期队列把 "new session" / "session closed" 事件做了批处理，这样一次 commit 可以在一次 drain 里 dispatch 多个 hook。`ingest_message` / `append_to_core_block` 这两个调用走的是显式 `observer=` 参数（per-write 通知），其他大多数 hook（event/thought/entity/session/mood）从模块级 `_observers` 注册表 fan-out——这个注册表由 `register_observer(...)` 在 daemon 启动时调一次即可。

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

### Entity-anchored retrieval（L5 旁路）

主向量打分之外有一条便宜的 alias 旁路：每次 retrieve 调用时，query 文本被 tokenize 后扔给 `find_query_entities()` 做精确匹配（case-sensitive，CJK 不切分），命中的 entity 通过 `concept_node_entities` junction 反查所有关联的 ConceptNode。这一批 node 直接进候选池，无视向量距离，并在 rerank 时获得 `WEIGHT_ENTITY_ANCHOR * ENTITY_ANCHOR_BONUS_VALUE`（默认 1.5 × 1.0）的加分。这条路径专门解跨语种 alias 召回——sentence-transformers 把 "Scott" 和 "黄逸扬" 拉不到一起，alias 精确匹配能。当一个 entity 的 `merge_status` 是 `'uncertain'` 时（embedding 在 0.65 ~ 0.85 中段，没办法自动 merge），retrieve 还会顺便给 system prompt 注入一段 `# Entity disambiguation pending` hint，让 persona 在自然对话节奏里向 user 主动问一句"Scott 是不是你之前说的黄逸扬啊"，由 user 的回答决定下次 consolidate 是 confirm 还是 disambiguate。

### Force-loaded pinned thoughts（绕过 query similarity）

`retrieve(force_load_user_thoughts=N)` 是给 `# About {speaker}` 段用的旁路：直接按 `recency × importance` 取当前 speaker 的 top-N L4 thoughts，**完全不查 query embedding**。理由是：当用户消息是 "?" / "嗯" / 一句单字时，query 几乎没有可索引的 topic，但 persona 仍然应该知道自己在跟谁说话。runtime 默认在 `assemble_turn` 里传 `force_load_user_thoughts=10`，并把已经在 rerank top_k 里出现的 node id 排除掉，避免渲染重复。返回值挂在 `RetrievalResult.pinned_thoughts` 上。

### 7 类 stimulus reactivity

记忆模块对 7 种 stimulus 的反应路径都已经实装，没有任何一个走"轮询 + cron"——全部 event-driven：

| # | Stimulus | 反应路径 |
| --- | --- | --- |
| 1 | 普通 user turn | `ingest_message` → session 计数 → length / idle 触发判断 · `assemble_turn` 入口检查 episodic decay · `retrieve`（含 entity_anchor + pinned）· LLM stream |
| 2 | 高情绪事件（\|impact\| ≥ 8）| 现有 SHOCK reflection（fast loop · `consolidate.py`）· slow_tick 把本 session 标 immediate（跳过 30 min cool-down） |
| 3 | 重复 mention | `entities.detect_mention_dedup` 在 extraction 阶段做 vector + entity overlap 命中 · 命中升级 `mention_count++` 并 append `source_turn_ids` · 不 insert 新节点 |
| 4 | 矛盾信息 | extraction 输出 `superseded_event_ids` · consolidate 把老节点的 `superseded_by_id` 指向新节点（soft delete · row 不删）· retrieve 默认过 `superseded_by_id IS NULL` |
| 5 | user 风格纠正 | owner 走 `POST /api/admin/persona/style` 显式写 `STYLE` block · 不做 NLP 关键词自动检测 |
| 6 | 安静期（无 user 消息）| `idle_scanner` 关 stale session（既有）· `assemble_turn` 入口的 12h decay 把 episodic_state mood 拍回 neutral · 没新 inbox material 时 slow_tick 不 fire |
| 7 | 主动回想（slow_tick 自发）| consolidate_worker `_process_one` 末尾的 G phase 在合适条件下跑 slow_cycle LLM · 产 `type='thought'` + `type='expectation'` 节点 · 下次 fast loop retrieve 大概率召回（这是"persona 在你不说话时也想到你"的物理实现）· 产出的 thought 会立刻通过 `on_thought_created` 广播 SSE topic `memory.thought.created` · Web chat 的 Memory Timeline 实时看到 |

时间介入只在三处合法兜底：L6 episodic 12h decay · `consolidate_worker` 5s polling · TIMER reflection（超 24h 无 thought 强制走一次 reflect）。其它一切"每周二 X / 每月初 Y"类调度模式被明确禁止——记忆模块只接 event-driven 触发。

### Slow_tick consolidate phase（L4 forward-looking）

slow_tick **不是独立 worker**——它是 `consolidate_worker._process_one` 末尾追加的 G 阶段（A-F 之外）。每次 session CLOSING 处理完之后检查：

- `is_trivial` → skip
- `now - persona.last_slow_tick_at < cool_down_minutes`（默认 30）AND 本 session 无 SHOCK → skip
- 否则跑

跑的时候做一次 `slow_cycle_llm` 调用，输入是过去 cool-down 周期内的 cross-session material（events + 已有 thoughts + 上次 slow_tick 留的 salient_questions），输出三块合一：`new_thoughts`（含 `subject='persona'` 自我反思和 `subject='user'` 对用户的判断）/ `new_expectations` / `salient_questions`（下次 slow_tick 的种子问题，写回 personas 表）。然后：

- `bulk_create_thoughts(new_thoughts)` 落 L4 thought 节点 + filling chain · `subject='persona'` 的那些就是 persona 对自己的反思，挂在 user prompt 的 `# How you see yourself lately` 段
- 写 `type='expectation'` 节点（带 `event_time_end` 作 due_at）· fast loop 在每条新 user message 上 embed-similarity 比 expectation.description · 命中标 fulfilled · 超期标 expired
- 顺便：扫每个 entity 的 `linked_events_count` · 越过阈值（默认 ≥ 3）就调 `update_entity_description(...)` 合成一段 description（slow_tick 永远走 source='slow_tick' · 受 `owner_override=true` 阻止）
- `personas.last_slow_tick_at = now`

**slow_tick 不写 L1 core_blocks · 一行都不写。** persona 的自我反思走 L4 thought[subject='persona']；persona 对人/地/组织的描述走 L5 entities.description。`BlockLabel` enum 只有 `persona` / `user` / `style` 三个值，`append_to_core_block` 不在 slow_tick 的执行链里——这条不变律由 `tests/memory/test_slow_cycle.py` 的 "L1-never-auto-update invariant" 测试钉死。

护栏（不可越界 invariant）：单 cycle token wall（input ≤ 8k · output ≤ 1k）· 每天 36 cycles + 150k input + 30k output · `cfg.slow_tick.enabled = false` 全局 kill switch · 每个 event 被 reflect 次数 ≤ 3。slow_tick 不能：写 L1 任何 block · 创建无原话证据的 intention · 自己 schedule 下次 · 改 in-place 已有节点 · 调外部 API · 创新的 NodeType / BlockLabel · 递归自触发 · 跳 token budget。封闭 tool 枚举 + schema 拒写守这些 invariant。Transcript 落盘到 `develop-docs/slow_tick_transcripts/<cycle_id>.json`，admin tab 有 `GET /api/admin/slow-tick/transcripts` 翻历史。

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

### 触发条件与阈值

上面那张流程图里每个数值都是模块级常量，不是 config 可调项:

| 迁移 | 触发 | 位置 | 常量 |
| --- | --- | --- | --- |
| L2 → (关 session) | idle 超时 | `memory/sessions.py` | `SESSION_IDLE_MINUTES = 30` |
| L2 → (关 session) | 长度上限 | `memory/sessions.py` | `SESSION_MAX_MESSAGES = 200` · `SESSION_MAX_TOKENS = 20_000` |
| L2 → (关 session) | runtime 生命周期 | channels / catchup | — |
| L3 → L4 | SHOCK — 新 event 撞到 impact 地板 | `memory/consolidate/phase_bce.py` | `SHOCK_IMPACT_THRESHOLD = 8`(取绝对值) |
| L3 → L4 | TIMER — 最近没有 reflection | `memory/consolidate/phase_bce.py` | `TIMER_REFLECTION_HOURS = 24` |
| L3 → L4(闸门) | 24h 反思上限 | `memory/consolidate/phase_bce.py` | `REFLECTION_HARD_LIMIT_24H = 3` |

两个新读者常绊的点:

- **驱动 SHOCK 的 `emotional_impact` 是抽取 LLM 自己给的**,不是另一个分类器。extraction prompt 要求模型对每条 event 输出 `[-10, +10]` 的带符号整数,consolidate 只是对本次新写的 events 算一个 `max(|impact|) >= 8`。prompt 里 `-7 = 严重失去 / 悲伤`、`+7 = 重大正面里程碑`是锚点(见 `prompts/extraction.py`)。
- **SHOCK 和 TIMER 是 OR · hard gate 叠在外层 AND。** 规则是:"如果 (shock OR timer_due) 成立 · 除非过去 24h 已经 3 条 thought · 那就 reflect"。gate 是防止一整天情绪密集的对话把 reflection 账单推到无底洞。

### 全流程推演 · 一条消息的一生

具体时间线,一条消息走完整条路径。时间是演示,实际数值见上面那张阈值表。

```
t = 0s             用户在 Web 频道打 "hi"
                   ↓
  WebChannel debounce(~2s) · 发 IncomingTurn 给 runtime
                   ↓
  runtime.handle_turn() 启动
                   ↓
┌─ memory.ingest_message(persona, user, channel, USER, "hi")
│    get_or_create_open_session(persona, user, channel)
│      · SELECT OPEN sessions WHERE (persona, user, channel, 未删除)
│      · 有 last_message_at > now - 30min 的?        → 直接返回
│      · 有 stale(idle > 30min)?                      → mark_closing('idle') · 建新
│      · 完全没行?                                    → INSERT 新 OPEN session
│    INSERT recall_messages(role=USER, content, turn_id, channel_id, day)
│    UPDATE session · message_count++ · total_tokens+=N · last_message_at=now
│    check_length_trigger
│      · message_count ≥ 200 OR total_tokens ≥ 20_000? → mark_closing('max_length')
│    db.commit()
│    触发 on_message_ingested(msg) · drain 生命周期队列
└─

t ≈ 0.1s           runtime 准备回复
                   ↓
┌─ memory.load_core_blocks(persona, user)
│    → 3 行(persona / style [共享] + user [per-user])
│
├─ assemble_turn 入口检查 L6 episodic_state · 12h 没更新就 decay 回 neutral
│
├─ memory.retrieve(persona, user, query=上一条用户消息, embed_fn, top_k=10,
│                  user_now=msg.received_at)
│    query_vec = embed_fn(query)
│    L5 entity-anchor 预先 cheap match · 命中 entity 关联 ConceptNode 进候选
│    backend.vector_search(query_vec, types=('event','thought','intention','expectation'), top_k=40)
│    读出 ConceptNode 行(未删除的 · 未被 superseded_by_id 替代的)
│    rerank: 0.5·recency + 3·relevance + 2·impact + relational_bonus + entity_anchor_bonus
│    砍掉 relevance < min_relevance(默认 0.4)            ← over-recall 地板
│    保留 top_k · UPDATE access_count++ · last_accessed_at
│    derive_event_status(event_time_*, user_now) · render_event_delta_phrase
│    可选:每条 event 命中 ±N 条 L2 邻居扩展
│    向量原始命中 < fallback_threshold 时 · 启用 L2 FTS 回退
│
├─ force_load_user_thoughts(persona, user, top_n=10) · pinned thoughts 旁路 query
│
├─ assemble_turn → LLM.stream(system_prompt, user_prompt)
│    详见 docs/zh/runtime.md § Prompt 段顺序。
│    system_prompt = opener + # Right now (双 TZ) + # Who you are (7 facts)
│                    + # How you feel right now (L6 episodic) + L1 core blocks
│                    + # Style preferences + # About {canonical_name} (alias 命中)
│                    + # Entity disambiguation pending + STYLE_INSTRUCTIONS
│    user_prompt   = # Recent sessions (day-bucket) + retrieved thoughts/events
│                    + # About {speaker} (pinned user-thoughts)
│                    + # How you see yourself lately (pinned persona-thoughts)
│                    + # Promises you've made + # You've been expecting
│                    + # Our recent conversation + # What they just said
│
└─ 流式 token → channel · 累积的 reply 再通过 ingest_message(PERSONA) 回写

t = 几秒           persona 回复落库。session 仍 OPEN。

                   (用户走开 · 没有新消息)

t ≈ 30-60min       idle_scanner 醒来(默认每 60s)
                   ↓
                   catch_up_stale_sessions(db, now)
                     · SELECT status=OPEN AND last_message_at < now - 30min
                     · 每行 mark_closing('catchup')
                     · commit

t ≈ 30-60min + 5s  consolidate_worker 轮询(默认每 5s)
                   ↓
                   SELECT status=CLOSING AND extracted=False
                   入队每个 session_id · 逐个:
                   ↓
┌─ consolidate_session(session)
│
│  [A] is_trivial(session)?                                ← 太短 + 无情绪
│        msgs < 3 AND tokens < 200 AND 无 peak 信号
│        是 → status=CLOSED, trivial=True, 触发 hook, 结束
│        否 → 继续
│
│  [B] 如果 session.extracted_events 是 False(首次):
│        events = await extract_fn(messages)               ← LLM 调用(SMALL tier)
│        for e in events:
│          INSERT concept_nodes(type='event', ...)
│          backend.insert_vector(id, embed_fn(e.description))
│        session.extracted_events = True
│        session.extracted_events_at = now
│        db.commit()                                       ← RESUME POINT
│      否则(上次崩了在重试):
│        events = SELECT concept_nodes WHERE source_session_id=session.id
│
│  [C] shock_event = 第一个满足 |e.emotional_impact| ≥ 8 的 event   ← 抽取器给的分
│
│  [D] timer_due = 过去 24h 没有 thought
│
│  [E] should_reflect = (shock_event OR timer_due)
│      AND 过去 24h 已有 thought 数 < 3                    ← 硬闸门
│      if should_reflect:
│        inputs = 过去 24h 内的 events(+ shock_event 如果不在里面)
│        thoughts = await reflect_fn(inputs, reason)       ← LLM 调用(SMALL tier)
│        for t in thoughts:
│          INSERT concept_nodes(type='thought', ...)
│          backend.insert_vector(id, embed_fn(t.description))
│          for src_id in t.filling:
│            INSERT concept_node_filling(parent=t.id, child=src_id)
│        db.commit()
│
│  [F] session.status = 'closed'
│      session.extracted = True
│      session.extracted_at = now
│      db.commit()
│      触发 on_session_closed(session)                     ← 生命周期队列 drain
│
│  [G] slow_tick consolidate phase — 仅在 should_run_slow_cycle 判 OK 时跑
│      (cool_down + non-trivial + daily-cap / token-wall 都没触顶):
│        run_slow_cycle(persona, recent_events, recent_thoughts, ...)
│          → typed ConceptNode 输出 · type IN ('thought','expectation')
│          → thought 带 subject='user' 或 subject='persona'
│            · subject='persona' 是 persona 自我反思的存放处
│              · 之后渲入 user prompt 的 # How you see yourself lately
│          → 每条带 filling_event_ids · expectation 强制非空 reasoning_event_ids
│          → 顺便扫每个 entity 的 linked_events_count · 越阈值则
│            update_entity_description(source='slow_tick') 合成一段 description
│          → personas.last_slow_tick_at = now
│      slow_tick 不写 L1 · 不调 append_to_core_block
│      失败不回滚 session([F] 已 commit) · 只 log warning
│
│  · session_mood_signal(由 [B] 抽取 LLM 顺便输出)在 [B]/[F] 之间已 UPDATE
│    personas.episodic_state JSON 列 · 不调额外 LLM
│  · L5 entities:[B] 抽 event 时同步走三层 dedup(alias / embedding / ask-user)
│    更新 entities + entity_aliases · 写 concept_node_entities junction
└─

下一 turn · retrieve 能看到新写的 L3 events + L4 thoughts · L6 episodic_state 也
反映这次 session 的情绪走向 · slow_cycle 产的 expectation 进 # You've been expecting 段。
```

最后 session 可能停留的三种状态:

| 状态 | 含义 | 能重试吗 |
| --- | --- | --- |
| `CLOSED` | happy path — `extracted=True` · session 走完了 | 不 |
| `FAILED` | `consolidate_worker` 用完了 `worker_max_retries` · 终态 | 不会自动;operator 把 `status=CLOSING`、`extracted=False` 翻回来才能重试 |
| `CLOSING`(卡住) | daemon 在 consolidate 中途挂了 · 下次启动的 catchup pass 会捡起来 | 下次 boot 自动 |

### 重试安全

B 阶段把抽取出来的 L3 events **与新的 `extracted_events=True` 标志位放在同一个事务里 commit**。如果 E 阶段（reflection）随后抛异常——瞬时 LLM 错误、超时、甚至 `SIGTERM`——worker 会从头重试 `consolidate_session`。函数顶端的 guard 读取 `extracted_events`，**直接跳过 B 阶段**：已持久化的 events 从数据库加载出来，喂给 SHOCK/TIMER 判断，reflection 对着它们跑。每个 session 最多调用一次抽取 LLM，无论反思失败多少次。

这个不变量在两个方向都成立：

- `extracted=True` 蕴含 `extracted_events=True`（F 阶段只有在 B 阶段的 flag 已 commit 后才会运行）
- `extracted_events=True` **不**蕴含 `extracted=True`——这正是中间断点的意义所在

处于 `extracted_events=True, status=CLOSING` 状态的 session 会被 worker 安全地重试；被推到 `FAILED` 状态的 session（`consolidate_worker._mark_failed` 的兜底分支）是终态,不会自动重试,需要管理员介入才能重置。

### 手动重抽

幂等性依赖持久化的 `Session.extracted` flag · **不依赖 worker 的 in-memory 状态**。任何一个跑着的 worker 实例 · 只要 session 被翻到 `status='closing', extracted=false` · 就会重新 consolidate —— **不需要重启 daemon**。这条路径对三个场景关键:debug(改了 consolidate 阈值想在老 session 上重跑)· 修复 `FAILED` session(解决根因后把它拉回重试)· 以及任何"麻烦再跑一次"的 ops 流程。

直接翻 flag:

```sql
UPDATE sessions
   SET status = 'closing', extracted = 0, extracted_events = 0
 WHERE id = 's_xxx';
```

一个 poll 周期内(默认 5 秒)worker 会捡起这个 session · 按**当前**配置跑 `consolidate_session`。`is_trivial` 在每次调用时根据 `[consolidate].trivial_*` 现值重新判断 —— 老阈值下被判 trivial 跳过的 session · 阈值调低后这次就会抽出 events。

同样的短路逻辑也保护了"有人误把已经 `extracted=True` 的 session id 塞回队列"的情况:`_process_one` 读到 flag 是 True 就立即 return · 不调抽取器。持久化 flag 是单一真相源 · 没有 in-memory `seen` set。

### Schema 迁移

`ensure_schema_up_to_date(engine)` 在 daemon 启动时于 `create_all_tables(engine)` 之前被调用。它走一条硬编码的 "add column if not exists" 和 "create table if not exists" 步骤列表，每一步都被 `PRAGMA table_info` 或 `sqlite_master` 查询守住。每个新列要么 nullable 要么有 SQL 默认值，所以旧的行不需要回填。迁移器不支持重命名、删列、类型变更——这些被推迟给未来的 migration framework。失败是致命的：一份半迁移的 schema 宁可在启动时 fail-fast，也不要在后续写入时悄悄炸掉。

### Observer 契约

Observer 是 fire-and-forget 的 post-commit 通知。Protocol 住在 `observers.py`，里面有 9 个 hook，按触发路径分两类——纯 per-write 和 lifecycle：

```
MemoryEventObserver  (Protocol · 源真相在 observers.py)
  # 纯 per-write hook · 只在调用方显式传 observer= 时触发
  on_message_ingested(msg)
  on_core_block_appended(append)

  # 7 个 lifecycle hook · 自动通过 _observers 注册表 fan-out
  on_event_created(event)
  on_thought_created(thought, source)        # source ∈ {reflection, slow_tick, import}
  on_entity_confirmed(entity)                # uncertain entity 不广播 · 见 plan §3.1
  on_entity_description_updated(entity, source)  # source ∈ {slow_tick, owner}
  on_new_session_started(session_id, persona_id, user_id)
  on_session_closed(session_id, persona_id, user_id)
  on_mood_updated(persona_id, user_id, new_mood_text)  # mood 实际住在 L6 · 不再写 L1
```

`on_event_created` / `on_thought_created` 是**两条路径都触发**的——既走 lifecycle 注册表（让 `RuntimeMemoryObserver` 看到），也接受 `consolidate_session` / `import_content` 的 `observer=` per-call 参数（让导入 pipeline / 测试拿自己的回调）。这条 fan-out 是 v0.5 加的 additive 改动；老的 per-call 调用语义没变。

所有方法都是同步 `def`（不是 `async def`）。Hook 抛出的异常在记忆边界被捕获，通过模块 logger 记成 log；触发这个 hook 的那次记忆写入在此时**已经 commit**，绝不会被回滚。只实现了部分 hook 的消费者依赖结构化 subtyping——`NullObserver` 作为一个 no-op 基类提供给继承使用。

生命周期事件流经 `sessions.py` 里一个很小的队列。修改 `session.status` 的那条代码路径把一个待决事件入队，提交完成的调用方在 `db.commit()` 返回之后立刻 drain 这个队列。这让一次 commit 能在一轮里 dispatch 好几个生命周期 hook，而不需要每个函数都知道哪个 hook 该触发。Entity hooks（`on_entity_confirmed` / `on_entity_description_updated`）则在 `entities.resolve_entity` / `entities.apply_entity_clarification` / `update_entity_description` 提交后直接 `_fire_lifecycle(...)`，不走 session 队列——它们和 session 边界无关。

runtime 侧的 `RuntimeMemoryObserver`（住在 `src/echovessel/runtime/wiring/memory_observer.py`）实现了上述全部 lifecycle hook，把每条变成一条 SSE topic 广播给所有暴露 `push_sse` 的 channel——具体 topic 名见 `docs/zh/runtime.md § Cross-Channel SSE` 与 `docs/zh/channels.md § Web channel`。

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
register_observer(logger)
# 注册后所有 lifecycle hook 自动触发（包括 on_event_created 与
# on_thought_created）。on_message_ingested / on_core_block_appended
# 仍然只在 caller 显式传 observer=logger 时触发。
```

注册后，所有 lifecycle hook（`on_new_session_started` / `on_session_closed` / `on_mood_updated` / `on_event_created` / `on_thought_created` / `on_entity_confirmed` / `on_entity_description_updated`）都会自动触发——不需要每个 caller 都显式传 `observer=`。纯 per-write hook（`on_message_ingested` / `on_core_block_appended`）依然只在调用方把 `observer=...` 传进 `ingest_message` / `append_to_core_block` 时才触发。`on_event_created` / `on_thought_created` 是双触发的：lifecycle 注册表 fan-out **加上** `consolidate_session` / `import_content` 接受的 per-call `observer=` 参数（让导入 pipeline / 测试拿自己的回调）。结构化 subtyping 意味着你只需要实现你在乎的那些 hook。

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
