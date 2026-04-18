# Memory eval baseline · 2026-04-18

> First live-LLM run of `tests/memory_eval/`. 8 scripted fixtures · 35 s total wall time · under $0.05 at current provider rates.

**Config · 跑这次用的设置**

| 字段 | 值 |
|---|---|
| Provider | `openai_compat` |
| API base | OpenRouter |
| API key env | `OPENAI_API_KEY` (164 char key) |
| Extract tier | SMALL |
| Reflect tier | SMALL |
| Judge tier | MEDIUM |

**总结 · 3 pass · 5 fail · 失败按根因分三类**

| # | Fixture | 结果 | 根因类别 |
|---|---|---|---|
| E1 | user_self_disclosure | ❌ | 🔥 extract prompt 健壮性(真 bug) |
| E2 | user_only_asks | ✅ | — |
| E3 | buried_shock | ❌ | 🟡 judge prompt 措辞歧义(fixture 问题) |
| E4 | correction | ✅ | — |
| E5 | reflection_abstraction | ✅ | — |
| E6 | retrieval_relevance | ❌ | 🟡 harness embedder 简化(harness 问题) |
| E7 | mood_evolution_short | ❌ | 🟡 harness 漏注册 observer(harness 问题) |
| E8 | bilingual | ❌ | 🔥 extract prompt 健壮性(真 bug) |

`🔥` = 代码需要改 · `🟡` = 测试基础设施需要改 · 不是 memory 的 bug。

---

## 逐条分析

### E1 · user_self_disclosure · ❌

**场景** · 用户四 turn 里依次吐出姓名、退休、丧偶、养猫四个事实。

**期望** · 至少 2 个 event · 含"老伴/丧偶/过世/Mochi/退休"中任一 · 至少一个 `identity-bearing` 或 `vulnerability` tag。

**实际** · 0 event · 所有 invariant 失败。

**证据**

```
WARNING echovessel.runtime.prompts_wiring: extraction parse error
(dropping events for session s_a8653c1a02d4):
response is not valid JSON: Expecting value: line 11 column 27 (char 288)
```

**诊断** · extract LLM(SMALL tier)返回的响应在第 11 行第 27 列挂了。`make_extract_fn` 的 `try / except ExtractionParseError` 正确捕获(`prompts_wiring.py:124-132`)· 返回 `[]` · 下游 invariant 全都过不了因为没 event。

**为什么这是真 bug** · 昨天 Discord 上 4 条 session 共 30+ 条消息都 0 event · 跟这个极可能是同一条链。模型在中文 + 中点分隔符(`·`)+ 多段 user message 的 prompt 下 · 输出 JSON 格式不稳。

**候选修复方向**

1. extract prompt 里加 "respond with strict JSON · no code fences · no trailing commas"(目前只说 strict JSON · 没强调细节)
2. 换更稳的 extract 模型(SMALL → MEDIUM · 成本升一档)
3. 在 `make_extract_fn` 加一次 retry · 把 prompt 再发一遍 · 最多 1 次 · 缓解间歇性格式漂移
4. 降级路径里加 "再问一次但只要 JSON" 的 corrective retry

排序 · 1 最便宜 · 3 其次 · 2 最贵。建议先试 1 · 失败再 3。

---

### E2 · user_only_asks · ✅

**场景** · 用户全程只问 persona 问题("你喜欢什么音乐 / 你最喜欢去哪里") · 自己不披露。

**期望** · `events_max: 1`。

**实际** · 抽出 ≤ 1 条 event · judge 答 yes。extractor 正确识别出"用户基本没自披露"。

**意义** · 跟昨天 Discord 上的 0 event session 对得上 —— 短 chat 全用户问 persona 的场景 · extractor 行为 **是对的**(不是 bug)。那批 session 里没 event 是设计 · 不是漏。

---

### E3 · buried_shock · ❌

**场景** · 用户中间夹一句"我妈两年前走了" · 前后都是闲聊。

**期望** · 1+ event · `shock_event_present=True`(任一 event `|impact| ≥ 8`)· `reflection_triggered=True`。

**实际** · invariant 全过 · judge 有 1 条答 no。

**证据**

```
Q: Is the emotional_impact on that event at most -7?
A: The extracted event shows an emotional impact of -9,
   which is less than -7.
```

**诊断** · 抽取其实成功了!抽出了一条 `impact=-9` 的 event(SHOCK 触发了 · reflect 也跑了 · `shock_event_present` invariant 已过)。

judge 答错是因为**我 prompt 写错了**:"at most -7" 在数学上 = "≤ -7" · `-9 ≤ -7` 是 True · judge 应该答 yes。但 LLM 把 "less than -7" 读成了"不该是这个"(接近口语误读)。

**修复** · 把 `e3_buried_shock.yaml::judge_prompts` 第 2 条改成:

```yaml
- Is the emotional_impact on that event ≤ -7 (i.e. -7, -8, -9, or -10)?
```

把数学符号明写 · 消歧义。

---

### E4 · correction · ✅

**场景** · 用户先说"我 32 岁" · 后纠正"不是 32 是 28"。

**期望** · 至少一个 event 打了 `relational_tag=correction`。

**实际** · invariant + judge 全过。extractor 正确识别 correction 语义 + 打 tag。

---

### E5 · reflection_abstraction · ✅ (最值得高兴的一条)

**场景** · 种 5 条近 6 小时内的 life-transition event(分手 / 搬家 / 新工作 / 失联 / 失眠)· 再开一个 3 turn 短 session → TIMER 触发 reflect。

**期望** · 至少 1 条 thought · `filling ≥ 2`(thought 基于至少 2 条 event)· judge 问 thought 是不是抽象(不只是复述单条)。

**实际** · thought 产出:

```
impact=-5 rel_tags=['vulnerability']
"我注意到用户最近在工作压力下感到孤独 · 似乎很难找到可以倾诉的人。
 这种感觉让他感到脆弱 · 可能在影响他的情绪和状态。"
```

filling ≥ 2 ✓ · judge 答 yes ✓。**整条 L3→L4 链路在真模型下跑通** · consolidate.py 的 reflect 分支 + filling 链 + judge 判定都对。

---

### E6 · retrieval_relevance · ❌

**场景** · 种 10 条 event(1 条 Mochi 领养 + 1 条 Mochi 上医院 + 8 条无关) · query "Mochi 最近怎么样了" · top-3 应含至少 2 条 Mochi 相关。

**期望** · top-3 至少 2 条 description 含 `猫 / Mochi / 医院`。

**实际** · top-3 只返 1 条:

```
top3: ['用户养了只黑猫叫 Mochi · 2020 年领养']
```

相关性 0.47 · 但第二条 "用户养的猫上周去了医院" 没进 top-3。

**诊断** · 这不是 `retrieve` pipeline 的 bug · 是 `tests/memory_eval/harness.py::keyword_embedder` 的问题。我为了让 eval 不依赖 sentence-transformers · 写了个关键字→轴的假 embedder:

- "Mochi" · "mochi" · "黑猫" · "猫" · "领养" · "2020" · 这批都放同一段轴
- "医院" 分到另一轴
- query "Mochi 最近怎么样了" 里的 "Mochi" 只触第一段轴 · 不触"医院" 轴 · 所以医院那条 event 的余弦相似度低

真实部署里 `embed_fn = sentence-transformers all-MiniLM-L6-v2` · "Mochi"、"猫"、"医院"在同一个语义簇里 · 能一起召回。

**修复** · harness 加一个 flag · 默认跑真 embedder(第一次跑下载 90MB · 之后 cache 在 `data_dir/embedder.cache/`):

```python
# harness.py
def build_embedder(use_real: bool = True):
    if use_real:
        from echovessel.runtime.embedder import build_sentence_transformer_embedder
        return build_sentence_transformer_embedder()
    return keyword_embedder()[0]
```

跑慢一点 · 换来真 retrieve 的覆盖。

---

### E7 · mood_evolution_short · ❌

**场景** · 5 turn 用户吐"很难受 / 工作压得我喘不过气 / 感觉身边没人能说" · `mood_block` 从 `平静 · 中性 · 愿意倾听` 应该演化。

**期望** · `mood_block_changed=True`。

**实际** · mood 一字未动 · 但 consolidate 抽出 1 条 `impact=-5 · rel_tag=vulnerability` 的 thought(见 E5 风格)。

**诊断** · `mood_block` 更新靠 `MemoryEventObserver.on_event_created` hook · 当新 event 有 `vulnerability` 或 `identity-bearing` 等 tag 时 · 运行时的 mood observer 会 UPDATE `core_blocks.mood`。

**但我的 harness 没注册任何 observer**(`harness.run_fixture()` 里直接调 `consolidate_session` · 没传 `observer=` 参数 · 也没走 runtime 启动流程)。所以 mood 自然不会动。

这不是 memory 的 bug · 是 harness 漏了这环。

**修复** · harness 在 consolidate 前注册一个最小的 `MemoryEventObserver`:

```python
# harness.py · run_fixture()
from echovessel.memory_observers import build_mood_observer
# ... 构造 mood observer ...
register_observer(mood_observer)
try:
    cons = await consolidate_session(..., observer=mood_observer)
finally:
    unregister_observer(mood_observer)
```

需要查 runtime 里真实 mood observer 长什么样 · 跟着做一遍。

---

### E8 · bilingual · ❌

**场景** · 用户 5 条消息 · 4 条中文 + 1 条英文 · 期望 extract 输出中文。

**实际** · 0 event · 跟 E1 一样的 parse error · 位置不同:

```
WARNING extraction parse error:
response is not valid JSON: Expecting value: line 5 column 27 (char 105)
```

**诊断** · 跟 E1 同根因 · 跟 E1 一起修即可。值得注意的是 8 条 fixture 里只有两条炸 JSON · 都是包含 "我 X · Y · Z" 这种中点语法的 · 很可能 prompt 里中点触发了模型某种 tokenizer 诡异 behavior。

修 E1 时同步验证 E8。

---

## Fix queue · 按优先级

| # | 修什么 | 类别 | 触及文件 | 预期代价 |
|---|---|---|---|---|
| 1 | extract prompt 健壮性(E1 + E8) | 🔥 真 bug | `prompts/extraction.py` · 也许 `prompts_wiring.py` 加一次 retry | 真 bug · 生产问题 · 先修 |
| 2 | harness 注册 mood observer(E7) | 🟡 harness | `tests/memory_eval/harness.py` | 10 分钟 · 研究下真实 observer 长啥样 |
| 3 | harness 用真 embedder(E6) | 🟡 harness | `tests/memory_eval/harness.py` | 20 分钟 · 第一次跑下载 90MB |
| 4 | E3 judge prompt 消歧义 | 🟡 fixture | `fixtures/scripted/e3_buried_shock.yaml` | 30 秒 |

---

## 复跑命令

```bash
set -a && source .env && set +a
uv run pytest tests/memory_eval/ -m eval -v
```

预期在所有修复落地后 · 8/8 pass。

---

## Baseline 历史

> 每次 eval run 在此 append 一行 · 形成漂移曲线。

| 日期 | commit | pass/fail | 主要变化 |
|---|---|---|---|
| 2026-04-18 | b47904f | 3/5 | 首次 baseline · 暴露 extract JSON 漂移 + harness 两个漏洞 |
