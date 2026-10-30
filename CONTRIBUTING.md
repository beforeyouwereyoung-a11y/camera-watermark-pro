# 贡献指南 · Contributing Guide

感谢你愿意为 **相机水印工具 Pro** 出一份力！本文档说明如何搭建开发环境、代码规范、提交与 PR 流程。无论是修 Bug、加功能、补文档、做翻译、贡献模板，都非常欢迎。

> English contributors: this guide is written in Chinese, but PRs in English are equally welcome.

---

## 一、环境准备

```powershell
git clone https://github.com/beforeyouwereyoung-a11y/camera-watermark-pro.git
cd camera-watermark-pro
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

要求 **Python 3.13+**、Windows 10/11（桌面窗口依赖 WebView2 运行时）。

自检 / 运行：

```powershell
python selftest.py          # 基础自检
python smoke_test.py        # 冒烟测试
python desktop_app.py       # 启动原生窗口
python app.py               # 浏览器模式 http://127.0.0.1:15050
```

## 二、分支与提交

- 从 `main` 切出特性分支，命名建议：`feat/短描述`、`fix/短描述`、`docs/短描述`、`chore/短描述`。
- 提交信息采用 [Conventional Commits](https://www.conventionalcommits.org/)：
  ```
  feat(processor): 支持按品牌自动缩放 Logo
  fix(core): 修复多进程下 EXIF 缓存被并发写坏
  docs: 补充 ExifTool 安装说明
  ```
- 一次提交只做一件事；避免把格式化、重构和功能混在一起。
- 请勿提交 `config/config.ini`、`input/`、`output/`、`logs/`、`exiftool/` 与本机绝对路径（见 `.gitignore`）。

## 三、代码规范

- 遵循现有代码风格：4 空格缩进、`snake_case`、模块顶部写清用途的中文 docstring。
- 不引入不必要的重量级依赖；新增依赖须同步更新 `requirements.txt` 与 `pyproject.toml`。
- 涉及用户可见行为时，同步更新相关文档（`docs/`、README）。
- 关键路径（图像管线、EXIF 解析、路径安全）请写清注释，说明「为什么」而非「是什么」。

## 四、测试

- 修改 `core/`、`processor/` 逻辑时，请补充或运行对应测试：
  ```powershell
  python test_features.py
  python test_v340_features.py
  python selftest.py
  ```
- 界面相关改动可用 `python ui_verify.py`（需要本机 Chrome / Playwright 环境）。
- 提交 PR 前请确保上述自检在 Windows 上通过；如环境受限无法运行，请在 PR 描述中说明。

## 五、贡献模板 / Logo / 字体

- 新模板请放在 `config/templates/`，文件名用中文短名，并在 README「内置模板」表格中登记。
- 贡献品牌 Logo 时，请确认你有权分发该素材（商标归厂商所有）；不确定时请先开 Issue 讨论。
- 新增字体请随附其许可证说明，并更新 `NOTICE`。

## 六、Pull Request 检查清单

- [ ] 分支基于最新 `main`
- [ ] 提交信息符合 Conventional Commits
- [ ] 已在本地运行相关自检 / 测试
- [ ] 已更新相应文档
- [ ] 未包含个人路径、密钥或运行期数据
- [ ] 关联了对应 Issue（如有），并简述改动与验证方式

提交 PR 后，维护者会尽快 review。感谢你的耐心与贡献！
