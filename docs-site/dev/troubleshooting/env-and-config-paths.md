---
audience: [dev, ops]
type: reference
status: stable
last_reviewed: 2026-10-19
---

# 环境变量与 config 路径

## 症状

容器启动时崩溃：

```
IndexError: list index out of range
  File "/app/app/config.py", line 23, in <module>
    BASE_DIR = Path(__file__).resolve().parents[3]
```

或：本地跑通 `.env` 一切正常，进了 docker 就报「无法定位 .env」。

## 根因

早期 `apps/api/app/config.py` 曾使用 `parents[3]` 假设宿主机目录布局：

```
ai-annotation-platform/        # parents[3]
└── apps/
    └── api/
        └── app/
            └── config.py
```

但容器内 `Dockerfile` 只 `COPY apps/api/app /app/app`，路径变成：

```
/app/app/config.py
└── parents[0] = /app/app
└── parents[1] = /app
└── parents[2] = /
└── parents[3] = ❌ IndexError
```

宿主机假设直接打到容器层级。

## 修复

容器内 env vars 由 `docker-compose` 注入，根本不需要再去硬盘上找 `.env`。在 config.py 加守卫：

```python
def _find_repo_root() -> Path | None:
    p = Path(__file__).resolve()
    for ancestor in p.parents:
        if (ancestor / "pyproject.toml").exists() or (ancestor / ".env.example").exists():
            return ancestor
    return None  # 容器场景：找不到就 None，不再 IndexError

REPO_ROOT = _find_repo_root()
ENV_FILE = REPO_ROOT / ".env" if REPO_ROOT else None
```

`pydantic-settings` 接受 `env_file=None`，会跳过文件加载只读 `os.environ`，与 docker-compose 行为一致。

## 教训

- 任何依赖**目录深度**的索引（`parents[N]`、`split('/')[N]`）都是定时炸弹。
- 改用**特征文件锚定**（`pyproject.toml`、`.git/`、`.env.example`）找根。
- 容器与宿主机的目录布局**一定不同**——配置代码必须两套都跑通。

## 相关

- commit: `0a99cc6` 端到端跑通修复段
- 代码：`apps/api/app/config.py`
