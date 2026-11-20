"""ADR-0050 · ML Backend request routing domain.

在 ``ml_backend_registry`` (物理实例, ADR-0044) 与服务池 (逻辑能力, ADR-0050) 之上
实现「项目请求一个逻辑能力 → 平台原子选择一个物理实例」的路由层。

模块边界 (ADR-0050 §8):
- ``MLBackendRouter`` 返回 selected ``MLBackendRegistry`` + route lease, 不发 HTTP 请求。
- 既有 ``MLBackendClient`` 仍只负责一个实例的 transport / auth / GPU dispatch。
- router 可依赖 registry model、routing ledger、capability; 不得导入 API router 或 worker。
- GPU arbitration 不导入 ml_routing; route selection 完成后单向调用实例 client。

Rollout (ADR-0050 §14 / D17): ``off`` / ``observe`` / ``enforce`` 是部署级单一开关,
``ML_BACKEND_ROUTER_MODE``。off / observe 保持现有业务路径 (legacy instance dispatch);
enforce 对所有 pool 在 Redis 不可用时 fail-closed。
"""
