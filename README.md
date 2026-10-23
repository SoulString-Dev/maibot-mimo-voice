# maibot-mimo-voice

把麦麦（MaiBot）的**纯文本回复**按策略替换成 MiMo 语音条，音色来自你自己提供的参考音频。

思路参考 [Ling-LA/ling-tts-bot](https://github.com/Ling-LA/ling-tts-bot)（MIT），本仓库做了取舍与重写，主要差异：

| 改动 | 原因 |
|---|---|
| 默认 `random` + `probability = 0.05` + 短句门槛 60 字 | 全量转语音太激进；语音集中在短句上更像真人插话 |
| 支持群聊/私聊 `all / whitelist / blacklist` | 灰度上线时可以先只在指定群或私聊里出声 |
| 参考音频支持固定单文件，也保留 `best_single / balanced / full_merge` | 单文件不需要 ffmpeg；需要自动挑选/拼接时仍可用原策略 |
| `temperature` 默认 0.15 | 实测同一句三次的中位音高漂移从 ~11Hz 降到 ~2Hz |
| 合成超时默认 20 秒，超时回退文字 | 上游 TTS p95 约 48 秒，不能把回复拖到一分钟 |
| 合成并发默认 1 路 | 3 路并发会把单路延迟推到 50–96 秒 |
| 不做音频缓存 | LLM 生成的回复几乎不会逐字重复，缓存命中率极低 |

## 效果与取舍

- 语音条**替换**文字：命中的那条回复只发语音，不再发文字。
- 超时或失败默认**回退发文字**（`delivery.fallback_to_text = true`），保证消息不丢。
- QQ 不支持「引用 + 语音」组合，转语音时会自动去掉引用段。
- `llm_trigger` 模式下模型可以先调 `mimo_voice_text_reply` 把这一轮改回文字（代码、表格、链接等）。

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
| `trigger.mode` | `random` | `random` 按概率；`llm_trigger` 语音优先、模型可改文字；`voice_only` 全部转语音 |
| `trigger.probability` | `0.05` | random 模式下每条回复转语音的概率 |
| `trigger.short_reply_max_chars` | `60` | 超过该字数不转语音（0 = 不限制） |
| `trigger.overflow_policy` | `text` | 超过 `general.max_text_length` 时保留文字；`truncate` 为截断后合成 |
| `filters.group_mode` | `all` | 群聊放行方式：`all` / `whitelist` / `blacklist` |
| `filters.private_mode` | `all` | 私聊放行方式：`all` / `whitelist` / `blacklist` |
| `filters.group_ids` | `[]` | 群号列表（配合 `group_mode`） |
| `filters.private_user_ids` | `[]` | 私聊对方 QQ 号列表（配合 `private_mode`） |
| `general.timeout` | `20.0` | 单次合成超时（秒） |
| `mimo.temperature` | `0.15` | 越低越稳；0.7 以上能听出"换人" |
| `mimo.max_concurrent` | `1` | 同时合成路数，建议保持 1 |
| `delivery.fallback_to_text` | `true` | 失败时发文字还是拦截 |

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

## 实测数据（2026-10-23，MiMo 官方接口）

- 克隆耗时：20 次顺序请求，p50 **5.1s**、p90 28.4s、max 47.6s，全部成功。
- 音色稳定性：`temperature=0.15` + 固定参考，同句多次中位音高差约 2Hz；默认采样下可达 11Hz。
- 并发：2 路正常；3 路开始排队；5 路直接 429。

## 隐私

参考音频只保存在本地插件目录，除了随请求发给你配置的 TTS 服务外，不会上传到任何地方。

## License

MIT。上游 `ling-tts-bot` 版权所有 (c) 2026 Ling-LA，本项目保留其许可声明。
