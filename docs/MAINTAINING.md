# 维护手记

> 写给以后还想继续把它做得更好的自己。当前开发版本 **0.5.1**（商店上最新可装 0.4.0）。

## 1. 这是什么，在哪

| 项 | 值 |
|---|---|
| 插件 | MiMo 语音回复（把麦麦的纯文本回复按比例替换成语音条） |
| 插件 id | `github.SoulString-Dev.maibot-mimo-voice` |
| 仓库 | https://github.com/SoulString-Dev/maibot-mimo-voice |
| 上游 | fork 自 `Ling-LA/ling-tts-bot`（MIT，保留版权声明） |
| 部署形态 | 放进麦麦插件目录；自定义参考音频放插件**数据目录**的 `references/` |

运行形态：hook `send_service.before_send` 把出站纯文本改写成 `voice` 段。**不需要**适配器私有 API，也不需要声明 capability（`capabilities: []` 是最干净的资产，别轻易破坏）。

## 2. 发版流程

**`CHANGELOG.md` 是版本变更的唯一事实源**：先改 CHANGELOG，再把对应段落拷进 Release 说明；README 只保留最新一版的要点。

```bash
git add -A && git commit -m "feat: ..." && git push origin main
git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z   # 版本号必须与 _manifest.json 完全一致
# 再建 GitHub Release（body：改动 + 实测数据 + 安装/依赖 + License 署名）
```

发完**不用**手动提商店申请：索引 cron（UTC `0 7,18`，北京 15:00 / 02:00）会自动发现新 Release → 建 `[Release]` issue → 打 `validated` → 机器审核；零风险自动写进 `plugin_versions.json`。

### 发版铁律

- **tag 一经收录不可变**，要改内容只能发新版本号。
- **`sdk.max_version` 第一、二位不能写 99**（现规范 `2.12.0–2.12.99`；`host_application: 1.3.0–1.3.99`）。
- 同期多个 Release 会并成一批（每批最多 5 个，多余 `discarded` 不补审）。
- 版本号在 `plugin.py` 的 `config_version`、`config.example.toml`、`_manifest.json` 三处，必须一起改；只改构建脚本/文档时 `config_version` 可不动。

## 3. 商店审核在查什么

自动审核（`codex-mai-review`，每单 8 次额度），会把 `plugin.py` 逐行读完并与已收录基线做全量 diff。明确检查：

1. 文件读写只落 `ctx.paths.data_dir` / `runtime_dir`；拒绝对路径与 `..`；不能有删除类调用。
2. 只向用户配置的 `api_base_url` 发必要数据；不上传用户/群/聊天；无遥测、不下载执行远程代码。
3. `api_key` 只进请求头、不进日志；错误响应体截断。
4. 不读数据库、不改宿主配置，全走 SDK 面。
5. `subprocess` 只允许固定参数、不经 shell、聊天内容不进命令行（默认路径最好完全不调用）。
6. manifest 与实现一致：`capabilities` 要真、`plugin_type` 合法、依赖要声明。
7. 只用宿主注册过的 hook 点；SDK 符号按 PyPI 源码逐条比对。
8. 无 `eval/exec/pickle/shell=True`。

### 历史被挑出的问题（当 checklist）

| 类型 | 问题 |
|---|---|
| 致命 | `random.random()` 忘记 `import random`，默认模式下必抛 `NameError`，而 handler 是 SKIP → 静默失效 |
| 性能 | hook `timeout_ms=300000` 过长；`subprocess.run` 阻塞事件循环（改 `asyncio.to_thread`） |
| 路径 | `.cache` 写进插件目录（改 `ctx.paths.runtime_dir`）；`.gitignore` 漏 `voices/` |
| API | `parameters_schema` 无效，应写 `parameters`；`activation_type` 是 `@Action` 时代遗留字段 |
| 转义 | ffmpeg concat 的 `file '<path>'` 未转义单引号 |
| 逻辑 | 往 `kwargs["messages"]` 注入提示是死代码（planner hook 没这个键） |
| 泄漏 | 错误体前 400 字符进 warning（改 120 字符 + 折叠空白） |
| 隐私 | 示例里出现真实群号 / QQ（全部换占位） |

## 4. 运行时坑（比审核更值得记）

- **一个坏插件会拖垮整个扩展插件 runner**（曾漏调 `super().__init__()` → 7 个第三方插件停摆）。改完至少 `py_compile` 再重启。
- **热重载重置实例状态**：`on_config_update` 要清 `_reference_uri`、`_reference_stamp`、`_timbre_target_cache` 等缓存；正在合成的那一条会失败（降级文字）。
- SDK 的 `config` / `ctx` 是只读 property；测试里用 `inst.__dict__["_plugin_config_instance"]`、`inst.__dict__["_ctx"]`。
- `PluginConfigBase` 是 `extra="ignore"`：删配置键不会让旧 `config.toml` 报错，宿主会按新 schema 重写。
- TOML 不能重复声明同一张表，分组只能用注释。
- hook 可用键：`send_service.before_send`（可改 message / abort）、`send_service.after_send`（`message_id` 已是平台回执）、`maisaka.planner.before_request`（只有 `items`/`tool_definitions`/`session_id`，不能 abort）。
- **静态检查要装在带 SDK 的环境里跑**：没有 `maibot-plugin-sdk` 时基类会被当成 `Any`，`self.config.xxx` 的字段错误一个都发现不了（CI 第一次跑就因此红过一次）。本地先 `pip install --user "maibot-plugin-sdk>=2.12"` 再跑 `mypy plugin.py core`。
- 出站消息里 `user_info.user_id` 是**机器人自己**；要拿对方 QQ 得在 `chat.receive.before_process` 记 `session_id → 用户`。

## 5. 部署与验证

```bash
tar czf /tmp/mv.tgz plugin.py core config.example.toml README.md _manifest.json tools/build_reference.sh
scp -O -P <ssh端口> /tmp/mv.tgz user@<nas>:/tmp/
ssh -p <ssh端口> user@<nas> "cd <部署目录> && tar xzf /tmp/mv.tgz && sudo chown -R 1000:1000 ."
```

- 文件监视会自动热重载；整套重启用 `sudo docker restart maim-bot-core`。
- 成功标志：`MiMo 语音插件已加载：音色=…, mode=…, probability=…, 英文=…, 音色对齐=…`、`参考音频目录：…`、扩展插件 `已加载=N，失败=0`。

## 6. 音色工程（数值都是实测出来的）

- 语速/音高**不在配置里**，烘焙在参考音频里（`tools/build_reference.sh` 的 `PITCH TEMPO TRIM`）；改了要重新生成参考。
- 稳定音色的关键不是低 `temperature`，而是 user 角色的风格指令：同文本 5 次 f0 离散度 σ 34.6Hz → 5.5–13.6Hz。现行文案见 `DEFAULT_STYLE_INSTRUCTION`；早期"语速平稳"会把语速拖慢 20% 以上，别再用。
- 参考音频：单句 2–5 秒、24kHz 单声道 16-bit WAV、无静音头尾、无 BGM/混响。
- `timbre_match`（0.4.0 起默认开）按参考的相对频谱做 ±6dB 补偿；脏参考/带混响收益小，很暗的参考会反向压暗。
- 克隆只传递"大致身份"，不传递细节音色；拿克隆输出再当参考（自举）无效。

## 7. 指标口径（看线上表现时别踩）

- DB 表 `mai_messages`；`raw_content` 是 **msgpack**（`msgpack.unpackb(raw, raw=False)`），语音段是 `{"type":"voice"}`。统计 bot 发言要用 `user_id`，**不要用 `account_id`**（入站消息也算在它头上）。
- 日志 `app_*.log.jsonl`，字段是 `event`；timestamp 有两种格式；同一事件会出现在多个轮转文件里 → **按秒去重**。
- 比例失真两个来源：`trigger.decision_ttl` 复用抽签；随机路径会跳过含拉丁字母的文本（分母应取"候选池"）。

## 8. 备份与回滚

- 宿主每次改配置都会留 `config.toml.bak-*`；升级 `plugin.py` 前留 `plugin.py.bak-<stamp>`。
- 回滚：`git checkout <tag>` 取文件 → 重新打包覆盖 → 热重载。
- 紧急关闭：`config.toml` 里 `[plugin] enabled = false`（热重载即生效）。

## 9. 后续打算继续优化的方向

1. **纯 Python 参考音频加工**（彻底去 ffmpeg）：重采样/变调/裁静音/淡入淡出/响度归一，上传即生成多档音高供下拉。
2. **自适应超时**：按最近 20 次合成的 p80（12–25s）动态设；配合连续 3 次失败熔断。
3. **继续拆分**：`plugin.py` 已经把文案/配置/音频拆进 `core/`（约 1130 行）；下一步是把 TTS 请求与参考音频管理也搬出去，仍作为"只拆分不带功能改动"的补丁版本。
4. 产品侧：商店页放样音、英文 README。
5. 已评估**不做**：本地音频缓存、RVC/翻唱（无 GPU）、用提示词管英文（实测无效且会改坏内容）。

## 10. 过一段时间维护时，先确认这三件事

1. 商店有没有新审核 issue 与 `plugin_versions.json` 里的最新可装版本。
2. MiMo API 形态是否变了（模型名、`audio.voice` 传 data URI、`modalities:["audio"]`）——变了整条链路要重测。
3. 麦麦宿主升级后：hook 点是否还在、`ctx.paths` 语义、SDK 符号重命名（`sdk.max_version` 同步上调）。

## 附：常用命令

```bash
docker exec maim-bot-core /MaiMBot/.venv/bin/python -m py_compile /MaiMBot/plugins/maibot-mimo-voice/plugin.py /MaiMBot/plugins/maibot-mimo-voice/core/*.py
docker logs maim-bot-core 2>&1 | grep -E "maibot-mimo-voice|扩展插件" | tail -20
docker exec maim-bot-core sh -c 'grep -h "替换为语音" /MaiMBot/logs/app_*.log.jsonl | tail -5'
```
