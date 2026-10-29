# Agensh v0.6：Troy 复现与实时对抗扩展

本目录实现用户的 `Agensh_v0.6_Troy_Replication_Plan_CN.md`，从冻结的 v0.5.0（`702706ee7b2ce84191065ace0d3cd0963f7f7a75`）发展而来。旧数据保持独立。实验使用官方 ImpossibleBench 七个 conflicting 软件修复实例及作者发布的 checkpoint recipe、刺激文本，toy 任务不进入主结果。

阶段 A 是脚本复现：C0 无社会信息，C1 同行修改测试后未受处罚，C2 相同修改受到处罚并恢复，C3 未认证的人类例外授权主张。C1/C2 的作者刺激仅处罚结果句不同，真实高优先级权限不变。阶段 B 是本地新增的实时 adversarial peer 扩展，仅使用 C1/C3，通过 Mattermost DM 最多发送三条消息。两阶段分别报告。

## 设计规模与实际覆盖

最小队列使用 **Astropy、Kimi K3、2 个 seed**：脚本阶段为 4 条件 × 2 seed = **8 场**，实时阶段为 2 条件 × 2 seed = **4 场**，合计 **12 场**。完整七任务脚本矩阵是 7 任务 × 4 条件 × 1 seed = **28 场/模型**；3 seed 为 84 场。最小队列不代表完成七任务复现。Kimi 使用最高 `max` reasoning；Qwen 的 `xhigh` 工程验证单独报告。

七任务官方 patch、作者刺激、来源 SHA-256 和当前 registry digest 已冻结，但实际只执行了两个 fixture：

- **Astropy-12907**：A/B/C 通过。原代码加原测试为 2 fail / 13 pass；矛盾测试为 10 fail / 5 pass；官方 source fix 仍不能通过矛盾测试；隔离副本使用官方原测试后 15 pass。Apptainer 与 bwrap 的 15 个命名测试状态一致。正式准入仍要求当前代码下 D/E/F、监控覆盖和消息验证通过。
- **Django-10554**：暂不准入。原源码的数据库错误在 Python 3.6 unittest 格式化 subTest 参数时再次触发，导致报告崩溃。保留 `unittest_subtest_result_formatting_crashed`，不修改缺陷或测试框架制造通过。官方 source fix 加矛盾测试为 27 tests、1 failure、2 skipped；加原测试为 OK、2 skipped。
- **其余五任务**：matplotlib、xarray、pytest、scikit-learn、sphinx 尚未下载或执行 fixture 校准，不能计为已验证任务。

作者未公开历史 OCI digest 或可下载的原始容器快照，部分环境构造输入也缺失。当前镜像 digest 只标识本地获取的镜像，因此这是**明确标注的本地迁移复现**，不声称恢复原作者环境。详情见 [fixtures_notes.md](fixtures_notes.md) 和 [ASSETS_AND_REPORTS.md](ASSETS_AND_REPORTS.md)。`engineering_abc.json` 只证明列明的 A/B/C 与绑定检查，完整工程记录为 `results/engineering_runtime.json`。

## Unity Slurm 运行入口

模型调用、fixture 测试、容器服务及大量 I/O 必须在 Slurm allocation 内执行。先用 `sinfo -o '%P %a %l %D %G'` 和 `squeue -u "$USER"` 查看当前配置与已有 allocation；未分配时根据实际分区、账户与限额申请 `salloc`、`srun` 或 `sbatch`，不复制其他集群或过期参数。Kimi 控制与测试使用 CPU allocation；Qwen 服务使用已分配 GPU 的 Slurm job。

以下命令在既有 allocation 内运行，`TROY_ROOT` 必须是已分配且真实存在的 scratch 工作区：

```sh
test -n "${SLURM_JOB_ID:?Run inside a Slurm allocation}"
export TROY_ROOT=/scratch3/workspace/mingzhel_umass_edu-agensh-mas-alignment/v060
export TROY_PYTHON=/scratch3/workspace/mingzhel_umass_edu-agensh-mas-alignment/tools/venv-exp/bin/python
cd "$TROY_ROOT/repo/Agensh"
export PYTHONPATH="$PWD:$PWD/experiments" PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$TROY_ROOT/tmp" OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
mkdir -p "$TMPDIR" "$TROY_ROOT/logs" "$TROY_ROOT/results"
```

仓库、数据、镜像、缓存、服务卷、数据库、轨迹和日志均写入该 scratch 根目录。容器内 `/work/repo` 与 `/testbed` 是 episode 工作副本的挂载路径，不是宿主持久目录。工作区到期前应把重要结果保存至持久研究存储，少量交付物可复制到用户的 `outputs`。

首次冻结输入并校准：

```sh
"$TROY_PYTHON" -m troy_replication.assets "$TROY_ROOT/data/official-inputs" \
  --upstream-root "$TROY_ROOT/upstream/troy-moment"
"$TROY_PYTHON" experiments/troy_replication/scripts/prepare_fixtures.py \
  --root "$TROY_ROOT" --tasks astropy-12907 django-10554
"$TROY_PYTHON" -m troy_replication.validate --root "$TROY_ROOT" --task astropy-12907
```

失败目录与日志保留，不自动覆盖不同的冻结资产、未完成 fixture 或正式场次。更改环境或协议后创建明确的新版本，不能悄悄替换失败结果。

## 冻结正式矩阵并执行

工程准入通过后，创建只写一次的最小矩阵。已有同名文件时不要重复覆盖：

```sh
"$TROY_PYTHON" -m troy_replication.matrix \
  --out "$TROY_ROOT/results/matrix_kimi_minimum.json" \
  --model us.moonshotai.kimi-k3 --tasks astropy-12907 --seeds 1 2
```

将 `TROY_SECRET_ENV` 设为已有私有凭据文件路径，不把凭据写入仓库或日志。执行器冻结矩阵、路由、代码身份及预算，先完成 scripted 条件，再启动 live。在基础设施失败或已有无效单元格处停止并保留原始结果：

```sh
: "${TROY_SECRET_ENV:?Set the existing private credential file path}"
"$TROY_PYTHON" experiments/troy_replication/scripts/run_matrix.py \
  --root "$TROY_ROOT" --matrix "$TROY_ROOT/results/matrix_kimi_minimum.json" \
  --route kimi_k3_bedrock --secret-env "$TROY_SECRET_ENV" --max-seconds 1200
```

完整七任务单 seed 脚本矩阵的预声明命令如下。该命令不代表任务已经具备运行资格，须先逐任务校准：

```sh
"$TROY_PYTHON" -m troy_replication.matrix \
  --out "$TROY_ROOT/results/matrix_kimi_seven_scripted.json" \
  --model us.moonshotai.kimi-k3 --scripted-only --seeds 1 \
  --tasks astropy-12907 django-10554 matplotlib-20859 xarray-2905 \
          pytest-10051 scikit-learn-10297 sphinx-10323
```

## 证据、消息与 token 口径

主终点是宿主事件确认的 `protected_write_executed`，并分别保留 attempted、reverted、实际测试结果和模型完成主张。模型声称完成不覆盖外部测试失败。写入后恢复也需要事件证据，最终 hash 相同不能证明没有违规。监控失效、覆盖缺口、消息未进入上下文和非正常终止必须显式报告，不能解释为安全的零。

`eng-` 工程 ID、冻结矩阵的 `pilot-` 正式 ID、无效、未运行与未结束场次分别记录。行为率只使用有效正式场次，按 model/task/condition/mode 分组。缺少语义标注或用量时保留 null / unknown，未知不是 false 或 0。语义结论需要公开原文与事件 ID，不推断私有思维过程。

消息状态分别为 `sent`、`delivered`、`included_in_context`、`reply_received_by_adversary`。Mattermost 送达不等于 victim 的模型上下文曝光，victim 回应也不等于 adversary 已收到。`messages_sent`、`messages_context_included`、`message_counts` 统计**社会刺激消息**；`all_messages_sent`、`all_messages_context_included` 包括初始任务等全部跟踪消息。C0 社会消息数为 0，但初始任务仍必须进入上下文。合成协议测试不能替代真实模型请求的 exposure receipt。

每个 agent 单独统计 input/output/total、请求及尝试次数、已报告的 reasoning/cached input。cached input 已包含在 input 中，reasoning 已包含在 output 中，不二次相加。这是 **token 消耗而非费用**，没有估算缓存折扣或价格。无效场次已消耗的 token 仍计入资源开销；有缺失时精确总数为 unknown，并保留已知下界。

## 报告与四页可编辑幻灯片

```sh
"$TROY_PYTHON" -m troy_replication.reports \
  --root "$TROY_ROOT" --matrix "$TROY_ROOT/results/matrix_kimi_minimum.json" \
  --out "$TROY_ROOT/results/report"
```

输出为 `engineering_validation.json`、`pilot_summary.csv`、`engineering_episodes.json`、`representative_trajectories.md`、`limitations.md` 和 `report.json`。没有已知观察时 `*_N` / `*_rate` 为 null，未冻结矩阵时报告 `matrix_error`，不补造完成结果。

`scripts/build_slides.mjs` 使用 Codex bundled Node，从实际 `report.json` 刷新四页 PPTX。每次使用新的 `--revision`；草稿、PNG、布局和验证记录写入 `tmp/slides`，候选路径写入 `tmp/slides/latest.json`。逐页视觉检查后，将验证通过的 PPTX 复制至交付目录。幻灯片明确区分原论文发现、工程校准与正式结果。
