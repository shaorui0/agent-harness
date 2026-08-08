# agent-harness

工作项目的 context engineering + harness engineering 机制。全局装一次，在任意 workspace 生效，行为由项目目录里的 `.harness/` 决定。

设计原理见 [DESIGN.md](DESIGN.md)。一句话版本：**必须发生的事挂在 hook 上，需要判断的事做成主动触发的 skill；写入不做判断，判断延迟到压缩。**

## 装

```bash
/Users/rshao/work/agent-harness/bin/harness install
/Users/rshao/work/agent-harness/bin/harness doctor
```

`install` 做四件事，全部幂等：把 `hooks/*.py` 软链进 `~/.claude/hooks/`，把 `skills/*` 软链进 `~/.claude/skills/`，
把 `install/hooks-manifest.json` 的条目并进 `~/.claude/settings.json`（写之前先备份，只增不改别的），
在 `~/.harness/` 下建 journal 和 run 目录并放一份全局默认配置。

`doctor` 检查每一条软链是否指向这个仓库、settings.json 引用的文件是否真存在、目录是否齐全。有 FAIL 时退出码非零，可以直接进 cron。

## 接一个项目

```bash
cd /path/to/repo
/Users/rshao/work/agent-harness/bin/harness init
```

然后在那个仓库里跑 `/harness-onboard`，它会摸底、填 `project.yml` 的自主边界、从现有文档蒸馏第一版 `STATE.md`。

## 三个 skill

| skill | 什么时候用 | 干什么 |
|---|---|---|
| `/harness-onboard` | 接入一个新仓库 | 装 `.harness/`，定边界，蒸馏首版 STATE.md |
| `/harness-compact` | 状态乱了，或定时 | 收敛 STATE.md，归档过期内容，git 留痕 |
| `/harness-explore` | 需要调查 | 拆正交维度并行派 subagent，结构化合并 |

## 四个 hook

| hook | 事件 | 作用 |
|---|---|---|
| `harness-session-start.py` | SessionStart | 注入 STATE.md、journal 尾部、自主边界、输出契约 |
| `harness-journal.py` | Stop | 每轮无条件 append 一条 WAL 记录，不做判断 |
| `harness-envelope.py` | PreToolUse | 安全区内直接放行，安全区外硬拦 |
| `harness-stop-contract.py` | Stop | 在 envelope 内停下来征求同意时推回去一次；散文长回复提醒一次 |

后两个每 session 最多各触发一次守卫，不会循环。所有 hook 在没有配置的目录下静默返回。

## 项目目录长这样

```
<repo>/.harness/
├── project.yml          # 边界、权威入口、输出契约
├── STATE.md             # 当前有效的结论（compaction 的产物，session 开头自动注入）
├── journal/
│   └── 2026-08-08.jsonl # WAL，只增不改
├── archive/
│   └── 2026-08-15/
│       ├── ARCHIVE-MANIFEST.md   # 每条：原路径 / 原因 / 被什么取代 / 时间
│       └── <被取代的文件>
└── .last-compact        # 上次收敛的时间戳
```

## 定时收敛

```bash
echo /path/to/repo >> ~/.harness/projects.txt
# crontab
0 3 * * * /Users/rshao/work/agent-harness/periodic/compact-cron.sh \
            --projects-file "$HOME/.harness/projects.txt" >> /tmp/harness-compact.log 2>&1
```

脚本自带三重守卫：没有 `.harness/` 跳过、距上次收敛不足 7 天跳过、工作区脏跳过。

## 拆

```bash
/Users/rshao/work/agent-harness/bin/harness uninstall
```

移除软链和 settings.json 里的 harness 条目，项目目录里的 `.harness/` 保持不动。

## 和 context-infrastructure 的分工

`context-infrastructure` 管个人思想的归纳，跟着人走。这个仓库管工作项目的运行机制，跟着项目走。两者互不依赖。
