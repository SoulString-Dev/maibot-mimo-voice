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

依赖：`aiohttp`（麦麦插件运行时已自带）。只有用 `best_single` / `balanced` / `full_merge` 参考策略时才需要宿主机上的 `ffmpeg`。

## 内置音色

| `voice.preset` | 音色 | 说明 |
|---|---|---|
| `bingtang` | 冰糖（默认） | MiMo 预置女声，按「升 12% 音高 + 22% 总语速」生成，约 2.9 秒参考 |
| `jasmine` | 茉莉 | 比冰糖略低、更柔和一些，约 3.2 秒参考 |
| `custom` | 自定义 | 用 `voice.reference_path` 指向插件目录内的参考音频 |

两套内置参考都是**合成音色**（不是真人录音），随插件分发。
生成方式：先用 MiMo 预置音色生成原始采样，再跑 `tools/build_reference.sh`（升调 1.12、`atempo` 1.08、裁首尾静音）。

## 换成自己的声音

推荐在**宿主机**上做，不用进容器：

```bash
# 1) 准备一段 5–20 秒、安静环境录的干净人声（或从喜欢的主播 / TTS 音色里截一段）
# 2) 按需做音高 / 语速调整，例如"升 12% / 提速 8%"：
ffmpeg -i source.wav -af "aresample=24000,asetrate=24000*1.12,aresample=24000,atempo=1.08" -ac 1 -ar 24000 reference.wav
```

> 实测：参考音频升调后，克隆输出会跟着抬高（1.06× 参考 → 输出约 1.05×），
> 所以"预设音色 → 升调提速 → 当参考"只需做一次。

把文件放进插件目录，配置改成：

```toml
[voice]
preset = "custom"
reference_path = "reference.wav"
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

## 配置说明（共 31 项，日常只需动前 12 项）

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

### 进阶

| 配置 | 默认 | 说明 |
|---|---|---|
| `plugin.config_version` | `0.3.1` | 配置版本号（内部使用，无需修改） |
| `mimo.api_base_url` | `https://api.xiaomimimo.com/v1` | 只填可信地址 |
| `mimo.model` | `mimo-v2.5-tts-voiceclone` | 也可用 `mimo-v2.5-tts`（预置音色） |
| `mimo.audio_format` | `mp3` | `mp3` / `wav` |
| `mimo.max_concurrent` | `1` | 同时合成路数，建议保持 1 |
| `mimo.retry_attempts` | `0` | 仅对 429 / 5xx / 网络错误重试 |
| `mimo.retry_on_timeout` | `true` | 单次合成超时后换一次请求重来（只重试一次） |
| `mimo.retry_backoff_seconds` | `1.5` | 重试退避基数，按 2 的幂增长 |
| `mimo.connect_timeout` | `10.0` | 建连超时（秒），与整体超时分开 |
| `voice.reference_path` | `reference.wav` | 仅 `preset = custom` 时生效，必须在插件目录内 |
| `voice.reference_strategy` | `single_file` | 需要 ffmpeg 的策略：`best_single` / `balanced` / `full_merge` |
| `trigger.decision_ttl` | `10.0` | 判定有效期（秒） |
| `trigger.overflow_policy` | `text` | 超长时：`text` 保留文字；`truncate` 截断后合成 |
| `trigger.english_policy` | `strip_tics` | `keep` 原样念；`strip_tics` 只删复读口癖；`strip` 删所有拉丁串（会误伤真词） |
| `trigger.random_requires_chinese` | `true` | 随机路径遇英文改发文字 |
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
| 10-28（半天） | 24 | 3 | 12.5% |

0.3.0（10-26 22:21）上线后共 46 条语音：

- 含拉丁字母 **0 条**，含 `ww` / `emmm` / `ohhh` 口癖 **0 条**（此前两天分别是 9 条和 4 条）。
- 全部为单段 `voice`，没有引用段残留（QQ 不支持「引用 + 语音」）。
- 同期 bot 文本中仍有 23.9% 带 2 个以上连续拉丁字母，这些在随机路径下被跳过、改发文字。

合成失败（去重后 43 次）全部是 `20s` 超时后的回退，集中在 10-27 14:07–20:51 上游变慢的窗口；10-28 全天 0 次。

## 已知限制

- QQ 不支持「引用 + 语音」组合，转语音时会去掉引用段。
- `random_requires_chinese` 只约束随机路径；模型主动请求的语音如果文本里有英文，仍会被逐字母念出来（此时可把 `english_policy` 设为 `strip`，代价是 `token`、`cos` 这类词也会被删）。
- `strip_tics` 的口癖白名单基于「整词重复辅音 / 元音 + 重复辅音≥3」的启发式，`yyds`、`awsl`、`nb` 这类网络缩写不会被删。
- 上游 TTS 延迟抖动大（p50 约 5 秒、p95 约 48 秒），超时时间不建议压到 10 秒以下。
- 单路并发是有意为之：3 路并发会让单路延迟翻十倍。

## 隐私

参考音频只保存在本地插件目录；使用 ffmpeg 参考策略时，处理结果缓存在宿主提供的 runtime 目录下的 `reference-cache/`（拿不到时退回插件目录）。

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

### 0.3.1

- 新增 `mimo.retry_on_timeout`（默认 `true`）：单次合成超过 `general.timeout` 后换一次请求重来，**最多只重试一次**。
- 依据：线上 27 次实测中，超时的请求大多直接跳到 30 秒以上（30.6 / 31.6 / 35.6 / 63.3 / 163.3 秒），把超时放宽到 30 秒几乎救不回来；而"再给一次机会"按两条独立估能把失败率从约 17% 压到 3–4%，正常时段体感不变。
- 代价：最坏情况下要等两个超时周期（默认 40 秒）才回退文字。
- 默认 `trigger.probability` 从 `0.05` 提到 `0.15`：0.05 下语音出现得太少，实际体感接近"不会说话"。
- 文档：新增「关于 ffmpeg（可选依赖）」一节，说明 `subprocess` 只出现在可选的参考音频处理策略里，参数固定、不经 shell、不联网。

### 0.3.0

- 新增内置音色 `voice.preset = bingtang | jasmine | custom`，自带冰糖 / 茉莉两套标准参考音频，开箱即用。
- 配置项从 40 个精简到 30 个：删掉 `top_p`、`sample_rate`、`voice_dir`、`ffmpeg_path`、`max_clip_duration`、`balanced_clip_duration`、`silence_threshold_db`、`reference_bitrate_kbps`、`max_reference_base64_mb`；三个判定有效期合并为 `trigger.decision_ttl`。
- 配置模板重排为「基础 / 进阶」两段，给所有枚举项标注了下拉候选，`plugin.config_version` 同步 0.3.0；旧 `config.toml` 的残留键会被自动忽略。
- 新增 `trigger.english_policy`（`keep` / `strip_tics` / `strip`）：默认只删 `ww`、`emmm`、`ohhhhhhh` 这类复读口癖，`LLM`、`token`、`cos` 等真词原样朗读。
- 新增 `trigger.random_requires_chinese`（默认开）：随机抽中的那次如果清洗后仍含英文，就直接发文字；模型主动请求、`llm_trigger`、`voice_only` 不受影响。

### 0.2.2

- 修复 `tools/build_reference.sh` 在 ffmpeg 7.1 及以上版本直接失败的问题：淡出改用「反转 → 20ms 淡入 → 再反转」，不再依赖 `afade` 的负数 `st`。

### 0.2.1

- 新增 `trigger.mode = "random_llm"` 与 `mimo_voice_say` 工具：随机抽签 + 模型主动请求双触发。
- 合成前清洗文本（表情、引号、波浪号、省略号、重复标点），并补 `style_instruction` 压住音高漂移。
- 硬化：空音频 / 过短音频判定、超时不再重试、可重试错误指数退避、建连超时独立、并发满直接发文字不排队、参考音频路径限制在插件目录内、工具加 `visibility`。

### 0.2.0

- 首个发布版本。

## 开发

```bash
# 语法与配置校验
python -m py_compile plugin.py
python -c "import tomllib; tomllib.load(open('config.example.toml','rb'))"
```

部署到运行中的麦麦（示例：NAS 上的容器）：

```bash
tar czf /tmp/mv.tgz plugin.py config.example.toml _manifest.json README.md
scp /tmp/mv.tgz user@nas:/tmp/
ssh user@nas 'cd <插件目录> && tar xzf /tmp/mv.tgz && sudo chown -R 1000:1000 .'
```

插件目录里的文件被改动后，麦麦的文件监视会自动重载扩展插件；`plugin.py` 的语法错误会让**所有**扩展插件一起停摆，改完务必先本地 `py_compile`。

回滚：`git checkout <上个版本> -- plugin.py` 后重新铺文件即可；配置项只增不减，旧配置一般无需改。

## License

MIT。上游 `ling-tts-bot` 版权所有 (c) 2026 Ling-LA，本项目保留其许可声明。
