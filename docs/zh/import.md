# 导入管线(Import)

> 一条通用管线接收任何人类书写的文本——日记、聊天记录、小说片段、简历、随笔——用 LLM 给每一段分类,再路由进正确的记忆表。没有格式专属 parser,没有内容专属分支。

导入管线是外部素材进入 persona 记忆的入口。它是一个刻意做得很薄、刻意写得很通用的模块:它对"内容"的唯一理解来自抽取 LLM 告诉它的东西,它对"记忆"的唯一理解来自 `memory.import_content` 暴露的五类白名单。其他所有东西——日记里一段话的意思、"这是用户的事实还是 persona 的事实"、"这是反思还是事件"——都塞进一份 prompt 里决定。

---

## 概述

朴素的导入器会每个格式写一个 parser、每类内容写一个 handler:"日记就抽日期段落,聊天记录就按 turn 分组,简历就拉职位信息"。这种形态在用户扔进一个作者没预设的文件那一刻就崩了,而且它把最有趣的活——判断一段话到底在说什么——埋进不断膨胀的 if 分支堆里。

EchoVessel 下了反方向的赌。关键洞察是:任何人类书写的文本都可以被一个足够强的 LLM 分类成"这段内容在讲什么";而一旦拿到这个分类,把它路由到对应的记忆表就只是一次查表。所以导入管线被收敛成五步机械流程——读字节、切块、问 LLM 每块讲了什么、写进记忆、跑 embedding——所有关于意义的判断都被推进抽取 prompt 里。

这笔交易换来三个具体好处:

1. **一条管线,任意来源**。同一段代码路径处理日记、导出的 Discord 聊天记录、传记的一章、求职简历,或者粘进文本框的一段散文。要支持一种新的个人素材,不需要写一行 Python。
2. **对用户没有预处理负担**。用户不需要在导入前转换、标注或整理内容,他们直接扔手边已有的文件进来;LLM 负责判断每一段是持久的事实、一次性事件,还是一段反思。
3. **新的内容类别是改 prompt,不是重写 parser**。有一天项目决定"喜欢的地方"值得单独一个记忆槽,这时的工作是改一下抽取 prompt、在 `routing.py` 里加一个分支,而不是加一个新文件格式 handler、也不是加一条新管线。

代价是:导入的质量被抽取 prompt 的质量和跑它的 LLM 的质量卡住。管线严肃地对待这一点,有四个具体措施:

- 每条抽取出来的写入都带一个原文 `evidence_quote`,它必须是源 chunk 的子串,伪造的引用会在到达 memory 之前被丢弃。
- 抽取 prompt 明确列举六个合法目标(`L1.persona_block` / `L1.self_block` / `L1.user_block` / `L1.relationship_block` / `L3.event` / `L4.thought`),其它任何写入目标都会被 JSON 校验器拒绝。
- 置信度低于 0.5 的 L1 写入被静默丢弃,避免低确定性的猜测污染 persona 的核心身份 block。
- 逐块失败以 `DroppedItem` 的形式被记录(带原因字符串和 payload 摘录),通过 `chunk.error` 事件暴露出去,并进入最终的 `PipelineReport`——没有东西被吞掉。

---

## 核心概念

**归一化(Normalization)**。把用户上传的任意字节转成纯 UTF-8 文本。这是管线里唯一被允许关心文件格式的地方。`.txt` 和 `.md` 原样解码;`.md` 的 front-matter 被展平成 `"key: value"` 行,让 LLM 看到元数据;`.json` 被解析然后展平成可读的行(dict 列表会插空行分隔,便于分块阶段在元素之间断开);`.csv` 原样透传(分块阶段处理行的批处理)。非 UTF-8 或解不了码的字节抛 `NormalizationError`,管线立刻中止。实现在 `src/echovessel/import_/normalization.py`。二进制格式(PDF、DOCX、音频、图像)在 MVP 里明确不在范围内——管线在归一化阶段就拒绝它们,而不是假装能猜出它们的内容。

**分块(Chunking)**。把归一化后的文本切成能让 LLM 一次吞下的小片。策略在 `src/echovessel/import_/chunking.py`:段落按空行切;超过 2000 字符的段落用 1500 字符滑动窗口 + 500 字符重叠进一步切;CSV 形状的文本按每 8 行一 chunk 批处理。输出是一个 `Chunk` dataclass 列表,每个带自己的 index、内容、offset 和原始 source label。分块器在归一化之后对格式无感——它对格式的唯一让步是一个轻量启发式:检查每一行非空行是否至少有一个逗号、并且中位行长度是否小于 400 字符,用来识别 CSV 形状的文本。

**抽取(Extraction)**。LLM 读 + 分类的那一步。`src/echovessel/import_/extraction.py` 拿注入进来的 LLM 发起一次调用:system prompt 列举六个合法写入目标,user prompt 携带一个 chunk。LLM 返回一个 JSON 对象,包含一个写入列表和一句话的 `chunk_summary`。抽取器校验 JSON 形状,把每条写入交给 `routing.translate_llm_write`,然后返回一个有类型的 `ContentItem` 列表 + 一个 `DroppedItem` 列表(那些没过校验的写入)。抽取默认跑在 `SMALL` LLM tier:每个 chunk 的调用短、结构化、在一次上传里被重复很多次,用便宜 tier 能把端到端成本压到可预测,而质量的损失并不明显。

**`ContentItem`**。经过抽取后代表一次记忆写入决定的 dataclass。它携带一个 `content_type`(五个白名单字符串之一)、一个形状匹配记忆导入 API 的 `payload` dict、源 `chunk_index`,以及从 chunk 原文里摘下来的 `evidence_quote`。用白名单之外的 `content_type` 构造 `ContentItem` 会在构造时抛 `ValueError`——白名单是在 dataclass 本身里强制的,不只是下游代码。

**Content type**。恰好五个字符串之一:`persona_traits`、`user_identity_facts`、`user_events`、`user_reflections`、`relationship_facts`。这是 `memory.import_content` 接受的白名单;任何白名单之外的值都会抛 `ValueError`。导入管线在 `models.py` 的 `ALLOWED_CONTENT_TYPES` 里镜像了同一份白名单,所以违反会在到达 memory 层之前就被抓住。

**路由(Routing)**。把每个 `ContentItem` 送到正确的记忆写入函数。`src/echovessel/import_/routing.py` 查看 `content_type`、解包 payload,然后调 `memory.append_to_core_block`(对于 L1 blocks)或 `memory.import_content`(后者底层再分派到 `bulk_create_events` / `bulk_create_thoughts`)。路由也是 `L1.self_block` 侧通道被处理的地方——见"架构"一节。Dispatcher 返回 `(ImportResult, new_concept_node_ids)` 元组,让 orchestrator 能够累积 embed pass 需要的 id,不用再二次查表。

**Embed pass**。写入后必跑的那一步:为每个新的 L3 event 和 L4 thought 行计算向量嵌入,写进 `concept_nodes_vec`。没有这一步,被导入的事件和 thought 在 SQLite 里确实存在,但在 `memory.retrieve` 的向量检索里永远看不见——对话时永远召回不到。Embed pass 在 `src/echovessel/import_/embed.py` 实现,且**不可省**:如果管线产出了 concept node 行但调用方传了 `embed_fn=None`,管线会抛 `EmbedError` 而不是静默跳过。

**管线进度(Pipeline progress)**。当前正在跑的管线的一个内存快照:`current_chunk`、`total_chunks`、`written_concept_node_ids` 和一个 `state` 字符串。住在 `ImporterFacade` 里面,这样 LLM 一次临时失败就可以暂停管线,之后的 `resume_pipeline` 调用能从下一块接着跑,不会重复处理任何已经写入的内容。不持久化到磁盘——daemon 重启会丢失所有进行中的管线,用户被期望重新上传。启动时的重复检测路径(`memory.count_events_by_imported_from(file_hash)`)会阻止重传静默地把每一行翻倍,所以只要用户确认,"resume via 重传"就是安全的。

**`PipelineReport`**。每一阶段跑完之后 `run_pipeline` 返回的聚合结果。携带最终 `status`(`"success"` / `"partial_success"` / `"failed"` / `"cancelled"`)、每类 content type 的写入计数、新 `concept_nodes` id 列表、`core_block_appends` id 列表、带原因的 `DroppedItem` 列表,以及 `embedded_vector_count`。Runtime 调用方把它翻译成给 UI 的"完成"摘要;测试针对它做断言来确认管线做了期望的事情。

---

## 架构

管线是五步顺序:

```
upload(bytes + suffix)
       |
       v
+----------------+
| normalization  |   字节  →  纯 UTF-8 文本
+----------------+
       |
       v
+----------------+
|   chunking     |   文本  →  list[Chunk]
+----------------+
       |
       v          (每 chunk 一次 LLM 调用, SMALL tier)
+----------------+
|   extraction   |   chunk →  list[ContentItem] + list[DroppedItem]
+----------------+
       |
       v
+----------------+
|    routing     |   ContentItem → 记忆写入函数
+----------------+       |
       |                 |                |
       v                 v                v
  persona_traits    user_identity    user_events
  relationship      user_reflections
       |
       v
+----------------+
|   embed pass   |   concept_node ids → 向量 → concept_nodes_vec
+----------------+
       |
       v
  PipelineReport
```

每一步都有一个干净的输入和一个干净的输出,每一步住在 `src/echovessel/import_/` 下自己的模块里。`pipeline.py` 里的 orchestrator 大多只是 glue:通过注入的 `event_sink` callable 发出生命周期事件(`pipeline.start`、`chunk.start`、`chunk.done`、`chunk.error`、`pipeline.done`),让 runtime facade 能把它们翻译成给 Web UI 的 SSE 事件。

这个顺序是刻意的。归一化每条管线只跑一次,之后的代码可以假设自己读到的就是纯 UTF-8 文本。分块是确定性的、只依赖文本,所以 resume 时重跑它不花钱。抽取是唯一对接 LLM 的阶段,因此也是唯一可能临时失败的阶段;把它放在 per-chunk 粒度上,意味着一个坏 chunk 不会牵连其他 chunks。路由是同步的、每次只处理一个 `ContentItem`,所以一条坏写入可以被单独丢弃而不影响邻居。Embed pass 被推到最末端,因为它要读回所有已经写入的内容——per-chunk 跑它意味着每次 LLM 调用都要开/关一次向量索引事务,工作严格变多而没有收益。

### 五类 content type 分别路由到哪里

| Content type          | 记忆写入函数                                              | 记忆目标                                 |
| --------------------- | --------------------------------------------------------- | ---------------------------------------- |
| `persona_traits`      | `append_to_core_block(label="persona")`                   | L1 persona block                         |
| `user_identity_facts` | `append_to_core_block(label="user")`                      | L1 user block                            |
| `user_events`         | `import_content` → `bulk_create_events`                   | L3 concept nodes,`type='event'`          |
| `user_reflections`    | `import_content` → `bulk_create_thoughts`                 | L4 concept nodes,`type='thought'`        |
| `relationship_facts`  | `append_to_core_block(label="relationship_block:<key>")`   | L1 relationship block,按人 key 分桶       |

L1 追加写入函数会在更新 `core_blocks.content` 的同一个事务里,往 `core_block_appends` 表写一条审计行,所以每一条被导入的 L1 事实的 provenance 都可以重建。L3 / L4 的批量插入会给每个新行打上 `imported_from = <file_hash>` 标记,这也是后续再上传同一份文件时做重复导入检测的依据。`concept_nodes` 上的 schema CHECK 约束强制 `imported_from` 和 `source_session_id` 互斥:一行要么是导入产生的,要么是 consolidation 产生的,绝不会两者都是——就算未来某条代码路径想混用它们,provenance 故事也仍然诚实。

### `L1.self_block` 侧通道

抽取 prompt 里列举的合法目标其实有**六**个,不是五个。第六个是 `L1.self_block`:persona 的第一人称自我概念,区别于 `persona_traits` 这种第三人称描述。"她很好奇,也很有耐心"是一条 `persona_trait`。"我焦虑的时候总是过度解释"是一条 `self_block` 语句。把两者合并会抹掉 prompt 刻意维持的这个区分。

然而记忆的导入 dispatcher 只接受五类白名单。侧通道住在 `routing.py`:当抽取器看到 `target: "L1.self_block"` 时,它仍然生产一个 `ContentItem`(`content_type="persona_traits"` 让白名单检查通过,payload 上打一个 `_self_block=True` 标记),然后 `dispatch_item` 发现这个标记后,直接调 `append_to_core_block(label="self", user_id=None)`,完全绕开 `import_content`。最终的行在 pipeline report 里被计入一个合成的 `persona_self_traits` 键,审计工具可以据此把 self block 追加和 persona block 追加区分开。白名单不变量被保留:`memory.import_content` 永远不会看到 `persona_self_traits`,而且一个单测专门断言把它传进去会抛 `ValueError`。

### Embed pass 是强制的

`memory.bulk_create_events` 和 `bulk_create_thoughts` 刻意不计算 embeddings——memory 模块没有对 `sentence-transformers` 的依赖,而且永远不会有,因为 memory 需要能在没有 ML 栈的环境里跑。这个纪律的代价就是向量落到了导入管线头上。

每个 chunk dispatch 完毕之后,orchestrator 把新的 `concept_nodes.id` 收集到 `all_new_concept_ids`。逐 chunk 循环结束后,`run_embed_pass` 开一个新的 DB session,读回 `(id, description)` 对,对整个 batch 调一次注入的 `embed_fn`,然后用注入的 `vector_writer` 逐行写入向量。如果管线产出了 concept node 行但 `embed_fn` 或 `vector_writer` 是 `None`,`run_embed_pass` 会抛 `EmbedError`。静默跳过是被明确禁止的:一次"看起来成功但向量缺失"的导入在账面上很健康,但它会让被导入的内容在 `retrieve.vector_search` 里永远看不见——这比一个明显的失败更糟糕。

### 失败模式分类

管线区分三类失败,每一类有自己的处理方式:

- **Transient(临时)**。LLM 超时、网络抖动、provider 预算耗尽。出事的 chunk 抛 `ExtractionError(fatal=False)`,管线发一个 `chunk.error` 事件、把它登记为 dropped item、**把 `progress.current_chunk` 推到它之后**,然后继续跑下一个 chunk。Dispatch 层失败(DB 竞用、下游异常)走同一条路径。失败的 chunk 在进度游标里被标为"已尝试完毕",之后的 `resume_pipeline` 调用会跳过它——该 chunk 不会被重跑。运营人员如果在意某个失败 chunk 的内容,恢复手段是重新 import 整个文件。
- **Permanent(永久)**。无法按 UTF-8 解码的文件、白名单外的 content type、重试多少次都会失败的 schema 违规。它们抛 `NormalizationError`、`ExtractionError(fatal=True)` 或 `ValueError`。管线发一个 `fatal=True` 的 `chunk.error`,停止处理后续 chunks,然后发 `status="failed"` 的 `pipeline.done`。
- **Partial success(部分成功)**。一部分 chunks 成功,另一部分以非致命错误失败。已经写入的记忆行留在磁盘——管线永远不会因为后面某个 chunk 失败而回滚前面 chunks 的写入。管线以 `status="partial_success"` 结束,由调用方决定要不要给用户弹一个警告。

这个区分之所以重要,是因为 transient 失败可恢复而 permanent 不可恢复。每次失败都回滚所有已写入的部分,要么需要一个跨多 chunk 的分布式事务(过度设计),要么强迫用户重跑时重新付每一次 LLM 调用的钱(浪费)。保留部分写入、让重复检测路径处理重跑,是更便宜也更诚实的默认。

取消是另一个独立的问题:当 facade 调 `task.cancel()` 时,管线的 `asyncio.CancelledError` 处理器会把当前 chunk index 记进进度快照、把 state 标为 `"cancelled"`,然后再把异常抛出去,让 facade 的 task 级处理器发最终的 `pipeline.done`(`status="cancelled"`)。已经写入的行被保留,符合 partial-success 规则。用户看到的效果是"取消等当前 chunk 跑完再停"——和用户对一个停止按钮的预期很接近,但又不浪费已经在飞的 LLM 调用。

### 事件流

管线的每一步都通过注入的 `event_sink` callable 向外汇报。事件是小小的 dict,带一个 `type` 字符串和一个 `payload` dict;runtime facade 把它们翻译成 `PipelineEvent` 实例,再 fan-out 给每一个订阅者队列。生命周期大致是:

1. `pipeline.registered` — facade 在 `start_pipeline` 返回 id 的那一刻发出,此时还没开始归一化。让已订阅的 UI 可以立刻渲染一个 pending 状态。
2. `pipeline.start` — 在归一化和分块完成之后发出,携带 `total_chunks` 和 resume offset。这是第一条 payload 反映真实工作量的事件。
3. `chunk.start` / `chunk.done` — 每个 chunk 一对。`chunk.done` 携带 `writes_count`、`dropped_in_chunk`,以及 LLM 产出的一句话 `summary`,让 UI 能流式渲染一份实时日志。
4. `chunk.error` — 任何逐 chunk 失败都会发,带 `fatal` 和 `stage` 键,让订阅方能区分临时错误和永久错误。
5. `pipeline.done` — 终态事件。总是会发,总是最后发,总是携带最终 `status` 和各 target 的写入计数。订阅方用它的到达作为关闭自己 async-for 循环的信号。

因为 facade 把每一条事件 fan-out 给每一个订阅队列,一个在 `pipeline.start` 之后才调用 `subscribe_events` 的晚来订阅方会错过已经发生的事件。要做回放语义,调用方应当持有 facade 返回的 `PipelineReport`,而不是尝试从事件流重建状态。

### 运行时集成

导入管线和 runtime 在五模块架构里是同层 sibling,而 layering 契约禁止 sibling 之间互相 import:`channels.web` 不能直接 import `import_.pipeline`。中介是 `src/echovessel/runtime/importer_facade.py`,它暴露任何调用方都需要的四个方法:

- `start_pipeline(upload_id, *, raw_bytes, suffix, persona_id, user_id, ...) -> pipeline_id` — 分配一个 pipeline id,构造 `ProgressSnapshot`,然后 `asyncio.create_task(run_pipeline(...))`。立刻发一个 `pipeline.registered` 事件让订阅方知道这个 id 已经激活。
- `cancel_pipeline(pipeline_id)` — 把管线标记为 cancelled 并调 `task.cancel()`。管线的 `asyncio.CancelledError` 处理器会写入进度快照,之后还可以 resume。
- `resume_pipeline(pipeline_id)` — 用同一份 kwargs 再启动管线 task;`run_pipeline` 读 `progress.current_chunk` 跳过已经处理的 chunks。
- `subscribe_events(pipeline_id) -> AsyncIterator[PipelineEvent]` — 返回一个由自己的 `asyncio.Queue` 支撑的新异步迭代器。多个订阅方可以独立读同一个管线,所以 Web UI 和日志收集器可以并行旁听。每个迭代器在管线完成或被取消时,会收到 facade 推进队列的 `None` sentinel,然后自然退出,消费者可以用普通的 `async for` 而不需要轮询。

Facade 同时拥有依赖注入权:`llm_provider`、`voice_service`、`memory_api` 在 runtime 启动时传给构造函数,每次 `start_pipeline` 调用都把它们串进管线 kwargs。管线本身永远不 import `runtime`、`channels`、`proactive`;它只依赖 `memory`、`core` 和自己的子模块。同一个 facade 也会支撑未来的 `echovessel import <file>` CLI 命令——CLI 入口会构造一个把每个事件打印到 stdout 的最小 event sink,然后调同一个 `start_pipeline` 方法,这样 Web 和 CLI 两种驱动共享同一条代码路径。

---

## 如何扩展

### 加一个新的归一化格式

假设你想导入一种自定义 markdown 变体,它用 `:::note` 围栏块;或者一种项目专用的已知结构 JSON。入口是 `src/echovessel/import_/normalization.py` 里的 `normalize_bytes`。在 `suffix` 上分派,加一个私有 helper 产出干净的 UTF-8 文本;不要试图从内容里抽取意义——那是 LLM 的活。

```python
# src/echovessel/import_/normalization.py

def normalize_bytes(raw: bytes, *, suffix: str = "") -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NormalizationError(...) from exc

    suffix = suffix.lower()
    if suffix == ".json":
        return _flatten_json_text(text)
    if suffix == ".md":
        return _merge_frontmatter(text)
    if suffix == ".mynote":                # 新格式
        return _unwrap_note_blocks(text)
    return text


def _unwrap_note_blocks(text: str) -> str:
    """把 `:::note ... :::` 块变成普通段落。"""
    out = []
    for line in text.splitlines():
        if line.strip() in (":::note", ":::"):
            out.append("")                 # 空行 → 分块器会在这里断开
        else:
            out.append(line)
    return "\n".join(out)
```

这就是全部改动。分块、抽取、路由、embed pass 全都不用动。LLM 会通过同一个 prompt 读产生的纯文本,然后把它分到相同的五个桶里。你加的是一个解码器,不是一个"理解内容意思"的 parser。

三条实现要点。第一,helper 保持纯 `(str) -> str` 函数,因为那是 `normalize_bytes` 对它的唯一契约。第二,保留逻辑段之间的空行——段落切分器依赖 `\n\n` 识别一段的结束和下一段的开始,你的格式自然边界应当以空行的形式出现在输出里。第三,避免双重解码:如果你的格式在 markdown 里嵌了 JSON,不要随手递归调 `_flatten_json_text`——通常更简单的做法是把嵌入的 JSON 当作字面字符留着,让 LLM 自己去读。

### 调整抽取 prompt

抽取 prompt 是 `src/echovessel/import_/extraction.py` 里的一个常量 `IMPORT_EXTRACTION_SYSTEM_PROMPT`。改它会改变 LLM 分类内容的方式,而不用动其他任何模块——这正是"prompt 即路由表"设计的全部意义。

这份 prompt 列举了**六个合法目标**:五类 memory content type 加上 `L1.self_block` 侧通道。加一个、去掉一个或收紧一个目标,需要两处协调改动:

1. 改 system prompt,让 LLM 知道新目标的存在、含义、必填字段,以及什么情况下应该优先选它而不是邻居。
2. 在 `routing.translate_llm_write` 里加一个匹配分支,校验 LLM 对这个目标的输出,然后产出一个 `content_type` 正确的 `ContentItem`。如果新目标映射到已有的 `content_type`,路由改动就是模式匹配 + payload 组装。如果它需要一个全新的记忆桶,改动还要延伸进 `memory.import_content` 和 `ALLOWED_CONTENT_TYPES` 白名单——那是一个更大的、跨越导入/记忆边界的改动,必须作为一次原子提交来做。

对于纯 prompt 微调——改某个目标准入规则的措辞、收紧封闭的 relational tag 词表、加一个新的反例——规则更简单:改常量,如果期望的解析输出有变化就顺手更新 `tests/import_/test_extraction_stub_llm_roundtrip.py` 里的单测,然后跑 `pytest tests/import_/` 确认管线还在产出合法的 `ContentItem`。

有几条规则是承重结构,应当在每一次 prompt 修订中被保留下来。`evidence_quote` 要求不可谈判——`routing.translate_llm_write` 里的子串校验是管线防御"编造抽取"的手段。封闭 `relational_tags` 词表(`identity-bearing`、`unresolved`、`vulnerability`、`turning-point`、`correction`、`commitment`)住在 `extraction.RELATIONAL_TAG_VOCAB`,被静默过滤:如果要加一个新 tag,请在同一次提交里同时改代码里的 set 和 prompt 里的列表。`emotional_impact` 的 `-10` 到 `+10` 整数范围在 routing 里被校验;没有匹配的 memory schema 迁移就别扩。

### 自定义抽取后 hook

有时候你想在每条抽取出来的 `ContentItem` 落进 memory 之前检查一下——过滤掉低质量的写入、补充额外的 metadata,或者实现一种 dry-run 模式。干净的做法是在 `extract_chunk` 和 `_dispatch_chunk_items` 之间塞一个 callback。管线目前没有内建的 hook slot,但加一个就几行代码,因为所有协作者已经通过关键字参数注入进来了。

```python
# 你的调用方代码——例如一层自定义的 runtime wiring。

from echovessel.import_.models import ContentItem
from echovessel.import_.pipeline import run_pipeline

def skip_low_confidence(items: list[ContentItem]) -> list[ContentItem]:
    """丢掉 emotional_impact 恰好等于 0 的导入事件——LLM
    往往把 0 用作'拿不准'的退路。
    """
    kept = []
    for item in items:
        if item.content_type == "user_events":
            events = item.payload.get("events", [])
            if events and events[0].get("emotional_impact") == 0:
                continue
        kept.append(item)
    return kept


async def run_with_hook(**kwargs):
    # 最简单的集成方式是加一层不带子类的 wrapper,
    # 用一个预先过滤过的 LLM stub 去调 run_pipeline。
    ...
```

更面向生产的集成是给 `run_pipeline` 加一个显式的 `item_filter: Callable[[list[ContentItem]], list[ContentItem]]` 关键字参数,在 `extract_chunk` 返回和 `_dispatch_chunk_items` 调用之间应用它。这个改动局限在 `pipeline.py`,不碰其他模块,而且因为 filter 本身是一个注入的 callable,它完全在测试控制之下。你写的任何 filter 都应当是纯函数:embed pass 还是会在"实际落盘的东西"上跑,所以 dispatch 之后再丢 item 会让 concept-node id 对不上,污染 report。

写这类 hook 时有两个常见错误要避开。第一,不要 in-place 修改 `ContentItem.payload` dict——`ContentItem` 是 frozen dataclass,正是因为后续阶段假设它的字段是稳定的。如果需要补充 payload,请构造一个带合并后 dict 的新 `ContentItem`。第二,不要在 filter 里直接调 memory 模块。Filter 在 orchestrator 循环里按 chunk 跑,而 orchestrator 才是拥有 DB session 的那层;filter 里的侧通道 memory 写入会和 `_dispatch_chunk_items` 竞争,并且打破 embed pass "`all_new_concept_ids` 是所有需要向量的新行"这个前提。

---

## 延伸阅读

- `docs/zh/memory.md` 讲清管线写入的 L1 / L3 / L4 表是怎么存的,以及检索端之后如何给被导入的内容打分并召回。
- `docs/zh/runtime.md` 介绍五模块架构,展示 `ImporterFacade` 在整个 runtime 表面里的位置。
- `docs/zh/voice.md` 描述语音栈——如果以后你打算在二进制格式归一化就绪后重新接入音频备忘,这篇相关。
- `docs/zh/configuration.md` 列出控制 LLM tier 选择和嵌入后端的配置键。

---

以上所有内容的权威来源是 `src/echovessel/import_/` 下的文件和 `src/echovessel/runtime/importer_facade.py`;代码和本文档持有相同的不变量,当两者冲突时以代码为准。`tests/import_/` 下的测试套是可执行的规格,是验证任何扩展的最快方式。
