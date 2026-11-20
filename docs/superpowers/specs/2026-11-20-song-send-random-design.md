# 定向发歌 + 随机发歌 设计（0.7.0 增量）

日期：2026-11-20
状态：已与用户确认（m02027），并入尚未发版的 0.7.0

## 目标

1. 管理员在私聊里把曲库里的一首歌直接发到指定群或指定用户。
2. 随机发一首：命令 `/voice sing random` 与意图层（自然语言）都能触发。

## 已确认的边界

| 项 | 结论 |
| --- | --- |
| 目标能否是群 | 能（`g<群号>` / `u<QQ号>` / 纯数字先群后私聊，与 `/voice say` 一致） |
| 随机按标签 | 暂不支持，只有全库随机 |
| 最近播放 | 排除该会话最近 3 首；可用曲目 ≤3 首时退化为全库随机 |
| 目标级节流 | **不做**（"同目标 60 秒 1 首"被用户否掉）；发起方共用现有点歌冷却 30 / 10 秒 |
| 目标被 `/voice off` | 拒绝发送，提示目标会话已关闭 |

## 命令

| 命令 | 位置 | 权限 |
| --- | --- | --- |
| `/voice sing send <目标> <歌名/别名/标签>` | 私聊 | 管理员 |
| `/voice sing random [目标]` | 当前会话（不带目标）/ 私聊（带目标） | 任何人 / 管理员 |
| `/voice sing <歌名>` | 当前会话 | 任何人 |
| `/voice sing list` | 当前会话 | 任何人 |

目标语法复用 `core/commands.py:parse_target` 与 `plugin.py:_resolve_command_target`：
先查活跃会话、再尝试新建；失败返回现有提示文案。

## 随机

- 选曲：`core/song_library.py:pick_random(songs, exclude_ids)`。
  - 先取"不在最近 3 首"的集合，非空则从中随机；
  - 为空（库太小）时退回全库随机，日志注明 `退化为全库`。
- 冷却：与点歌共用 `_sing_cooldown`（会话 30 秒 / 人 10 秒），只对**发起方**计。
- 发送后把歌曲 id 记进该会话的最近播放队列（容量 3，先进先出）。

## 意图层

- 新配置 `songs.random_patterns: List[str]`，默认 `["(?:随机|随便|任意)(?:来|放)?(?:一)?首"]`；
  编译走现有 `compile_patterns`（单条 ≤200 字符、最多 20 条，写错跳过并告警一次）。
- `core/song_intent.py` 新增 `matches_any(text, patterns)`：先 `strip_mentions`，任一正则 `search` 命中即真。
- `inject_song_intent` 的顺序：先按 `intent_patterns` 匹配具体歌名；没命中再按 `random_patterns` 匹配随机。
  命中随机时不注入候选列表，而是注入一行「用户想随机听一首，可调用 `mimo_voice_play_random`」。
- 工具暴露：`mimo_voice_play_song` 只在"具名"命中轮次可见，
  `mimo_voice_play_random` 只在"随机"命中轮次可见；两者都用 `_intent_armed` 风格的时间戳，
  分别存在 `_intent_armed` / `_random_armed`（TTL 120 秒）。
- 群聊仍受 `require_mention` 约束（@ 或被点名）。

## 发送路径复用

从 `_sing_play` 抽出 `_deliver_song(stream_id, song) -> str`（返回错误文案，空串表示成功）：
校验 `/voice off` → 读文件 → 大小上限 → `_send_voice`。点播、随机、定向三处共用；
定向还要额外校验目标会话的 `_filter_allows` 与开关。

## 失败文案

| 情况 | 回复 |
| --- | --- |
| 目标解析失败 | 复用 `_resolve_command_target` 的错误文案 |
| 目标不在放行名单 | `目标会话不在语音放行名单里。` |
| 目标已 `/voice off` | `目标会话语音已关闭（/voice on 可恢复）。` |
| 曲库为空 | `曲库是空的，先私聊管理员用 /voice sing add <歌名> 入库。` |
| 随机无可用歌（全被排除且库空） | 同上 |
| 冷却中 | 复用 `_sing_cooldown` 文案 |

## 测试

- `tests/test_song_library.py`：`pick_random` 排除最近、库小时退化、`parse_sing` 的 `send` / `random`。
- `tests/test_song_intent.py`：随机正则命中/不命中、@ 与文本前缀剥离。
- `tests/test_songs.py`：随机发一首、连续两次随机不重复（库 ≥4）、定向发到群/私聊、
  非管理员被拒、目标滤镜拒绝、目标 `/voice off` 拒绝、冷却共用、工具只在随机命中轮次暴露。

## 不做

- 随机按标签、目标级独立节流、随机「不重复直到播完一整轮」（库小时行为不可预期）。
