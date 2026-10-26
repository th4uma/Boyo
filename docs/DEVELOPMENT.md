# 开发说明

## 运行环境

Boyo 是 Python / PySide6 原生 Windows 应用，源码位于 `src/boyo`。当前验证环境为 Windows x64、Python 3.11。第一次安装见仓库 README；日常开发沿用旁边的 `local/environment`。

```powershell
..\local\environment\Scripts\python.exe .\launcher.py
```

通过 `launcher.py` 或 `run.cmd` 启动，会把临时文件、字节码和模型缓存放到 `local`，保持源码目录整洁。

## 模块划分

| 模块 | 职责 |
| --- | --- |
| `native_ui`、`workspace_ui`、`review_viewer` | 主工作区、缩略图复核和独立大图 |
| `scanner`、`analysis`、`grouping`、`screening` | 扫描、特征缓存、相似构图及候选 |
| `quality`、`motion_quality` | 清晰度、曝光和方向性模糊测量 |
| `operations`、`matching`、`protection_watch` | 文件配对、保护校验、移动与复制日志 |
| `collections` | 图集成员、占用、自动选图和导出 |
| `preferences`、`embeddings`、`auto_learning` | 反馈、小模型与后台验证 |
| `xmp_style`、`photoshop_renderer`、`colour_match` | XMP 参考渲染与色调匹配 |
| `config`、`store`、`portability` | 本机数据、SQLite 和工作区迁移 |

应用名称与图标配置位于 `branding.py`。`launcher.py` 通过 `BOYO_LOCAL` 指定运行数据目录，默认使用源码目录同级的 `local`。

## 测试

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\运行检查.ps1
```

脚本将临时目录设为 `local/tmp`，运行 `unittest` 全集。测试涵盖保护关系、移动与恢复、图集去重、习惯学习、迁移及界面交互；测试照片在临时目录创建。

XMP 自动化测试使用替代渲染器检查参数传递、源文件保护和导出校验；Adobe 渲染效果由单独的集成验证覆盖。

## 只读扫描与分析

```powershell
..\local\environment\Scripts\python.exe .\launcher.py --workspace 'E:\照片工作区' --scan 'E:\照片工作区\一次旅行'
```

加 `--analyze` 可在扫描后分析。扫描与分析读取照片，将结果和工作区设置写入 `local`。文件移动通过桌面界面执行。

## 依赖和打包

`requirements.txt` 声明基础依赖范围，`requirements-lock.txt` 固定基础环境版本，`requirements-learning.txt` 声明视觉学习依赖。CPU 版 PyTorch / torchvision 由安装脚本单独安装。

`pyproject.toml` 定义基础 Python 包及图标、Adobe 脚本资源。完整桌面环境由 `安装环境.ps1` 建立，包括 Python 运行时、视觉学习依赖和模型权重。

## 源码结构

```text
src/boyo/          桌面界面与业务模块
tests/             自动化测试
docs/              技术与使用文档
launcher.py        启动入口与运行环境配置
portable_env.py    环境路径修复
```
