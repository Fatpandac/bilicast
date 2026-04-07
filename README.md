# bilicast
A generator for creating Bilibili podcasts.

## 已实现功能

- 从 `config.yaml` 里的 `podcasts` 列表拉取视频
- 仅下载音频（`only_audio`）并落库
- 自动生成 RSS（以播客 `name` 作为路径）

## API

- `GET /podcasts`：获取所有配置中的播客
- `GET /podcasts/{name}`：获取单个播客信息和 RSS 链接
- `GET /rss/{name}`：订阅该播客 RSS
- `GET /media/{name}/{file_name}`：获取生成后的音频文件

例如 RSS 订阅：
`https://<host>:<port>/rss/bilicast1`

## Neovim + Pyright

如果看到 `apscheduler` 的 `reportMissingImports`，先确认使用项目虚拟环境：

```bash
uv sync --group dev
uv run pyright src
```

项目已新增 `pyrightconfig.json`，会把解释器定位到本仓库 `.venv` 并把 `src` 作为源码搜索路径。修改完依赖或配置后，重启 Neovim 的 Pyright LSP 即可刷新。 

## 运行

```bash
uv sync --group dev
uv run src/main.py
```

如果只需要运行测试：

```bash
uv run pytest --cov=src --cov-report=html --cov-fail-under=90
```
