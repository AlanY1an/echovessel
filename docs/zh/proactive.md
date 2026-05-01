# 主动引擎(Proactive)

## Overview

真实的关系不是请求/响应式的。人会主动开口——朋友记得你说过周一有面试，周日晚上发一条消息问问；伴侣在考试当天的早晨发一句安静的关心。一个只会 *回应* 用户输入的 persona 会显得没有生命力：技术上在场、关系上缺席。**proactive** 模块就是 EchoVessel 用来补上这一块的子系统。它负责决定 persona 什么时候 **主动开口**，不需要任何用户输入作为触发——而且关键在于，它说的内容是用户真正提到过的事，锚定在用户正在朝向的某个未来时刻。

这条"锚"是把 EchoVessel proactive 跟那些按日程驱动的"数字陪伴产品"区分开的关键。后者挑一个时间点丢一条模板化的"早上好"上去；按日程驱动的 proactive 会显得机械，因为 persona 没有"为什么要说话"的理由——只是钟点到了。EchoVessel 永远不因为时钟说话。它说话是因为用户告诉过它的某件事(一场面试、一台手术、一篇论文 deadline、妈妈的体检)正在临近、正在发生、或者刚刚结束。**memory 是 persona 关心什么的真相源；proactive 是一个 reactive view，在 memory 给某个 event 设的"钟"敲响时醒来。**

这个形态够特别，值得明说。proactive 自己**没有**一张 follow-up state 表，**没有**一次 proactive 侧的 follow-up 检测 LLM 调用，**没有**一条 proactive 侧的轮询循环在扫日历。memory 的 Phase B 抽取(那次原本就要写 L3 events 的 LLM 调用)给每条 disclosure 顺便标注 `follow_up_at` / `follow_up_hint` / `advance_pre_hours` / `advance_post_hours`；proactive 订阅 memory 的 `on_event_created` lifecycle hook。当一个带 `follow_up_at` 的 event 落地时，proactive 给对应的 phase 窗口装一只 asyncio timer。timer 醒来的时候，5-gate policy 引擎问 proactive 唯一会问的那个问题——*在 quiet hours / forbidden topics / in-flight turns / rate limit / engagement score 这五条 gate 之下，persona 此刻应该说话吗？*——然后要么 fire 一条消息，要么写一条 suppress 记录到 audit trail。

每一次 gate 决策、**包括每一次决定保持沉默**，都会写入 `proactive_decisions`。生成一条主动消息的那次贵 LLM 调用是 proactive 做的 *最后一件* 事，不是第一件。所以最常见的情况(gate 拦下、没有话要说)几乎不花钱。运维侧能回答用户唯一真正会问的问题：*它为什么在那个时间说话？*——或者更常见的那个，*它为什么没说话？*

## Core Concepts

**Follow-up event(可跟进事件)。** 一行 `concept_nodes` (memory L3 event)，且 `follow_up_at` / `follow_up_hint` 非空。Phase B 抽取在 LLM 判断这条 disclosure 有 persona 自然会回头关心的"未来 arc"时打上这个标记：下周一的面试、三天后的手术、一篇正在进行的论文。纯过去时陈述("中午吃了三明治")让 `follow_up_at` 留空；用户显式表态"我自己处理 你别问"的事件也留空。

**Phase window(阶段窗口)。** FollowUpScheduler 的机械计算：把一个 `follow_up_at` 翻译成最多三个 fire 窗口——`pre`(铺垫期 · persona 开始关心)、`on`(当天)、`post`(回访 · persona 问问怎么样了)。phase 由 event 的 `advance_pre_hours` 和 `advance_post_hours` 决定：手术给 72h pre + 0h post(让用户休息)、面试给 24h pre + 24h post、随手提醒给 0h/0h(只 fire `on`)。phase 不落任何盘——每次都从 live event row 现算。

**Reminder request(提醒请求)。** Phase B 对"10 分钟提醒我" / "今晚 9 点叫我"这类话的特殊形态——`advance_pre_hours == 0` 且 `advance_post_hours == 0`。scheduler 在 `follow_up_at` 那一刻 fire 恰好一次 `on` phase，不 fire `pre`，不 fire `post`(reminder 没有"准备阶段"也没有"事后回访")。

**Policy gate(策略门控)。** policy 引擎里的一次单点检查，可以让一次 follow-up fire 被跳过。5 条 gate 按固定优先级跑，第一个命中的 gate 直接短路。每一次被 gate 拦下都会写出一个具名的 `suppress_reason` 落到 audit trail 里——`quiet_hours` / `forbidden_topics` / `in_flight_turn` / `rate_limit` / `low_engagement`。

**Smart cooldown(智能冷却)。** 当一次 fire 被 suppress，下一次能否重试取决于 *被哪条 gate* 拦下。`forbidden_topics` 永久关闭这个 event(写 `proactive_suppressed_at`)；`quiet_hours` 在窗口结束的瞬间允许立即重试(没有人造冷却时间)；`rate_limit` 和其他通用 gate 等 4 小时让 24h 滚动窗口自然清掉。要点不是纠缠——一次被 suppress 的 fire 应该在 gate 实际放行的那个物理理由上重试，而不是按一个任意的 timer。

**Supersede close(被替代关闭)。** 当用户报告 outcome("面试结束了 还行")，Phase B 抽出一条新的 event 并在 `superseded_event_ids` 里指回原 disclosure。memory 把旧 event 的 `superseded_by_id` 标到新 event 上。FollowUpScheduler 的 `_eligible` 检查过滤 `superseded_by_id IS NULL`，所以一旦用户自己 close 了这个 loop，后续的 pre / on / post 都不会再 fire。**没有任何独立的 close-detection LLM 调用**——同一套支持矛盾处理的 memory 机制，顺手承担了 proactive close。

**Audit trail(审计轨迹)。** 每一次 fire 和每一次 suppress 都写一行 `proactive_decisions`，含 `phase`、`action`(`fire` / `suppress`)、`suppress_reason` 和一个用于事后排查的 `gate_state_snapshot` JSON("是哪条 gate 说了不？当时它看到的状态是什么？")。这些记录同时是 policy 引擎自己的读源——rate limit 数行数，smart cooldown 读每个 `(event_id, phase)` 的最后一行。

**`PersonaView`。** runtime 注入到 scheduler 的"实时读取适配器"。它把 `voice_enabled` 和 `voice_id` 暴露成 `@property`，每次属性访问都从当前 runtime 上下文重新读一次值。当运维通过 persona 管理 API 切换 voice 开关时，*下一次* fire 立刻就能读到新值——不需要重启 scheduler，也不需要 reload hook。

**Delivery inheritance(投递继承)。** proactive 永远不自己决定"用 voice 还是 text"。它在发送时读取 `persona.voice_enabled`，直接继承这个答案。当 `voice_enabled == True` 并且 `voice_id` 已配置，它会调用 `VoiceService.generate_voice()` 生成可播放的音频工件；否则直接发纯文字。voice 路径的任何失败都会降级到 text——audit trail 记下 `voice_error`，但 channel 发送永远还有 payload。

## Architecture

### 在五模块栈里的位置

```
               Layer 4   runtime
                         │
                         ▼
               Layer 3   channels   proactive      ◄── 本模块
                            │          │
                            ▼          ▼
               Layer 2    memory     voice
                            │          │
                            ▼          ▼
               Layer 1              core
```

proactive 是 Layer 3 模块，和 `channels` 并列。它的 import 预算被刻意收得很小：从 `memory` 只拿读能力加一个用来记录 persona 发出消息的 `ingest_message` 写入入口，从 `voice` 拿一个 `VoiceService` 的鸭子类型视图，从 `channels.base` 只拿 Protocol(绝不拿具体的 channel 实现)，再加上 `core` 的数据类型。它从不被 memory 或 voice 反向 import——依赖箭头严格向下。proactive 内部子包的依赖顺序是 `execution → engines → core`，由 `import-linter` 锁死。

runtime 在 proactive 之上，daemon 启动时构造它并注入它全部的依赖：一个 `MemoryApi` facade、一个 `ChannelRegistryApi`、runtime 自己构造好的 LLM callable `proactive_fn`、一个 `PersonaView`、一个可选的 `VoiceService`，以及一个 `is_turn_in_flight` 谓词——这个谓词是 runtime 侧一个闭包，持有对 runtime channel 注册表的引用。runtime 同时把 `MemoryFollowUpObserver` 注册到 memory 的生命周期 hook 上，让每一条新抽出的、带 `follow_up_at` 的 event 都能到达 scheduler。

### Layer 1 · PROFILE

`persona_profile` 是 persona 的关系策略面，三个 derive 出来的字段直接驱动 proactive：

- **`style_summary`** — 一段对 persona 说话风格的简短散文锚，喂给 generator prompt，让发出去的消息听起来像 persona 而不是泛 LLM。
- **`forbidden_topics`** — persona 拒绝主动提起的子串清单。匹配的对象是候选 event 的 `follow_up_hint`(不是完整 description——hint 才是 proactive 侧的锚)。命中即对该 event 永久关闭 proactive。
- **`quiet_hours`** — `[start_hour, end_hour]` 本地时间，跨午夜可处理。和 runtime 的 reactive reply 路径独立——quiet hours 期间用户主动发话仍然会被回复。

profile 由 `proactive/engines/profile_derivation.py` 在低频节奏上从 L1 core blocks + 近期 L4 thoughts 推出，但读侧是实时的：admin 侧任何编辑通过 admin API 后下一次 fire 就生效。

### Layer 2 · DECISION · 5 条 gate

当 FollowUpScheduler 的 timer 为某个 `(event_id, phase)` 触发时，它把一个 `FOLLOW_UP_DUE` 事件 push 进 proactive scheduler 的队列。proactive scheduler 排干队列然后调用 `PolicyEngine.evaluate(events, ...)`，引擎按固定优先级走下面这个阶梯。第一个命中的 gate 短路其余：

```
  ┌───────────────────────────────────────────────────────────┐
  │  1.  quiet_hours          按本地小时的时段检查           │
  │      命中  ─────────►     skip(quiet_hours)               │
  ├───────────────────────────────────────────────────────────┤
  │  2.  forbidden_topics     hint vs profile 子串清单        │
  │      命中  ─────────►     skip(forbidden_topics)          │
  ├───────────────────────────────────────────────────────────┤
  │  3.  in_flight_turn       不打断进行中的对话回合          │
  │      命中  ─────────►     skip(in_flight_turn)            │
  ├───────────────────────────────────────────────────────────┤
  │  4.  rate_limit           24h 滚动窗口最多 3 条           │
  │      命中  ─────────►     skip(rate_limit)                │
  ├───────────────────────────────────────────────────────────┤
  │  5.  engagement_score     BA 偶联回报式软性抑制器         │
  │      命中  ─────────►     skip(low_engagement)            │
  │      放行  ─────────►     action = fire                   │
  └───────────────────────────────────────────────────────────┘
```

每条 gate 在自己这个位置上各有理由：

1. **Quiet hours** 最便宜、最绝对。就是对 `now.hour` 做一次算术。如果用户正在睡觉，其它一切都无关紧要。
2. **Forbidden topics** 把候选 event 的 `follow_up_hint` 跟 `persona_profile.forbidden_topics` 做大小写不敏感的子串比对。命中在这里被当作 *永久* 关闭：smart cooldown 层把 event 的 `proactive_suppressed_at` 设成 now，scheduler 永远不再为它 arm timer。
3. **In-flight turn** 是唯一一条语义安全 gate。runtime 注入一个谓词闭包，扫自己的 channel 注册表看有没有任何 channel 的 `in_flight_turn_id` 非空。只要有任何一个 channel 在进行回合，proactive 就退避——没有任何合理场景需要"允许 proactive 打断一个 live turn"。
4. **Rate limit** 是一条对 `proactive_decisions` 表做的 24h 滚动计数(`max_per_24h`，默认 3)。更细粒度的"最小发送间隔"节流被刻意砍掉了：和日度上限功能重复，UX 没有增益。
5. **Engagement score** 是来自 BA contingent-reward 回路的软性抑制器(住在 `proactive_state`)。每一次未被回复的 fire 衰减分数；用户回复一次重建。低于 pass 阈值这条 gate fire `low_engagement`——persona 一直在对沉默说话，应该退后。reminder request 这种高置信度场景旁路掉这条 gate，避免一句"10 分钟提醒我吃药"被 engagement 分数一脚踢掉。

那次贵的 LLM 调用(`generator.generate(...)` 写出最终消息)只在所有 5 条 gate 都放行之后才跑。

### Trigger · 事件驱动 · `on_event_created` + asyncio timer

proactive 自己**没有任何后台时间循环**。没有按秒 tick，没有按分钟 scanner，没有 consolidate-worker 钩子。trigger 表面只有一个 asyncio 对象——`FollowUpScheduler`——把 memory 生命周期 hook 翻译成 per-event 的唤醒。

```
     memory.consolidate Phase B 抽出一行 ConceptNode
                      │
                      │  follow_up_at != None ?
                      ▼
            on_event_created(event)
                      │
                      ▼
       MemoryFollowUpObserver.on_event_created
                      │
                      ▼
          FollowUpScheduler.on_event_created
                      │
                      │  计算最早可触发的 phase 唤醒
                      │  (pre / on / post / check_N)
                      ▼
       loop.call_later(delay, _fire_follow_up_due)
                      │
                      ▼  (timer 醒来)
       FOLLOW_UP_DUE event → proactive scheduler 队列
                      │
                      ▼
          5-gate evaluate → fire | suppress
                      │
                      ▼
       audit · 然后给剩余 phase 装下一个 timer
```

由这个形态衍生出三件事：

- **冷启动**由 `FollowUpScheduler.start()` 处理：daemon 起来时 scheduler 跑一次查询 `concept_nodes WHERE follow_up_at IS NOT NULL AND superseded_by_id IS NULL AND deleted_at IS NULL AND proactive_suppressed_at IS NULL`，给每条算下一个 phase，arm timer。这次 reload 一个进程生命周期里只发生一次。
- **没有任何轮询**会去扫表找"到点的 event"。scheduler 在启动后唯一去查数据库的理由就是 timer callback 里的 `_eligible` 检查(再次确认 supersede / suppress / soft-delete 状态没在 timer arm 之后变过)以及 `_smart_cooldown_passed` 的 audit 查询。
- **每个 `(event_id, phase)` 一只 timer**。一次 fire(或一次非永久 suppress 配 cooldown)之后，scheduler 给剩下的 phase arm 下一只 timer。asyncio loop 拥有调度，scheduler 拥有"把 event state 翻译成 delay"。

### Phase 窗口

对于带 `event_time_end` 的 event(日期型——面试、手术、考试):

```
  target = follow_up_at
  pre  : [target - advance_pre_hours,  target - 1h)
  on   : [target - 1h,                 target + 1h)
  post : [target + advance_post_hours, target + 2*advance_post_hours)
```

`advance_post_hours == 0` 是一个刻意的信号：这件事之后用户应该不被打扰(术后休养、回家后"她回来了别管她")。scheduler 直接跳过 `post` phase。reminder request(`advance_pre_hours == 0 AND advance_post_hours == 0`)塌缩成"在 `target` 那一刻 fire 一次 `on`"——不 fire `pre`，不 fire `post`。

对于不带 `event_time_end` 的 event(ongoing arc、unresolved 情绪线):

```
  follow_up_at 充当回访时间
  第一次 fire = check_1 在 follow_up_at
  之后        = check_2 / check_3 / …  (递增编号)
```

递增编号让 generator 知道何时退场——`check_1` 轻轻打开，`check_3` 是放弃话题前最后一次温和试探。

### Resolution close · supersede 链

关闭一个 follow-up loop 的机制在 proactive 这一侧零代码。当用户报告 outcome，Phase B 把这条报告抽成一行新的 `concept_nodes`，并把原 disclosure 的 id 写进 `superseded_event_ids`。memory 的 consolidate 路径把旧 event 的 `superseded_by_id` 指向新 event。FollowUpScheduler 的 `_eligible` 检查(每次 timer 唤醒发出 `FOLLOW_UP_DUE` 之前都跑)实时重读 event 行，过滤 `superseded_by_id IS NULL`。一只为已被 supersede 的 event 唤醒的 timer 是 no-op——不 fire，也不写 audit。

用户侧"别再提这件事"也走同一条机制。admin UI 的 `PATCH /api/admin/memory/events/{id}` body `{proactive_suppressed_at: now}` 由同一个 `_eligible` 检查读到；下一次 timer 唤醒看到它就 drop。

### Audit · `proactive_decisions`

每一次 fire 和每一次 suppress 写一行：

| 列 | 含义 |
|---|---|
| `id` | UUID |
| `timestamp` | 评估这次决策的时刻 |
| `persona_id` / `user_id` | 这次决策属于谁 |
| `trigger_type` | `follow_up`(v3 只有一条 fire 通道) |
| `source_event_id` | 底层 `concept_nodes.id` |
| `phase` | `pre` / `on` / `post` / `check_1` / `check_2` / … / NULL |
| `action` | `fire` 或 `suppress` |
| `suppress_reason` | `quiet_hours` / `forbidden_topics` / `in_flight_turn` / `rate_limit` / `low_engagement` / fire 时为 NULL |
| `gate_state_snapshot` | 评估时刻每条 gate 输入状态的 JSON 快照 |
| `send_ok` / `send_error` | channel 发送结果(仅 fire 路径填) |
| `ingest_message_id` | persona 那条出向消息的 L2 row id |
| `delivery` | `text` / `voice_neutral` |
| `voice_used` / `voice_error` | voice 路径结果(仅 fire 路径填) |
| `llm_latency_ms` | generator LLM 调用耗时 |

`gate_state_snapshot` 是这套系统能 debug 的关键。当用户问"她今天早上为什么没问我？"，行里写着 gate 阶梯当时看到的每一个变量——quiet_hours 还没到 7 点、discord 那条 channel `in_flight_turn = true`、过去 24h rate_limit 计数 4。audit 行是"发生了什么、为什么"的唯一真相源。

两阶段写：骨架行(timestamp / trigger / phase / action / reason)在 LLM 调用前 commit，这样发送途中崩溃也留下证据。channel send 完成后 `update_latest` 把 outcome 字段(`send_ok`、`ingest_message_id`、`delivery`、`voice_used`、`voice_error`、`llm_latency_ms`)补丁到同一行。

### 发送流程 · 先 ingest 再 send 的不变量

policy 返回 `action = fire` 之后:

```
       generator.generate(decision)                  prompt 装配 · LLM 调用
              │
              ▼
       delivery.pick_channel(...)                    用户最近活跃的 channel · 否则 'web'
              │
              ▼
       memory.ingest_message(PERSONA, text)          ◄── 永远先 ingest 再 send
              │                                         (拿到 message_id)
              ▼
       delivery.prepare_voice(                       voice 开启则生成音频 · 否则 text
           text, message_id,
           persona.voice_enabled,
           persona.voice_id,
       )
              │
              ▼
       channel.send(text)                            可能失败；memory 已经有记录
              │
              ▼
       audit.update_latest(                          两阶段写收尾
           send_ok, send_error,
           ingest_message_id, delivery,
           voice_used, voice_error,
           llm_latency_ms,
       )
```

不变量是：**`memory.ingest_message` 必须在 `channel.send` 之前跑完，也必须在 `VoiceService.generate_voice` 之前跑完。** 有两条理由。

第一，如果 channel 发送失败——网络掉线、传输错误、对端拒收——persona 的 memory 里仍然有关于这句话的记录。内部状态保持对自身一致，即使外部世界没跟上。反过来(先 send、成功后再 ingest)会让 persona 的记忆与它实际发出去的东西悄无声息地分叉。

第二，voice 缓存以 `message_id` 为键——那个 id 是 `ingest_message` 返回的 L2 row id。voice 生成必须在 ingest *之后*，否则根本没有一个稳定的 id 能拿来缓存音频工件。这同时也是 voice 幂等的来源：重发同一个 `message_id` 命中磁盘缓存，不会重复向 TTS provider 计费。

### Generator prompt · `follow_up_hint` + phase guidance

出向消息由一次 LLM 调用产出。输入：

- `persona_profile.style_summary` —— 风格
- 被 follow up 的那条 event 的完整 `ConceptNode` 行 —— 上下文
- `phase` —— `pre` / `on` / `post` / `check_N`
- `PHASE_GUIDANCE[phase]` —— "怎么开口"的简短指令

`PHASE_GUIDANCE` 住在 `proactive/engines/generator.py`，是一个以 phase 为 key 的扁平 dict。`pre` 指令告诉模型问准备情况、不预设结果；`on` 指令要简短温暖；`post` 指令询问结果但避免预判好坏；`check_N` 指令递进退场。generator 永远不自己造锚——它就用 `event.follow_up_hint`(Phase B 产出的 5-15 字锚，比如 "面试结果" 或 "妈体检")作为消息谈论的话题。

### Delivery inheritance

scheduler 在调用 `prepare_voice` 之前现场读取 `persona.voice_enabled` 和 `persona.voice_id`。如果运维在两次 fire 之间把 voice 切掉，下一次 fire 的属性访问立刻看到新值。`DeliveryRouter.prepare_voice` 决定最终 delivery：

| 条件                                        | Delivery        |
|---------------------------------------------|-----------------|
| `persona.voice_enabled == False`            | `text`          |
| `voice_service is None`                     | `text`          |
| `persona.voice_id` 为 `None` 或空字符串     | `text`          |
| `generate_voice(...)` 抛出任何错误          | `text`(降级；`voice_error` 记录原因) |
| `generate_voice(...)` 成功返回              | `voice_neutral` |

`prepare_voice` 永远不会抛。任何 voice 侧的失败——瞬时 provider 故障、永久配置错误、预算用尽、未预期的异常——都会被解析成一次文字回退，channel 发送永远至少还有一段文字可以推。失败原因记在 `voice_error`。

## Admin UI

`/admin/proactive` 把 proactive 的运维表面暴露给操作者：

- **Active events** —— `GET /api/admin/proactive/events` 返回所有 `concept_nodes` 行：`follow_up_at` 非空、未被 supersede、未被 suppress、未被 soft-delete。前端按 phase 窗口(`pre` / `on` / `post` / `check_N`) 分组，让运维看到 "persona 即将提起什么、什么时候提"。
- **Decision history** —— `GET /api/admin/proactive/decisions` 返回近期 `proactive_decisions` 行，含 `phase`、`action`、`suppress_reason`，以及 `action = fire` 时的出向消息正文。这个视图回答"她有没有 fire？为什么有/为什么没有？"。
- **Suppress** —— `PATCH /api/admin/memory/events/{id}` body `{proactive_suppressed_at: now}` 永久关闭某个 event 的 proactive。scheduler 不再为它 arm timer；已经 arm 的 timer 醒来时 no-op。event 本身仍在 memory 里——reactive reply 路径仍能召回。

## 配置

所有可调项住在 `[proactive]` 和 `[memory]` 两个 TOML 段，daemon 启动时被解析成 Pydantic 模型。

```toml
[proactive]
enabled                          = true   # 主开关
max_per_24h                      = 3      # rate-limit 上限(24h 滚动)

# Quiet hours(本地时间 24h 制；start > end 时窗口跨午夜)
quiet_hours_start                = 23
quiet_hours_end                  = 7

# Engagement gate
engagement_pass_threshold        = 0.4    # 低于这个分数 low_engagement fire

# Smart cooldown(被非永久 suppress 之后)
default_cooldown_hours           = 4

# 停机
stop_grace_seconds               = 10     # stop() 等待当前 fire 的宽限期

[memory]
session_idle_minutes             = 10     # 早期版本是 30 · 调低让短 reminder
                                          # 在 session close 后 10 min 内 fire
```

两条运维提示：

- **配置只在 scheduler 构造时读一次。** proactive 不监听 TOML 文件，也不响应 SIGHUP。要让新值生效，重启 daemon。在一个 fire 正在跑的时候热更新 policy 引擎，比它带来的收益复杂得多。
- **`session_idle_minutes`** 是 memory 侧旋钮，但它直接限定了 proactive 对短 reminder 的最低延迟。"8 分钟提醒我"这种请求在 session 关之前 fire 不出来(Phase B 还没抽 event)；`session_idle_minutes = 10` 时这次 fire 最多比用户期望时刻晚 10 分钟。再往下调允许，但要权衡 reflection 成本(每次 session close 都可能触发抽取)。

## 已知限制

v3 把 reminder request 当作 memory disclosure 的一种特殊形态处理(LLM 在 Phase B 给它打 `advance_pre_hours = 0` 和 `advance_post_hours = 0`)。这种组合诚实地承认它今天做不好的几件事：

1. **比 `session_idle_minutes` 还短的 reminder。** 用户说"3 分钟后提醒我"时，session 还没空闲到关。Phase B 还没跑、event 还不存在、scheduler 没东西可 arm。fire 发生在 session 关之后——最多晚 `session_idle_minutes`，但对很短的窗口永远是迟到的。
2. **对话中持续 reminder。** 用户每隔几分钟就说一句("10 分钟提醒我哈，对了，再说……")，session 永远不进入 idle。session 不关，Phase B 不跑，reminder 等着。等用户最终空闲 `session_idle_minutes` 之后才 fire，可能比预期晚很多。
3. **跨 idle 边界的 reminder。** 8:55pm 说"今晚 9 点叫我"通常 work(session 在 9:05pm 关，scheduler arm 一只 timer 指向 9pm 立刻 fire)；但用户在 9pm 之后还在打字的边界情况漏。

这些是 v3 接受的 trade-off，不是 bug。正解是一个 `set_reminder` tool，让 LLM 在 turn 内识别 reminder request 并直接写入 `concept_nodes(follow_up_at)`，绕过 session close 等待。这需要一套 tool-execution 架构(LLM provider 抽象层、tool result 反馈循环、跨 channel tool dispatch)，是项目级的改动——推到 v4。

## 设计取舍 · 为什么 follow-up state 住在 memory

这个模块更显而易见的形态会是：proactive 自己持有一张 `follow_up_threads` 表，由一次 proactive 侧的 LLM 调用扫近期 events、判断哪些值得 follow up；再加一次 close-detection LLM 调用监视 outcome 报告、关闭 thread。这个形态结构上很有吸引力——proactive 自管 state，memory 不需要知道 follow-up 的存在。

v3 把这个形态塌进 memory Phase B 的理由不是审美，是运维。Phase B 本来就在跑一次 LLM 调用扫 session 消息抽 events，包含 emotional impact、relational tags、用于矛盾处理的 `superseded_event_ids`。再加一次 LLM 调用做 follow-up 检测会读同一份输入消息、产出同一组判断的严格子集(这事是不是未来 arc？锚在哪？)。两次调用做重复工，更糟的是它们能不一致——proactive 留着一条 follow-up thread 指向 memory 已经标 supersede 的 event，反过来也成立。

塌缩之后保住了 memory 的不变量：**memory 是"发生了什么 / 接下来期待什么"的唯一真相源。** proactive 是一个 reactive view，按 memory 的 annotation 装 timer，并通过 5 条 gate 决定某次 fire 该不该出门。这种和项目"memory 是共享底座"原则的对齐，是这套架构能撑起 local-first bias 的根本：每次 session close 一次 LLM 调用、一条 supersede 链同时承担矛盾处理和 follow-up close、一行 row per event。

## How to Extend

### 1. 加一条自定义 suppress reason

suppress reason 是 `proactive/core/base.py` 里的字符串常量，原样写入 `proactive_decisions.suppress_reason`。加一条新 gate 是四步改动：

1. 在已有 reason 旁边加常量。
2. 在 `PolicyEngine.evaluate()` 里把 gate 插入合适位置——位置很重要：便宜 / 绝对的 gate 在前，软 gate 在后。
3. 在 `FollowUpScheduler._smart_cooldown_passed` 里决定 smart-cooldown 语义：永久关闭(写 `proactive_suppressed_at`) / 立即重试 / 通用 4h cooldown。
4. 在 admin UI 的 decision history 过滤器里加上新 reason，让运维能在上下文里看到。

### 2. 挂一个自定义 audit sink

默认 sink 写入 `proactive_decisions`。`AuditSink` Protocol 来自 `proactive/core/base.py`：

```python
class AuditSink(Protocol):
    def record(self, decision: ProactiveDecision) -> None: ...
    def update_latest(self, decision_id: str, **outcome_fields) -> None: ...
    def recent_sends(self, *, last_n: int) -> list[ProactiveDecision]: ...
    def count_sends_in_last_24h(self, *, now: datetime) -> int: ...
```

自定义 sink 可以 tee 到 JSONL、推 Prometheus、流到第三方可观测平台。两条规则：`record()` 永远不能抛(scheduler 的 tick 路径没有给爆炸 sink 留恢复路径)；`recent_sends` / `count_sends_in_last_24h` 是 policy 引擎的读侧——把它们 stub 成 `[]` / `0` 等于直接禁用了 rate-limit 和 engagement-history。

### 3. 给一类新的 event 调 phase 窗口

`advance_pre_hours` 和 `advance_post_hours` 是 Phase B 产出的，不是 proactive 产出的。给一类 event 调窗口(比如"每个生日给 6h pre 和 0h post")是 memory 侧 prompt 的事——改 `prompts/extraction.py` 的 PART F。proactive scheduler 在 Phase B 写出的下一条 event 上立刻拿到新窗口。proactive 这一侧没有任何标定旋钮——这就是这套架构的要点。

更完整的参考请直接看 `src/echovessel/proactive/`——每个文件都有详尽 docstring；policy 引擎的 gate 顺序在 `tests/proactive/` 下的单元测试里被锁住。memory PART F prompt 和它写入的 6 个 `concept_nodes` 列详见 [`memory.md`](./memory.md)。
