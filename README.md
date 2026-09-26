# 专利与技术秘密档案管理服务

这是一个面向研发机构、法务部门和保密办公室的模块化后端，集中管理专利交底资料、技术秘密载体、移交批次、受控副本签发、查阅借阅、对外披露、归还、合规处置、载体盘点、版本与载体来源、密级库位、泄密事件、登录权限、审计以及可恢复后台任务。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 已有能力

- 身份与权限：支持引导管理员、登录、会话、用户、角色和细粒度权限。
- 批次与二维码：移交批次保存项目、数量和稳定二维码载荷。
- 档案登记：登记专利交底、工艺文档、源代码介质等资产，保存密级库位和生命周期状态。
- 受控副本签发：一次事务内扣减来源载体、创建副本、记录损耗和版本来源事件。
- 查阅借阅归还：保存查阅用途、到期时间、部分归还和最终归还状态。
- 对外披露登记：使用幂等键登记合作方、披露范围和载体消耗，防止重复请求二次扣减。
- 位置脱敏：普通权限只能看到受限库位的替代码，授权人员可查看精确位置。
- 双人审批：合规处置、敏感库位解密等高风险操作要求申请人与审批人分离，并累计不同审批人的决定。
- 泄密事件追踪：事件可以关联档案或移交批次，保存严重度、调查状态和处置结果。
- 审计与任务：关键身份及业务操作留痕，后台任务支持去重、领取与完成。
- 专利期限服务：按辖区（CN/US/EP）规则从申请日、优先权日、公开日、授权日生成优先权、国家阶段、公布与逐年年费节点；周末/假日顺延；区分待确认、已处理、逾期三种状态；支持人工确认、延期、缴费凭证登记与显式重开；提醒调度可恢复、幂等、不重复，逾期判定按辖区时区的本地历法日，跨时区不会把同一天误判为逾期；支持按任意时间点重算并追溯规则条款来源。

## 专利期限服务

- 规则引擎位于 `app/deadlines/rules.py`，规则集带版本号（`RULES_VERSION`），节点留存生成时的规则代码、条款来源与逐步解释。
- 日期全程使用辖区本地历法日：UTC 时刻仅在判定逾期时换算到辖区时区取日，期满当天不会因观测者时区而提前判逾期。
- 提醒通过既有 `background_jobs` 机制调度：去重键含节点、提前天数与排程代次，并有 `deadline_reminder_dispatches` 唯一约束兜底；重复登记、重复重算、重复恢复都不会产生第二条提醒。
- 已处理（缴费）节点在任何重算中保持关闭，旧提醒任务触发时只空跑；只有显式“重开”才会恢复跟踪。
- 服务重启时启动钩子自动调用恢复；也可由 cron 执行：

```bash
python -m app.cli deadlines-recover   # 补齐未完成节点的提醒排程（幂等）
python -m app.cli deadlines-dispatch  # 领取并发送到期提醒（可反复调用）
python -m app.cli deadlines-sweep     # 按辖区本地日把过期节点标记为逾期
```

主要接口：`POST /api/deadlines/applications`、`PATCH .../anchors`、`POST .../recompute`、
`GET .../preview?as_of=YYYY-MM-DD`、`GET /api/deadlines/nodes`、
`POST /api/deadlines/nodes/{id}/confirm|extend|payment|reopen`、
`GET /api/deadlines/rules/{rule_code}`。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/archives.db`，可用 `ARCHIVE_DATABASE_PATH` 指定其他路径。

## 初始化与完整性检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动 API

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 测试

```bash
python -m pytest
```

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```
