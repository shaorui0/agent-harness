# harness-openspec-aware：该不该开 OpenSpec change

状态：已实现并测试，**未注册**（不在 `install/hooks-manifest.json` 里）。

## 设计理由

原则一：确定性触发交给 hook，「该不该开」的判断交给模型和人。

- hook 只做能算出来的事：session 开始时列出在做的 change；session 写了很多次、动了很多文件、却没有关联任何 change 时，提醒一次。
- 「这是不是一个值得开 change 的任务」（要真改东西、≥5 步且有先后依赖、大概率跨 session）hook 判断不了，也不该判断。提醒文案把这三条判据交回给模型和用户，由他们决定开或忽略。
- 因此：只提醒，不拦截，不返回 block，不改 prompt；任何异常静默 exit 0。

## 触发条件

范围（opt-in）：以 event 的 cwd 往上找，先找 `.harness/project.yml` 所在的项目根，找不到就找最近的含 `openspec/changes` 的祖先目录。该根目录下存在 `openspec/changes/`，或 `project.yml` 里 `openspec.nudge: true` 才生效，否则静默。

**SessionStart**：列出 `openspec/changes/*`（排除 `archive`）里未全部完成的 change，格式 `id  done/total  标题`（标题取 `proposal.md` 第一个 `# `，缺省用 id），按目录内文件 mtime 新到旧，最多 5 行，超出写「…另有 N 个」。没有可列的就静默。done/total 只数 `tasks.md` 里的 `- [ ]` / `- [x]` 行，hook 独立解析，不依赖 tracker。

**UserPromptSubmit**（每 session 最多一次），同时满足：

1. 本 session 未关联任何 change：没有 `openspec/changes/*/tracker.yml` 的 `sessions` 列表含该 session_id（完整 id 或前 8 位），且 oplog 里该 session 没写过 `openspec/changes/` 下的文件。
2. `<根>/.harness/oplog.jsonl`（只读末尾 1MB）里该 session 的记录数 ≥ `nudge_min_writes`，且不同 target 数 ≥ `nudge_min_files`。

提醒后在 `~/.harness/run/` 写标记文件，之后本 session 不再提醒。除此之外 hook 不写任何文件。

已知限制：oplog 只读末尾 1MB，一个 session 的早期记录若已被后来的记录挤出窗口，会低估其写次数，表现为偏保守（少提醒），不会多提醒。

## 可配置项（`.harness/project.yml`）

```yaml
openspec:
  nudge: true            # 没有 openspec/changes 目录时也启用；false 显式关闭
  nudge_min_writes: 10   # 默认 10
  nudge_min_files: 3     # 默认 3
```

## 演练

不注入任何内容、不写标记文件，只打印每个 session 会不会被提醒：

```bash
python3 hooks/harness-openspec-aware.py --dry-run \
  [--root <dir>] [--oplog <path>] [--since YYYY-MM-DD] \
  [--min-writes N] [--min-files M]
```

每个 session 一行（前 8 位、写次数、不同文件数、是否已关联及依据、是否会提醒），末尾汇总「共 X 个 session，会提醒 Y 个」，并打印该根目录 SessionStart 会注入的文本。演练读整个 oplog，不受 1MB 窗口限制。建议注册前先用不同 `--min-writes` 跑几遍，选阈值。

## 注册方法

确认后手动加入 `install/hooks-manifest.json`，再运行 `bin/harness install`。在 `SessionStart` 与 `UserPromptSubmit` 两个数组里各加一项：

```json
{
  "hooks": [
    {"type": "command", "command": "python3 ~/.claude/hooks/harness-openspec-aware.py", "timeout": 5}
  ]
}
```

注册后它会作用于你所有 session，但只在满足上面「范围」的 workspace 里有输出。

## 如何关闭

- 单个 workspace：`project.yml` 写 `openspec.nudge: false`。
- 全部：从 `install/hooks-manifest.json` 删掉上述两项，并手动从 `~/.claude/settings.json` 删掉对应条目（`install` 只增不改，不会替你删）。
