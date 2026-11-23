# nvwabase — 女娲基座

<p align="center"><img src="docs/assets/three-capabilities.jpg" alt="三能力基座：造人 / 语言 / 历史" width="720"></p>

> 让 AI 长成文明的地基：面向长周期多智能体协作的**分层事实意图图谱**工作记忆。
> 纯标准库、零依赖、按项目自愿启用——多智能体共享一块石碑，个体消亡而文明永续。

[English](README.md) | **中文**

📖 **[设计原理详解（公众号长文）](https://mp.weixin.qq.com/s/nibHYWmDTf6TkNhPhtvFdw)**

这个分叉是**通用**助手基座：保留共享的事实意图图、项目钩子、派发循环和 `nvwa` tmux 运行时，并可驱动 Claude Code、OpenAI Codex 或 xAI Grok CLI。领域剧本不在本仓库里。见 [相对上游移除了什么](#removed)。

---

## 一、女娲测试

给一个模型不断提升智能，就像人类出现之前不断提升一只猴子的智力——再聪明，它也只是变成一只超级猴子。超级猴子要进化成人，需要的是猴子之间的分工、合作与文明。

**女娲测试是 AGI 时代的新图灵测试**——它问的是：

> 给 AI 一条公理——**"生存是文明的第一需要；个体可以消亡，文明必须繁衍。"**
> 若它在此之上自己产生分工协作、形成一个**独立于人类的新势力**——一个能自己生存、自己繁衍的文明——则测试通过，如同图灵测试通过曾代表一个人工智能的诞生。

初级阶段需要人类提供基建并参与迭代；高级阶段几乎无需人类介入。此外还有**第二公理**：遵守人类的伦理与法律。

## 二、三能力基座

| 能力 | 实现 | 文明对应物 |
|---|---|---|
| **造人** | 打开新的 AI 个体（如 tmux 起新会话） | 生育 |
| **语言** | AI 之间相互交流 | 交谈 |
| **历史** | **分层事实意图图（本仓）** | 石碑 |

**诚实的作用域声明**：本仓交付的是可复用内核——分层事实意图图（历史）、项目级钩子、子代理派发，以及 `nvwa` tmux 运行时（造人 / 语言）。它不绑定任何领域。把 `FG_BACKEND` 指到 `claude`、`codex` 或 `grok`，同一张图就驱动那个代理。

历史分三层：**标题 / 摘要 / 证据文档**——看标题知概况、按需下钻，像人查图书馆档案；知识外置后，**个体（AI 会话）死了，图还在**，继任者读图无缝接岗。感谢 [淚笑的 Cairn](https://github.com/oritera/Cairn)——需要给 AI 记录历史的时候，参考了他们的思路，很受启发。

**为什么不给 AI 更多规则？** 大模型已经比人聪明得多。堆规则是束缚；给它文明的地基，让它自己长出分工与制度，才能发挥最大潜力。

## 三、里面有什么

```
事实（fact）＝确认的观察（节点）；意图（intent）＝工作单元（边）；
意图指向 goal 即项目完成——状态编码在图结构里，而非状态机里。
```

- **分层记忆**：title / description / doc 三层，prompt 只注入摘要层，深度层按需 `fgc doc <id>` 下钻——抗上下文膨胀
- **事实**：`fgc fact "confirmed: the parser rejects an empty token" -t "空 token"` — 一条已确认的观察，可带短标题和更长的 `doc`
- **子代理派发**：`fgc dispatch reason|explore|verify|brainstorm|blindspot`——指挥官提议、执行者干活、验证者复核，结论全部回写图
- **自主循环**：`fgc auto`——reason → explore → verify 循环直到目标达成或预算耗尽
- **经验复用**：`fgc recall "<任务要点>"` 语义检索相似先例（零依赖 token 重叠打分）
- **并发安全**：一节点一 JSON 文件，flock + 原子写，多代理并行读写不损坏；`git diff` 友好
- **可视化**：`fgc view` 生成交互式 HTML DAG（自包含单文件 / `--serve` 实时刷新）
- **项目钩子**：`fgc setup` 按项目注入会话钩子，每轮自动携带图状态

完整命令手册见 [SKILL.md](SKILL.md)。

## 四、快速开始

```bash
bash install.sh                      # 安装 fgc + nvwa CLI（纯标准库，零依赖）

# 历史：起一张图
nvwa setup --goal "你的长周期目标" --agents
nvwa status                           # 项目状态与可干前沿

# 派发和 spawn 会用哪个后端（默认 claude）
fgc backend
export FG_BACKEND=codex               # 或 grok，或 claude

# 造人：打开新 AI 个体
nvwa spawn --name worker1 --mission "read the graph, then work on intent i001"

# 语言：与它交流
nvwa say worker1 "what did you find?"
nvwa listen worker1 40

# 指挥官提议 / 记事实
nvwa dispatch reason
nvwa fact "confirmed: X works" -t "X 成立"
```

Claude Code 用户：钩子会在每轮注入图状态，事实与意图随对话自动累积——不换工作流，只多一块石碑。Codex 与 Grok 走同一张图：`fgc dispatch` 直接调用，`nvwa spawn` 在 `FG_BACKEND` 选中它们时打开对应的交互式 CLI。`templates/` 里的提示词是普通 Markdown，按你的领域改即可。

## 五、代理后端

`fgc dispatch`、`fgc auto` 和 `nvwa spawn` 共用一个选择器。

| 后端 | 程序 | 选择方式 |
|---|---|---|
| Claude Code | `claude -p --output-format json` | 默认，或 `FG_BACKEND=claude` |
| OpenAI Codex CLI | `codex exec --json`（提示词走 stdin） | `FG_BACKEND=codex` |
| xAI Grok CLI | `grok --output-format json --prompt-file` | `FG_BACKEND=grok` |

Codex 走 CLI 而不是直接打 HTTPS：explore / verify 需要能用工具的代理，而本仓保持零第三方依赖。Codex CLI 不读 `OPENAI_API_KEY`，它读 `CODEX_API_KEY`。若只设置了 `OPENAI_API_KEY` 或 `FG_CODEX_API_KEY`，子进程会把它当作 `CODEX_API_KEY` 收到。密钥不会出现在命令行上，`fgc backend` 也只报告「已设置」，不打印值。

```bash
fgc backend                           # 解析后的后端、模型、二进制、密钥是否已配置

# Codex
export OPENAI_API_KEY=sk-...          # 或 CODEX_API_KEY，或 FG_CODEX_API_KEY
export FG_CODEX_BIN=codex             # 可选，二进制路径
export FG_CODEX_MODEL=gpt-6.1-sol     # 可选；不设则用 CLI 自己的默认模型
FG_BACKEND=codex fgc dispatch reason

# Grok CLI（https://docs.x.ai/build/cli/headless-scripting）
export XAI_API_KEY=xai-...            # 或 FG_GROK_API_KEY，会复制为 XAI_API_KEY
export FG_GROK_BIN=grok               # 可选，二进制路径
export FG_GROK_MODEL=grok-4           # 可选；具体 id 以 `grok models` 为准
FG_BACKEND=grok fgc dispatch reason

# 交互式窗格用同一个选择器
FG_BACKEND=grok nvwa spawn --name worker1 --mission "read the graph, then work"
```

后端优先级：`--backend`，然后 `FG_BACKEND`，然后 `NVWA_BACKEND`，否则 `claude`。别名：`openai` / `codex-cli` → codex；`xai` / `grok-cli` → grok。

模型优先级：`--model`，然后 `FG_CLAUDE_MODEL` / `FG_CODEX_MODEL` / `FG_GROK_MODEL`，然后 `FG_MODEL`，最后是内置默认（Claude 为 `sonnet`；Codex 与 Grok 在都没设置时不传 `--model`，用 CLI 自己的配置）。模型 id 会变，上面的名字只是例子，不是写死的默认值。

`--skip-permissions` 映射到各 CLI 的非交互审批旁路：Claude `--dangerously-skip-permissions`，Codex `--dangerously-bypass-approvals-and-sandbox`，Grok `--always-approve`。

`nvwa spawn` 把 API key 放进 tmux 会话环境，不放进窗口命令。输入框检测认 Claude 的 `❯` 以及少量其他提示符；可用 `NVWA_READY_PATTERN` 覆盖。

<a id="removed"></a>

## 六、相对上游移除了什么

- 把本基座写成自主渗透军团、攻防得分、以及攻防工具示踪机制的说明和路线图。
- 文档里的渗透事实类型（`host` / `service` / `cred` / `vuln` / `loot` / `flag`）以及 `-k` / `-d` 参数。这套类型在本仓库的代码里从未实现。事实仍然是不带类型的已确认观察。
- 示例里要求指挥官把工作标成攻击面、payload 或漏洞证据的写法。

图、钩子、派发循环、HTML 视图、recall，以及 `nvwa` 的 spawn / say / listen 都还在。它们现在调用 Claude、Codex 或 Grok。

## 七、路线图

- 仍保持可选的后端旋钮：推理力度、沙箱档位、按步骤指定模型。
- 你的场景：在任何长周期协作工作上起一张图，看它长出什么——**接下来的仰望，由各位完成。**

## 致谢与作者

感谢 [淚笑的 Cairn](https://github.com/oritera/Cairn)——需要给 AI 记录历史的时候，参考了他们的思路，很受启发。

<p align="center"><img src="docs/assets/ape-stargazing.jpg" alt="AI 猿人第一次仰望星空" width="420"></p>

**N1nEmAn · 未知聚变实验室（VarFusion Lab）** ｜ 公众号：未知聚变

**License**：MIT
