# maibot-mimo-voice（MiMo TTS 语音回复）

把麦麦（MaiBot）的**纯文本回复**按策略换成 MiMo 语音条。开箱自带「理塘王」「可爱少女」与 MiMo 默认女角色音色，也可以换成自己的参考音频。

思路参考 [Ling-LA/ling-tts-bot](https://github.com/Ling-LA/ling-tts-bot)（MIT），本仓库重写了触发、文本清洗与工程细节，差异见下表。

## 它能做什么

- 语音条**替换**文字：命中的那条只发语音、不发文字；合成失败或超时回退文字，消息不会丢。
- 四种触发方式：随机抽签、随机 + 模型主动请求（默认）、语音优先、全量。
- 群聊 / 私聊各自独立的 `all / whitelist / blacklist` 放行策略。
- 合成前清洗文本（表情、引号、波浪号、省略号、重复标点），并用风格指令压住音色漂移；英文默认只删 `ww` / `emmm` 这类复读口癖，`LLM`、`token`、`cos` 等真词原样朗读。

| 与上游的差异 | 原因 |
|---|---|
| 内置理塘王 + MiMo 默认女角色参考，`voice.preset` 写中文名切换 | 开箱就能出声；理塘王实测克隆距离中位 3.39 dB，比女声更稳 |
| 默认 `random_llm` + `probability = 0.15` + 短句门槛 60 字 | 全量太吵；15% 下约每 7–10 条回复出一条，且模型可被点名开口，装好就能试听 |
| `temperature` 0.15、`style_instruction`、朗读前清洗文本 | 表情 / 引号 / 波浪号 / 省略号会让音高漂移 2–4 个半音并带来首尾爆音；清洗后 f0 抖动 σ 从 34.6 Hz 降到 5.5–13.6 Hz |
| 单次超时 25 秒、一条语音只发一次请求；超时/失败回退文字 | 账号级 TTS 并发名额只有 1，重复请求必被 429 拒（见「429 与超时」） |
| 并发 1 路，满了直接发文字（`skip_when_busy`） | 3 路并发把单路延迟推到 50–96 秒 |
| 参考音频只认插件目录 / 数据目录内的相对路径 | 防止配置被改成 `/etc/...` 之类的越界读取 |
| 不做音频缓存，只缓存参考特征与瘦身结果 | LLM 回复几乎不逐字重复；参考是固定输入，值得缓存 |

## 部署

### 从插件市场部署（推荐）

1. 打开麦麦 WebUI 的插件市场，搜「MiMo TTS」部署。
2. 先去 MiMo 开放平台拿 API Key（见下一节）。
3. 回到插件配置页，把 Key 填进 `mimo.api_key`，保存即热重载。`config.toml` 由运行时按插件声明的配置模型自动生成，**不需要**手动复制 `config.example.toml`。
4. 音色默认是内置 `理塘王`；想要年轻明亮的少女声切 `可爱少女`，想要 MiMo 官方女声切 `冰糖` / `茉莉`；想用自己的声音就按「换成自己的声音」准备文件。下拉里只有中文名。
5. 升级新版本后重启核心，插件目录里的 `config.toml` 与数据目录的 `references/` 都会保留。

### 第一次用：注册并获取 API Key

插件不自带模型，语音由小米 MiMo 开放平台的 TTS 接口合成，所以要先有 API Key（**没有 Key 插件会加载，但一条语音也发不出来**）。

1. 打开 <https://platform.xiaomimimo.com>，点右上角**登录**，用小米账号登录；没有账号先注册。
2. 进入控制台 <https://platform.xiaomimimo.com/console>，左侧找到 **API Keys**（也称「API 密钥」），点**新建**。
3. 复制 Key。**它只完整显示一次**，关掉页面就看不到了，丢了只能重建。
4. 回到麦麦：插件配置页 → `mimo.api_key` → 粘贴 → 保存。
5. 自检：核心日志应出现「MiMo 语音插件已加载：音色=…」；再让麦麦说一句话（触发方式见下文）。

- 接口地址默认 `mimo.api_base_url = "https://api.xiaomimimo.com/v1"`，不用改；用中转 / 自建网关时才改。
- 费用、免费额度与限流以平台定价页为准，插件侧只受 `general.timeout`、`mimo.max_concurrent` 影响。
- 也可以换别的兼容接口：把 `mimo.api_base_url` / `mimo.model` 改成对应服务商的值。

### 手动放进插件目录

1. 把仓库放进麦麦插件目录（如 `data/MaiMBot/plugins/maibot-mimo-voice`）。
2. 重启核心。首次加载时若没有 `config.toml`，运行时会按配置模型生成一份（值即默认值），之后按上面第 3、4 步填 Key、选音色。

维护与发版细节见 [`docs/MAINTAINING.md`](docs/MAINTAINING.md)。

依赖：`aiohttp`（插件运行时已自带）。只有用 `best_single` / `balanced` / `full_merge` 参考策略时才需要宿主机上的 `ffmpeg`。

## 内置音色

| `voice.preset` | 音色 | 说明 |
|---|---|---|
| `理塘王` | 理塘王 | 低沉、带口音的男声，约 4.8 秒参考（两句拼接）。克隆输出与参考的频谱距离中位 3.39 dB，比女声更稳 |
| `可爱少女` | 可爱少女 | 年轻、明亮、偏可爱的女声，约 8.4 秒参考（原样保留、不切不拼，仅峰值归一到 −3 dBFS；A/B/C 实测原样版离群值最少） |
| `冰糖` | MiMo 默认女角色 · 冰糖 | MiMo 官方预置女声，偏亮，约 2.9 秒参考 |
| `茉莉` | MiMo 默认女角色 · 茉莉 | 同系列女声，比冰糖略低、更柔和，约 3.2 秒参考 |
| `自定义` | 自定义 | 用 `voice.reference_path` 指向你自己的参考音频（放数据目录的 `references/`） |

配置下拉里只有中文名。旧写法 `keai` / `cute` / `bingtang` / `jasmine` / `litang` / `custom` 还能读，读进来自动换成中文名。

四套内置参考都是**合成音色**（不是真人录音），随插件分发在 `presets/` 下，加载时按版本清单校验、跳过体检与自动瘦身。
生成方式：先用 MiMo 预置音色生成采样，再跑 `tools/build_reference.sh`（升调 1.12、`atempo` 1.08、裁首尾静音）。

## 换成自己的声音

参考音频放**插件数据目录**的 `references/`（升级不覆盖；目录不存在时自动创建，启动日志会打印 `参考音频目录：…`）：

```
data/plugins/github.SoulString-Dev.maibot-mimo-voice/references/我的音色.wav
```

`voice.reference_path` 只写文件名；插件先找数据目录的 `references/`，再找插件根目录。参考要 2–5 秒、安静环境、单人干声——超长或静音偏多时插件会自己切句拼接（`voice.auto_prepare_reference`）。

要调音高 / 语速，在**宿主机**上做：

```bash
# 例：升 12% 音高 + 提速 8%；tools/build_reference.sh 是它的封装（另做静音裁剪与淡入淡出）
ffmpeg -i source.wav -af "aresample=24000,asetrate=24000*1.12,aresample=24000,atempo=1.08" -ac 1 -ar 24000 reference.wav
```

> 实测参考升调后输出会跟着抬高（约 1.05×），所以"预置音色 → 升调提速 → 当参考"只需做一次。

```toml
[voice]
preset = "自定义"
reference_path = "我的音色.wav"
```

## 触发方式

| `trigger.mode` | 行为 |
|---|---|
| `random_llm`（默认） | 随机抽签照常，同时给 planner 暴露 `mimo_voice_say`，模型可以主动要求"这条用语音回" |
| `random` | 每条回复按 `probability` 抽签；抽中且不超过 `short_reply_max_chars` 才转语音（不暴露工具，最省 token） |
| `llm_trigger` | 默认语音优先，模型可调 `mimo_voice_text_reply` 把这一轮改回文字（代码、表格、链接等） |
| `voice_only` | 全部转语音（仍受 `max_text_length` 与放行策略约束） |

- 默认 `random_llm`：刚装好时不用等抽签，直接让麦麦"说一句"就能试听；`random` 只按概率抽签、不暴露任何模型工具。
- 同一聊天流在 `trigger.decision_ttl`（默认 10 秒）内复用上一次的抽签结果，避免连发多条时概率失真。
- **默认概率 0.15**：低于 0.1 几乎察觉不到，想更频繁调到 0.25–0.4、想安静降到 0.05；再叠加下面两条过滤，实际出语音比例会略低于这个数。
- `random_requires_chinese = true`（默认）时，随机抽中但清洗后仍含英文字母的改发文字（`token`、`LLM` 会被逐字母念）；模型主动请求、`llm_trigger`、`voice_only` 不受限制。两种模型工具只在对应模式下暴露。
- 两个可选的加强项，**默认都关**：`trigger.mention_priority` 让被 @ / 点名后的下一条回复按 0.6 概率出声（一次性、90 秒过期，`@所有人` 不算）；`trigger.emotion_weighting` 让带感叹号、连续问号、波浪号、叠字、表情的回复按项加概率（每项 +0.04，最多 +0.2）。判定用**清洗前的原始回复**（清洗会把 `？？`、`～`、`…`、emoji 折掉），两者只影响抽签，不改写文本。

## 429 与超时

MiMo 对 TTS 的限制不是只有 RPM / TPM：**每个账号的模型并发名额只有 1**。2026-11-06 的实验（长文本起飞后 1 秒再发一条）证明——第二条无论是同一句还是完全不同的句子，一律在 0.34–0.40 秒内返回 `429 Too many requests`；而上一条**完成之后**再发同一句，可以正常成功。

这解释了历史现象：客户端超时（旧版 20 秒）时，服务端那条其实还在生成、仍占着名额（探针实测最长 72.7 秒才释放，生产日志最长成功耗时 33.0 秒）。所以"超时后补发"必然撞 429，而旧版对 429 是退避重试，等于在名额被占着的时候多烧几次请求。

0.5.4 起的处理方式：

- **一条语音只发一次请求**：`general.timeout` 固定 25 秒，超时即回退文字；`timeout_mode`（p80 自适应）与 `retry_on_timeout` 两个配置项已删除。
- **不补发、不重试超时**：`mimo.retry_attempts` 默认 0，保留给 5xx / 网络错误场景；超时不属于可重试错误。
- **本地并发 1 路 + `skip_when_busy`**：自己已经有合成在跑时，新消息直接发文字，避免自己撞自己的名额。
- **25 秒的取舍**：实测成功请求里 20–25 秒区间为 0 条，25 秒与 20 秒等价；25–33 秒那 6% 的长尾主动放弃（给 40 秒它们也不会回来，只是白等）。

效果（11-06 13:30 → 11-07 21:47，32.3 小时）：32 条成功 / 4 条超时 / **0 次 429**；健康时段（每小时成功 p50 ≤ 8 秒）占比 93.1%。

## 群聊 / 私聊过滤

两者互不影响，例如"群聊只放行一个群、私聊全放行"：

```toml
[filters]
group_mode = "whitelist"
group_ids = ["123456789"]
private_mode = "all"
```

`group_mode` 与 `private_mode` 都不是 `all` 时，插件会记录入站消息的群号 / 对方 QQ 号（`chat.receive.before_process`），出站时按名单判断；两者都是 `all` 时不记录，零额外开销。拿不到对方身份时（映射过期等）一律保持文字，不误放行。

## 配置说明（共 43 项，日常只需动前 12 项）

### 基础

| 配置 | 默认 | 说明 |
|---|---|---|
| `plugin.enabled` | `true` | 总开关 |
| `mimo.api_key` | 空 | MiMo 官方 API Key |
| `voice.preset` | `理塘王` | 音色：`理塘王` / `可爱少女` / `冰糖` / `茉莉` / `自定义`（后两者是 MiMo 官方预置女声；旧写法会自动换成中文名） |
| `trigger.mode` | `random_llm` | 见「触发方式」 |
| `trigger.probability` | `0.15` | 随机模式下每条回复转语音的概率。低于 0.1 基本感知不到，太高会显得吵 |
| `trigger.short_reply_max_chars` | `60` | 随机转语音的字数门槛（0 = 不限制）；模型主动请求不受限 |
| `general.max_text_length` | `300` | 单条语音最多念多少字，超出按 `overflow_policy` 处理 |
| `general.timeout` | `25.0` | 单次合成超时（秒），固定值、超时不重发（见「429 与超时」） |
| `mimo.temperature` | `0.15` | 越低越稳；0.7 以上能听出"换人" |
| `voice.style_instruction` | 见模板 | 随请求发送的朗读风格指令，留空则不发 |
| `delivery.fallback_to_text` | `true` | 合成失败 / 超时时发文字还是拦截 |
| `delivery.skip_when_busy` | `true` | 并发路数占满时直接发文字，不排队 |
| `delivery.timbre_match` | `true` | 音色对齐：按参考音频的频谱补偿每条输出（需要 WAV 参考，默认开） |
| `delivery.timbre_match_strength` | `1.0` | 音色对齐强度 0–1，补偿量限制在 ±6 dB；与参考差距 >8 dB 时按比例减弱 |

### 进阶

| 配置 | 默认 | 说明 |
|---|---|---|
| `plugin.config_version` | `0.5.4` | 配置版本号（内部使用，无需修改） |
| `mimo.api_base_url` | `https://api.xiaomimimo.com/v1` | 必须是 https 公网地址；写成内网/本机地址会回退官方地址并告警 |
| `mimo.model` | `mimo-v2.5-tts-voiceclone` | 也可用 `mimo-v2.5-tts`（预置音色） |
| `mimo.audio_format` | `mp3` | `mp3` / `wav`；开了任一音频后处理时自动改用 `wav`，此项实际被忽略 |
| `mimo.max_concurrent` | `1` | 同时合成路数，建议保持 1（名额只有 1） |
| `mimo.retry_attempts` | `0` | 仅对 429 / 5xx / 网络错误重试；超时不属于可重试错误 |
| `mimo.retry_backoff_seconds` | `1.5` | 重试退避基数，按 2 的幂增长 |
| `mimo.connect_timeout` | `10.0` | 建连超时（秒），与整体超时分开 |
| `mimo.allow_private_base_url` | `false` | 允许把音频发往内网/本机地址（只有自建代理时才需要） |
| `voice.reference_path` | `reference.wav` | 仅 `preset = 自定义` 时生效，填 `references/` 下的文件名 |
| `voice.reference_strategy` | `single_file` | 需要 ffmpeg 的策略：`best_single` / `balanced` / `full_merge` |
| `voice.auto_prepare_reference` | `true` | 参考 WAV 超过 5 秒或静音偏多时，自动按句拼接成 3–5 秒并缓存 |
| `trigger.decision_ttl` | `10.0` | 判定有效期（秒） |
| `trigger.overflow_policy` | `text` | 超长时：`text` 保留文字；`truncate` 截断后合成 |
| `trigger.english_policy` | `strip_tics` | `keep` 原样念；`strip_tics` 只删复读口癖；`strip` 删所有拉丁串（会误伤真词） |
| `trigger.random_requires_chinese` | `true` | 随机路径遇英文改发文字 |
| `trigger.mention_priority` | `false` | 被 @ / 点名后抬高下一条回复的语音概率 |
| `trigger.mention_probability` | `0.6` | 点名那一次的语音概率，与 `probability` 取较大值 |
| `trigger.mention_ttl_seconds` | `90.0` | 点名优先的有效期（秒），用掉即清 |
| `trigger.emotion_weighting` | `false` | 按情绪标记给抽签加分 |
| `trigger.emotion_step` / `emotion_cap` | `0.04` / `0.2` | 每项加分与总上限 |
| `delivery.trim_tail_silence_ms` | `250` | 尾部静音超过这个毫秒数就裁掉（只留 150ms），0 = 不裁 |
| `delivery.tail_pad_ms` | `120` | 处理完后补多少毫秒尾静音，避免播放端吃掉最后一个字 |
| `delivery.peak_normalize` | `true` | 峰值统一拉到 −1.5 dBFS；单条最多提 +6 dB 并软限幅，避免放大破音 |
| `filters.group_mode` | `all` | 群聊放行：`all` / `whitelist` / `blacklist` |
| `filters.private_mode` | `all` | 私聊放行：`all` / `whitelist` / `blacklist` |
| `filters.group_ids` | `[]` | 群号列表 |
| `filters.private_user_ids` | `[]` | 私聊对方 QQ 号列表 |

旧版本的残留键会被自动忽略（`extra="ignore"`），升级时不需要手工清理 `config.toml`。

## 线上表现

参考与超时策略几经调整，完整的按天数据与归因在 [`docs/maintenance.md`](docs/maintenance.md)。当前状态（2026-11-07）：

- 语音占 bot 消息的比例：默认抽签 15% 时约 **13–14%**；开启点名 / 情绪加权后约 **20–27%**（健康时段实测 21.0%、27 条/132 条）。
- 触发来源（11-05 → 11-07 共 73 条）：`random` 39 / `mention` 21 / `emotion` 13（12 条「感叹」+ 1 条「拟声」）；情绪判定原先跑在清洗之后，「问句 / 波浪号 / 省略号 / emoji」四类永远命不中，11-07 已改为看原始回复。
- 失败全部是上游超时，失败时回退文字；**429 = 0**（见「429 与超时」）。
- 克隆距离（输出与参考的频谱距离，越低越像）：内置理塘王中位 3.39 dB，可爱少女 7.66–8.57 dB（素材本身的声线离 MiMo 更远，四套参考里最不稳定的一档）。

## 目录结构

```
<插件目录>/
├── plugin.py                    # 入口：生命周期 / Hook / 过滤 / 合成编排
├── core/                        # 可复用的纯逻辑
│   ├── constants.py             # 阈值与默认值
│   ├── config.py                # 配置模型 + api_base_url 校验
│   ├── text.py                  # 朗读清洗 / 英文口癖策略
│   ├── audio.py                 # WAV 读写 / 频谱曲线 / 音色对齐 / 参考瘦身
│   ├── trigger.py               # 点名窗口 / 情绪加分
│   └── timeout.py               # 失败归类
├── tests/                       # 169 条断言，不依赖 pytest
├── config.example.toml
├── _manifest.json
├── assets/icon.svg              # 商店图标
├── presets/                     # 四套内置参考（随插件发布）
│   ├── reference-bingtang.wav
│   ├── reference-jasmine.wav
│   ├── reference-keai.wav
│   └── reference-litang.wav
└── docs/MAINTAINING.md
```

数据目录一般位于 `data/plugins/<插件 id>/`，启动日志会打印一行 `参考音频目录：...`。
`voice.reference_path` 只写文件名，插件会先找数据目录，再回退插件根目录（兼容旧版布局）。

## FAQ

**为什么这条回复没出语音？** 走随机路径（默认 `random_llm` 也是随机抽签）时要同时满足四条：

1. `trigger.probability` 抽中（默认 15%）；
2. 文本不超过 `trigger.short_reply_max_chars`（默认 60 字）；
3. 文本里没有 2 个以上连续英文（`trigger.random_requires_chinese`）；
4. 该群 / 私聊在 `filters` 放行名单里。

模型主动请求（`random_llm` 的 `mimo_voice_say`）只跳过第 2 条，其余仍生效。

**明明抽中了，收到的却是文字？** 合成超时（25 秒）、上游 5xx、返回静音或音频异常都会回退文字，日志里有一行 `语音合成失败（...），回退文字`。

**为什么不等久一点、或者失败了再试一次？** 账号并发名额只有 1，超时那条在服务端还占着名额，再发也是一次 429（详见「429 与超时」）。

**听起来不像参考音频？** 参考换成 1–5 秒、安静环境、单人、无背景音乐的干声；`voice.auto_prepare_reference`（默认开）会把过长的参考自动切句拼到 3–5 秒并做响度归一。`delivery.timbre_match` 能补一部分频谱差，但补不了混响和削顶失真。

## 已知限制

- QQ 不支持「引用 + 语音」组合，转语音时会去掉引用段。
- `random_requires_chinese` 只约束随机路径；模型主动请求的语音如果文本里有英文，仍会被逐字母念出来（此时可把 `english_policy` 设为 `strip`，代价是 `token`、`cos` 这类词也会被删）。
- `strip_tics` 的口癖白名单基于「整词重复辅音 / 元音 + 重复辅音≥3」的启发式，`yyds`、`awsl`、`nb` 这类网络缩写不会被删。
- 上游延迟抖动大（不同时段 p50 3–5 秒、p95 8–48 秒），单次超时定在 25 秒：更长的等待只会牺牲吞吐，长尾那 6% 主动放弃。
- 单路并发是有意为之：3 路并发会让单路延迟翻十倍。

## 隐私

参考音频只保存在本地（插件目录，或数据目录下的 `references/`）；参考瘦身结果与 ffmpeg 参考策略的处理结果缓存在 runtime 目录的 `reference-cache/`（拿不到时退回插件目录）。

除了随请求发给你配置的 TTS 服务（当前实现为小米 MiMo 官方 API：待合成文本与参考音频会发往 `api.xiaomimimo.com`）外，不会上传到任何地方。`mimo.api_base_url` 决定请求发往哪里，请只填写可信地址。

## 关于 ffmpeg（可选依赖）

内置音色与默认的 `voice.reference_strategy = "single_file"` 都**不调用任何外部命令**，大多数用户不需要装 ffmpeg。

只有选了 `自定义` 又把 `reference_strategy` 改成 `best_single` / `balanced` / `full_merge` 时，才会用宿主机的 `ffmpeg` 对**你自己提供的参考音频**做裁静音 / 截段 / 合并 / 重采样：

- 调用方式是固定参数列表（`subprocess.run([...], check=True)`），不经 shell、不做字符串拼接，也没有任何聊天内容或用户输入拼进命令行；
- 输入只有 `references/`（或插件根目录）下的音频文件和你配置的数值；
- 不联网、不下载任何东西，只在本机做转码；
- 结果缓存在 runtime 目录的 `reference-cache/`，同一份参考只处理一次。

宿主机没有 `ffmpeg` 时，插件会在加载时退回 `single_file`，不会报错。

## 更新日志

### 0.5.4

- **一条语音只发一次请求，超时固定 25 秒**：删除 p80 自适应超时（`timeout_mode`）与超时补发（`retry_on_timeout`）。依据是 11-06 的名额实验：账号级 TTS 并发名额只有 1，超时后服务端仍在生成（最长 72.7 秒），补发必 429。
- **点名优先 + 情绪加权**（默认关）：被 @ / 点名后下一条回复按 0.6 概率出声；带感叹号、叠字、表情的回复按项加概率（+0.04/项，上限 +0.2），判定看清洗前的原始回复。
- **埋点补字数与音频时长**：成功行变成 `语音合成耗时：3.42s（timeout 25.0s，第 1 次，12 字，音频 3.33s）`，失败行补 `，12 字`。
- **内置「可爱少女」参考换成原样 8.41 秒版**：A/B/C 实测原样版离群值最少（两句拼接版中位 7.10、最大 12.49 dB；单句中位 8.66），代价是每次多传约 220 KB。

更早的 0.5.0–0.5.3 改动、以及每个版本的数字依据，见 [CHANGELOG.md](CHANGELOG.md)。

## 开发

```bash
# 语法与配置校验
python -m py_compile plugin.py core/*.py
python -c "import tomllib; tomllib.load(open('config.example.toml','rb'))"

# 静态检查 + 类型检查 + 单测（依赖见 requirements-dev.txt）
pip install -r requirements-dev.txt
ruff check .
mypy plugin.py core   # 需先装 maibot-plugin-sdk，否则基类被当作 Any，会漏掉 self.config 的类型问题
python tests/run_all.py     # 文本/音频/配置/超时/触发五组共 169 条断言
```

CI（`.github/workflows/ci.yml`）在每次 push / PR 上跑同样三条命令。测试不依赖 pytest，只用标准库，容器里 `/MaiMBot/.venv/bin/python tests/run_all.py` 也能直接跑。

部署到运行中的麦麦（示例：NAS 上的容器）：

```bash
tar czf /tmp/mv.tgz plugin.py core presets config.example.toml _manifest.json README.md CHANGELOG.md
scp /tmp/mv.tgz user@nas:/tmp/
ssh user@nas 'cd <插件目录> && tar xzf /tmp/mv.tgz && sudo chown -R 1000:1000 .'
```

插件目录里的文件被改动后，麦麦的文件监视会自动重载扩展插件；`plugin.py` 的语法错误会让**所有**扩展插件一起停摆，改完务必先本地 `py_compile`。

仓库里只提交四套内置音色（`presets/reference-{bingtang,jasmine,keai,litang}.wav`，白名单放行）；
你自己的参考音频和 `references/`、`voices/` 都已被 `.gitignore` 排除，不会被误提交。

回滚：`git checkout <上个版本> -- plugin.py` 后重新铺文件即可；配置项只增不减，旧配置一般无需改。

## License

MIT。上游 `ling-tts-bot` 版权所有 (c) 2026 Ling-LA，本项目保留其许可声明。
