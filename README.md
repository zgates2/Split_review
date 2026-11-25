# split_review — 长程 episode 审查 + 子任务切分（LeRobot v3.0）

在 thu_zz 上对一个 LeRobot v3 数据集做**视频审查**：在时间轴上打切点（帧级精确、无损、关键帧吸附可选），把一条长程 episode 拆成若干子任务段，给每段设 prompt、丢弃坏段，最后**每个子任务导出一个独立的 v3 数据集**。

> 状态：M1（切分/校验）+ M2（审查界面）+ M3（systemd 常驻）已完成。
> 部署位置：`/home/zz/wang/g2/split_review`，服务 `g2-split-review.service`（127.0.0.1:18770）。

---

## 1. 服务与启动（要不要手动起？）

**日常不用手动启动。** 打开 `http://127.0.0.1:18770/` 本身**不会**启动服务——这个网页就是该服务提供的，服务没跑时浏览器只会报“连接被拒绝”。但它已经配成**开机自启 + 崩溃自动重启**，正常情况下一直在跑。

| 检查项 | 当前值 | 含义 |
| --- | --- | --- |
| `systemctl --user is-enabled g2-split-review.service` | `enabled` | 随用户 systemd 会话自动启动 |
| `loginctl show-user zz -p Linger` | `Linger=yes` | 不登录、机器重启后也会自己起来 |
| `Restart` / `RestartSec` | `on-failure` / `3` | 进程异常退出后 3 秒自动重启 |

因此：

- **thu_zz 重启后**：等十几秒，直接打开网页即可，不需要敲任何命令；
- **关浏览器、断 SSH、关终端**：服务照常运行，审查结果已存在服务端 `state/review.db`；
- **只有两种情况需要手动起**：① 你自己 `stop` 过；② 进程被 `kill` 或正常退出（`on-failure` 只管异常退出）。

### 常用命令（在 thu_zz 上执行）

```bash
systemctl --user status  g2-split-review.service    # active (running) 才正常
systemctl --user start   g2-split-review.service    # 手动启动
systemctl --user stop    g2-split-review.service    # 停止
systemctl --user restart g2-split-review.service    # 重启（改完后端代码时用）
journalctl  --user -u g2-split-review.service -n 50 --no-pager   # 看日志

curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18770/  # 200 即正常
```

改 HTML / CSS / JS 不需要重启，刷新浏览器即可；改 Python 才需要 `restart`。

### 访问方式

```bash
# thu_zz 本机浏览器直接打开
http://127.0.0.1:18770/

# 从别的电脑访问（服务只监听 127.0.0.1，必须走隧道）
ssh -L 18770:127.0.0.1:18770 thu_zz
```

### 两个注意点

1. `127.0.0.1` 只有 thu_zz 本机能打开。要让同一局域网的人直接访问，把 unit 文件里的 `--host 127.0.0.1` 改成 `--host 0.0.0.0`，再 `systemctl --user daemon-reload && systemctl --user restart g2-split-review.service`。
2. 不要同时用 `./run.sh` 再起一个实例，端口会冲突；`run.sh` 只用于前台调试，日常交给 systemd。

首次使用：在顶部输入 LeRobot v3 数据集路径 → 「登记」→ 点左侧 episode 开始审。

## 2. 一条数据的完整审查流程

1. **设模板**：右上「子任务模板 ⚙」→ 每行一个子任务（如 `拿起手机` / `移动到位` / `放进盒子` / `合上盒盖`）→ 保存。第 n 段默认套第 n 行。
2. **选 episode**：左侧列表点一条（`#0000` 这类）。首次打开约 5–10 s 生成预览，之后秒开。
3. **看片打点**：`空格` 播放；看到动作分界处按 `M` 打标记（也可直接点时间轴）。拖动标记可微调，双击删除。
   - 时间轴上方刻度是关键帧位置，`吸附关键帧` 勾选后标记会落在刻度上；
   - 当前数据 GOP≈8.3 s，默认不吸附，标记精确到帧。
4. **设每段 prompt**：下方每个段卡片右侧下拉选择子任务；下拉最后一项「自定义…」可临时输入。
5. **丢弃废段**：段卡片右侧 ✕（或播放到该段按 `D`）把这段标为丢弃；再点一次恢复。整条都不想要就把所有段都标丢弃。
6. **保存**：`S` 或「保存 (S)」；`N`/「保存并下一条 →」保存后自动跳到下一条未审的。
7. **看进度**：顶部 `全部/已审/跳过/未审` 计数；左侧圆点 灰=未审、绿=已审、红=跳过。
8. **导出**：审完（或审了一部分）点「导出子任务数据集」→ 填目录名 → 后台跑，按钮显示百分比。
   - 产物：`<顶部「输出到」的目录>/<目录名>/task_01_xxx/ …`，每个子任务一个独立 v3 数据集（默认输出根目录 `/data/g2/lerobot_v2.1`）；
   - 同 prompt 的多条 episode 自动归并；报告在同级 `export_report.json`。
9. **校验**：`python -m split_review verify --dataset <产物目录>`，`ok=true` 才算可用。

页面上也内置了同一份说明：画面下方的「怎么用（3 分钟看完）」面板（可「收起」）。顶部多了 **「输出到」** 输入框，直接改导出根目录，浏览器会记住上次的值。

## 3. 审查界面

| 区域 | 说明 |
| --- | --- |
| 顶部 | 数据集选择/登记、子任务模板、输出目录、导出按钮、审查进度 |
| 左侧 | episode 列表（状态点：灰=未审、绿=已审、红=跳过），显示时长与保留段数 |
| 中间 | 三路相机同步播放（主轨驱动，从轨 >80 ms 自动纠偏） |
| 时间轴 | 点击空白=加标记，拖动=移动，双击=删除；勾选「吸附关键帧」时对齐到关键帧刻度 |
| 段面板 | 每个切点把 episode 分成若干段，逐段选 prompt、点 ✕ 丢弃该段 |
| 底部 | 保存 / 保存并下一条，及「怎么用（3 分钟看完）」内嵌说明面板 |

**快捷键**：`空格` 播放暂停 · `M` 标记 · `←/→` ±1 s · `,`/`.` ±1 帧 · `D` 丢弃当前段 · `S` 保存 · `N` 保存并下一条 · `Delete` 删最近的标记。

**切点语义**：k 个标记 → k+1 段。

**切点精度**：默认**帧级精确**（12.3 s ↔ 第 369 帧），仍然**无损**——每路相机从自己前一个关键帧开始 `-c copy`，把多出来的前导帧记在 `videos/<cam>/from_timestamp` 里，播放时正好从目标帧开始，不解码不重编码。

`吸附关键帧` 勾选后标记会对齐到关键帧网格（省一点前导帧）。系统会按数据实际情况给默认值：G2 头部相机 GOP=15（0.5 s）时默认吸附；合并版数据 GOP≈8.3 s 时默认关闭——否则标记只能落在 8.3 s 的网格上，切不准。

---

### 预览预生成（切条不等待）

打开第 k 条时前端会自动请求 `POST /api/prefetch`，服务端用 3 个后台线程把 **k+1、k+2** 的三路预览先转好放进 `state/preview/`，等你点「下一条」时直接命中缓存。

实测（108 s、三相机 episode）：冷开 **5.6 s** → 预热后 **0.1 s**；短 episode 冷开 1 s → 预热后 **5 ms**。

- 只写预览缓存，不碰原始数据、不影响导出；
- 缓存键是 `(数据集, episode, 相机)`，源文件 mtime 变了会自动重建；
- 占盘约 30 MB/条（三路 720p），`state/preview/` 可随时整目录删掉，下次打开自动重建；
- 状态查询：`GET /api/prefetch` 返回 `queued / pending / done / failed / dropped`。

## 4. 导出（每个子任务一个数据集）

```bash
PY=/data/g2/software/miniconda3/envs/lerobot/bin/python
$PY -m split_review --db state/review.db export \
  --dataset /data/g2/lerobot_v3/<数据集> --name split_20261116 --out /data/g2/lerobot_v2.1
```

产物：

```
/data/g2/lerobot_v2.1/split_20261116/
├── task_01_拿起手机/        ← 独立 v3 数据集（tasks.parquet 只有 1 条 prompt）
├── task_02_移动到位/
├── task_03_放进盒子/
└── export_report.json      每段的源 episode/帧区间/帧数/时长/输出位置
```

导出时逐段强校验：**视频帧数 == parquet 行数**，不一致直接报错并指出是哪一段（默认不产出坏数据）。

---

## 5. 命令行

```bash
$PY -m split_review scan      --path <父目录>            # 把目录下每个 v3 包登记成一个集合
$PY -m split_review info      --dataset <path>          # 数据集摘要
$PY -m split_review audit     --dataset <path>          # 首次接触新数据时做契约体检
$PY -m split_review keyframes --dataset <path> --episode 0
$PY -m split_review mark      --dataset <path> --episode 0 --marks 1.07,2.83
$PY -m split_review export    --dataset <path> --name split_x --out <dir> [--dry-run]
$PY -m split_review export    --collection col-xxxx --name split_x --out <dir>
$PY -m split_review verify    --dataset <输出的 task 目录>   # 官方 loader 加载校验
$PY -m split_review serve     --host 127.0.0.1 --port 18770
```

`audit` 是真实数据到手后的第一件事：它会列出非标准列、每路相机的 codec/尺寸/GOP、行数与视频帧数不一致的 episode。

---

## 6. 数据契约

- 只支持 `codebase_version = v3.x`；路径模板来自 `meta/info.json`（`data_path` / `video_path`），不硬编码相机数与维度。
- 兼容厂商扩展列（`observation.state_valid`、`*_repaired`、`source_h5_row`、`source_video_frame_index` 等），切分后原样保留。
- 一条数据集可以是多 episode（合并版），也可以是"目录下多个单 episode 数据集"（用 `scan` 登记成集合，导出时归并到同一批子任务数据集）。
- 特征数量/名称/shape 全部读元数据：3 相机或 1 相机、24 维或其它维度都能跑。
- 源数据集**只读**；审查状态在 `state/review.db`（SQLite），导出写新目录。
- 输出保持源数据集的 parquet schema（含厂商自定义列，如 `source_timestamp_ns`、`*_valid`），只重算 `index / episode_index / frame_index / task_index / timestamp` 与 `stats/*`。
- 审查用的预览视频转成 H.264（`state/preview/`），**导出用的是原码流 `-c copy`**，两者互不影响。

---

## 7. 验证

```bash
cd /home/zz/g2/split_review
PYTHONPATH=. $PY -m pytest tests -q     # 3 passed
```

契约测试链路：**官方 writer 造数据 → 切分 → 官方 loader 加载**，断言 episode 数、长度、task 文本、首帧可解码。

已跑的实证（GOP=15 的 HEVC fixture，5 s / 150 帧）：

| 标记 | 吸附后 | 段 | 帧数 | 导出结果 |
| --- | --- | --- | --- | --- |
| 1.07 / 2.83 | 1.0 / 3.0 | 3 段 | 30 / 60 / 60 | 3 个 task 数据集，`verify` 全部 ok |
| 1.02 / 2.5 / 3.98 | 1.0 / 2.5 / 4.0 | 4 段 | 30 / 45 / 45 / 30 | 同上 |

并逐段解码首帧核对内容：第 2 段首帧 = 源第 30 帧、第 3 段 = 源第 90 帧（fixture 把帧号编码进画面）。

**真机数据实证**（`/home/zz/wang/data_raw_v3.0`，ep0 = 3260 帧 / 108.7 s，3 相机 640×400 + 2×1280×1056）：

| 标记 | 段 | 帧数 | `from_timestamp` | 校验 |
| --- | --- | --- | --- | --- |
| 12.30 / 30.70（不吸附） | 3 段 | 369 / 552 / 2339 | 0 / 3.967 / 5.700 | 4 个 task 全部 `ok=true` |
| 20.00 / 45.00（吸附） | 3 段 | 599 / 750 / 1366 | 0 / 3.300 / 3.300 | 同上 |

内容核对：段 2 的 `from_timestamp=3.967` 处解出的帧与源第 369 帧 **逐像素相同（diff=0.00）**，腕部大 GOP 相机同样为 0.00——证明"前一关键帧 + 偏移"的无损切分在真实数据上精确成立。

---

## 8. 目录

```
split_review/
├── run.sh                     启动脚本
├── split_review/
│   ├── dataset.py             v3 读取（元数据/行/视频引用）
│   ├── video.py               关键帧探测、无损切分、H.264 预览
│   ├── stats.py               逐 episode 与全局统计（复用 lerobot 实现）
│   ├── writer.py              输出 v3 数据集
│   ├── split.py               切点吸附 / 分段 / by-task 导出
│   ├── reviewdb.py            SQLite 审查状态
│   ├── server.py              FastAPI（含 Range 视频流）
│   ├── verify.py              官方 loader 校验
│   └── audit.py               新数据契约体检
├── static/                    审查界面（原生 JS，无构建）
├── fixtures/                  合成 fixture（官方 writer ← "标准答案"）
├── tests/                     契约测试
├── state/review.db            审查状态（每日备份建议）
└── out/                       演示导出结果
```

---

## 9. 数据源说明

| 路径 | 内容 | 本工具 |
| --- | --- | --- |
| `/home/zz/wang/data_raw_v3.0/` | **合并版 v3 数据集**（57 episodes / 133 953 帧 / 30 fps）+ 57 个单 episode 子包 + `quality_review/` | 作为主数据集登记，57 条直接审查 |
| `/home/zz/wang/data_mcap_test/` | 108 条**原始 MCAP** 录制（`raw.mcap` + `quality/plan.jsonl`） | 不读取；v3 就是由它转出来的，目录名一一对应 |

如果需要在审查时同时看 MCAP 侧的质检结论（`report.json` / `plan.jsonl`），可以再加一个只读面板——目前未实现。

## 10. 已知边界

- 预览转码在首次请求时同步执行（约 1–3 s/路），之后走缓存。
- 关键帧吸附精度 = 源流 GOP；若某相机 GOP 与其它相机不同，取三方关键帧交集，交集为空时退化为并集（会在报告里提示）。
- 真实 v3 数据到手后先跑 `audit`：若出现非标准命名/额外列/非 30 fps，按审计结果补映射，不动主干。
- 尚未做：systemd 常驻服务、多用户并发锁、导出任务取消。
