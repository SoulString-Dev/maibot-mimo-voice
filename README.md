# maibot-mimo-voice

把麦麦（MaiBot）的**纯文本回复**按策略换成 MiMo 语音条。开箱自带两套标准音色（冰糖 / 茉莉），也可以换成自己的参考音频。

思路参考 [Ling-LA/ling-tts-bot](https://github.com/Ling-LA/ling-tts-bot)（MIT），本仓库在触发方式、文本清洗与工程细节上做了重写，差异见下表。

## 它能做什么

- 语音条**替换**文字：命中的那条回复只发语音，不发文字。
- 四种触发方式：纯概率、概率 + 模型主动请求、语音优先、全量。
- 群聊 / 私聊各自独立的 `all / whitelist / blacklist` 放行策略。
- 合成失败或超时默认**回退发文字**，消息不会丢。
- 合成前清洗文本（表情、引号、波浪号、省略号、重复标点），并用风格指令压住音色漂移。
- 英文处理策略可配：默认只删 `ww` / `emmm` / `ohhhhhhh` 这类复读口癖，`LLM`、`token`、`cos` 等真词原样朗读。

| 与上游的差异 | 原因 |
|---|---|
| 内置冰糖 / 茉莉两套参考音频，`voice.preset` 一键切换 | 自带的克隆样本不具普适性；开箱就能出声，想定制再换 `custom` |
| 默认 `random` + `probability = 0.15` + 短句门槛 60 字 | 全量转语音太激进；语音集中在短句上更像真人插话。0.15 下大约每 7–10 条回复出一条语音，既有存在感又不吵 |
| `random_llm` 双触发：随机抽签 + 模型可调 `mimo_voice_say` 主动要语音 | 群友想听声音时能立刻出声，日常仍由概率控制频率 |
| 支持群聊 / 私聊 `all / whitelist / blacklist` | 灰度上线时可以先只在指定群或私聊里出声 |
| 参考音频支持固定单文件，也保留 `best_single / balanced / full_merge` | 单文件不需要 ffmpeg；需要自动挑选 / 拼接时仍可用原策略 |
| 朗读前清洗文本 + `style_instruction` 风格指令 | 表情 / 引号 / 波浪号 / 省略号会让音高漂移 2–4 个半音并带来首尾爆音，清洗后 σ 从 34.6Hz 降到 5.5–13.6Hz |
| `temperature` 默认 0.15 | 实测同一句三次的中位音高漂移从 ~11Hz 降到 ~2Hz |
| 合成超时默认 20 秒，超时回退文字且**不重试** | 上游 TTS p95 约 48 秒，不能把回复拖到一分钟 |
| 合成并发默认 1 路，满了直接发文字（`skip_when_busy`） | 3 路并发会把单路延迟推到 50–96 秒 |
| 参考音频路径限制在插件目录内 | 防止配置被改成 `/etc/...` 之类的越界读取 |
| 不做音频缓存 | LLM 生成的回复几乎不会逐字重复，缓存命中率极低 |

## 安装

1. 把仓库放进麦麦的插件目录（如 `data/MaiMBot/plugins/maibot-mimo-voice`）。
2. 复制 `config.example.toml` 为 `config.toml`，填好 `[mimo] api_key`。
3. 音色默认用内置的冰糖（`voice.preset = "bingtang"`），不需要自己准备参考音频；
   想换成自己的声音时，把参考音频放进插件目录并把 `voice.preset` 改成 `custom`。
4. 生效方式：重启核心，或者直接改插件目录里的文件——麦麦的文件监视会自动重载扩展插件。

维护与发版细节见 [`docs/MAINTAINING.md`](docs/MAINTAINING.md)。

依赖：`aiohttp`（麦麦插件运行时已自带）。只有用 `best_single` / `balanced` / `full_merge` 参考策略时才需要宿主机上的 `ffmpeg`。

## 内置音色

| `voice.preset` | 音色 | 说明 |
|---|---|---|
| `bingtang` | 冰糖（默认） | MiMo 预置女声，按「升 12% 音高 + 22% 总语速」生成，约 2.9 秒参考 |
| `jasmine` | 茉莉 | 比冰糖略低、更柔和一些，约 3.2 秒参考 |
| `custom` | 自定义 | 用 `voice.reference_path` 指向你自己的参考音频（放插件数据目录的 `references/` 下） |

两套内置参考都是**合成音色**（不是真人录音），随插件分发。
生成方式：先用 MiMo 预置音色生成原始采样，再跑 `tools/build_reference.sh`（升调 1.12、`atempo` 1.08、裁首尾静音）。

## 换成自己的声音

自己的参考音频请放在**插件数据目录**的 `references/` 下，和插件代码分开放，升级插件不会覆盖：

```
data/plugins/github.SoulString-Dev.maibot-mimo-voice/
└── references/
    └── 我的音色.wav
```

目录不存在时插件会自动创建，启动日志里也会打印完整路径（`参考音频目录：…`）。
`voice.reference_path` 只写文件名即可（为兼容旧版本，放在插件根目录也仍能读到）。

推荐在**宿主机**上做，不用进容器：

```bash
# 1) 准备一段 5–20 秒、安静环境录的干净人声（或从喜欢的主播 / TTS 音色里截一段）
# 2) 按需做音高 / 语速调整，例如"升 12% / 提速 8%"：
ffmpeg -i source.wav -af "aresample=24000,asetrate=24000*1.12,aresample=24000,atempo=1.08" -ac 1 -ar 24000 reference.wav
```

> 实测：参考音频升调后，克隆输出会跟着抬高（1.06× 参考 → 输出约 1.05×），
> 所以"预设音色 → 升调提速 → 当参考"只需做一次。

把文件放进 `references/`（上面那个目录），配置改成：

```toml
[voice]
preset = "custom"
reference_path = "我的音色.wav"
```

`tools/build_reference.sh` 是上面命令的封装，额外做了首尾静音裁剪与 20ms 淡入淡出。

## 触发方式

| `trigger.mode` | 行为 |
|---|---|
| `random`（默认） | 每条回复按 `probability` 抽签；抽中且不超过 `short_reply_max_chars` 才转语音 |
| `random_llm` | 随机抽签照常，同时给 planner 暴露 `mimo_voice_say`，模型可以主动要求"这条用语音回" |
| `llm_trigger` | 默认语音优先，模型可调 `mimo_voice_text_reply` 把这一轮改回文字（代码、表格、链接等） |
| `voice_only` | 全部转语音（仍受 `max_text_length` 与放行策略约束） |

- 同一聊天流在 `trigger.decision_ttl`（默认 10 秒）内复用上一次的抽签结果，避免连发多条时概率失真。
- **默认概率 0.15**：低于 0.1 时语音出现得太少、几乎察觉不到；想更频繁可以调到 0.25–0.4，想安静一点降到 0.05。再叠加 `short_reply_max_chars` 与 `random_requires_chinese` 的过滤，实际出语音的比例会略低于这个数。
- 两种模型工具只在对应模式下暴露，其它模式自动隐藏，防止误调。
- `random_requires_chinese = true`（默认）时，随机抽中的那条如果清洗后仍含英文字母，就直接发文字——`token`、`LLM` 这类会被逐字母念出来。模型主动请求的语音、`llm_trigger`、`voice_only` 不受此限制。

## 群聊 / 私聊过滤

两者互不影响，例如"群聊只放行一个群、私聊全放行"：

```toml
[filters]
group_mode = "whitelist"
group_ids = ["123456789"]
private_mode = "all"
```

`group_mode` 与 `private_mode` 都不是 `all` 时，插件会记录入站消息的群号 / 对方 QQ 号（`chat.receive.before_process`），出站时按名单判断；两者都是 `all` 时不记录，零额外开销。拿不到对方身份时（映射过期等）一律保持文字，不误放行。

## 配置说明（共 37 项，日常只需动前 12 项）

### 基础

| 配置 | 默认 | 说明 |
|---|---|---|
| `plugin.enabled` | `true` | 总开关 |
| `mimo.api_key` | 空 | MiMo 官方 API Key |
| `voice.preset` | `bingtang` | 音色：`bingtang` / `jasmine` / `custom` |
| `trigger.mode` | `random` | 见「触发方式」 |
| `trigger.probability` | `0.15` | `random` / `random_llm` 模式下每条回复转语音的概率。低于 0.1 基本感知不到，太高会显得吵 |
| `trigger.short_reply_max_chars` | `60` | 随机转语音的字数门槛（0 = 不限制）；模型主动请求不受限 |
| `general.max_text_length` | `300` | 单条语音最多念多少字，超出按 `overflow_policy` 处理 |
| `general.timeout` | `20.0` | 单次合成超时（秒），超时不重试 |
| `mimo.temperature` | `0.15` | 越低越稳；0.7 以上能听出"换人" |
| `voice.style_instruction` | 见模板 | 随请求发送的朗读风格指令，留空则不发 |
| `delivery.fallback_to_text` | `true` | 合成失败 / 超时时发文字还是拦截 |
| `delivery.skip_when_busy` | `true` | 并发路数占满时直接发文字，不排队 |
| `delivery.timbre_match` | `true` | 音色对齐：按参考音频的频谱补偿每条输出（需要 WAV 参考，默认开） |
| `delivery.timbre_match_strength` | `1.0` | 音色对齐强度 0–1，补偿量始终限制在 ±6 dB；与参考差距 >8 dB 时自动按比例减弱 |

### 进阶

| 配置 | 默认 | 说明 |
|---|---|---|
| `plugin.config_version` | `0.5.2` | 配置版本号（内部使用，无需修改） |
| `mimo.api_base_url` | `https://api.xiaomimimo.com/v1` | 必须是 https 公网地址；写成内网/本机地址会自动回退官方地址并告警 |
| `mimo.model` | `mimo-v2.5-tts-voiceclone` | 也可用 `mimo-v2.5-tts`（预置音色） |
| `mimo.audio_format` | `mp3` | `mp3` / `wav`；开了任一音频后处理（音色对齐 / 尾音裁剪 / 补尾音 / 峰值归一）时自动改用 `wav`，此项实际被忽略 |
| `mimo.max_concurrent` | `1` | 同时合成路数，建议保持 1 |
| `mimo.retry_attempts` | `0` | 仅对 429 / 5xx / 网络错误重试 |
| `mimo.retry_on_timeout` | `true` | 单次合成超时后换一次请求重来（只重试一次） |
| `mimo.retry_backoff_seconds` | `1.5` | 重试退避基数，按 2 的幂增长 |
| `mimo.connect_timeout` | `10.0` | 建连超时（秒），与整体超时分开 |
| `mimo.allow_private_base_url` | `false` | 允许把音频发往内网/本机地址（只有自建代理时才需要） |
| `voice.reference_path` | `reference.wav` | 仅 `preset = custom` 时生效，填 `references/` 下的文件名 |
| `voice.reference_strategy` | `single_file` | 需要 ffmpeg 的策略：`best_single` / `balanced` / `full_merge` |
| `voice.auto_prepare_reference` | `true` | 参考 WAV 超过 5 秒或静音偏多时，自动按句拼接成 3–5 秒的干净样本并缓存（0.5.2 新增） |
| `trigger.decision_ttl` | `10.0` | 判定有效期（秒） |
| `trigger.overflow_policy` | `text` | 超长时：`text` 保留文字；`truncate` 截断后合成 |
| `trigger.english_policy` | `strip_tics` | `keep` 原样念；`strip_tics` 只删复读口癖；`strip` 删所有拉丁串（会误伤真词） |
| `trigger.random_requires_chinese` | `true` | 随机路径遇英文改发文字 |
| `delivery.trim_tail_silence_ms` | `250` | 尾部静音超过这个毫秒数就裁掉（只留 150ms，0.5.2 前是 50ms），0 = 不裁 |
| `delivery.tail_pad_ms` | `120` | 处理完后补多少毫秒尾静音，避免播放端吃掉最后一个字 |
| `delivery.peak_normalize` | `true` | 把峰值统一拉到 −1.5 dBFS，解决有的条小声有的条刺耳；单条最多提 +6 dB 并软限幅，避免放大破音 |
| `filters.group_mode` | `all` | 群聊放行：`all` / `whitelist` / `blacklist` |
| `filters.private_mode` | `all` | 私聊放行：`all` / `whitelist` / `blacklist` |
| `filters.group_ids` | `[]` | 群号列表 |
| `filters.private_user_ids` | `[]` | 私聊对方 QQ 号列表 |

旧版本的残留键会被自动忽略（`extra="ignore"`），升级时不需要手工清理 `config.toml`。

## 线上表现

自 2026-10-24 起在真实群聊中运行，语音条数占 bot 消息的比例：

| 日期 | bot 消息 | 语音条 | 占比 |
|---|---|---|---|
| 10-24 | 257 | 25 | 9.7% |
| 10-25 | 159 | 12 | 7.5% |
| 10-26 | 410 | 28 | 6.8% |
| 10-27 | 427 | 43 | 10.1% |
| 10-28 | 354 | 51 | 14.4% |
| 10-29（到 14:40） | 358 | 50 | 14.0% |

**为什么 10-27 的占比突然上来了？** 不是因为 bot 变得更爱说话，而是 10-26 那次上游抖动把语音吃掉了：

- 能被抽中的"候选回复"并没有变多：10-26 是 303 条、10-27 反而只有 257 条（口径：单条文本、≤60 字、不含 2 个以上连续英文）。
- 变的是"抽中之后能不能合成成功"：10-26 合成 33 次成功 / 23 次超时失败（**41%**），10-27 是 44 / 12（21%），10-28 加上 0.3.1 的超时重试后是 55 / 0。
  失败会回退成文字，所以 10-26 的抽中量被消耗掉近一半，表里只剩 6.8%。
- 一个小放大器：`trigger.decision_ttl = 10` 让同一个聊天流在 10 秒内复用上一次的抽签结果，实测 20% 的语音与同会话上一条语音相隔 ≤10 秒 —— 偶尔一次"中签"会连着出两条。

0.3.0（10-26 22:21）上线后到 10-29 下午共 **144 条**语音：

- 含拉丁字母 **0 条**，含 `ww` / `emmm` / `ohhh` 口癖 **0 条**（此前两天分别是 9 条和 4 条）。
- 全部为单段 `voice`，没有引用段残留（QQ 不支持「引用 + 语音」）。
- 同期 bot 文本中仍有 **25.8%**（172 / 667）带 2 个以上连续拉丁字母，这些在随机路径下被跳过、改发文字。

合成失败全部是 `20s` 超时后的回退：10-26 占 41%、10-27 占 21%，都集中在上游变慢的窗口；
0.3.1（10-28 凌晨）加上超时重试后，10-28 全天 0 失败（重试只被用上 1 次）。

## 目录结构

```
<插件目录>/
├── plugin.py                    # 入口：生命周期 / Hook / 过滤 / 合成编排
├── core/                        # 可复用的纯逻辑
│   ├── constants.py             # 阈值与默认值
│   ├── config.py                # 配置模型 + api_base_url 校验
│   ├── text.py                  # 朗读清洗 / 英文口癖策略
│   └── audio.py                 # WAV 读写 / 频谱曲线 / 音色对齐矩阵
├── tests/                       # 79 条断言，不依赖 pytest
├── config.example.toml
├── _manifest.json
├── assets/icon.svg              # 商店图标
├── reference-bingtang.wav       # 内置参考（随插件发布）
├── reference-jasmine.wav
└── test.wav                     # 你自己的参考（仅本机，已被 .gitignore 忽略）

<插件数据目录>/                   # 宿主提供，升级不会覆盖
└── references/                  # 自定义参考统一放这里
    └── my-voice.wav
```

数据目录一般位于 `data/plugins/<插件 id>/`，启动日志会打印一行 `参考音频目录：...`。
`voice.reference_path` 只写文件名（例如 `my-voice.wav`），插件会先找数据目录，再回退插件根目录（兼容旧版布局）。

## FAQ

**为什么这条回复没出语音？** 随机模式下要同时满足四条：

1. `trigger.probability` 抽中（默认 15%）；
2. 文本不超过 `trigger.short_reply_max_chars`（默认 60 字）；
3. 文本里没有 2 个以上连续英文（`trigger.random_requires_chinese`）；
4. 该群 / 私聊在 `filters` 放行名单里。

模型主动请求（`random_llm` 模式的 `mimo_voice_say`）只跳过第 2 条，其余仍生效。

**明明抽中了，收到的却是文字？** 合成超时（默认 20 秒）、上游 5xx、返回静音或音频异常都会回退文字，日志里有一行 `语音合成失败（...），回退文字`。

**听起来不像参考音频？** 参考换成 2–5 秒、安静环境、单人、无背景音乐的干声；`voice.auto_prepare_reference`（默认开）会把过长的参考自动切句拼到 3–5 秒并做响度归一，启动日志里对过长的参考也会给出提示。`delivery.timbre_match` 默认开启，能补一部分频谱差，但补不了混响和削顶失真。

## 已知限制

- QQ 不支持「引用 + 语音」组合，转语音时会去掉引用段。
- `random_requires_chinese` 只约束随机路径；模型主动请求的语音如果文本里有英文，仍会被逐字母念出来（此时可把 `english_policy` 设为 `strip`，代价是 `token`、`cos` 这类词也会被删）。
- `strip_tics` 的口癖白名单基于「整词重复辅音 / 元音 + 重复辅音≥3」的启发式，`yyds`、`awsl`、`nb` 这类网络缩写不会被删。
- 上游 TTS 延迟抖动大（p50 约 5 秒、p95 约 48 秒），超时时间不建议压到 10 秒以下。
- 单路并发是有意为之：3 路并发会让单路延迟翻十倍。

## 隐私

参考音频只保存在本地（插件目录，或插件数据目录下的 `references/`）；参考瘦身结果与 ffmpeg 参考策略的处理结果都缓存在宿主提供的 runtime 目录下的 `reference-cache/`（拿不到时退回插件目录）。

除了随请求发给你配置的 TTS 服务（当前实现为小米 MiMo 官方 API：待合成文本与参考音频会发往 `api.xiaomimimo.com`）外，不会上传到任何地方。`mimo.api_base_url` 决定请求发往哪里，请只填写可信地址。

## 关于 ffmpeg（可选依赖）

插件默认 `voice.reference_strategy = "single_file"`，**不调用任何外部命令**，所以大多数用户不需要装 ffmpeg。

只有把 `reference_strategy` 改成 `best_single` / `balanced` / `full_merge` 时，才会用宿主机的 `ffmpeg` 对**你自己提供的参考音频**做裁静音 / 截段 / 合并 / 重采样：

- 调用方式是固定参数列表（`subprocess.run([...], check=True)`），不经 shell、不做字符串拼接，也没有任何聊天内容或用户输入拼进命令行；
- 输入只有 `voices/` 目录下的音频文件和你配置的数值；
- 不联网、不下载任何东西，只在本机做转码；
- 处理结果缓存在 runtime 目录的 `reference-cache/`，同一份参考音频只处理一次。

宿主机没有 `ffmpeg` 时，插件会在加载时退回 `single_file`，不会报错。

## 更新日志

### 0.5.2

- **参考音频自动瘦身**（`voice.auto_prepare_reference`，默认开）：参考 WAV 超过 5 秒或静音偏多时，纯 numpy 按句切分、拼接到 3–5 秒、峰值归一到 −3 dBFS，缓存复用；实测把 8.41 秒 / 4 段话 / 18.5% 静音的参考压到 3.83 秒 / 静音 6.8%，与人工挑选的片段平均频谱差 0.16 dB。
- 启动体检：参考超过 6 秒、静音超过 15% 或最长静音超过 0.4 秒时打 warning，提示音色漂移风险（"2–5 秒单句"效果最好）。
- 后处理修正：峰值归一上限从 +12 dB 收紧到 **+6 dB** 并加软限幅（不再放大破音）；音色对齐与参考差距 >8 dB 时自动减强度、相邻频带平滑（减少发闷）；尾部保留 50 ms → **150 ms**。
- 不做"同一句合成两条挑一条"：实测 8 个客观指标与人工盲听标注重叠，自动挑等于抛硬币，反而翻倍成本与 429 概率。

### 0.5.1

- 内部拆分：常量、配置模型、朗读清洗与音频工具移入 `core/` 子包，`plugin.py` 从 1672 行降到 1134 行。
- **无行为变化**：配置结构、默认值、Hook 与发送链路全部未动，79 条断言 + `ruff` + `mypy` 通过；属于跟在 0.5.0 后面的小版本。

### 0.5.0

- 新增尾部处理三件套（裁尾静音 / 补尾巴 / 峰值归一到 −1.5 dBFS）与合成结果校验（时长、RMS、WAV 头）。
- `mimo.api_base_url` 增加校验：只接受 https 公网地址，内网/本机自动回退并告警。
- 后处理合并为一次解析写回（单条 8.4 → 6.9 ms），参考缓存只留 6 个并清理分片目录。
- 修复重构遗留的崩溃点（音色对齐失败会抛 `AttributeError`）、目标曲线不随参考失效、抽签结果被复用等 10 余处问题。
- 新增图标、单测（79 条断言）、ruff/mypy + CI、维护手记 `docs/MAINTAINING.md`。

完整历史见 [CHANGELOG.md](CHANGELOG.md)。

## 开发

```bash
# 语法与配置校验
python -m py_compile plugin.py core/*.py
python -c "import tomllib; tomllib.load(open('config.example.toml','rb'))"

# 静态检查 + 类型检查 + 单测（依赖见 requirements-dev.txt）
pip install -r requirements-dev.txt
ruff check .
mypy plugin.py core   # 需先装 maibot-plugin-sdk，否则基类被当作 Any，会漏掉 self.config 的类型问题
python tests/run_all.py     # 文本/音频/配置三组共 74 条断言
```

CI（`.github/workflows/ci.yml`）在每次 push / PR 上跑同样三条命令：ruff、mypy、单测。
测试不依赖 pytest，只用标准库，容器里 `/MaiMBot/.venv/bin/python tests/run_all.py` 也能直接跑。

部署到运行中的麦麦（示例：NAS 上的容器）：

```bash
tar czf /tmp/mv.tgz plugin.py core config.example.toml _manifest.json README.md
scp /tmp/mv.tgz user@nas:/tmp/
ssh user@nas 'cd <插件目录> && tar xzf /tmp/mv.tgz && sudo chown -R 1000:1000 .'
```

插件目录里的文件被改动后，麦麦的文件监视会自动重载扩展插件；`plugin.py` 的语法错误会让**所有**扩展插件一起停摆，改完务必先本地 `py_compile`。

仓库里只提交两套内置音色（`reference-bingtang.wav` / `reference-jasmine.wav`，白名单放行）；
你自己的参考音频和 `references/`、`voices/` 都已被 `.gitignore` 排除，不会被误提交。

回滚：`git checkout <上个版本> -- plugin.py` 后重新铺文件即可；配置项只增不减，旧配置一般无需改。

## License

MIT。上游 `ling-tts-bot` 版权所有 (c) 2026 Ling-LA，本项目保留其许可声明。
