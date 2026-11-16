# 推荐反馈 · 样本与评估应用（零依赖本地演示）

围绕推荐反馈生命周期（展示 → 点击 → 购买 → 取消 / 退货，以及个性化许可撤回），
从零实现的本地样本构建与评估应用。仅使用 Python 标准库 + SQLite，不接真实推荐平台，
基线模型为纯 Python 逻辑回归。

## 启动

```bash
python -m recapp.demo --db ./data/recapp.db --reset
# 打开 http://127.0.0.1:8000 （可用 --port 改端口）
```

## 测试

```bash
python -m unittest tests.test_invariants -v   # 16 个不变量单元测试
# 端到端冒烟（需先启动服务，默认端口 8765）：
python -m recapp.demo --db /tmp/recapp_smoke/demo.db --reset --port 8765 &
python tests/smoke_http.py
```

## 核心规则

### 1. 标签：退款后绝不再当正例

以一次曝光（impression）为时间锚点 `anchor`，成熟窗默认 14 天：

| 锚点后事件 | 标签 | 状态 |
|---|---|---|
| 退货 refund（哪怕迟于成熟窗） | 0 负例 | mature，原因 `refund_after_purchase` |
| 取消 cancel（无退款） | -2 | excluded，不进训练/评估 |
| 购买且无取消/退款 | 1 正例 | mature |
| 仅点击/无互动 | 0 负例 | mature |
| 成熟窗到达前仍无终态 | 临时标签 | pending |

退款优先级最高，且迟到的退款由**标签回补**任务生成样本新版本把旧正例翻成负例；
样本行不可变，旧版本保留并置 `superseded=1`，`label_history` 记录每次翻转与触发事件。

### 2. 防泄漏：不能用目标发生后的特征训练同一个目标

- 特征查询条件硬约束 `event_ts <= anchor_ts`，落库字段 `feature_cutoff_ts`
  恒等于 `anchor_ts`，并有断言兜底；样本详情页可直接核对。
- 特征在建样本时冻结为快照；回补只翻标签、不改特征，事后事件无法污染旧样本。
- 评估按时间序 80/20 切分，不随机打乱，未来样本不进训练集。

### 3. 冻结观察窗 vs 滚动成熟标签（新鲜度与完整性的取舍）

| | 冻结观察窗 frozen | 滚动成熟标签 rolling |
|---|---|---|
| 入样条件 | 仅 `anchor + H ≤ now` 的成熟样本 | 窗内全部曝光，未成熟者先入 pending |
| 标签完整性 | 完整，无 pending 噪声 | 回补完成前不完整 |
| 新鲜度 | 最近 H 天曝光不可用，牺牲新鲜度 | 最新曝光立即可用，新鲜度高 |
| 适用 | 离线评估、版本对比 | 在线迭代、需要尽快纳入新反馈 |

页面支持同输入同时创建两种窗口，直接比较样本数与指标差异。

### 4. 许可撤回的可追溯处置

撤回一个用户时（全部动作写只追加的 `disposal_log`）：

1. `consent_versions` 追加 `granted=0`，用户置 `withdrawn`；
2. **原始明细**：逐行归档 `disposal_archive` 后从 `events` 硬删除；
3. **训练样本**：该用户所有样本版本打墓碑 `tombstoned=1`，数据集快照同步标记；
4. **缓存**：引用该用户的 `cache_entries` 全部失效并留痕；
5. **任务**：排队/运行中的相关任务置 `aborted`，worker 写库前再做授权复检；
6. 撤回后接入该用户的新事件直接被拒绝（`ConsentError`）。

恢复（演示能力）写新的授权版本、从归档还原事件并解除墓碑，同样全程留痕；
旧任务不会因恢复而复活——任务固定在创建时的授权版本上。

### 5. 并行任务的固定输入与授权版本

回补、样本重建、窗口构建、评估都是 jobs 台账里的任务：

- 创建时固定 `pinned_consent_version`；评估任务另固定数据集版本的 `input_hash`，
  执行前重算哈希，不一致即拒绝；
- 任务原子认领（条件 UPDATE），多 worker 不会重复执行同一任务；
- 评估产物记录 input_hash / consent_version / code_version / feature_version，
  同输入两次评估指标完全一致（确定性随机数 + 固定训练轮数）；
- 墓碑样本在评估中跳过并计数，撤回数据绝不参与训练。

## 模块结构

```
recapp/
  db.py          SQLite 建表与连接
  models.py      时间工具、标签状态机、防泄漏特征、授权判断、稳定哈希
  ingestion.py   事件接入、缓存登记/失效、许可撤回与恢复处置
  samples.py     样本版本化、回补、重建、窗口与数据集版本
  evaluation.py  纯 Python 逻辑回归、指标、输入哈希校验与评估存储
  jobs.py        任务台账、原子认领、后台 worker
  seed.py        确定性演示数据（含迟到退款、待撤回用户、未成熟样本）
  webui.py       标准库 HTTP 服务与 JSON API
  static/        单页前端（样本来源/切窗口/撤回/任务/指标比较/审计六个页签）
tests/
  test_invariants.py  16 个不变量单元测试
  smoke_http.py       端到端 HTTP 冒烟
```

## 页面能做什么

- **样本来源**：点开任一样本看原始事件链、锚点与特征截止时间、全部版本与标签翻转历史；
- **切窗口**：同输入创建冻结/滚动窗口并各自构建版本、比较样本数；
- **撤回与恢复**：对 `u_withdraw` 走完整处置链并查看缓存失效；
- **任务**：入队回补/重建/构建/评估，观察固定授权版本与中止状态；
- **指标比较**：时间序切分的逻辑回归指标（固定 0.5 与自适应阈值两套），
  含 AUC、precision/recall/f1、混淆矩阵与全部版本指纹；
- **处置审计**：只追加日志与归档明细，支持追溯到单个 event/sample。
