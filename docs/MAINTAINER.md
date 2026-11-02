# 维护者发布流程 · Maintainer Release Workflow

本文件面向项目维护者，描述如何把新版本从本地源码发布到 GitHub（含推送、截图更新、Release 上传与 CI 排查）。贡献者一般只需阅读 [CONTRIBUTING.md](../CONTRIBUTING.md)。

> 本文为公开版，不含任何个人路径、账号凭据或密钥；内部环境相关的完整手册请由维护者在其本地保存。

---

## 1. 仓库与目录约定

- 仓库：`beforeyouwereyoung-a11y/camera-watermark-pro`（Public，Apache-2.0）
- 工作副本（本地克隆）
- 发版源码：由发布流程产出的源码树（`app.py`、`core/`、`processor/`、`static/`、`templates/`、`assets/`、`config/{fonts,logos,templates}`、测试与 `pyproject.toml` 等）

## 2. 哪些进仓库、哪些不进

| 进仓库 | 不进仓库（`.gitignore`） |
|---|---|
| 源码、前端、模板/字体/Logo、测试、文档 | 构建产物：`*.exe`、`便携版-*/`、`单文件版-*/`、`*.zip`、`dist/`、`build/` |
| `requirements.txt`、`pyproject.toml`、`LICENSE`、`NOTICE` | 运行期数据：`config/config.ini`、`config/presets/`、`input/`、`output/`、`logs/`、`.cwp_write_probe` |
| `selftest.py`、`smoke_test.py`、`test_*.py`、`ui_verify.py`、`_selftest/sample.jpg` | 第三方大件：`exiftool/`（改为可选依赖，见 docs/BUILDING.md） |
| `README*.md`、`docs/`、`.github/` | 本地临时：`.piptmp/`、`.pylibs/`、`.wheels/`、`_runlog/`、`_uidemo/` |

口诀：**源码与素材进库，产物与数据不进库；含本机路径的脚本先脱敏。**

## 3. 发版标准流程

1. **同步源码**：把新版本源码覆盖到工作副本对应位置（保留 `.git/`、`LICENSE`、`NOTICE`、`README*`、`docs/`、`screenshots/`、`.github/`）。
2. **改版本**：`pyproject.toml` 的 `version`；在 `CHANGELOG.md` 顶部加一节；同步更新 README/文档。
3. **更新截图**（界面有变化时）：用 `ui_verify.py` 在当前版本重新生成（见第 5 节）。
4. **安全扫描**：确认没有个人绝对路径、密钥、token（`git status` + 全文搜索）。
5. **提交**：`git add -A && git commit -m "feat: ..."`。
6. **推送**（见第 4 节）。
7. **发 Release**（见第 6 节）。
8. **校验**：CI 绿色、`HEAD == origin/main`、Release 资产齐全。

## 4. 推送（推荐方式：token 直推）

某些受限/沙箱环境里 Git 的 credential helper 会因命名管道限制失败（报 `could not read Username` 或 `signal pipe, Win32 error 5`）。此时用已登录的 `gh` 令牌直接注入 URL 推送，**不要把 token 写入 `git remote`**：

```powershell
$env:GIT_TERMINAL_PROMPT = "0"
$tok = (gh auth token).Trim()
git push "https://x-access-token:$tok@github.com/beforeyouwereyoung-a11y/camera-watermark-pro.git" main:main
```

首次建仓（如尚无远程）：

```bash
gh repo create beforeyouwereyoung-a11y/camera-watermark-pro --public --source=. --remote=origin \
  --description "相机水印工具 Pro — 本地批量相机水印桌面工具（Flask + WebView2）"
# --push 可能因凭据助手失败，创建后用上面的 token 直推即可
```

## 5. 用 ui_verify.py 重新生成界面截图

`ui_verify.py` 会驱动 Chrome、用 `_selftest/sample.jpg` 造 demo 照片、走完 15 个界面状态并截图到 `_uidemo/screenshots/`，同时执行 111 项断言。

前置：
- Python 3.13 环境，`pip install -r requirements.txt` 以及 `playwright`；
- 本机有 Chrome；
- `exiftool/` 就位（否则界面显示「Pillow 降级模式」）；
- 能监听 `127.0.0.1:15380`。

运行（先起服务，再跑脚本）：

```python
# serve.py —— 必须带 __main__ 保护！Windows multiprocessing 是 spawn，
# 否则每个子进程会重新执行本文件 → 起多个 Flask 服务 → 批处理卡死
import multiprocessing, app
if __name__ == "__main__":
    multiprocessing.freeze_support()
    app.run_server(host="127.0.0.1", port=15380, debug=False)
```

```bash
python serve.py &
python ui_verify.py     # 结束应看到「通过 111 项，失败 0 项」
```

**常见坑**：
- `WinError 5 / signal pipe`：运行环境禁命名管道 → 用具备完整权限的会话运行。
- 截图里带了本机用户名路径：把项目复制到中性路径（如某个盘根的短目录）再跑。
- 截图版本号/文案过期：确认连的是新起的服务（先清掉占用 15380 的旧进程），并同步更新 `static/app.js` 的 `TEMPLATE_DESC`。
- 「标准水印Pro」等卡片描述与实际版式不符：改 `static/app.js` 的 `TEMPLATE_DESC`。

## 6. 发布 Release

```bash
# 首次创建（附单文件版 exe）
gh release create vX.Y.Z "CameraWatermarkPro.exe" --title "vX.Y.Z — 相机水印工具 Pro" --notes "..."

# 追加文件夹版 zip（先剔个人 config/日志/探针再压缩）
gh release upload vX.Y.Z "CameraWatermarkPro-portable-vX.Y.Z.zip" --clobber

# 修改说明
gh release edit vX.Y.Z --notes "..."
```

分发包注意：便携版 zip 必须剔除 `config/config.ini`、`.cwp_write_probe`、`input/`、`output/`、`logs/`、`__pycache__`，保证用户拿到的是干净初始状态。

## 7. CI 说明与排查

`.github/workflows/ci.yml`：`windows-latest` + Python 3.13，执行编译检查、导入冒烟、模板 JSON 校验。

已知坑：
- **Windows runner 控制台是 cp1252**，`print()` 中文会 `UnicodeEncodeError` → 工作流已设 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`，校验脚本只打印 ASCII。
- **`core.PROJECT_INFO` 是 Path 不是 dict** → 读版本请用 `core.configs.load_project_info()`。
- `gh run view --log-failed` 在受限环境可能因写缓存失败 → 改用 `gh api repos/<owner>/<repo>/actions/jobs/<job_id>/logs`。

## 8. 发布前检查清单

- [ ] `pyproject.toml` 版本号已改、`CHANGELOG.md` 已更新
- [ ] 截图已用当前版本重生成（111/111 通过）
- [ ] 无个人路径 / 密钥 / token
- [ ] `git status` 无 `config.ini`、`config/presets/`、`exiftool/`、大文件
- [ ] CI 绿色、`HEAD == origin/main`
- [ ] Release 资产齐全（exe + portable zip）
