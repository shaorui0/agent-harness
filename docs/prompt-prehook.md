# prompt-prehook：发 prompt 前的认知对齐

`hooks/harness-prompt-prehook.py`，挂在 `UserPromptSubmit`。属于 DESIGN.md 里「必须发生的事挂在 hook 上」那一类，但它只做一件很小的事：agent 动手之前，先把你自己的认知递给它。

## 为什么

agent 的产出质量，上限由 prompt 决定。同一个 agent，给它一句「搞个 X」和给它「产出什么、怎么验证、不碰什么」，结果差一个量级。而写 prompt 的时刻，恰好是人最容易偷懒、最不会去翻自己那套原则的时刻（原则一：需要人在某一刻想起来的机制，合规率会衰减到零）。

所以把这件事交给事件：每次发 prompt，hook 用确定性规则检查两件事，命中才说话：

1. 你的 prompt 碰到了你自己写下的某条原则（比如「技术选型」会触发「先列约束再比方案」），就把这条原则的 ID 和一行含义递给 agent；
2. 这是个构建类请求，却没写清产出、验收、范围边界，就提醒 agent 先自拟并说明成功标准，别默认。

原则放在你自己的文件里，仓库只提供机制，不带任何个人内容。

## 它注入什么

示例输出（中文，总长 ≤ 600 字）：

```
【对齐·公理】V02 可验证性：能被一条命令验证的才算做完；A06 约束先行：先列约束再比方案
【对齐·prompt】构建类请求未写清：产出什么(文件/格式/位置)、怎么验证做完(可跑的命令/判据)、不碰什么(范围边界)。先自拟并说明成功标准，别默认；极简优先，不加没要的功能。
```

- 公理段：命中触发词的公理，最多 4 条，每条含义截到约 36 字。
- prompt 段：命中构建动词（搞个/做个/实现/重构/build/implement…）且没出现验收、产出、范围类词（验收/DoD/只改/不碰/scope…）时出现。

## 它刻意不做什么

- **不调模型。** 纯关键词 + 正则，< 200 ms，在关键路径上（DESIGN.md「hook 里不调模型」）。
- **不拦截。** 只追加 `additionalContext`，永远 exit 0；任何异常静默降级。
- **短 prompt 静默。** 少于 8 个字（go / 跑 / ok / 继续）、裸路径、裸 URL、斜杠命令都不输出，不加噪音，也不读任何配置文件。
- **不判断你的 prompt 好不好。** 它只做「有没有提到验收」这种字面检查；真正的判断留给 agent 和你。
- **不内置任何个人原则。** 没配索引时只有第 2 类通用提示。

## 配置：公理索引放哪

按下面顺序解析，先命中先用：

| 顺序 | 来源 | 写法 |
|---|---|---|
| 1 | 环境变量 | `HARNESS_PREHOOK_AXIOMS=/path/to/INDEX.md` |
| 2 | 项目 `.harness/project.yml` | `prehook:` 下写 `axioms_index: docs/axioms/INDEX.md`（相对路径按项目根解析） |
| 3 | 全局 `~/.harness/config.yml` | 同样的 `prehook:` 块，对任意 workspace 生效 |
| 4 | 都没有 | 只出通用 prompt 质量提示，没有公理段 |

```yaml
# ~/.harness/config.yml 或 <repo>/.harness/project.yml
prehook:
  axioms_index: ~/notes/axioms/INDEX.md
```

文件不存在或读不出来，等同于没配（静默降级，不报错）。项目配置覆盖全局配置，环境变量覆盖两者。

### 索引文件格式

运行时解析，改文件即生效，不用动代码。需要两种行，样例见 `hooks/fixtures_prehook/axioms_INDEX.md`：

```markdown
## 主动检索触发
- 「验证」「可靠」→ V02, V01        # 关键词（任一命中）→ 公理 ID 列表

| ID | 公理 | 含义 |
|---|---|---|
| V02 | [可验证性](v02.md) | 能被一条命令验证的才算做完 |
```

ID 形如一个大写字母加两位数字（`V02`、`A06`）。触发行里出现、但表里没有定义的 ID 会被跳过。

## 安装

`bin/harness install` 会把 `hooks/harness-prompt-prehook.py` 软链进 `~/.claude/hooks/`，并按 `install/hooks-manifest.json` 把下面这条并进 `~/.claude/settings.json` 的 `UserPromptSubmit`（与 `harness-prompt-contract.py` 并列，两者的 `additionalContext` 会叠加）：

```json
{"type": "command", "command": "python3 ~/.claude/hooks/harness-prompt-prehook.py", "timeout": 5}
```

手动试：

```bash
echo '{"prompt":"这个方案怎么验证它可靠","cwd":"."}' | HARNESS_PREHOOK_AXIOMS=hooks/fixtures_prehook/axioms_INDEX.md python3 hooks/harness-prompt-prehook.py
```

回滚：删掉 settings.json 里这一条（或 `bin/harness uninstall`）。

## 怎么调

| 想改什么 | 改哪 |
|---|---|
| 加/改一条触发词、公理含义 | 你的索引文件，不用动代码 |
| 哪些动词算「构建类请求」 | `harness-prompt-prehook.py` 的 `BUILD_RE` |
| 哪些词算「已经写清了目标/验收/范围」 | `GOAL_RE` |
| 多短算对话式确认 | `MIN_LEN`（默认 8） |
| 一次最多提几条公理、总长上限 | `MAX_AXIOMS`（4）、`MAX_CHARS`（600） |

调的方向：嫌吵就收紧 `BUILD_RE` 或抬高 `MIN_LEN`，嫌漏就补触发词。每次提示都应该是「这次确实该想一想」，否则会像 CLAUDE.md 里越写越长的勘误一样被忽略。

## 测试

```bash
python3 hooks/test_prompt_prehook.py
```

测试用 `hooks/fixtures_prehook/` 里的合成索引，隔离 HOME，覆盖触发、配置解析顺序、构建类提示、短 prompt 静默、输出协议和耗时。
