# 工业传感器时序采集与降采样平台

面向工业传感器高频时序数据的测点接入、边缘缓冲上送、单位标定、时间对齐、降采样归并与分层归档的采集平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              19个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 测点台账 | `sensor_point` | 测点档案 | 测点编号、所属产线、传感器型号 |
| 边缘缓冲 | `edge_buffer` | 缓冲分片 | 分片编号、测点编号、起始时刻 |
| 批量上送 | `upload_batch` | 上送批次 | 批次编号、分片范围、测点数量 |
| 单位标定 | `unit_calibrate` | 标定记录 | 标定编号、测点编号、原始单位 |
| 时间对齐 | `time_align` | 对齐规则 | 规则编号、对齐粒度、时区基准 |
| 断点填补 | `gap_fill` | 填补区间 | 区间编号、测点编号、缺失起点 |
| 尖峰剔除 | `spike_filter` | 异常采样 | 异常编号、测点编号、采样时刻 |
| 降采样 | `downsample` | 降采样任务 | 任务编号、源序列、源采样频率 |
| 窗口归并 | `rollup_window` | 归并窗口 | 窗口编号、测点编号、窗口类型 |
| 序列索引 | `series_index` | 索引分区 | 索引编号、测点编号、时间分区 |
| 冷热分层 | `tier_storage` | 分层策略 | 策略编号、时间边界、热区范围 |
| 采集心跳 | `device_health` | 心跳记录 | 心跳编号、采集终端、上报时刻 |
| 接入水位 | `ingest_watermark` | 水位标记 | 水位编号、测点编号、事件时间水位 |
| 区间重采 | `range_replay` | 重采任务 | 重采编号、测点编号、起始时刻 |
| 采集血缘 | `series_lineage` | 血缘链路 | 链路编号、测点编号、上送批次 |
| 序列导出 | `series_export` | 导出任务 | 导出编号、测点范围、时间范围 |
| 时序质量 | `series_quality` | 质量报告 | 报告编号、测点编号、统计窗口 |
| 序列快照 | `series_snapshot` | 版本快照 | 快照编号、口径说明、测点范围 |
| 标定复验 | `reverify_session` | 复验单 | 复验编号、标定引用、采用范围、待续查时段 |

## 换算出处与标定复验

单位标定每次确认（或手动「登记换算出处」）都会把**实际采用的样本**写入
`data/sample_index/<测点>_conversion_index.jsonl`：一条索引对应
「测点 + 标定编号 + 标定版本」，样本自带原始单位（如 mV）与换算后的计量单位
（如 V），并记录换算系数、零点偏移、来源与生效区间。同一测点可并存多个版本，
后一次标定不会覆盖前一次。

- 标定生效区间是**闭区间**：边界上的采样点可能同时属于两次标定。复验在边界处
  按 `生效起点 → 标定版本 → 编号` 择优，并把全部候选写进运行快照。
- 血缘链路的「标定版本」保存原引用（`标定编号@版本`），新增标定后旧链路仍按
  原引用可核对。

复验单的状态流转：

```text
草稿 → 复验中 → 待续查 → … → 已复验
```

- 复验时段用**半开区间** `[start, end)`；一轮只核前半段时，剩余时段记为待续查。
- 每次提交都会在 `data/reverify_runs/` 追加一条运行快照，记录本次范围、实际读到的
  版本、每条样本的单位与读数、边界候选和摘要。切换标定后仍能回看旧版。
- 「按提交记录重开」按某次快照的引用与范围另起复验单，边界处钉住旧版，不会滑到
  后来获准的更正版；「单点复验」围绕单个样本时刻另起复验单。
- 已复验的单据不能再续查；待续查未清空时不能标记完成。

### 相关接口

```text
GET  /api/series_lineage/locate?point=&unit=&start=&end=&rule=
GET  /api/reverify_session/{id}              复验单详情（含提交记录）
GET  /api/reverify_session/{id}/runs         提交记录列表
GET  /api/reverify_session/{id}/runs/{run}   单次运行快照

POST /api/series_lineage/{id}/actions        {"action":"定位样本"|"发起复验", "values":{...}}
POST /api/reverify_session/{id}/actions      {"action":"提交复验"|"续查"|"标记完成"|
                                                       "按提交记录重开"|"单点复验", "values":{...}}
POST /api/unit_calibrate/{id}/actions        {"action":"登记换算出处"}
```

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
