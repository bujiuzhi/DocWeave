# 贡献指南

## 开发环境

- Python 3.12
- pnpm
- Docker Engine 与 Docker Compose（可选）

后端依赖安装及启动方式见主 [README](README.md)，前端命令见 [frontend/README.md](frontend/README.md)。本地配置从 `.env.example` 复制到未被 Git 跟踪的 `.env`，不得使用生产凭据或真实业务数据提交测试材料。

```bash
python3 -m pip install -r backend/requirements-dev.txt
pnpm --dir frontend install --frozen-lockfile
```

## 变更要求

- 变更应聚焦单一目的，不修改无关文件。
- Python 公共函数应保留类型注解和 docstring；业务注释、日志和文档使用中文。
- Bug 修复应增加回归测试；功能变更应覆盖核心成功路径和主要异常路径。
- 新增依赖前应核验必要性、许可证、最新稳定版本和现有环境兼容性。
- 提交前检查凭据、个人信息、运行数据、调试输出、构建产物和无用依赖。

## 本地验证

```bash
python3 -m pytest backend/tests -q
pnpm --dir frontend build
pnpm --dir frontend audit --prod
python3 -m pip_audit -r backend/requirements.txt
docker compose config -q
```

提交信息沿用 `feat/fix/docs/refactor/chore/test/perf/build/ci/revert: 中文摘要` 格式。
