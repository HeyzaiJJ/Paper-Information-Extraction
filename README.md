# Paper Information Extraction

项目现在按前后端分层：

- `frontend/`：页面、样式和原生 JavaScript。
- `backend/`：FastAPI 应用、处理服务、模型集成、数据库和存储代码。
- `backend/config/`：运行时、模型和 Marker 配置。
- `backend/data/`：上传文件、导出文件、日志和 SQLite 数据库（不纳入 Git）。
- `backend/tests/`：自动化测试。

## 启动

推荐使用新的模块入口：

```powershell
.venv\Scripts\python.exe -m backend.main --host 127.0.0.1 --port 8000
```

## 数据迁移

停止服务后执行：

```powershell
.venv\Scripts\python.exe backend\scripts\migrate_data.py
```

脚本会把 `backend/data_legacy/` 复制到 `backend/data/`，校验文件内容并检查 SQLite 完整性。

## 测试

```powershell
.venv\Scripts\python.exe -m pytest -q
```
