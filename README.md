# maibot-mimo-voice

把麦麦（MaiBot）的**纯文本回复**按策略替换成 MiMo 语音条，音色来自你自己提供的参考音频。

思路参考 [Ling-LA/ling-tts-bot](https://github.com/Ling-LA/ling-tts-bot)（MIT），本仓库做了取舍与重写，主要差异：

| 改动 | 原因 |
|---|---|
| 默认 `random` + `probability = 0.05` + 短句门槛 60 字 | 全量转语音太激进；语音集中在短句上更像真人插话 |
| `random_llm` 双触发：随机抽签 + 模型可调 `mimo_voice_say` 主动要语音 | 群友想听声音时能立刻出声，日常仍由概率控制频率 |
| 支持群聊/私聊 `all / whitelist / blacklist` | 灰度上线时可以先只在指定群或私聊里出声 |
| 参考音频支持固定单文件，也保留 `best_single / balanced / full_merge` | 单文件不需要 ffmpeg；需要自动挑选/拼接时仍可用原策略 |
| 朗读前清洗文本 + `style_instruction` 风格指令 | 表情/引号/波浪号/省略号会让音高漂移 2–4 个半音并带来首尾爆音，清洗后 σ 从 34.6Hz 降到 5.5–13.6Hz |
| `temperature` 默认 0.15 | 实测同一句三次的中位音高漂移从 ~11Hz 降到 ~2Hz |
| 合成超时默认 20 秒，超时回退文字且**不重试** | 上游 TTS p95 约 48 秒，不能把回复拖到一分钟 |
| 合成并发默认 1 路，满了直接发文字（`skip_when_busy`） | 3 路并发会把单路延迟推到 50–96 秒 |
| 参考音频路径限制在插件目录内 | 防止配置被改成 `/etc/...` 之类的越界读取 |
| 不做音频缓存 | LLM 生成的回复几乎不会逐字重复，缓存命中率极低 |

## 效果与取舍

- 语音条**替换**文字：命中的那条回复只发语音，不再发文字。
- 超时或失败默认**回退发文字**（`delivery.fallback_to_text = true`），保证消息不丢。
- QQ 不支持「引用 + 语音」组合，转语音时会自动去掉引用段。
- `llm_trigger` 模式下模型可以调 `mimo_voice_text_reply` 把这一轮改回文字（代码、表格、链接等）。
- `random_llm` 模式下模型可以调 `mimo_voice_say` 主动要语音；随机抽签同时保留，互不冲突。
- 两种模型工具只在对应模式下暴露给 planner，其它模式自动隐藏，避免误调。
- 合成前会把文本整理成"能念"的样子：去掉表情/零宽字符/首尾引号，`～` 和省略号折成逗号，
  重复感叹号收敛，末尾补齐句号。

## 安装

1. 把仓库放进麦麦的插件目录（如 `data/MaiBot/plugins/maibot-mimo-voice`）。
2. 复制 `config.example.toml` 为 `config.toml`，填好 `[mimo] api_key`。
3. 准备参考音频放到插件目录（见下一节），默认文件名 `reference.wav`。
4. 生效方式：重启核心，或者直接改插件目录里的文件——麦麦的文件监视会自动重载扩展插件。

依赖：`aiohttp`（麦麦插件运行时已自带）。

## 准备参考音频

推荐在**宿主机**上做，不用进容器：

```bash
# 1) 准备一段 5–20 秒、安静环境录的干净人声（或从喜欢的主播/TTS 音色里截一段）
# 2) 按需做音高/语速调整，例如"升 12% / 提速 8%"：
ffmpeg -i source.wav -af "aresample=24000,asetrate=24000*1.12,aresample=24000,atempo=1.08" -ac 1 -ar 24000 reference.wav
```

> 实测：参考音频升调后，克隆输出会跟着抬高（1.06× 参考 → 输出约 1.05×），
> 所以"预设音色 → 升调提速 → 当参考"只需做一次。

`tools/build_reference.sh` 是上面命令的封装。

## 配置说明

| 配置 | 默认 | 说明 |
|---|---|---|
| `trigger.mode` | `random` | `random` 按概率；`random_llm` 概率 + 模型主动请求；`llm_trigger` 语音优先、模型可改文字；`voice_only` 全部转语音 |
| `trigger.probability` | `0.05` | random / random_llm 模式下每条回复转语音的概率 |
| `trigger.llm_request_ttl` | `20.0` | random_llm 模式下，模型请求语音的有效期（秒） |
| `trigger.short_reply_max_chars` | `60` | 超过该字数不走随机转语音（0 = 不限制）；模型请求不受限 |
| `trigger.overflow_policy` | `text` | 超过 `general.max_text_length` 时保留文字；`truncate` 为截断后合成 |
| `filters.group_mode` | `all` | 群聊放行方式：`all` / `whitelist` / `blacklist` |
| `filters.private_mode` | `all` | 私聊放行方式：`all` / `whitelist` / `blacklist` |
| `filters.group_ids` | `[]` | 群号列表（配合 `group_mode`） |
| `filters.private_user_ids` | `[]` | 私聊对方 QQ 号列表（配合 `private_mode`） |
| `general.timeout` | `20.0` | 单次合成超时（秒），超时不重试 |
| `voice.style_instruction` | 见模板 | 随请求发送的朗读风格指令，留空则不发 |
| `mimo.temperature` | `0.15` | 越低越稳；0.7 以上能听出"换人" |
| `mimo.max_concurrent` | `1` | 同时合成路数，建议保持 1 |
| `mimo.retry_attempts` | `0` | 仅对 429/5xx/网络错误重试 |
| `delivery.fallback_to_text` | `true` | 失败时发文字还是拦截 |
| `delivery.skip_when_busy` | `true` | 并发满时直接发文字，不排队 |

群聊和私聊互不影响，例如"群聊只放行一个群、私聊全放行"：

```toml
[filters]
group_mode = "whitelist"
group_ids = ["589957759"]
private_mode = "all"
```

`group_mode` 与 `private_mode` 都不是 `all` 时，插件会记录入站消息的群号/对方 QQ 号
（`chat.receive.before_process`），出站时按名单判断；两者都是 `all` 时不记录，零额外开销。
拿不到对方身份时（映射过期等）一律保持文字，不误放行。

## 实测数据（2026-10-23 ~ 10-24，MiMo 官方接口）

- 克隆耗时：20 次顺序请求，p50 **5.1s**、p90 28.4s、max 47.6s，全部成功。
- 音色稳定性（同一句合成 5 次，测中位 f0 的离散度）：
  - 原始文本 + `temperature=0.15`：σ **34.6Hz**、range 82.4Hz
  - 只做文本清洗：σ 23.3Hz
  - 清洗 + 默认 `style_instruction`：σ **5.5–13.6Hz**、range 13.6–42.3Hz
- 标点的影响（原始文本，2 次）：`～` 让中位音高冲到 313–320Hz，emoji 让两次结果差 68Hz（≈4 个半音），
  `……` 产生 0.45–0.49s 尾部静音，`！` 会尾部截断并带噪声。这就是先清洗文本的原因。
- 并发：2 路正常；3 路开始排队；5 路直接 429。

## 更新日志

### 0.2.2

- 修复 `tools/build_reference.sh` 在 ffmpeg 7.1 及以上版本直接失败的问题：淡出改用「反转 → 20ms 淡入 → 再反转」，
  不再依赖 `afade` 的负数 `st`（新版 ffmpeg 会报 `Value -0.020000 for parameter 'st' out of range`）。

### 0.2.1

- 新增 `trigger.mode = "random_llm"` 与 `mimo_voice_say` 工具：随机抽签 + 模型主动请求双触发。
- 合成前清洗文本（表情、引号、波浪号、省略号、重复标点），并补 `style_instruction` 压住音高漂移。
- 硬化：空音频/过短音频判定、超时不再重试、可重试错误指数退避、建连超时独立、
  并发满直接发文字不排队、参考音频与 `voice_dir` 限制在插件目录内、工具加 `visibility`。

### 0.2.0

- 首个发布版本。

## 隐私

参考音频只保存在本地插件目录（使用 ffmpeg 参考策略时，处理结果缓存在宿主提供的 runtime 目录下的
`reference-cache/`，拿不到时退回插件目录），

除了随请求发给你配置的 TTS 服务（当前实现为小米 MiMo 官方 API：待合成文本与参考音频会发往 `api.xiaomimimo.com`）外，不会上传到任何地方。

`mimo.api_base_url` 决定请求发往哪里，请只填写可信地址（默认即官方接口）；把它指向内网或本机地址，
插件会把待合成文本与参考音频发过去。

## License

MIT。上游 `ling-tts-bot` 版权所有 (c) 2026 Ling-LA，本项目保留其许可声明。
