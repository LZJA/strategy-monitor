# A 股策略信号监控平台

轻量策略监控台，用于服务器部署和域名访问。平台负责保存每日策略扫描结果、用户注册登录、账户快照录入、持仓跟踪和策略信号复盘。

## 范围

第一版包含：

- 用户注册、登录、退出
- 公共策略信号查询
- 管理员导入扫描 CSV
- 用户账户快照录入
- 当前持仓浮盈亏计算
- 持仓与近期策略信号提示
- 扫描临时文件入库成功后默认清理

第一版不包含：

- 回测调参
- 自动模拟盘交易
- 逐笔交易流水
- 飞书推送定时任务管理
- 券商交易接口

## 本地启动

后端：

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

前端：

```bash
cd frontend
npm install
npm run dev
```

访问：

```text
http://localhost:5173
```

首次启动时如果数据库没有用户，前端会进入注册页面，第一个注册用户自动成为 `admin`。

## 配置

复制环境变量：

```bash
cp .env.example .env
```

关键配置：

```env
REGISTRATION_ENABLED=0
KEEP_SCAN_ARTIFACTS=0
SCANNER_PROJECT_PATH=/Users/lizijian/项目/a-share-pattern-scan-tool
SCANNER_BOARD=main_board
```

- `REGISTRATION_ENABLED=0`：已有用户后关闭开放注册。
- `KEEP_SCAN_ARTIFACTS=0`：CSV 入库成功后删除临时文件。
- `SCANNER_PROJECT_PATH`：手动扫描按钮调用的形态扫描工具目录。
- `SCANNER_BOARD`：手动扫描范围，常用值为 `main_board` 或 `all`。

## 数据原则

- 日线缓存放在 `data/kline_cache/`
- 扫描临时文件放在 `data/tmp/`
- 扫描成功入库后，默认删除 CSV/JSON 临时文件
- 数据库保存用户、信号、账户快照、持仓状态
- 回测产物不进入平台

## API 概览

认证：

```text
GET  /api/auth/bootstrap
POST /api/auth/register
POST /api/auth/login
POST /api/auth/logout
GET  /api/auth/me
```

信号：

```text
GET /api/signals/today
GET /api/signals
GET /api/signals/{id}/chart
GET /api/signals/by-symbol/{symbol}
```

账户：

```text
GET    /api/account/current
GET    /api/account/snapshots
POST   /api/account/snapshots
GET    /api/account/snapshots/{id}
DELETE /api/account/snapshots/{id}
GET    /api/account/changes
```

管理员：

```text
POST /api/admin/scan/today
POST /api/admin/scan/import-csv
```

## CSV 导入

管理员登录后可在页面点击“扫描今日信号”，后端会优先调用 `SCANNER_PROJECT_PATH` 下的 `pattern_scan_tool.py`，扫描完成后导入当天 `*_matched.csv`；如果没有配置扫描工具，则读取 `data/scan_results/` 下最新的 CSV。重复点击会刷新当天信号，避免重复累加。

命中标的支持点击查看 K 线结构图。图表数据来自 `SCANNER_PROJECT_PATH/data/kline_cache/{代码}.csv`，并从扫描结果中的 `A点`、`B点`、`C点`、`D点` 字段解析形态标记。

也可以通过接口导入扫描 CSV。当前后端会识别常见中文列名：

- `代码`
- `名称`
- `周期`
- `收盘价`
- `最高价`
- `突破价`
- `止损价`
- `止盈价`

后续可以继续接入 `a-share-pattern-scan-tool` 的扫描脚本，改成扫描完成后直接入库并清理产物。
