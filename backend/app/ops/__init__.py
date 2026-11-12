"""ops - 平台运维命令（人工 / CI 调用，不在运行链路内）。

本包的模块都带 ``__main__`` 入口，用 ``python -m app.ops.<name>`` 执行：

    setup_db.py   建库 + 只读/受限角色 + 列权限自检（幂等，需可写账号）
    seed.py       装载 xlsx 业务表并跑真值断言（ETL 引擎在 app/analytics/seed.py）
    reconcile.py  PG 登记表与 Qdrant 向量库对账
    backup.py     PG 两库 dump + Qdrant 快照 + 清理旧备份
    recovery.py   本地隔离恢复演练（绝不覆盖源库/源集合）
    sql/          建库初始化 SQL（只读角色与授权）

边界：**运行态（api / worker / agent / analytics）不得 import 本包**。
`tests/test_ops_not_imported_by_app.py` 会守住这条规则 —— 运维模块一被运行链路
引到，进程启动就会带上 pg_dump / qdrant 快照这些只该在命令行发生的东西。
"""
