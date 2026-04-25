# Voice

## Overview

在 EchoVessel 里,voice 是 persona 身份的一级载体,不是可有可无的装饰。一个只会用文字说话的 persona 只算存在了一半——voice 模块的意义,就是让同一个 persona 能用自己的声音念出自己写的话、听懂用户的口头输入、并在每一个支持音频的 channel 上保持同一副音色。因为 daemon 的其他部分早就把每个 persona 当作跨 channel 的统一实体处理,voice 自然以一个小 facade(`VoiceService`)插入这个设计:不管底层 provider 是云端合成器、本地二进制还是进程内的 stub,它对外暴露的形状完全一样。

模块覆盖三种能力。**TTS**(文字转语音)把要发出去的 persona 消息合成为音频字节,由 channel 流式推给用户。**STT**(语音转文字)把进来的语音消息——浏览器 `MediaRecorder` blob、Discord 语音、手机录的 `.m4a`——转成下游 pipeline 消费的文字。**Voice cloning**(声音克隆)把一段短参考样本注册到支持 reference model 的云端 provider,让 persona 以这副音色说话。这三条路径被刻意拆开:TTS 和 cloning 共享一个 provider Protocol,因为它们跑在同一个厂商账号上;STT 独立在另一个 Protocol 下,因为现实里的 provider 几乎不会同时提供(FishAudio 有 TTS 但没有 STT;OpenAI Whisper 有 STT 但没有 TTS)。

`VoiceService.generate_voice()` 是 channel 和 proactive scheduler 真正会调的唯一入口。它是底层 `TTSProvider.speak()` 原语的 facade,而不是替代品。内部流程是:按 `message_id` 查本地缓存,未命中才调 `speak()`,再原子落盘、估一下成本,然后返回一个 `VoiceResult`——它的字段与 Web channel 发出的 `chat.message.voice_ready` SSE payload 一一对应。facade 所增加的一切——缓存、成本估算、幂等、非 neutral tone 的优雅回退——全都能集中在一处,正是因为下层 `speak()` 保持成一个朴素的流式原语。

## Core Concepts

**TTS provider。** 对接外部文字转语音合成器的抽象。在 `src/echovessel/voice/base.py` 里以 `runtime_checkable` Protocol 定义。一个 provider 暴露一个流式的 `speak()` 方法输出音频字节,另外加几个身份属性(`provider_name` / `is_cloud` / `supports_cloning`)。FishAudio 是当前的云端实现;`StubVoiceProvider` 是测试和 dry run 使用的无网络确定性实现。

**STT provider。** 独立的语音转文字 Protocol,用于从用户处或离线转写里导入语音消息。真正的方法只有一个 `transcribe()`,接受原始 bytes 或 async chunk iterator,返回一个 `TranscriptResult`。之所以把 STT 从 TTS 里拆出来,是因为现实里的 provider 几乎不会两边都做——强行合并 Protocol 会让每个具体类里有一半的方法是空的。

**Voice clone。** 一段短参考音频(通常 10–60 秒),上传到支持 reference model 的云端 provider 之后拿到一个稳定的 `voice_id`。EchoVessel 之后就以这个 id 作为该 persona 的合成音色。克隆路径被刻意挡在 per-turn 热路径之外——它只由一次 CLI 子命令触发,拿到的 id 写进 persona 配置后就不再动。

**Voice profile。** `(provider_name, voice_id)` 的二元组,存在 `config.toml` 的 persona section 里。Runtime 启动时读取,把 `voice_id` 当作 `default_voice_id` 传给 `VoiceService`,从此 daemon 其他地方都以这副音色合成,而完全不用关心它是哪个 provider 铸的。

**`VoiceResult`。** `generate_voice()` 返回的 frozen dataclass,定义在 `src/echovessel/voice/types.py`。五个字段全部承载语义:`url`(Web channel 提供音频的相对路径)、`duration_seconds`(进度条用的尽力估计)、`provider`(审计日志用的不透明标签)、`cost_usd`(写死表估出的每次调用 USD 估算,命中缓存时为 `0.0`)、`cached`(结果是否跳过了底层 provider)。

**Voice cache。** 磁盘缓存位于 `~/.echovessel/voice_cache/<message_id>.mp3`,`generate_voice()` 第一次运行时才懒建立。它让该方法按 message id 幂等:第二次调同一个 message id 直接返回缓存文件、把 `cached` 标为 `True`、`cost_usd` 报 `0.0`,完全不碰 provider。它与声音克隆用的 fingerprint 缓存(`~/.echovessel/voice-cache.json`)是不同的文件位置,清其中一个永远不影响另一个。

## Architecture

Voice 位于五模块栈的 Layer 2,和 Memory 并列。Runtime / Channels / Proactive 都在 Voice 之上,可以 import voice;Voice 自身只能 import 它正下方的 core 类型。分层由 CI 里的 `import-linter` 强制,所以依赖方向是构建期保证,不是约定。

```
┌────────────────────────────────────────────────────┐
│        runtime  |  channels  |  proactive          │
└────────────────────────────┬───────────────────────┘
                             │  constructor injection
                             ▼
┌────────────────────────────────────────────────────┐
│                VoiceService  (facade)              │
│  ┌──────────────────────────────────────────────┐  │
│  │  generate_voice(text, voice_id, message_id) │  │
│  │     · cache check  · speak()  · atomic write │  │
│  │     · cost estimate  · VoiceResult           │  │
│  └──────────────────────────────────────────────┘  │
│        │                              │            │
│        ▼                              ▼            │
│   TTSProvider                    STTProvider       │
│   (Protocol)                     (Protocol)        │
└────────┼──────────────────────────────┼────────────┘
         │                              │
         ▼                              ▼
   FishAudioProvider              WhisperAPIProvider
   StubVoiceProvider              StubVoiceProvider
```

两个 Protocol,一个 facade。`TTSProvider.speak()` 是底层原语:接受一段文字、一个可选的 `voice_id`、一个 `AudioFormat` 字面量,返回 `AsyncIterator[bytes]` 形式的音频分片。哪怕是先把整个 HTTP 响应读完再 yield 的 provider,也一样暴露这个流式签名,这样等以后真正的流式 provider 接进来时,接口不用改。`STTProvider.transcribe()` 是 STT 一侧的镜像原语:接收 bytes 或 async chunk iterator,加一个 `InputAudioFormat` 提示,返回 `TranscriptResult`。`InputAudioFormat` 的集合更宽(`mp3` / `wav` / `pcm16` / `webm` / `m4a` / `ogg`),对应浏览器、手机、语音留言 app 真实产出的容器格式——无法处理某种格式的 provider 应当抛 `VoicePermanentError`,而不是私自转码。

`VoiceService`(在 `src/echovessel/voice/service.py`)把一个 `TTSProvider` 和一个 `STTProvider` 与缓存目录、可选的 `FingerprintCache`(克隆幂等)、默认音频格式组合起来。Runtime 在启动时构造恰好一个实例,通过构造注入交给 channels 和 proactive scheduler;上层代码永远不碰 provider 实例,这就是为什么换 provider(比如从 FishAudio 换到 stub 做离线演示)只需要改一处配置。

`generate_voice(text, *, voice_id, message_id, tone_hint="neutral") -> VoiceResult` 是 channel 每次 persona 回复都会调一次的高层入口。它的契约是:

1. **按 `message_id` 幂等。** 同一 message id 的第二次调用返回缓存音频、`cached=True`、`cost_usd=0.0`,并且绝对不碰底层 provider。
2. **原子落盘。** 合成路径先写 `<cache_dir>/<message_id>.mp3.tmp`,`fsync` 文件句柄,再 `os.replace` 到最终名。中途崩溃永远不会留下一个半截的缓存文件。
3. **写死的成本估算。** `estimate_tts_cost(provider, text)` 用 `len(text)` 乘以 `src/echovessel/voice/pricing.py` 里一张小表给出的每字符 USD 费率。估算刻意不是实时计费查询——权威数字在 provider 自己的 dashboard 上,`VoiceService` 在构造时就记一行 disclaimer 日志,让每次进程都留下面包屑:这个字段是估值。任何在 UI 里显示 `cost_usd` 的地方都必须标注为估算值。
4. **MVP 的 tone hint 处理。** `tone_hint` 只认 `"neutral"`。传 `"tender"` 或 `"whisper"` 会记一条 warning 然后静默回退到 neutral 路径;返回的 `VoiceResult.provider` 不会带任何 tone 标记。支持更多取值是后续的事。
5. **错误原样冒上去。** 空文字的 `ValueError`、5xx / 超时 / 限流的 `VoiceTransientError`、4xx / 鉴权 / 非法 voice id 的 `VoicePermanentError`、配额耗尽的 `VoiceBudgetError`——都直接从 `generate_voice()` 抛出去,不被吞。如果 `speak()` 抛错,缓存文件不会留下任何残片。

FishAudio 那条路径是模块里唯一一处对 async-I/O 规则的合法例外。`fish-audio-sdk` 是同步 SDK,于是 `FishAudioProvider.speak()` 在 `asyncio.to_thread(...)` 里收 chunk 以避免阻塞事件循环。这一点在 `src/echovessel/voice/fishaudio.py` 里有明确记录,也是整个 voice 模块唯一允许的阻塞 I/O 例外——其他所有网络调用都走 async client(`httpx.AsyncClient` 或 `openai.AsyncOpenAI`)。

声音克隆是独立于 per-message 合成的另一条路径。`VoiceService.clone_voice_interactive(sample, *, name)` 读样本字节、算一个稳定指纹(`sha256:<hex>:<size>`)、查 `~/.echovessel/voice-cache.json` 的 `FingerprintCache`:命中就返回缓存的 `CloneEntry`,未命中就调 `TTSProvider.clone_voice()` 真正上传,再把结果写缓存。对同一个文件跑两次 `echovessel voice clone sample.wav`,网络侧只有一次请求。把拿到的 `voice_id` 写进 `config.toml` 是 CLI 子命令的事,不是 service 的事。

错误层次有意浅薄,且在语义上与 LLM 模块的重试语义对齐,这样上层可以共用一套 `try/except` 模式:

```
VoiceError
  ├── VoiceTransientError   (可重试:5xx / 超时 / 限流)
  └── VoicePermanentError   (不重试:4xx / 鉴权 / 非法 voice_id)
        └── VoiceBudgetError (配额耗尽——禁用 voice 直到下次重启)
```

上游 channel 在每次调用外包一个 `except VoiceError`,优雅降级为纯文字回复。Runtime 额外捕获 `VoiceBudgetError`,翻一个进程内开关,直到 daemon 重启前都不再触发任何 voice 操作。

### `generate_voice` 的数据流

```
          text  voice_id  message_id  tone_hint
            │      │         │          │
            │      │         │          │ (非 neutral 记 warning 并回退)
            ▼      ▼         ▼          ▼
        ┌──────────────────────────────────┐
        │   VoiceService.generate_voice    │
        └──────────────┬───────────────────┘
                       │
              ┌────────┴────────┐
              │  缓存查询       │  <voice_cache_dir>/<message_id>.mp3
              └────────┬────────┘
                       │
          ┌────────────┴────────────┐
          │                         │
       命中│                         │ 未命中
          │                         │
          ▼                         ▼
   VoiceResult(              TTSProvider.speak(text, voice_id, format="mp3")
     cached=True,                    │
     cost_usd=0.0,                   ▼
     ... )                 收集 async 分片 → audio_bytes
                                     │
                                     ▼
                     原子写盘:tmp + fsync + os.replace
                                     │
                                     ▼
                         estimate_tts_cost(provider, text)
                                     │
                                     ▼
                              VoiceResult(
                                url="/api/chat/voice/<message_id>.mp3",
                                duration_seconds=<启发式>,
                                provider=<provider_name>,
                                cost_usd=<估算>,
                                cached=False,
                              )
```

## How to Extend

### 1. 加一个新的 TTS provider

一个新 provider 就是任何满足 `TTSProvider` Protocol(`src/echovessel/voice/base.py`)的类。没有基类要继承——Protocol 是 `runtime_checkable` 且结构化的,实现对应方法即可。最小表面是:`provider_name` / `is_cloud` / `supports_cloning` / `speak()` / `clone_voice()` / `list_voices()` / `health_check()`。

```python
# src/echovessel/voice/myprovider.py
from collections.abc import AsyncIterator
from pathlib import Path

from echovessel.voice.base import AudioFormat, VoiceMeta
from echovessel.voice.errors import VoicePermanentError


class MyTTSProvider:
    def __init__(self, *, api_key: str | None) -> None:
        self._api_key = api_key

    @property
    def provider_name(self) -> str:
        return "myprovider"

    @property
    def is_cloud(self) -> bool:
        return True

    @property
    def supports_cloning(self) -> bool:
        return False

    async def speak(
        self, text: str, *, voice_id: str | None = None,
        format: AudioFormat = "mp3",
    ) -> AsyncIterator[bytes]:
        if not text:
            raise ValueError("speak: text is empty")
        # ... 调 async HTTP 客户端,yield 分片 ...
        yield b""  # 占位

    async def clone_voice(self, sample, *, name: str) -> str:
        raise NotImplementedError

    async def list_voices(self) -> list[VoiceMeta]:
        return []

    async def health_check(self) -> bool:
        return bool(self._api_key)
```

然后在 `src/echovessel/voice/factory.py` 的 `build_tts_provider` 里加一条分支把它注册进去:

```python
if provider == "myprovider":
    from echovessel.voice.myprovider import MyTTSProvider
    api_key = os.environ.get(api_key_env) if api_key_env else None
    return MyTTSProvider(api_key=api_key)
```

在 `config.toml` 里把 `[voice] tts_provider` 设成 `"myprovider"`,下次启动 factory 就会挑起新类。`tests/voice/` 下按 FishAudio 测试的布局添上一套——stub 是"最小绿"测试长什么样的参考。

### 2. 加一个新的 STT provider

同样是结构化 Protocol 的故事,只是目标换成 `STTProvider`。表面更小:`provider_name` / `is_cloud` / `transcribe()` / `health_check()`。

```python
# src/echovessel/voice/mystt.py
from collections.abc import AsyncIterator

from echovessel.voice.base import InputAudioFormat, TranscriptResult
from echovessel.voice.errors import VoicePermanentError


class MySTTProvider:
    def __init__(self, *, api_key: str | None) -> None:
        self._api_key = api_key

    @property
    def provider_name(self) -> str:
        return "mystt"

    @property
    def is_cloud(self) -> bool:
        return True

    async def transcribe(
        self,
        audio: bytes | AsyncIterator[bytes],
        *,
        language: str | None = None,
        format: InputAudioFormat = "wav",
    ) -> TranscriptResult:
        if isinstance(audio, (bytes, bytearray)):
            data = bytes(audio)
        else:
            data = b"".join([chunk async for chunk in audio])
        if not data:
            raise VoicePermanentError("no speech detected")
        text = await self._call_api(data, language=language, fmt=format)
        return TranscriptResult(text=text, language=language)

    async def health_check(self) -> bool:
        return bool(self._api_key)
```

在 `src/echovessel/voice/factory.py` 的 `build_stt_provider` 里仿照 `whisper_api` 那条加一条 `if provider == "mystt":` 分支。务必守好契约:静音 / 无语音的输入应当抛 `VoicePermanentError`,而不是返回空字符串——channel 依赖这一点给用户显示一条准确的"没听到语音"提示。

### 3. 克隆一个声音

克隆用的是和 per-message 合成同一个 `VoiceService` 实例。用样本(原始 bytes 或 `Path`)和一个可读名字调 `clone_voice_interactive`,然后把返回的 `voice_id` 写进 persona 配置。

```python
from pathlib import Path

from echovessel.voice.factory import (
    VoiceServiceConfig,
    build_voice_service,
)

async def register_voice(sample_path: Path, label: str) -> str:
    cfg = VoiceServiceConfig(
        tts_provider="fishaudio",
        stt_provider="whisper_api",
        tts_api_key_env="FISH_API_KEY",
        stt_api_key_env="OPENAI_API_KEY",
        clone_cache_path=Path.home() / ".echovessel" / "voice-cache.json",
    )
    svc = build_voice_service(cfg)
    entry = await svc.clone_voice_interactive(sample_path, name=label)
    # `entry.voice_id` 此刻已在本地缓存。用同一个样本文件再跑一次
    # 这个函数,在网络层面是无操作。
    return entry.voice_id
```

然后改 `config.toml` 的 persona section:

```toml
[persona]
voice_id = "v_abc123"   # 贴入上面返回的 id
```

下次 daemon 启动时会通过 `VoiceServiceConfig.default_voice_id` 接到新 id,从此每次 `generate_voice()` 都用这副音色合成。因为 `clone_voice_interactive` 带指纹缓存,对同一段样本再次跑注册(例如在一个给多个环境做 provision 的脚本里)在第一次上传之后就零成本。
