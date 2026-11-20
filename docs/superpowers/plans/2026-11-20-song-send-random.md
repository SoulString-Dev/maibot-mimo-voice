# 计划：定向发歌 + 随机发歌（0.7.0 增量）

规格：`docs/superpowers/specs/2026-11-20-song-send-random-design.md`

## 步骤

1. `core/song_library.py`
   - 新增 `pick_random(songs, exclude_ids=()) -> Optional[Song]`；
   - `parse_sing` 增加 `send <目标> <歌名>` → `("send", 歌名, "", 目标)`、`random [目标]` → `("random", "", "", 目标)`；
   - 验证：`python -c` 冒烟 + `tests/test_song_library.py` 追加断言。
2. `core/song_intent.py`
   - 新增 `matches_any(text, patterns) -> bool`、`build_random_hint(limit) -> str`；
   - 验证：`tests/test_song_intent.py` 追加断言。
3. `core/config.py` + `config.example.toml`
   - `SongsConfig.random_patterns: List[str]`，默认一条；示例配置加同名键（75 → 76 项）；
   - 验证：`tests/test_config.py`（模板键数断言是否需同步）。
4. `plugin.py`
   - 状态：`_sing_recent: Dict[str, Deque[str]]`、`_random_armed: Dict[str, float]`；
   - 抽 `_deliver_song(stream_id, song) -> str`，`_sing_play` 改用它；
   - 新增 `_sing_random(stream_id, songs, user_id, target=None)`、`_sing_send(...)`；
   - `_handle_sing_command` 分派 `send` / `random`；`send` 与带目标 `random` 仅私聊 + 管理员；
     `handle_voice_command` 的群聊白名单加入 `random`（仅不带目标）；
   - `_random_patterns()`、`_song_random_armed()`；`configure_planner_tools` 按命中类型隐藏/暴露两个工具；
   - `inject_song_intent` 增加随机分支与对应日志；新增 `@Tool("mimo_voice_play_random")`；
   - `core/commands.py` 的 `_USAGE` / `render_help()` 补 `send`、`random`。
5. 测试：`tests/test_songs.py` 追加（随机两次不重复、定向群/私聊、非管理员、滤镜、`/voice off`、冷却、工具暴露）；
   跑 `python tests/run_all.py`、`ruff check`、`mypy plugin.py core`。
6. 文档：README 命令表/配置表、`CHANGELOG.md` 0.7.0 条目、`docs/song-jukebox-design.md` §4/§5、
   `docs/roadmap.md` F5 状态、`00-总览.md` 计数。
7. 部署 NAS：tar → ssh 解包 → `docker restart maim-bot-core`（改工具定义/配置需重启），看启动日志；
   **不主动往任何群发测试消息**，需要时先问用户。
8. 提交并 push；**不打 tag**（0.7.0 仍在观察期）。

## 验收

- 全量断言 ≥ 600 条全绿；ruff/mypy 干净；
- 私聊 `/voice sing random` 能发语音且连续两次不同（库 ≥ 4）；
- 管理员私聊 `/voice sing send g<群号> <歌名>` 把目标会话日志里出现一条语音发送；
- 群聊 `random` 可用，`send` 被拒；
- NAS 启动日志显示曲库数量与 `唱歌点播=开`。
