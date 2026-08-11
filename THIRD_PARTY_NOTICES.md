# 第三方依赖说明

本文件记录首次公开发布前的许可证审查结果，不替代各依赖随附的许可证文本，也不构成法律意见。发布者应在每次正式发布前重新生成软件物料清单并复核直接与传递依赖。

## 关键运行依赖

| 依赖 | 当前版本 | 许可证要点 |
|---|---:|---|
| FastAPI | 0.141.1 | MIT |
| Starlette | 1.6.0 | BSD-3-Clause |
| Uvicorn | 0.39.0 | BSD-3-Clause |
| HTTPX | 0.28.1 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| ReportLab | 5.0.0 | BSD |
| pdfplumber | 0.11.10 | MIT |
| pypdf | 6.15.0 | BSD-3-Clause |
| PyMuPDF | 1.26.7 | AGPL-3.0 或 Artifex 商业许可证 |
| Vue | 3.5.40 | MIT |

## 项目许可证选择

PyMuPDF 是双许可证依赖。DocWeave 选择其 AGPL-3.0 开源许可路径，项目整体以 `AGPL-3.0-only` 发布。对外分发或通过网络提供修改版本时，需要遵守 AGPLv3 的完整义务。

如未来改用 Artifex 商业许可证或替换 PyMuPDF，应在重新完成许可证兼容性审查后再考虑调整项目许可证。第三方依赖的许可证不会因为项目采用 AGPLv3 而被替换。
