# 地方台网历史地震目录归档

按 UTC 日期从地方台网的历史目录接口采集地震事件，逐日跟踪查全进度，支持为无法通过接口查全的日期补档全日 CSV，并在全部日期查全后导出完整目录。

## 功能

- 选择最多 **31 个连续 UTC 日** 建立归档任务。
- 接口按“日期段”返回事件列表，每次最多 **20 条**：
  - 返回不足 20 条 → 该段已查尽，段内**每一天**都记为“已查全”（零条的日子也算）。
  - 返回满 20 条 → 段内可能还有事件，把日期段对半拆开继续查询。
  - 拆到单日仍满 20 条 → 该日标记为“待补档”，已取到的首页记录先入库。
- 页面按天列出进度：待查询 / 已查全 / 待补档 / 待重试，并显示条数与最近更新时间。
- 请求失败的日子记为“待重试”。点击“采集 / 重试”只会重查“待查询”和“待重试”的日期，**不会**重复请求已查全、待补档的日期。
- 待补档日可上传全日 CSV：
  - 文件必须只包含该天（UTC）的事件；
  - 事件编号在文件内唯一，且不能与其他日期已归档的编号冲突；
  - 接口已采到的记录必须**逐条原样**出现在 CSV 中（编号、时刻、震级都一致）；
  - 任一校验失败都不会改动已有进度（补档写入在单个事务中，失败自动回滚）。
- 只有当任务内**每一天**都是“已查全”或补档成功后才允许下载完整目录（否则下载入口返回任务页并提示）。

## 目录结构

```
archive_app/
  catalog.py      # 事件模型、模拟数据源、SQLite 存储
  collector.py    # 按日期段拆分/收敛的采集逻辑
  csv_import.py   # 补档 CSV 解析与校验
  web.py          # Flask 路由与页面
  templates/      # 页面模板
  static/style.css
scripts/generate_catalog.py   # 生成演示用模拟目录
seed/catalog.json             # 演示目录（2024-03-01 起 61 天）
seed/backfill_2024-03-12.csv  # 演示用单日补档 CSV（27 条）
tests/test_archive.py         # 采集、校验与页面流程测试
Dockerfile, docker-compose.yml, docker-entrypoint.sh
```

## 用 docker compose 启动

```bash
docker compose up --build
# 打开 http://localhost:8000
```

首次启动会把 `seed/catalog.json` 复制到数据卷中作为模拟接口的数据源，之后持久化在 `archive-data` 卷里（`/data/archive.db` 为归档库）。

演示流程：

1. 新建任务，例如 `2024-03-01` ~ `2024-03-31`，点击“采集 / 重试”。
   - 大部分日期会被判为“已查全”（含零事件的日子）；
   - `2024-03-12` 有 27 条，单日满页，显示为“待补档”；
   - 想演示“待重试”，可设置环境变量 `MOCK_FAIL_DATES=2024-03-18` 后启动，采集时该日会失败，清空该变量重启后再次点击“采集 / 重试”即可。
2. 在 `2024-03-12` 一行上传 `seed/backfill_2024-03-12.csv`。
3. 全部查全后，“下载完整目录”按钮可用，导出 CSV（`event_id,utc_time,magnitude`）。

### 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE` | `/data/archive.db` | SQLite 数据库路径 |
| `CATALOG_FILE` | `/data/catalog.json` | 模拟目录数据文件 |
| `MOCK_FAIL_DATES` | 空 | 逗号分隔的日期，模拟接口在这些日期所在区间返回失败 |
| `SECRET_KEY` | `dev-only-change-me` | Flask 会话密钥 |

## 本地运行（不使用 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
python scripts/generate_catalog.py seed/catalog.json   # 如需重新生成演示数据
DATABASE=./data/archive.db CATALOG_FILE=$PWD/seed/catalog.json flask --app wsgi run --port 8000
```

运行测试：

```bash
python -m pytest -q
```

## 采集规则要点

采集器把待查日期按连续区间分组，每组最长 31 天，然后从整段开始递归：

```
fetch(段) -> 不足 20 条：段内每天记为已查全（写入返回的事件）
           -> 满 20 条且段 > 1 天：对半拆分，分别 fetch
           -> 满 20 条且段 = 1 天：该日记为待补档（保留首页 20 条）
```

对接口返回的越界事件（不在请求区间内）或超过 20 条的响应，按请求失败处理，不会把错误数据写入其他日期。

## 接入真实接口

`archive_app/catalog.py` 中的 `CatalogSource` 是唯一需要替换的抽象：实现 `fetch_page(start, end) -> list[Event]`（最多返回 20 条，失败抛 `SourceError`），并在 `source_from_env()` 中返回它即可。采集、进度、补档与导出逻辑与数据源无关。
