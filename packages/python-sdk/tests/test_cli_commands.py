"""aap CLI 命令测试: CliRunner + respx mock HTTP。

- 凭据通过 CliRunner env 注入 (AAP_BASE_URL / AAP_API_KEY), 不读真实 config 文件
  (autouse fixture 把 config_path 重定向到 tmp_path)。
- snapshot 风格: 固定 COLUMNS=120 + 禁色, 对 rich 输出断言关键行/列。
- --json 契约: stdout 可被 json.loads 解析, 断言 schema 关键字段。
"""

import json
import re
from uuid import uuid4

import httpx
import pytest
from typer.testing import CliRunner

from ai_annotation.cli.main import app

from .conftest import API, BASE

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(text: str) -> str:
    """去 ANSI 转义 + 归一空白, 让 rich 输出断言稳定 (typer CliRunner 下 rich 仍着色)。"""
    return re.sub(r"\s+", " ", _ANSI.sub("", text))


# 固定宽度 + 禁色, 让 rich 输出稳定可断言
ENV = {
    "AAP_BASE_URL": BASE,
    "AAP_API_KEY": "ak_test",
    "COLUMNS": "120",
    "NO_COLOR": "1",
    "TERM": "dumb",
}
NO_AUTH_ENV = {"COLUMNS": "120", "NO_COLOR": "1", "TERM": "dumb"}

PROJECT = {
    "id": str(uuid4()),
    "display_id": "P-1",
    "name": "demo",
    "type_label": "目标检测",
    "type_key": "object_detection",
    "data_type": "image",
    "status": "active",
    "created_at": "2026-06-11T00:00:00Z",
    "total_tasks": 10,
    "completed_tasks": 3,
}

DS_ID = str(uuid4())
DATASET = {
    "id": DS_ID,
    "display_id": "D-1",
    "name": "ds",
    "data_type": "image",
    "file_count": 0,
    "total_size": 0,
}

JOB_ID = str(uuid4())


def _job(status: str, **extra) -> dict:
    return {
        "id": JOB_ID,
        "kind": "export",
        "status": status,
        "progress_pct": 0,
        "payload": {},
        "result": {},
        "error_message": None,
        **extra,
    }


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    # 隔离宿主机环境变量与真实 config.toml; 轮询不真实 sleep
    monkeypatch.delenv("AAP_BASE_URL", raising=False)
    monkeypatch.delenv("AAP_API_KEY", raising=False)
    monkeypatch.setattr(
        "ai_annotation.config.config_path", lambda: tmp_path / "config.toml"
    )
    monkeypatch.setattr("ai_annotation.client.time.sleep", lambda s: None)


# ---------- projects ----------


def test_projects_list_table(respx_mock):
    respx_mock.get(f"{API}/projects").mock(
        return_value=httpx.Response(200, json=[PROJECT])
    )
    result = runner.invoke(app, ["projects", "list"], env=ENV)
    assert result.exit_code == 0
    plain = _plain(result.output)
    for cell in [
        "名称",
        "类型",
        "状态",
        "任务进度",
        "P-1",
        "demo",
        "image",
        "active",
        "3/10",
    ]:
        assert cell in plain


def test_projects_list_json(respx_mock):
    respx_mock.get(f"{API}/projects").mock(
        return_value=httpx.Response(200, json=[PROJECT])
    )
    result = runner.invoke(app, ["projects", "list", "--json"], env=ENV)
    assert result.exit_code == 0
    # 裸 JSON, 无 rich 装饰
    data = json.loads(result.stdout)
    assert data[0]["display_id"] == "P-1"
    assert data[0]["status"] == "active"
    assert data[0]["total_tasks"] == 10


def test_projects_create(respx_mock):
    route = respx_mock.post(f"{API}/projects").mock(
        return_value=httpx.Response(200, json=PROJECT)
    )
    result = runner.invoke(
        app, ["projects", "create", "--name", "demo", "--type", "image"], env=ENV
    )
    assert result.exit_code == 0
    body = json.loads(route.calls.last.request.content)
    assert body["name"] == "demo"
    assert body["data_type"] == "image"
    assert PROJECT["id"] in _plain(result.output)  # 成功输出新项目 id


def test_projects_create_json(respx_mock):
    respx_mock.post(f"{API}/projects").mock(
        return_value=httpx.Response(200, json=PROJECT)
    )
    result = runner.invoke(
        app,
        ["projects", "create", "--name", "demo", "--type", "image", "--json"],
        env=ENV,
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["id"] == PROJECT["id"]


# ---------- 错误路径 ----------


def test_401_hints_login(respx_mock):
    respx_mock.get(f"{API}/projects").mock(
        return_value=httpx.Response(401, json={"detail": "invalid api key"})
    )
    result = runner.invoke(app, ["projects", "list"], env=ENV)
    assert result.exit_code == 1
    assert "aap login" in _plain(result.stderr)
    assert "Traceback" not in result.stderr


def test_401_json_mode_plain_error(respx_mock):
    respx_mock.get(f"{API}/projects").mock(
        return_value=httpx.Response(401, json={"detail": "invalid api key"})
    )
    result = runner.invoke(app, ["projects", "list", "--json"], env=ENV)
    assert result.exit_code == 1
    assert result.stdout.strip() == ""  # stdout 无半截 JSON
    assert "aap login" in result.stderr


def test_unconfigured_exits_with_hint():
    result = runner.invoke(app, ["projects", "list"], env=NO_AUTH_ENV)
    assert result.exit_code == 1
    plain = _plain(result.stderr)
    assert "aap login" in plain
    assert "AAP_BASE_URL" in plain


# ---------- login ----------


def test_login_saves_config_0600(respx_mock, tmp_path):
    respx_mock.get(f"{API}/projects").mock(return_value=httpx.Response(200, json=[]))
    result = runner.invoke(
        app, ["login", "--url", BASE, "--api-key", "ak_new"], env=NO_AUTH_ENV
    )
    assert result.exit_code == 0
    cfg = tmp_path / "config.toml"
    assert cfg.is_file()
    assert 'api_key = "ak_new"' in cfg.read_text()
    assert (cfg.stat().st_mode & 0o777) == 0o600
    plain = _plain(result.output)
    # 去掉所有空白再比对: 长 tmp 路径在窄终端会被 rich 折行, _plain 把换行归一成空格,
    # 会把 "config.toml" 拆成 "co nfig.toml", 故剥离空白还原后再断言
    nospace = plain.replace(" ", "")
    assert "config.toml" in nospace  # 提示配置文件路径
    assert "0600" in nospace  # 提示权限说明


def test_login_prompts_hidden_api_key(respx_mock, tmp_path):
    respx_mock.get(f"{API}/projects").mock(return_value=httpx.Response(200, json=[]))
    result = runner.invoke(
        app, ["login", "--url", BASE], input="ak_prompt\n", env=NO_AUTH_ENV
    )
    assert result.exit_code == 0
    assert "ak_prompt" not in result.output  # 隐藏输入不回显
    assert 'api_key = "ak_prompt"' in (tmp_path / "config.toml").read_text()


def test_login_prompts_url(respx_mock, tmp_path):
    respx_mock.get(f"{API}/projects").mock(return_value=httpx.Response(200, json=[]))
    # 省略 --url / --api-key, 依次交互输入平台地址与 key
    result = runner.invoke(
        app, ["login"], input=f"{BASE}\nak_prompt\n", env=NO_AUTH_ENV
    )
    assert result.exit_code == 0
    cfg = (tmp_path / "config.toml").read_text()
    assert f'base_url = "{BASE}"' in cfg
    assert 'api_key = "ak_prompt"' in cfg


def test_login_invalid_key_does_not_save(respx_mock, tmp_path):
    respx_mock.get(f"{API}/projects").mock(
        return_value=httpx.Response(401, json={"detail": "bad key"})
    )
    result = runner.invoke(
        app, ["login", "--url", BASE, "--api-key", "ak_bad"], env=NO_AUTH_ENV
    )
    assert result.exit_code == 1
    assert not (tmp_path / "config.toml").exists()


def test_login_json(respx_mock, tmp_path):
    respx_mock.get(f"{API}/projects").mock(return_value=httpx.Response(200, json=[]))
    result = runner.invoke(
        app, ["login", "--url", BASE, "--api-key", "ak_new", "--json"], env=NO_AUTH_ENV
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["base_url"] == BASE
    assert data["config_path"] == str(tmp_path / "config.toml")


# ---------- datasets ----------


def test_datasets_create(respx_mock):
    route = respx_mock.post(f"{API}/datasets").mock(
        return_value=httpx.Response(201, json=DATASET)
    )
    result = runner.invoke(app, ["datasets", "create", "--name", "ds"], env=ENV)
    assert result.exit_code == 0
    assert json.loads(route.calls.last.request.content)["data_type"] == "image"
    assert DS_ID in _plain(result.output)


def test_datasets_upload_dir(respx_mock, tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "a.jpg").write_bytes(b"a")
    (d / "b.jpg").write_bytes(b"b")
    item_ids = [str(uuid4()), str(uuid4())]
    init = respx_mock.post(f"{API}/datasets/{DS_ID}/items/upload-init").mock(
        side_effect=[
            httpx.Response(
                200, json={"item_id": iid, "upload_url": f"http://minio.local/b/{i}"}
            )
            for i, iid in enumerate(item_ids)
        ]
    )
    respx_mock.put(host="minio.local").mock(return_value=httpx.Response(200))
    for iid in item_ids:
        respx_mock.post(f"{API}/datasets/{DS_ID}/items/upload-complete/{iid}").mock(
            return_value=httpx.Response(200, json={"status": "ok", "item_id": iid})
        )
    result = runner.invoke(app, ["datasets", "upload", DS_ID, str(d)], env=ENV)
    assert result.exit_code == 0
    assert init.call_count == 2
    plain = _plain(result.output)
    assert "上传完成" in plain
    assert "2 个文件" in plain


def test_datasets_upload_single_file_json(respx_mock, tmp_path):
    f = tmp_path / "a.jpg"
    f.write_bytes(b"a")
    item_id = str(uuid4())
    respx_mock.post(f"{API}/datasets/{DS_ID}/items/upload-init").mock(
        return_value=httpx.Response(
            200, json={"item_id": item_id, "upload_url": "http://minio.local/b/a"}
        )
    )
    respx_mock.put(host="minio.local").mock(return_value=httpx.Response(200))
    respx_mock.post(f"{API}/datasets/{DS_ID}/items/upload-complete/{item_id}").mock(
        return_value=httpx.Response(200, json={"status": "ok", "item_id": item_id})
    )
    result = runner.invoke(
        app, ["datasets", "upload", DS_ID, str(f), "--json"], env=ENV
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data[0]["item_id"] == item_id
    assert data[0]["file_name"] == "a.jpg"


def test_datasets_upload_zip(respx_mock, tmp_path):
    z = tmp_path / "data.zip"
    z.write_bytes(b"PK\x03\x04zip")
    respx_mock.post(f"{API}/datasets/{DS_ID}/items/upload-zip").mock(
        return_value=httpx.Response(
            200,
            json={
                "added": 3,
                "deduped": 1,
                "skipped": 0,
                "errors": [],
                "total_in_zip": 4,
            },
        )
    )
    result = runner.invoke(app, ["datasets", "upload", DS_ID, str(z), "--zip"], env=ENV)
    assert result.exit_code == 0
    plain = _plain(result.output)
    assert "added=3" in plain
    assert "deduped=1" in plain


def test_datasets_link_waits_async_job(respx_mock):
    pid = str(uuid4())
    respx_mock.post(f"{API}/datasets/{DS_ID}/link").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "linking",
                "dataset_id": DS_ID,
                "project_id": pid,
                "async_job_id": JOB_ID,
            },
        )
    )
    poll = respx_mock.get(f"{API}/async-jobs/{JOB_ID}").mock(
        side_effect=[
            httpx.Response(
                200, json=_job("running", kind="dataset_link", progress_pct=50)
            ),
            httpx.Response(
                200, json=_job("completed", kind="dataset_link", progress_pct=100)
            ),
        ]
    )
    result = runner.invoke(app, ["datasets", "link", DS_ID, pid], env=ENV)
    assert result.exit_code == 0
    assert poll.call_count == 2  # 自动 jobs.wait 轮询到终态
    assert "completed" in _plain(result.output)


def test_datasets_link_sync(respx_mock):
    pid = str(uuid4())
    respx_mock.post(f"{API}/datasets/{DS_ID}/link").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "linked",
                "dataset_id": DS_ID,
                "project_id": pid,
                "created_tasks": 7,
            },
        )
    )
    result = runner.invoke(app, ["datasets", "link", DS_ID, pid, "--json"], env=ENV)
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["link"]["created_tasks"] == 7
    assert data["job"] is None


# ---------- predictions ----------


def test_predictions_import(respx_mock, tmp_path):
    pid = str(uuid4())
    f = tmp_path / "result.json"
    f.write_text("{}")
    route = respx_mock.post(f"{API}/projects/{pid}/predictions/import").mock(
        return_value=httpx.Response(
            200, json={"imported": 5, "skipped": 1, "errors": [], "dry_run": False}
        )
    )
    result = runner.invoke(
        app, ["predictions", "import", pid, str(f), "--format", "aap_json"], env=ENV
    )
    assert result.exit_code == 0
    params = route.calls.last.request.url.params
    assert params["format"] == "aap_json"
    assert params["dry_run"] == "false"
    assert "imported=5" in _plain(result.output)


def test_predictions_import_json_dry_run(respx_mock, tmp_path):
    pid = str(uuid4())
    f = tmp_path / "result.json"
    f.write_text("{}")
    route = respx_mock.post(f"{API}/projects/{pid}/predictions/import").mock(
        return_value=httpx.Response(
            200, json={"imported": 5, "skipped": 0, "errors": [], "dry_run": True}
        )
    )
    result = runner.invoke(
        app, ["predictions", "import", pid, str(f), "--dry-run", "--json"], env=ENV
    )
    assert result.exit_code == 0
    assert route.calls.last.request.url.params["dry_run"] == "true"
    data = json.loads(result.stdout)
    assert data["imported"] == 5
    assert data["dry_run"] is True


# ---------- jobs ----------


def test_jobs_wait_success(respx_mock):
    poll = respx_mock.get(f"{API}/async-jobs/{JOB_ID}").mock(
        side_effect=[
            httpx.Response(200, json=_job("pending")),
            httpx.Response(200, json=_job("running", progress_pct=50)),
            httpx.Response(200, json=_job("completed", progress_pct=100)),
        ]
    )
    result = runner.invoke(app, ["jobs", "wait", JOB_ID], env=ENV)
    assert result.exit_code == 0
    assert poll.call_count == 3
    assert "completed" in _plain(result.output)


def test_jobs_wait_json(respx_mock):
    respx_mock.get(f"{API}/async-jobs/{JOB_ID}").mock(
        side_effect=[
            httpx.Response(200, json=_job("pending")),
            httpx.Response(200, json=_job("completed", progress_pct=100)),
        ]
    )
    result = runner.invoke(app, ["jobs", "wait", JOB_ID, "--json"], env=ENV)
    assert result.exit_code == 0
    data = json.loads(result.stdout)  # 无进度条干扰, stdout 可整体解析
    assert data["status"] == "completed"
    assert data["progress_pct"] == 100


def test_jobs_wait_failed(respx_mock):
    respx_mock.get(f"{API}/async-jobs/{JOB_ID}").mock(
        return_value=httpx.Response(200, json=_job("failed", error_message="boom"))
    )
    result = runner.invoke(app, ["jobs", "wait", JOB_ID], env=ENV)
    assert result.exit_code == 1
    assert "boom" in _plain(result.stderr)
    assert "Traceback" not in result.stderr


def test_jobs_cancel(respx_mock):
    route = respx_mock.post(f"{API}/async-jobs/{JOB_ID}/cancel").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    result = runner.invoke(app, ["jobs", "cancel", JOB_ID], env=ENV)
    assert result.exit_code == 0
    assert route.called
    assert "已请求取消" in _plain(result.output)


def test_jobs_cancel_conflict(respx_mock):
    respx_mock.post(f"{API}/async-jobs/{JOB_ID}/cancel").mock(
        return_value=httpx.Response(409, json={"detail": "cannot cancel terminal job"})
    )
    result = runner.invoke(app, ["jobs", "cancel", JOB_ID], env=ENV)
    assert result.exit_code == 1
    assert "Traceback" not in result.stderr


# ---------- ml-backends ----------


def _ml_backend() -> dict:
    return {
        "id": str(uuid4()),
        "project_id": str(uuid4()),
        "name": "sam2-backend",
        "url": "http://gpu-host:9000",
        "state": "connected",
        "health_meta": {
            "model_version": "v1.2",
            "gpu_info": {"gpu_utilization_percent": 73},
        },
        "error_message": None,
        "last_checked_at": "2026-06-11T00:00:00Z",
        "created_at": "2026-06-11T00:00:00Z",
        "updated_at": "2026-06-11T00:00:00Z",
    }


def test_ml_backends_list_table(respx_mock):
    pid = str(uuid4())
    respx_mock.get(f"{API}/projects/{pid}/ml-backends").mock(
        return_value=httpx.Response(200, json=[_ml_backend()])
    )
    result = runner.invoke(app, ["ml-backends", "list", "--project", pid], env=ENV)
    assert result.exit_code == 0
    plain = _plain(result.output)
    for cell in [
        "名称",
        "状态",
        "model_version",
        "sam2-backend",
        "connected",
        "v1.2",
        "73%",
    ]:
        assert cell in plain


def test_ml_backends_list_json(respx_mock):
    pid = str(uuid4())
    respx_mock.get(f"{API}/projects/{pid}/ml-backends").mock(
        return_value=httpx.Response(200, json=[_ml_backend()])
    )
    result = runner.invoke(
        app, ["ml-backends", "list", "--project", pid, "--json"], env=ENV
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data[0]["state"] == "connected"
    assert data[0]["health_meta"]["model_version"] == "v1.2"


# ---------- export ----------


def _mock_export_flow(respx_mock, pid: str) -> None:
    respx_mock.post(f"{API}/projects/{pid}/export").mock(
        return_value=httpx.Response(202, json={"job_id": JOB_ID})
    )
    respx_mock.get(f"{API}/async-jobs/{JOB_ID}").mock(
        side_effect=[
            httpx.Response(200, json=_job("running", progress_pct=40)),
            httpx.Response(
                200,
                json=_job(
                    "completed",
                    progress_pct=100,
                    result={"download_url": "http://minio.local/exports/out.zip"},
                ),
            ),
        ]
    )
    respx_mock.get(host="minio.local", path="/exports/out.zip").mock(
        return_value=httpx.Response(200, content=b"PK\x03\x04zipbytes")
    )


def test_export_project_full_flow(respx_mock, tmp_path):
    pid = str(uuid4())
    _mock_export_flow(respx_mock, pid)
    out = tmp_path / "out.zip"
    result = runner.invoke(
        app,
        ["export", "project", pid, "--target", "aap_json", "--out", str(out)],
        env=ENV,
    )
    assert result.exit_code == 0
    assert out.read_bytes() == b"PK\x03\x04zipbytes"
    assert "导出完成" in _plain(result.output)


def test_export_project_json(respx_mock, tmp_path):
    pid = str(uuid4())
    _mock_export_flow(respx_mock, pid)
    out = tmp_path / "out.zip"
    result = runner.invoke(
        app,
        ["export", "project", pid, "--target", "aap_json", "--out", str(out), "--json"],
        env=ENV,
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["job_id"] == JOB_ID
    assert data["status"] == "completed"
    assert data["out"] == str(out)
    assert out.is_file()


def test_export_project_multi_target_options(respx_mock):
    pid = str(uuid4())
    route = respx_mock.post(f"{API}/projects/{pid}/export").mock(
        return_value=httpx.Response(202, json={"job_id": JOB_ID})
    )
    # --no-wait: 只创建, 不下载; 多 target + 选项透传到 query
    result = runner.invoke(
        app,
        [
            "export",
            "project",
            pid,
            "--target",
            "coco",
            "--target",
            "yolo-det",
            "--no-include-attributes",
            "--axis-frame",
            "source",
            "--no-wait",
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["job_id"] == JOB_ID and data["waited"] is False
    q = route.calls.last.request.url.params
    assert q.get_list("targets") == ["coco", "yolo-det"]
    assert q["include_attributes"] == "false"
    assert q["axis_frame"] == "source"


def test_export_project_wait_requires_out(respx_mock):
    pid = str(uuid4())
    _mock_export_flow(respx_mock, pid)
    # --wait (默认) 但缺 --out → BadParameter, 非 0 退出
    result = runner.invoke(app, ["export", "project", pid, "--target", "coco"], env=ENV)
    assert result.exit_code != 0


# ---------- batches / members / me ----------


def test_batches_list_table(respx_mock):
    pid = str(uuid4())
    respx_mock.get(f"{API}/projects/{pid}/batches").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": str(uuid4()),
                    "project_id": pid,
                    "display_id": "B-1",
                    "name": "batch-alpha",
                    "status": "active",
                    "total_tasks": 20,
                    "completed_tasks": 12,
                    "review_tasks": 3,
                    "rejected_tasks": 1,
                    "progress_pct": 60.0,
                    "annotator": {
                        "id": str(uuid4()),
                        "name": "标注员甲",
                        "email": "a@x.io",
                        "avatar_initial": "甲",
                    },
                    "reviewer": None,
                    "created_at": "2026-06-11T00:00:00Z",
                }
            ],
        )
    )
    result = runner.invoke(app, ["batches", "list", pid], env=ENV)
    assert result.exit_code == 0
    plain = _plain(result.output)
    assert "batch-alpha" in plain
    assert "12/20" in plain
    assert "标注员甲" in plain


def test_batches_list_status_filter_json(respx_mock):
    pid = str(uuid4())
    route = respx_mock.get(f"{API}/projects/{pid}/batches").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = runner.invoke(
        app, ["batches", "list", pid, "--status", "reviewing", "--json"], env=ENV
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout) == []
    assert route.calls.last.request.url.params["status"] == "reviewing"


def test_members_list_table(respx_mock):
    pid = str(uuid4())
    respx_mock.get(f"{API}/projects/{pid}/members").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": str(uuid4()),
                    "user_id": str(uuid4()),
                    "user_name": "张三",
                    "user_email": "zhang@x.io",
                    "role": "annotator",
                    "platform_role": "employee",
                    "version": 1,
                    "assigned_at": "2026-06-10T00:00:00Z",
                }
            ],
        )
    )
    result = runner.invoke(app, ["members", "list", pid], env=ENV)
    assert result.exit_code == 0
    plain = _plain(result.output)
    assert "张三" in plain
    assert "annotator" in plain
    assert "employee" in plain


def test_me_command(respx_mock):
    respx_mock.get(f"{API}/auth/me").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": str(uuid4()),
                "email": "me@x.io",
                "name": "Me",
                "role": "project_admin",
                "status": "active",
            },
        )
    )
    result = runner.invoke(app, ["me"], env=ENV)
    assert result.exit_code == 0
    plain = _plain(result.output)
    assert "project_admin" in plain
    assert "me@x.io" in plain


# ---------- stats / dashboard ----------


def test_stats_command(respx_mock):
    respx_mock.get(f"{API}/projects/stats").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_data": 100,
                "completed": 60,
                "ai_rate": 0.4,
                "pending_review": 8,
                "total_data_series": [10, 50, 100],
                "completed_series": [5, 30, 60],
                "ai_rate_series": [0.1, 0.3, 0.4],
                "pending_review_series": [2, 5, 8],
            },
        )
    )
    result = runner.invoke(app, ["stats"], env=ENV)
    assert result.exit_code == 0
    plain = _plain(result.output)
    assert "总量 100" in plain
    assert "40%" in plain


def test_stats_command_json(respx_mock):
    respx_mock.get(f"{API}/projects/stats").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_data": 5,
                "completed": 1,
                "ai_rate": 0.2,
                "pending_review": 0,
                "total_data_series": [],
                "completed_series": [],
                "ai_rate_series": [],
                "pending_review_series": [],
            },
        )
    )
    result = runner.invoke(app, ["stats", "--json"], env=ENV)
    assert result.exit_code == 0
    assert json.loads(result.stdout)["total_data"] == 5


def test_dashboard_people_table(respx_mock):
    respx_mock.get(f"{API}/dashboard/admin/people").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "user_id": "u1",
                        "name": "甲",
                        "email": "a@x.io",
                        "role": "annotator",
                        "status": "online",
                        "project_count": 1,
                        "main_metric": 30,
                        "main_metric_label": "本周完成",
                        "throughput_score": 82,
                        "quality_score": 91,
                        "activity_score": 70,
                        "sparkline_7d": [3, 5, 4, 8, 6, 9, 7],
                        "rejected_rate": 0.05,
                        "alerts": [],
                    }
                ],
                "total": 1,
                "period": "7d",
            },
        )
    )
    result = runner.invoke(app, ["dashboard", "people"], env=ENV)
    assert result.exit_code == 0
    plain = _plain(result.output)
    assert "甲" in plain
    assert "82" in plain


def test_dashboard_people_forbidden_exit1(respx_mock):
    respx_mock.get(f"{API}/dashboard/admin/people").mock(
        return_value=httpx.Response(403, json={"detail": "forbidden"})
    )
    result = runner.invoke(app, ["dashboard", "people"], env=ENV)
    assert result.exit_code == 1
    assert "403" in _plain(result.stderr)
