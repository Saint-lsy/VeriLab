# VeriLab v1

[English](README.md) | 简体中文

VeriLab 是一个范围明确、以本地运行为主的可信实验控制器。Codex Executor 可以帮助修改实验代码、设计实验并提交不可变的实验说明，但正式实验必须由 Controller 授权执行；分数必须由可信 Grader 根据封存产物独立重算；实验完成后还必须经过一个全新的只读 Reviewer 审查，才有资格进入同一评测口径下的排行榜。

```text
网页对话 → Executor Codex → 冻结的 ExperimentSpec → 独立 Git worktree
         → 受监督的实验进程 → 封存产物 → 可信 Grader 重算指标
         → 全新只读 Reviewer → 中文实验变化总结 → Verified 排行榜
```

VeriLab 的目标是提供“可复算、可追溯的证据”，不是抵抗拥有 root 权限的恶意攻击者。它能阻止实验代码自己写出的 `score.json` 直接成为官方成绩，也能发现数据库事件、实验产物或评测代码发生漂移；但它不声称能够防御恶意 root 用户、同一 UID 的进程故意读取私有标签或内核级攻击。

## 适合解决什么问题

VeriLab 适合在单机或通过 SSH 登录的服务器上管理科学实验，尤其适合以下场景：

- 需要把“实验声称的结果”和“独立重算的可信结果”严格分开；
- 需要保存每次实验的代码版本、命令、进程证据、产物哈希、评测结果和审查结论；
- 需要确保不同实验只在相同数据、划分、预处理、评测器和指标定义下比较；
- 需要在实验完成后，由独立 Reviewer 判断证据是否完整，并用自然语言说明相对父实验改了什么、预期有什么效果、实际结果怎样；
- 需要保留失败、拒绝、撤回和证据受损的实验，而不是只保留最高分。

VeriLab v1 是本地、单用户、单正式流水线设计。它不提供多用户 RBAC、分布式 GPU 调度或面向公网的服务。

## 三个必须分开的目录

使用时请先区分三类内容：

1. **VeriLab 程序仓库**：本仓库，只保存通用应用、测试和文档。
2. **实验项目仓库**：需要被管理的实验代码，必须是一个独立且干净的 Git 仓库。正式实验绑定其中一个可从 `HEAD` 到达的 commit。
3. **Controller 状态目录**：保存 SQLite、事件链、运行目录、封存产物、Reviewer bundle、策略快照和 capability token，必须放在实验仓库之外。

挑战项目可以作为独立 Git 仓库放在 `results/` 下，但其数据、checkpoint、预测和 Controller 状态不应提交到 VeriLab 主仓库。VeriLab 运行时不依赖 CORAL 或 Argus。

## 安装

需要 Python 3.11 或更高版本。推荐使用 `uv`：

```bash
cd /path/to/VeriLab
uv venv --python 3.12
uv sync --extra dev
.venv/bin/verilab --version
```

默认 Reviewer 会调用当前已经登录的 `codex exec`。正式使用前请确认该命令在启动 VeriLab 的同一用户环境中可用。

## 五分钟跑通示例

示例项目只是模板，因为正式实验项目必须拥有自己的 Git 历史。

```bash
cp -a examples/dummy-project /tmp/verilab-dummy
cd /tmp/verilab-dummy
git init
git add .
git commit -m "dummy baseline"

COMMIT=$(git rev-parse HEAD)
sed "s/__GIT_COMMIT__/$COMMIT/" spec.template.json \
  > /tmp/verilab-dummy-spec.json
```

设置 VeriLab 程序和状态目录，然后安装可信策略：

```bash
VERILAB_BIN=/path/to/VeriLab/.venv/bin/verilab
VERILAB_STATE=/tmp/verilab-dummy-state

"$VERILAB_BIN" policy install policy.json \
  --project-root "$PWD" \
  --state-dir "$VERILAB_STATE"
```

启动本地 Controller 和网页：

```bash
"$VERILAB_BIN" serve \
  --project-root "$PWD" \
  --state-dir "$VERILAB_STATE"
```

浏览器打开 <http://127.0.0.1:8765>。右上角的 `中文` / `English` 可以切换完整的结构化界面，选择会保存在浏览器 Cookie 中。实验原始内容和审计原始证据不会被自动翻译。

在另一个终端提交正式实验：

```bash
export VERILAB_API_URL=http://127.0.0.1:8765
export VERILAB_CAPABILITY_FILE=/tmp/verilab-dummy-state/capability.token

"$VERILAB_BIN" submit /tmp/verilab-dummy-spec.json
"$VERILAB_BIN" status
```

Controller 会按 FIFO 顺序自动执行队列中的实验。示例成功后，实验会依次经历运行、评分和审查，并在通过全部门禁后显示为 `ACCEPTED`。

## 日常使用流程

### 1. 准备独立实验项目

实验项目必须满足：

- 项目根目录就是 Git 仓库顶层；
- 工作区没有已修改、已暂存或未跟踪文件；
- `ExperimentSpec.git_commit` 指向已存在且可从当前 `HEAD` 到达的 commit；
- 正式命令使用 argv 数组，不通过一整段 shell 字符串提交；
- `cwd` 和产物 glob 都不能逃出 Controller 分配的工作目录；
- secret 通过 `secret_refs` 声明，不直接写入 `env`、spec 或 Git。

直接在网页对话框中让 Executor 运行的调试命令属于 **untracked** 操作，不会自动成为正式成绩。需要排名的实验必须先提交不可变的 `ExperimentSpec`。

### 2. 定义并安装可信策略

`ProjectPolicy` 决定哪些实验可以互相比较，以及 Controller 应怎样独立评分。它至少需要固定：

- 项目和评测协议：`project_id`、`protocol_id`；
- 数据、划分和评测口径：`dataset`、`split`、`cohort`、`preprocessing`、`evaluator`；
- 主指标及方向：`primary_metric`、`direction`；
- 可信 Grader 命令和代码：`grader_command`、`grader_code_paths`；
- 正式实验必须提供的产物角色：`required_artifact_roles`；
- 运行和 Reviewer 超时、产物封存阈值等控制参数。

安装策略时，Controller 会计算 Grader 相关文件的 SHA256，并生成不可变策略快照。任何会改变比较口径的策略变化都会产生新的 `policy_hash` 和 `comparison_key`，旧实验不会与新口径混排。

```bash
verilab policy install policy.json \
  --project-root /path/to/experiment-project \
  --state-dir /path/to/controller-state
```

已排队实验继续使用提交时绑定的策略快照，不会被后续安装的新默认策略悄悄改变。

### 3. 编写 ExperimentSpec

一个最小的正式实验说明如下：

```json
{
  "schema_version": 1,
  "title": "Baseline",
  "hypothesis": "固定基线应得到可复算结果。",
  "parent_experiment_id": null,
  "git_commit": "完整或可唯一解析的提交哈希",
  "command": ["python3", "train.py"],
  "cwd": ".",
  "env": {},
  "secret_refs": [],
  "protocol_id": "public-oof-v1",
  "expected_artifacts": [
    {
      "role": "predictions",
      "glob": "outputs/predictions.json",
      "required": true
    }
  ],
  "resource_claim": {
    "gpu_ids": [],
    "cpu_cores": 1,
    "memory_gib": 1
  },
  "metadata": {}
}
```

关键字段含义：

- `title`、`hypothesis`：说明做什么以及为什么做；
- `parent_experiment_id`：声明科学上的直接父实验，用于实验谱系和差异解释；根基线填 `null`；
- `git_commit`：冻结本次正式执行使用的代码；
- `command`、`cwd`、`env`：定义实际运行入口；
- `expected_artifacts`：按角色声明需要从 `VERILAB_RUN_DIR` 中封存的文件；
- `resource_claim`：记录计划使用的 GPU、CPU 和内存；若设置 `CUDA_VISIBLE_DEVICES`，它必须与 `gpu_ids` 一致；
- `protocol_id`：必须与当前可信策略一致。

实验程序应把正式产物写到环境变量 `VERILAB_RUN_DIR` 指向的目录。Controller 还会注入 `VERILAB_EXPERIMENT_COMMIT`，方便程序记录实际绑定的 commit。

### 4. 提交、查看和取消

```bash
export VERILAB_API_URL=http://127.0.0.1:8765
export VERILAB_CAPABILITY_FILE=/path/to/controller-state/capability.token

verilab submit /path/to/spec.json
verilab status
verilab status EXPERIMENT_ID
verilab follow RUN_ID
verilab cancel RUN_ID
```

相同策略下完全相同的 spec 会被去重，不会创建第二张正式运行票据。`follow` 输出与指定 run 有关的规范事件；`cancel` 只适用于排队中或运行中的任务。

### 5. 理解状态

主流程是：

```text
DRAFT → QUEUED → RUNNING → GRADING → REVIEW_PENDING
                                      ├→ ACCEPTED
                                      ├→ REJECTED
                                      ├→ REVIEW_BLOCKED
                                      └→ NEEDS_HUMAN
```

其他终止或异常状态包括：

- `FAILED`：实验进程或流水线执行失败；
- `CANCELLED`：任务被取消；
- `VERIFICATION_FAILED`：产物、指标一致性或预审完整性验证失败；
- `ORPHANED`：Controller 恢复时无法安全确认原进程身份或接管状态；
- `REVIEW_BLOCKED`：Reviewer 调用失败、输出格式错误、证据引用不合法或说明不完整；
- `NEEDS_HUMAN`：Reviewer 明确认为现有只读证据不足以自动决定。

低分本身不是拒绝理由。只要实验执行、产物、评测和协议证据完整，真实的低分也可以被接受并进入同口径排行榜。

### 6. 查看网页中的结果

Dashboard 提供四类核心视图：

- **Verified 排行榜**：只显示经过可信 Grader 重算和 Reviewer 放行的成绩；
- **All runs / Rejected / Untracked**：保留所有正式状态和非正式调试边界；
- **实验谱系**：按 `parent_experiment_id` 展示父子关系，先显示 Reviewer 给出的中文变化总结，再按需展开 spec 和 Git 技术差异；
- **实验详情**：显示状态时间线、命令与进程证据、computed/verified 指标、产物哈希、Reviewer 八项检查和可下载审计 bundle。

Audit Inbox 集中显示待审、被阻塞、需人工判断、验证失败、被拒绝或证据健康度异常的实验。网页中可以：

- 重试处于 `REVIEW_BLOCKED` 或 `NEEDS_HUMAN` 的 Reviewer；
- 给实验追加人工备注；
- 撤回已接受但不应继续参与排名的实验。

撤回不会删除原实验、分数或审计证据，只会将其从当前 Verified 排名中隐藏，并在规范事件链中追加撤回事件。

## 一个成绩怎样进入 Verified 排行榜

实验进程退出成功并不等于结果已经可信。正式入榜需要依次完成：

1. Controller 根据冻结 spec 和策略签发 run ticket；
2. 在对应 commit 的 detached worktree 中启动受监督进程；
3. 记录 PID、`/proc` 启动标识、命令指纹、心跳、日志、资源采样和退出回执；
4. 按 spec 封存必需产物并记录 SHA256；
5. 可信 Grader 从封存产物独立计算指标；
6. 核对实验报告指标与 computed 指标是否一致（若策略要求报告指标）；
7. 全新的只读 Reviewer 检查固定 bundle，并完成八项强制检查；
8. Reviewer 用简体中文给出有证据支持的变化总结；
9. Controller 先把总结及其哈希写入追加式事件链，再写入 `experiment.accepted` 和排行榜投影。

八项 Reviewer 检查是：授权执行、执行证据、产物完整性、指标可复算性、协议合规、数据与划分完整性、结果一致性、必需产物齐全。缺一项、出现 `unknown`、bundle 哈希不匹配或引用了不存在的事件/哈希，都会失败关闭，不能入榜。

## 产物怎样保存

VeriLab 采用混合封存：

- 小于或等于策略中 `small_artifact_limit_mib` 的文件会复制到按 SHA256 寻址的只读对象库；
- 更大的文件记录绝对路径、大小和 SHA256，不再复制一份，以避免重复占用大量磁盘；
- Reviewer 开始前若源文件缺失或哈希改变，实验进入 `VERIFICATION_FAILED`；
- 实验接受后若证据丢失或漂移，历史排行榜记录仍保留，但 `evidence_health` 会变为 `degraded`。

因此，大 checkpoint 的原始路径仍然属于正式证据的一部分，不能在没有迁移和重新封存方案的情况下随意清理。

## Controller 状态目录

未指定 `--state-dir` 时，默认位置是：

```text
~/.local/share/verilab/<project-id>/
```

典型结构如下：

```text
state.sqlite3                         SQLite 主状态、投影和追加式事件链
state.sqlite3-wal / state.sqlite3-shm SQLite 运行期 WAL 文件
capability.token                      本地变更操作凭据，权限为 0600
policy.json                           当前默认可信策略
policies/<policy-hash>.json           不可变策略快照
runs/<run-id>/                        日志、回执、manifest 和实验输出
worktrees/<run-id>/                   对应 commit 的 detached Git worktree
objects/sha256/<前两位>/<其余哈希>    小产物的内容寻址对象库
reviewer-bundles/<review-id>/          Reviewer 输入、输出、事件和 bundle 清单
codex/                                Executor Codex 的本地会话输出
```

`state.sqlite3` 中的主要表包括：

- `experiments`：冻结 spec、父子关系、策略、comparison key 和当前状态；
- `runs`：run ticket、命令、进程身份、心跳、退出码和运行目录；
- `artifacts`：产物角色、路径、SHA256、大小、对象库位置和健康度；
- `metrics`：严格区分 `reported`、`computed`、`verified`；
- `reviews`：每次 Reviewer 尝试的 bundle 哈希、状态、输出和错误；
- `events`：带前序哈希的规范追加式事件链，数据库触发器禁止更新和删除；
- `leaderboard_entries`：由已接受事件重建的排行榜投影；
- `codex_sessions`、`messages`：Executor/Reviewer 会话和消息记录。

不要手工修改 SQLite 或排行榜表。正式状态只能通过 Controller 流程变化。

## 完整性审计

执行：

```bash
verilab audit verify \
  --project-root /path/to/experiment-project \
  --state-dir /path/to/controller-state
```

该命令会：

- 重新计算 SHA256 事件链；
- 从规范事件重建实验状态和排行榜预期值，并与 SQLite 投影比较；
- 验证策略快照及其 comparison key；
- 检查封存产物当前是否仍然存在且哈希一致；
- 检查 accepted 事件是否绑定了此前记录的变化总结哈希。

返回结果中的 `ok: true` 表示事件链和投影一致；还应同时查看 `artifact_health` 和 `health_changes`，确认是否存在证据退化。

对于在“中文变化总结”门禁加入之前已接受的历史实验，管理员可以追加一份明确标记的历史说明，而不改写原 Reviewer 和成绩：

```bash
verilab summary import summaries.json \
  --project-root /path/to/experiment-project \
  --state-dir /path/to/controller-state
```

该命令不会覆盖已经存在的规范 Reviewer 总结。

## 远程服务器访问

服务默认只监听 `127.0.0.1:8765`。在本机通过 SSH 隧道访问远程服务器：

```bash
ssh -L 8765:127.0.0.1:8765 USER@SERVER
```

然后在本机浏览器打开 <http://127.0.0.1:8765>。不建议把 `VERILAB_HOST` 改为公网地址。`capability.token` 只授权窄范围的 Controller 变更操作，不应写入 spec、事件、产物、bundle、Git 或聊天内容。

## 开发验证

```bash
.venv/bin/ruff check src tests
.venv/bin/pytest
node --check src/verilab/static/verilab.js
```

## v1 明确不包含的功能

VeriLab v1 不包含：实验开始前的 LLM 审核、多用户/RBAC、局域网监听、并发正式实验、分布式 GPU 调度、Docker/UID 隔离、远程 scorer、自动产物清理、浏览器 shell，以及绕过证据链的“人工强制 verified”。历史 CORAL/Argus 数据也不会被自动导入；如需迁移，必须通过明确的适配器重新封存、重算和审查。
