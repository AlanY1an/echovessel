# Memory 测试

> memory pipeline 如何被覆盖 · 按"数据从写到读的流向"组织 · 每条都给出 test 文件路径。

Memory 是 EchoVessel 里最密集的一层 : 一次写 操作牵涉到 schema / session 生命周期 / 向量索引 / 后台 consolidate 队列; 一次读 要组合 L1 core block / 向量召回 / rerank / FTS 回退。每一步都有自己的失败模式。这份文档沿着 pipeline 自上而下走一遍,每一站告诉你"测试套件钉住了哪些行为、文件在哪"。

除非特别说明,所有路径相对 `tests/`。默认的 `pytest` 一律在 stub provider 下跑(免费 · 确定性) · 文末 eval 层才会调 live LLM,默认跳过。

```
 ingest              consolidate                 retrieve
   │                     │                          │
 L1 ──────────────────── L1 ──── L1 core blocks + facts
 L2  messages ────────►  L2 (批量读)
                         │ extract
                         ▼
                         L3 events ──────────►  向量 / FTS rerank
                         │ reflect (shock|timer)
                         ▼
                         L4 thoughts ────────►  retrieve 里跟 L3 混合
```

---

## 第 1 层 · L1 core blocks + 生平事实

**这一层做什么。** 五段散文 core blocks(`persona / self / user / relationship / style`) 加上 `personas` 行上的 15 个结构化生平字段(`full_name` / `gender` / `birth_date` / `timezone` / `occupation` / …)。两者每个 turn 都会被重新载入、拼进 system prompt;15 个 fact 里只有 5 个会渲进 prompt 的 `# Who you are` 段(C 方案契约)。当下情绪状态不在 L1 而在 L6 episodic state(见 [`memory.md`](./memory.md))。

**测试覆盖。**

- Prompt 模板 + parser — JSON 往返 / enum · date 归一化 / 烂 JSON 降级 · `tests/prompts/test_persona_facts.py` (20 条)
- Runtime orchestrator — 默认 LARGE tier / `existing_blocks` 确实进 prompt / parser 异常变 `PersonaExtractionError` · `tests/runtime/test_persona_extraction.py` (7 条)
- Admin API — 带/不带 facts 的 onboarding / GET 返回 15 个 keys / PATCH 部分更新 + 显式 null 清空 / 越界 enum 降级 vs 烂 date 返 422 · `tests/channels/web/test_persona_facts_routes.py` (17 条)
- System prompt 契约 — 只渲 5 fact / `birth_date.year` 不渲完整 ISO / `timezone` 不进 prompt / 空 view 等价 legacy prompt · `tests/runtime/test_interaction.py` (新增 6 条) + `tests/memory/test_stage1_facts_addons.py` (4 条)
- Schema 迁移 + 幂等 — 新装 + 老装 DB 各 15 列 / 重跑零动作 · `tests/memory/test_migrations_idempotent.py` · `test_migrations_from_old_db.py`

---

## 第 2 层 · L1 → L2 · ingest

**这一层做什么。** 每条 user / persona 消息原样写进 `recall_messages` 并带 `channel_id` 溯源 · 相应 session 的 `(message_count, total_tokens, last_message_at)` 同步前进。Session 按 `(persona_id, user_id, channel_id)` 三元组分片。生命周期触发:

- **IDLE** — 30 分钟没新消息 → 下次 `catch_up_stale_sessions` 把它标 closing
- **MAX_LENGTH** — `message_count ≥ 200` 或 `total_tokens ≥ 20 000` → 立即 close
- **启动 catchup** — 上次 boot 留下的 stale OPEN · 启动首次 scan 转 CLOSING
- **并发** — Web / Discord / iMessage ingest + idle scanner + consolidate worker 同时写一个 SQLite 文件

**测试覆盖。**

- 单条消息 + session 创建 / per-channel 分片 / turn_id 分组 / 假时钟驱动的 idle 触发 / MAX_LENGTH close / catchup scan · `tests/memory/test_ingest.py` · `test_sessions_concurrency.py` · `test_recall_messages_turn_id.py`
- **WAL + busy_timeout PRAGMA** — connect 时钉住 `journal_mode=wal / synchronous=NORMAL / busy_timeout=5000` · `tests/memory/test_engine_pragmas.py` (4 条)
- **跨 channel 并发写** — 3 线程 · 3 channel · 每 15 条消息 · 零 `OperationalError: database is locked` · `tests/memory/test_stage2_concurrency_and_catchup.py`
- **Worker 拾取孤儿 CLOSING session** — 模拟 daemon 重启 · `initial_session_ids` + `drain_once()` 把上次留下的 session 消耗掉 · 同文件
- **超过 max_retries → FAILED** — `LLMTransientError` 超出重试预算后 session 标 FAILED · `close_trigger` 盖上成因 · 无关的 CLOSED session 不受污染(no contagion) · 同文件

---

## 第 3 层 · L2 → L3 · consolidate (extract)

**这一层做什么。** 一个 session close 后 · consolidate worker 读这个 session 的所有 L2 消息 · 问 extraction LLM 要零或多个 event · 每条 event embed · 写进 sqlite-vec 伴生表 · 最后把 session 翻到 CLOSED。Event 带 `source_session_id`(可选 `source_turn_id`)支持追溯 · 带 `extracted_events` resume flag 让中途失败可以续跑。

**关心的失败面。**

- extractor 返回烂 JSON(LLM 偏离了 schema)
- `relational_tag` 越界 enum
- 中途 vector insert 抛(backend 挂了)
- session 卡在 FAILED · 需要人工重试

**测试覆盖。**

- Trivial 跳过(msg<3 或 tokens<200) · 正常 session 产 event · SHOCK 触发 reflect · 幂等重跑 · resume flag 生效 · 已经 CLOSED 的 session 是 no-op · bootstrap 产正确的 block 形状 · `tests/memory/test_consolidate.py`
- **`make_extract_fn` 对烂 JSON 返 `[]`** — session 不会标 FAILED · top-level-array shape 错也降级到空 · `tests/memory/test_stage3_consolidate_addons.py`
- **Enum 越界 `relational_tag` 被 parser 过滤** · 在完整的 `make_extract_fn` round-trip 中验证 · 同文件
- **原子性 bug 已被暴露(xfail · strict)** — `backend.insert_vector` 用 `engine.begin()` 打开独立连接并 auto-commit · 中途抛会留一条 event 在库里但 resume flag 还是 `False` · 修复方向有两条(把 vector 写入塞进 SQLAlchemy 事务 / 或 events 先整体 commit · vector 后补) · `tests/memory/test_stage3_consolidate_addons.py::test_consolidate_atomic_when_vector_insert_raises_mid_event`
- **FAILED → CLOSING 手动重试** — operator 解卡路径能跑通 · worker 正常把这条 retry 掉 · 同文件
- Parser 层 — 15+ 种边角情形(烂 JSON / impact 越界 / impact 是小数 / bool 冒充 int / 未知 enum tag / 截断) · `tests/prompts/test_extraction.py`

---

## 第 4 层 · L3 → L4 · reflect

**这一层做什么。** extract 为这个 session commit 完 event 后 · consolidate 决定要不要触发反思:

- **SHOCK** — 本次新建的 event 中任一 `|emotional_impact| ≥ 8`
- **TIMER** — 过去 24 小时没有 thought
- **Hard gate** — 24 小时内不论什么触发都最多产 3 条 thought

反思读过去 24 小时的 events · 问 reflection LLM 要更抽象的 thought · 写 `ConceptNode(type=thought)` 加 `concept_node_filling` 链接到源 events。

**测试覆盖。** 全在 `tests/memory/test_stage4_reflect.py` — 这一层之前完全没有专属 test。

- SHOCK 触发 · `reflect_fn` 以 `reason="shock"` 被调
- TIMER 触发 · 没有 24 小时内的 thought 时 reflect 跑(就算 events 都温和)
- TIMER 被抑制 · 24 小时内已经有 thought 且没有 SHOCK · reflect 不跑
- 24 小时 hard gate · 种 3 条 thought + 一条 SHOCK event · reflect 被 gate 掉 · session 仍 CLOSED
- Filling chain 正确 · parent → child 映射清晰 · `orphaned=False`
- 软删除其中一条源 event · 对应 filling 行 `orphaned=True` · thought 保留(forgetting-rights 契约)
- reflect 抛错留下可重放状态 · 同一 session 再跑 consolidate 跳过 extract(resume flag)· 只重试 reflect · event 不翻倍

---

## 第 5 层 · Retrieve

**这一层做什么。** persona 开口前 · retrieve 组装:

1. 所有 L1 core blocks
2. 向量召回 `concept_nodes`(拿 `top_k` 候选)
3. rerank 公式 `0.5 * recency + 3 * relevance + 2 * impact + relational_bonus_weight * relational_bonus`
4. minimum-relevance 地板 · 在 rerank 能把高 impact 推上去之前砍掉正交命中
5. session-context 扩展 · 每条 event 命中拉周围几条 L2 消息
6. 向量层本身返太少时 · 回退到 L2 FTS

**D4 铁律。** 读路径任何函数都不接 `channel_id` 参数 · 永远。

**测试覆盖。**

- 加载所有 core blocks · 共享 vs per-user 行 · 向量召回找最近 · access_count 每次命中 +1 · rerank 尊重 `relational_bonus_weight` · 向量空返时 FTS 回退 · 跨 channel 的 list_recall_messages 统一 · `tests/memory/test_retrieve.py`
- **D4 签名守卫** — `inspect.signature(retrieve)` 和 `list_recall_messages` 都断言无 `channel_id` 形参 · `tests/memory/test_stage5_retrieve_addons.py`
- **`min_relevance` 地板砍掉正交 SHOCK** — `|impact|=-9` 但向量轴跟 query 无关的 event 默认阈值下不出来 · 把阈值调 0 证明它本来会压过温和对齐命中 · 同文件
- **FTS 回退不乱触发** — 向量返够了 hits 就不该跑 FTS · 防止双倍延迟 + L2 闲聊污染 prompt · 同文件
- **Recent L2 窗口独立于 retrieve** — concept_nodes 为零时 · `retrieve.memories == []` · 但 `list_recall_messages` 照常返最近 N 条(runtime 塞进 user prompt 的窗口) · 同文件

---

## 跨层铁律

- **F10 · prompt 不出现任何 transport 身份** — `assemble_turn` 不会把 `channel_id` 或任何 transport 名字泄进 system/user prompt · 混合历史也一样 · `tests/runtime/test_f10_no_channel_in_prompt.py`
- **跨 channel 统一 persona** — 同一 `(persona, user)` 在 Discord 和 Web 共享记忆 · 任一 channel 的 event 在另一边也被检索到 · `tests/integration/test_cross_channel_unified_persona.py`
- **L6 episodic state 写入** — extraction 输出的 `session_mood_signal` 在 consolidate 收尾时 UPDATE `personas.episodic_state` JSON 列 · `tests/memory/test_episodic_state.py`
- **Forget + orphan** — 删 event 可选 cascade · orphan · cancel · `tests/memory/test_forget.py`

---

## Eval 层 · live LLM + judge(默认跳过)

**这一层做什么。** 上面所有 stage 测试跑在 stub LLM 上 — 它们钉逻辑 / schema / 并发。它们不告诉你"你真实的 prompt + 模型组合到底有没有抽到该抽的 event"。`tests/memory_eval/` 填上这个空白:八条 fixture 场景 · 每条用 live LLM 过一遍真实 consolidate / retrieve pipeline · 再拿另一次 LLM 调用做 judge。

**结构。**

```
tests/memory_eval/
├── fixtures/
│   ├── scripted/         · 我手写的 YAML · 确定性
│   └── synthesized/      · LLM 代笔的同题对话 · 生成一次 · commit 入库
├── harness.py            · 加载 fixture → 跑 pipeline → 检查 invariant
├── judge.py              · 同一个 LLM 回答 yes/no
├── synthesize.py         · 生成 synthesized 对照组(LARGE tier)
└── test_eval_fixtures.py · 参数化全部 YAML · @pytest.mark.eval
```

**Fixture。** 一个 YAML 一个场景。scripted 和 synthesized 共用同一套 invariant + judge_prompts · 所以同一个 harness 两边都能跑;唯一差别是"user 消息谁写的"。

| # | 场景 | 检验 |
|---|---|---|
| **E1** | 用户在闲聊里丢出生平 + 丧偶 | user-centric 抽取 · relational_tag=identity-bearing / vulnerability |
| **E2** | 用户只问 persona 问题 · 不自披露 | 抽取正确返 0-1 条 event |
| **E3** | 藏在闲聊里的 SHOCK(母亲过世) | 抽取抓到 peak · reflection 触发 |
| **E4** | 用户更正之前说错的事实 | relational_tag=correction |
| **E5** | 5 条 seed event + 短 session | TIMER reflect 跑 · thought 是抽象 · filling ≥ 2 |
| **E6** | 10 条 seed event · 查 Mochi | top-3 里至少 2 条关于猫 / 医院 |
| **E8** | 中英混讲 · 中文占多 | 抽取输出为中文 |

**硬 invariants**(由 `harness.check_invariants` 检查): event 数区间 / `shock_event_present` / `reflection_triggered` / `must_mention_any` 子串匹配 / `must_have_relational_tag_any` / `filling_min` / `top3_relevant_min` / `output_language`。

**软 invariants**(由 `judge.judge_prompts` 检查): 每条 fixture 带 1+ 个 yes/no 问题,由同一 LLM 在 MEDIUM tier 上读 `harness.render_evidence` 产出的 evidence 回答,回答 no 即算失败。

**怎么跑。**

```bash
# 跑一遍 eval 层(每次花几分钱 LLM 费用)
uv run pytest tests/memory_eval/ -m eval -v

# 生成 synthesized 对照 fixture(一次性 · 由 LLM 代笔)
uv run python -m tests.memory_eval.synthesize
#   → 写进 tests/memory_eval/fixtures/synthesized/e*.yaml
#   人工过一遍 · 微调 · commit
```

Scripted fixture 钉可以逐字推理的回归。Synthesized fixture 把 pipeline 扔进没预料到的措辞里。两边共用同一个 test runner。

---

## 怎么跑整套

```bash
# 除 eval 之外全部(默认)
uv run pytest

# 只 memory 相关
uv run pytest tests/memory/ tests/prompts/ tests/runtime/

# eval 层 · 用 live LLM · 花钱
uv run pytest tests/memory_eval/ -m eval -v

# lint + import 契约(每次改 memory 都该跑)
uv run ruff check src/ tests/
uv run lint-imports
```

今天有 1 个 `xfail` 是预期的 — Stage 3 暴露的 consolidate 非原子性 bug。那条翻红表示 bug 修了(该把 `xfail` 改回普通 test)。
