# bilicast
A generator for creating Bilibili podcasts.

## 运行

```bash
uv sync --group dev
uv run src/main.py
```

如果只需要运行测试：

```bash
uv run pytest --cov=src --cov-report=html --cov-fail-under=90
```
