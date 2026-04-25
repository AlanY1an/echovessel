# 主动引擎(Proactive)

## Overview

真实的关系不是请求/响应式的。人会主动开口——朋友想起你上周说过的一句话,隔天来问一声近况;伴侣注意到你安静得反常,轻轻发一条消息。一个只会 *回应* 用户输入的 persona 会显得没有生命力:技术上在场、关系上缺席。**proactive** 模块就是 EchoVessel 用来补上这一块的子系统。它负责决定 persona 什么时候 **主动开口**,不需要任何用户输入作为触发。

那 proactive 为什么不是一个日程表?因为按日程触发的"数字陪伴产品"挑一个时间点、丢一条模板化的"早上好!",瞬间就机械化了。它们既不能根据 *为什么* 此刻应该关心用户来做出反应,也不会在"此刻应该安静"时保持沉默。EchoVessel 的 proactive 模块因此被设计成 **事件驱动 + 周期性 tick** 的混合:memory 子系统把生命周期事件(抽出了一个高情绪冲击的事件、某个会话结束、关系状态变化)推到一个有界队列里;一个后台 tick 循环——默认 60 秒一次——醒来、清空队列,然后向 policy 引擎问一个单一问题:*基于刚才发生的事,persona 现在应该说点什么吗?要说,说什么?*

每一个决策,**包括每一次决定保持沉默**,都会走完一整套 policy gate。这些 gate 的唯一职责是保护用户——不打扰、不纠缠、不在半夜把人吵醒。gate 先跑,消息生成只在所有 gate 都放行之后才发生。这个顺序很重要:写主动消息的那次 LLM 调用是最贵的一步,它被放在 *最后*,而不是最前面。于是最常见的情况(gate 拦住、没有话要说)几乎不花钱。每一次 gate 决策都被写入一个 audit trail,这样运维能回答用户唯一真正会问的问题:*它为什么在那个时间说话?* ——或者更常见的那个,*它为什么没说话?*

## Core Concepts

**Policy gate(策略门控)。** policy 引擎里的一次单点检查,可以让一次潜在的主动发送被 skip。gate 按固定优先级顺序跑,第一个命中的 gate 直接短路:后面的 gate 不再评估,消息生成也不会发生。每一次被 gate 拦下都会写出一个具名的 `SkipReason`,落到 audit trail 里——比如 `quiet_hours` / `rate_limited` / `in_flight_turn` / `low_presence_mode`。

**Relationship trigger(关系触发)。** gate 的正向对偶。trigger 回答的问题是"什么会让 persona *想要* 开口?"。MVP 发两种:`HIGH_EMOTIONAL_EVENT`(一个 memory 事件的绝对情绪冲击达到或超过 shock 阈值)和 `LONG_SILENCE`(用户最近一条消息的时间早于配置里的 `long_silence_hours`)。两者都在 **所有 gate 都放行之后** 才评估,所以 trigger 永远不会绕过 gate——quiet hours 始终赢。

**Cold user(冷用户)。** 给新用户(或者已经停止响应的用户)的保护模式。如果 persona 连续发了 N 条主动消息、每一条都在响应窗口之内没有收到回复,proactive 就进入冷用户 skip 状态,在用户主动开口之前不再继续发。这条规则防住了主动子系统最糟的失败模式:一个 persona 持续对着一个早已离开的人说话。

**In-flight turn(进行中的对话回合)。** runtime 在某个 channel 上接收了一条用户消息、此刻正在生成 persona 的反应式回复——这种状态就叫 in-flight。如果允许 proactive 在这个窗口里说话,对外的输出顺序就会变成 `[用户提问] → [proactive 插话] → [真正的回复]`,这是一个竞态级别的 UX 缺陷。`no_in_flight_turn` gate 禁止这种情况。它没有配置项:没有任何合理场景会需要"允许 proactive 打断一个进行中的回合"。

**Audit trail(审计轨迹)。** 每一次 `PolicyEngine.evaluate()` 调用都产出恰好一条 `ProactiveDecision` 记录,无论结果是 `send` 还是 `skip`。记录默认写入 `~/.echovessel/logs/proactive-YYYY-MM-DD.jsonl`。scheduler 执行发送时用的是一个两阶段写:先写骨架行(这样即使发送途中崩溃也留下证据),再把发送结束后的 outcome 字段(`send_ok` / `ingest_message_id` / `delivery` / `voice_used` / `voice_error` / `llm_latency_ms`)补丁回去。

**`PersonaView`。** runtime 注入到 scheduler 里的一个"实时读取适配器"。它把 `voice_enabled` 和 `voice_id` 暴露成 `@property`,每次属性访问都会从当前 runtime 上下文重新读一次值。这样一来,当运维通过 persona 管理 API 切换 voice 开关时,*下一次* tick 立刻就能读到新值——不需要重启 scheduler,也不需要 reload 钩子。proactive 是读者,runtime 是写者,中间这个适配器让二者解耦。

**Delivery inheritance(投递继承)。** proactive 永远不自己决定"用 voice 还是 text"。它在发送时读取 `persona.voice_enabled`,直接继承这个答案。当 `voice_enabled == True` 并且 `voice_id` 已配置,它会调用 `VoiceService.generate_voice()` 生成可播放的音频工件;否则直接发纯文字。这是投递决策的唯一来源——proactive 这一侧没有另一个开关。

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

proactive 是 Layer 3 模块,和 `channels` 并列。它的 import 预算被刻意收得很小:从 `memory` 只拿读能力加一个用来记录 persona 发出消息的 `ingest_message` 写入入口,从 `voice` 拿一个 `VoiceService` 的鸭子类型视图,从 `channels.base` 只拿 Protocol(绝不拿具体的 channel 实现),再加上 `core` 的数据类型。它从不被 memory 或 voice 反向 import——依赖箭头严格向下。

runtime 在 proactive 之上,通过 `build_proactive_scheduler(...)` 在 daemon 启动时构造它,注入它全部的依赖:一个 `MemoryApi` facade、一个 `ChannelRegistryApi`、runtime 自己构造好的 LLM callable `proactive_fn`、一个 `PersonaView`、一个可选的 `VoiceService`,以及一个 `is_turn_in_flight` 谓词——这个谓词是 runtime 侧一个闭包,持有对 runtime channel 注册表的引用。

### Policy gate 的顺序

tick 循环醒来、清空队列、调用 `PolicyEngine.evaluate(events, ...)` 之后,引擎按固定优先级走下面这个阶梯。第一个命中的 gate 会短路剩下的全部:

```
  ┌─────────────────────────────────────────────────────┐
  │  1.  quiet hours        按本地小时的时段检查        │
  │      命中  ─────────►   skip(quiet_hours)           │
  ├─────────────────────────────────────────────────────┤
  │  2.  cold user          新用户 / 不回复用户保护     │
  │      命中  ─────────►   skip(low_presence_mode)     │
  ├─────────────────────────────────────────────────────┤
  │  3.  rate limit         24h 滚动窗口最多几条        │
  │      命中  ─────────►   skip(rate_limited)          │
  ├─────────────────────────────────────────────────────┤
  │  4.  no in-flight turn  不打断进行中的对话回合      │
  │      命中  ─────────►   skip(in_flight_turn)        │
  ├─────────────────────────────────────────────────────┤
  │  5.  trigger match      有任何注册的 trigger 命中?  │
  │      无    ─────────►   skip(no_trigger_match)      │
  │      命中  ─────────►   action = send               │
  └─────────────────────────────────────────────────────┘
```

每条 gate 之所以放在自己这个位置上,都有具体的理由:

1. **Quiet hours** 最便宜、也最绝对。它就是对 `now.hour` 做一次算术。如果用户正在睡觉,其他一切都无关紧要。
2. **Cold user** 是对 audit trail 的一次读取——引擎问自己"最近 N 条主动发送里,有没有任何一条在响应窗口之内收到了用户回复?"。如果一条都没有,这个用户处于冷状态,proactive 退后。
3. **Rate limit** 是对 audit trail 的一次粗粒度"最近 24 小时发了几次?"读取。MVP 只有一个日度上限(`max_per_24h`,默认 3)。更细粒度的"最小发送间隔"节流被刻意砍掉了:它对 UX 没有增益,却和日度上限功能重复,只会增加配置表面。
4. **No in-flight turn** 是唯一一条语义安全 gate。runtime 注入一个谓词闭包,这个闭包会扫描它自己的 channel 注册表,看有没有任何一个 channel 的 `in_flight_turn_id` 非 `None`。只要有任何一个 channel 处于 in-flight,proactive 就退避一次。如果谓词没有被注入(比如老版本的 runtime、或单元测试),这条 gate 是放行态——从不拦截,这符合规格里"没有 channel 可读 → 没有 in-flight turn"的约定。
5. **Trigger match** 是最后一步。它把被排干的事件批次走一遍,先找 `HIGH_EMOTIONAL_EVENT` 的匹配,再找 `LONG_SILENCE`。如果都没有,最终决策是 `skip(no_trigger_match)`——这是一个完全正常的结果,只是意味着"此刻没有什么值得说的"。

### Tick 循环

```
┌─────────────────────────────────────────────────────────────┐
│  asyncio 后台任务:proactive-scheduler                      │
└─────────────────────────────────────────────────────────────┘
        │
        │   每隔 tick_interval_seconds(默认 60)
        ▼
┌─────────────────────────────────────────────────────────────┐
│  tick_once()                                                │
│    1. 自己往队列 push 一个心跳 TICK 事件                    │
│    2. drain 队列                                            │
│    3. policy.evaluate(events) → ProactiveDecision           │
│    4. audit.record(decision)          ◄── 永远写,skip 也写 │
│    5. if action == send: _handle_send_action(...)           │
└─────────────────────────────────────────────────────────────┘
```

整个循环是一个单一的 asyncio 任务。memory 的 observer 回调、runtime 的 turn-completed 钩子,都通过 `scheduler.notify(event)` 把事件推入队列;这个方法是非阻塞的,可以从任何 async 或 sync 上下文安全调用。溢出由队列自己处理:当 `max_events_in_queue` 达到上限,队列会丢掉最老的 non-critical 事件,内部溢出计数器自增,下一次 tick 会写一条 `trigger = queue_overflow` 的元审计决策,这样运维能在 audit 文件里看到丢包。

### 发送流程与"先 ingest 再 send"的顺序不变量

当 policy 返回 `action = send`,scheduler 接手:

```
       generator.generate(decision)                  构造 snapshot、调 LLM
              │
              ▼
       delivery.pick_channel(...)                    用户最近活跃的 channel,否则 'web'
              │
              ▼
       memory.ingest_message(PERSONA, text)          ◄── 永远先 ingest,再 send
              │                                         (拿到 message_id)
              ▼
       delivery.prepare_voice(                       voice 开启则生成音频,否则 text
           text, message_id,
           persona.voice_enabled,
           persona.voice_id,
       )
              │
              ▼
       channel.send(text)                            可能失败;memory 已经有记录
              │
              ▼
       audit.update_latest(                          两阶段写收尾
           send_ok, send_error,
           ingest_message_id, delivery,
           voice_used, voice_error,
           llm_latency_ms,
       )
```

不变量是:**`memory.ingest_message` 必须在 `channel.send` 之前跑完,也必须在 `VoiceService.generate_voice` 之前跑完。** 有两条理由。

第一,如果 channel 发送失败——网络掉线、传输错误、对端拒收——persona 的 memory 里仍然有关于这句话的记录。内部状态保持对自身一致,即使外部世界没跟上。反过来(先 send、成功后再 ingest)会让 persona 的记忆与它实际发出去的东西悄无声息地分叉,这比"memory 和外线有差异"糟得多。

第二,voice 缓存是以 `message_id` 为键的——那个 id 是 `ingest_message` 返回的 L2 行 id。voice 生成必须在 ingest *之后*,否则根本没有一个稳定的 id 能拿来缓存音频工件。这同时也是 voice 幂等的来源:重发同一个 `message_id` 会命中磁盘缓存,不会重复向 TTS provider 计费。

### Delivery inheritance

scheduler 会在调用 `prepare_voice` 之前现场读取 `persona.voice_enabled` 和 `persona.voice_id`。如果运维在 tick N 和 tick N+1 之间把 voice 切掉,tick N+1 的下一次属性访问就能看到新值。之后 `DeliveryRouter.prepare_voice` 决定最终 delivery:

| 条件                                        | Delivery        |
|---------------------------------------------|-----------------|
| `persona.voice_enabled == False`            | `text`          |
| `voice_service is None`                     | `text`          |
| `persona.voice_id` 为 `None` 或空字符串     | `text`          |
| `generate_voice(...)` 抛出任何错误          | `text`(降级;`voice_error` 记录原因) |
| `generate_voice(...)` 成功返回              | `voice_neutral` |

`prepare_voice` 永远不会抛。任何 voice 侧的失败——瞬时 provider 故障、永久配置错误、预算用尽、未预期的异常——都会被解析成一次文字回退,这样 channel 发送永远至少还有一段文字可以推。失败原因记在 audit trail 的 `voice_error` 字段里。

### 与 slow_tick 的关系（v1 暂不集成）

memory 模块里跑着 slow_tick reflection phase（详见 [`memory.md` § Slow_tick consolidate phase](./memory.md#slow_tick-consolidate-phase-l4-forward-looking)）。slow_tick 在 session 之间跑,产 `type='thought'` 节点和 `type='expectation'` 子类节点（带 `event_time_end` 作为 due_at）。expectation 表达的语义是"我预期 user 会在某个时间窗里做某件事"——这看起来天然就是 proactive 的触发源:expectation 临近 due_at 时由 proactive 主动起话头("你之前说今晚会更新一下,有进展吗？")。

**当前 proactive 没有把这条线接上**——proactive 当前只读 L3 events / L4 thoughts / RecallMessages,不读 expectation 节点,不会因为 expectation 即将到期而 fire 主动消息。理由是:

- expectation 是 slow_tick 的产物,目前还没有足够的 dogfood 数据校准 LLM 对 expectation 的命中率;先让 expectation 在 fast loop 里被 user 自然回复 fulfill / expire,等到 1-2 周线下数据后再决定要不要把它接成 proactive trigger。
- proactive 的 Send-N-per-window 闸门已经够紧;再多塞一个 trigger source 容易越界。
- expectation 的 due_at 精度只到 day,proactive tick 是 60s——粗细对不上;接的话还得引入 day-bucket 节流逻辑,工作量不在这次 ship 范围。

v2 计划:在 `proactive/triggers/` 下加一个新 trigger（类似 `relationship_anniversary`）读 `concept_nodes WHERE type='expectation' AND event_time_end BETWEEN now AND now + 24h AND superseded_by_id IS NULL`,通过现有 policy gate 链(quiet hours / 24h cap / cool-down 等)再决定是否真的发。今天读这一段的人:**don't add it yet**——等 dogfood 数据。

memory 写 expectation 时触发的 `on_thought_created` observer hook 会同时被 proactive 看见,但 v1 proactive 的实现就是个 no-op(收下不动作)。

## How to Extend

### 1. 加一条新的关系触发

MVP 阶段的 trigger 住在 `PolicyEngine._match_trigger` 里。要给它加一条新的 trigger,最小扰动的做法是继承 policy 引擎、往队列里推一个合成事件,然后让已有的 audit 路径把决策记下来。下面这个"反复提到同一个担心"的 trigger 会在用户近一周里至少提到某个话题三次时命中。

```python
from datetime import datetime, timedelta
from echovessel.proactive.core.base import (
    EventType,
    ProactiveEvent,
    TriggerReason,
)
from echovessel.proactive.engines.policy import PolicyEngine, TriggerMatch


class ExtendedPolicyEngine(PolicyEngine):
    """增加第三条 trigger:用户最近反复提到某个担心。"""

    min_mentions: int = 3
    lookback_days: int = 7
    keywords: tuple[str, ...] = ("worried", "anxious", "stressed")

    def _match_trigger(self, events, persona_id, user_id, now):
        base = super()._match_trigger(events, persona_id, user_id, now)
        if base is not None:
            return base

        since = now - timedelta(days=self.lookback_days)
        recent_events = self.memory.get_recent_events(
            persona_id, user_id, since=since, limit=50,
        )
        hits = [
            e for e in recent_events
            if any(
                kw in (getattr(e, "summary", "") or "").lower()
                for kw in self.keywords
            )
        ]
        if len(hits) >= self.min_mentions:
            return TriggerMatch(
                reason=TriggerReason.HIGH_EMOTIONAL_EVENT,  # MVP 阶段复用枚举
                payload={
                    "trigger_event_id": getattr(hits[-1], "id", None),
                    "match_label": "recurring_concern",
                    "hit_count": len(hits),
                },
            )
        return None
```

要真正触发这条 trigger,往 scheduler 队列里 push 一个合成事件即可——任何基于时间的唤醒事件都行:

```python
scheduler.notify(
    ProactiveEvent(
        event_type=EventType.TICK,
        persona_id="default",
        user_id="self",
        created_at=datetime.now(),
        payload={},
        critical=False,
    )
)
```

因为 `PolicyEngine.evaluate` 总是先走完 gate 再问 trigger,你的新 trigger 自动继承了 quiet hours、cold-user、rate limit 和 in-flight-turn 这四条安全栏——无需你自己再写一遍。

### 2. 调 policy 阈值

所有可调的旋钮都在 `[proactive]` TOML 段里,daemon 启动时被解析成一个 `ProactiveConfig` Pydantic 模型。下面这些是最可能需要改的字段:

```toml
[proactive]
enabled                          = true   # 主开关
tick_interval_seconds            = 60     # 循环唤醒间隔(10-3600)

# Quiet hours(本地时间 24 小时制;start > end 时窗口跨午夜)
quiet_hours_start                = 23     # 23:00 本地
quiet_hours_end                  = 7      # 07:00 本地 —— 窗口是 23:00-07:00

# 速率限制
max_per_24h                      = 3      # 日度上限(0-100)

# 冷用户保护
cold_user_threshold              = 2      # 连续 N 次无回复就进入冷模式
cold_user_response_window_hours  = 6      # 在这个窗口内的回复会重置状态

# 长沉默触发
long_silence_hours               = 48     # 沉默达到这个时数 → 进入 nudge 候选

# 队列
max_events_in_queue              = 64     # 硬上限;溢出时丢最老的 non-critical

# 停机
stop_grace_seconds               = 10     # stop() 等待当前 tick 的宽限期
```

有两条运维侧的提示值得强调:

- **配置只在 scheduler 构造时读一次。** proactive 不监听 TOML 文件,也不响应 SIGHUP。要让新值生效,重启 daemon。这是刻意的——在一个 tick 正在跑的时候热更新 policy 引擎,比它带来的收益复杂得多。
- **`persona_id` 和 `user_id`** 默认是 `"default"` 和 `"self"`,与 MVP 单 persona 形态匹配。多 persona 场景下,每个 persona 都是一台独立的 scheduler,各自持有自己的 `ProactiveConfig`。

### 3. 挂一个自定义 audit sink

默认 sink 是 `JSONLAuditSink`,每行一个 JSON 对象写入 `~/.echovessel/logs/proactive-YYYY-MM-DD.jsonl`。它实现了 `echovessel.proactive.core.base` 里的 `AuditSink` Protocol:

```python
class AuditSink(Protocol):
    def record(self, decision: ProactiveDecision) -> None: ...
    def update_latest(self, decision_id: str, **outcome_fields) -> None: ...
    def recent_sends(self, *, last_n: int) -> list[ProactiveDecision]: ...
    def count_sends_in_last_24h(self, *, now: datetime) -> int: ...
```

想把决策推到别处——一张 SQLite 表、一个 Prometheus exporter、一个第三方可观测平台——实现这个 Protocol,把实例通过 `build_proactive_scheduler(audit_sink=...)` 传进去即可,scheduler 会使用你的 sink 而不是默认的。

下面这个最小例子把每条决策同时写到一个旁边的 JSONL 文件里,同时把两个 policy 读方法(`recent_sends` / `count_sends_in_last_24h`)委托给标准 JSONL sink,这样 rate limit 和 cold-user 的读取仍然有数据源:

```python
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from echovessel.proactive.core.base import AuditSink, ProactiveDecision
from echovessel.proactive.execution.audit import JSONLAuditSink


class TeeJSONLAuditSink(AuditSink):
    """写一份自定义 JSONL;读请求委托给标准 sink。"""

    def __init__(self, custom_path: Path, stock_log_dir: Path):
        self._custom_path = Path(custom_path).expanduser()
        self._custom_path.parent.mkdir(parents=True, exist_ok=True)
        self._stock = JSONLAuditSink(log_dir=stock_log_dir)

    def record(self, decision: ProactiveDecision) -> None:
        self._stock.record(decision)           # 让读查询仍然能工作
        try:
            with self._custom_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(_to_json(decision), ensure_ascii=False) + "\n")
        except OSError:
            # record() 永远不能抛;记日志后吞掉。
            pass

    def update_latest(self, decision_id: str, **outcome_fields) -> None:
        self._stock.update_latest(decision_id, **outcome_fields)

    def recent_sends(self, *, last_n: int) -> list[ProactiveDecision]:
        return self._stock.recent_sends(last_n=last_n)

    def count_sends_in_last_24h(self, *, now: datetime) -> int:
        return self._stock.count_sends_in_last_24h(now=now)


def _to_json(d: ProactiveDecision) -> dict:
    raw = asdict(d)
    raw["timestamp"] = d.timestamp.isoformat()  # datetime 需要 isoformat
    return raw
```

在你的 runtime 引导代码里这样接上:

```python
from pathlib import Path
from echovessel.proactive import build_proactive_scheduler

scheduler = build_proactive_scheduler(
    config=proactive_config,
    memory_api=memory_facade,
    channel_registry=registry,
    proactive_fn=proactive_fn,
    persona=persona_view,
    voice_service=voice_service,
    is_turn_in_flight=lambda: registry.any_in_flight(),
    audit_sink=TeeJSONLAuditSink(
        custom_path=Path("~/.echovessel/logs/proactive-tee.jsonl"),
        stock_log_dir=Path("~/.echovessel/logs"),
    ),
)
```

自定义 sink 有两条实现注意事项:

- **`record()` 永远不能抛。** scheduler 的 tick 循环没法容忍一个会抛的 audit sink。如果你的 sink 做 I/O,用 `try`/`except` 把它裹住、记日志、吞掉——不要让异常冒出去。
- **`recent_sends` 和 `count_sends_in_last_24h` 是 policy 引擎的读侧。** 如果你想让冷用户检测和速率限制继续工作,要么像上面那样委托给标准 sink,要么在你自己的存储上实现这两个方法。把它们粗暴地 stub 成 `return []` / `return 0` 等于直接禁用了这两条 gate。

更完整的参考请直接看 `src/echovessel/proactive/`——每个文件都有详尽的 docstring;policy 引擎的 gate 顺序在 `tests/proactive/` 下的单元测试里被锁住。
