# 智慧校园大数据管理演示系统

这是一个面向《大数据管理》课程大作业的演示系统，统一实现了以下三个子系统：

1. 智慧图书馆子系统
2. 智慧教务系统
3. 实践教学综合管理平台

系统采用异构数据库的课程设计思路：

- `SQLite` 模拟 `MySQL`，承载事务型和结构化业务数据
- 优先连接本机真实 `Redis`（`127.0.0.1:6379`，密码 `123456`），承载缓存、排行榜、锁、配额和看板摘要
- 若真实 Redis 未启动，则自动回退到 `redis_state.json` 模拟存储，保证系统仍可演示
- 优先连接本机真实 `MongoDB`（`127.0.0.1:27017/smart_campus`），承载日志、周报、预警画像、事件流、地理空间数据与 GridFS 资产
- 若真实 MongoDB 未启动，则自动回退到 `data/mongo/*.json` 模拟文档存储
- 优先连接本机真实 `Neo4j`（`bolt://127.0.0.1:7687`），配置文件位于 `data/neo4j_runtime.json`
- 内置图分析实验室，可导出 `Cypher` 到 Neo4j，演示跨系统关系建模与多跳查询

## 扩展功能

- 图书馆新增多粒度排行榜：`日榜 / 周榜 / 月榜 / 总榜 / 分类日榜`
- 教务新增课程热选榜与容量预警速览
- 实践平台新增任务推进榜与实验室利用榜
- 数据中心新增排行榜键策略看板，便于说明 Redis Key 的过期策略与更新时机
- MongoDB 新增 `JSON Schema Validator / Capped Collection / TTL / 2dsphere / GridFS / Aggregate Pipeline`
- 新增“数据治理”页面，展示主数据目录、质量评分、规则检查、Mongo 质量快照与数据血缘
- 新增“图谱创新”页面，支持课程推荐、图书推荐、多跳路径、中心性指标和跨系统关联分析
- 页面整体做了统一化展示与布局优化

## 运行方式

在当前目录执行：

```powershell
python -m pip install -r requirements.txt
python app.py
```

启动成功后，在浏览器访问：

```text
http://127.0.0.1:5050
```

## 一键启动

Windows 环境可直接双击运行：

`launch_demo.bat`

若要单独启动本机 Neo4j 演示库，可运行：

`start_neo4j_demo.bat`

如果只想先验证 Redis，可双击运行：

`..\tools\redis_portable\start_redis_123456.bat`

然后执行：

`..\tools\redis_portable\python_redis_test.py`

## 页面说明

1. 首页：查看总体架构和三类数据库的职责分工。
2. 智慧图书馆：查看借阅、归还、座位预约和热门图书排行。
3. 智慧教务：查看选课、退课、成绩、学业预警和课程热选榜。
4. 实践教学平台：查看实验室预约、签到、周报提交、任务推进榜和实验室利用榜。
5. 数据中心：查看 Redis 键快照、Mongo 聚合、GridFS 和地理空间能力。
6. 数据治理：查看主数据业务键、质量评分、治理规则与数据血缘。
7. 图谱创新：查看课程推荐、多跳路径、中心性与跨系统关联分析。

## 数据重置

页面右上角提供“重置演示数据”按钮，可将系统恢复到初始状态。

## 静态导出与截图

如果需要同步更新答辩截图和静态演示页面，可在服务运行后执行：

```powershell
python capture_screenshots.py
python export_static.py
```

执行完成后：

- 最新页面截图会更新到 `screenshots/*_latest.png`
- 静态答辩页面会更新到 `dist/`

## Render 免费部署

如果希望老师通过公网链接直接访问动态演示版，可把当前目录作为单独仓库上传到 GitHub，然后在 Render 中新建 `Web Service`：

1. 选择本项目仓库。
2. Runtime 选择 `Python`。
3. Build Command 使用 `pip install -r requirements.txt`。
4. Start Command 使用 `gunicorn app:app --bind 0.0.0.0:$PORT`。
5. 实例类型选择 `Free`。

项目根目录已经提供：

- `render.yaml`
- `.python-version`

因此也可以直接按 Blueprint 方式导入。

免费版说明：

- 公网访问地址通常为 `https://<service-name>.onrender.com`
- 空闲一段时间后实例会休眠，首次访问可能需要等待约 `30-60` 秒冷启动
- 若未额外配置云 Redis / MongoDB / Neo4j，Render 免费版会自动使用项目内置 fallback，不影响老师访问和页面操作
- 免费版适合答辩演示，但不适合作为长期稳定生产环境

如果生成最终报告时要把公网访问地址写到报告开头，可执行：

```powershell
python generate_assignment_report.py --output-dir "C:\Users\css\Desktop\大数据管理大作业" --student-name "你的姓名" --student-no "你的学号" --class-name "你的班级" --public-url "https://你的服务地址.onrender.com"
```

## 文件说明

- `app.py`：Flask 启动入口
- `demo_backend.py`：数据库初始化、业务逻辑，以及“真实 Redis / Mongo 优先、失败自动回退”的多库接入层
- `mongo_real_backend.py`：真实 MongoDB 能力封装，包含校验器、TTL、Geo、GridFS 与聚合分析
- `graph_backend.py`：图建模、图算法分析、Cypher 导出和可选 Neo4j 同步
- `capture_screenshots.py`：基于 Playwright 批量生成最新页面截图
- `render.yaml`：Render 免费部署配置
- `data/neo4j_runtime.json`：本机 Neo4j 演示实例连接参数
- `templates/`：页面模板
- `static/`：样式和交互脚本
- `data/`：运行后生成的结构化数据与文档数据
