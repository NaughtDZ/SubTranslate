# SubTranslate

单文件字幕翻译工具：`subtranslate.py`（零第三方依赖，Python 3.10+ 标准库即可运行）。

> **下载**：不想装 Python 的话，直接取 [Releases](https://github.com/NaughtDZ/SubTranslate/releases/latest) 里的 `SubTranslate.exe`（单文件免安装，Windows x64）。

- 支持**远端**（OpenAI 兼容 / DeepSeek / 任意中转）与**本地**（LM Studio、Ollama、llama.cpp server）LLM
- 导入**一个或多个目录**，递归扫描，批量翻译
- 源语言可指定，或先**自动识别**（本地字符集启发式 + 可选 LLM 复核）
- 输出 `<原名>.<目标语言>.<扩展名>`，保留原编码、原时间轴、原样式
- 支持文本字幕：**SRT / ASS / SSA / VTT / LRC / SMI / SBV / SUB(MicroDVD) / MPL2 / TTML**
- 断点缓存、并发、失败保留原文、Ctrl+C 可停

## 快速开始

```bat
:: 图形界面
python subtranslate.py

:: 命令行
python subtranslate.py --cli D:\movies --to zh-CN --model qwen2.5-7b-instruct ^
    --base-url http://127.0.0.1:1234/v1 --batch 20 --workers 1

:: 先看看会处理哪些文件，不调模型
python subtranslate.py --cli D:\movies --dry-run

:: 自检（离线，验证 10 种格式的解析/写回与容错逻辑）
python subtranslate.py --selftest
```

本地 LM Studio：Base URL 填 `http://127.0.0.1:1234/v1`，模型名点「刷新模型」选。
Ollama：协议选 `ollama`，Base URL 填 `http://127.0.0.1:11434`。
OpenAI / DeepSeek：预设里选，填 API Key。

## 界面

- **输入**：可以「添加目录」「添加文件」，也可以**直接把文件或文件夹拖进窗口**（拖到列表区同样有效）。重复项自动忽略，非字幕扩展名会跳过并在日志里说明。
- **输出位置**：独立的输出目录输入框，带「浏览 / 打开 / 清空」。留空 = 与源字幕同目录；目录不存在会自动创建。命名规则 `原名.后缀.扩展名`（如 `movie.zh-CN.srt`），后缀默认等于目标语言。
- **停止**：点击后 0.1–0.3 秒内中断当前请求并停下。在途 HTTP 请求放在 daemon 线程，主线程轮询取消标志，不再等 socket 超时（Windows 上跨线程 shutdown 唤不醒阻塞的 recv，所以不依赖打断 socket）。
- **刷新模型**：按 `Base URL` + 协议拉模型列表（OpenAI 兼容走 `/v1/models`，Ollama 走 `/api/tags`）。拉取中按钮变「拉取中…」并禁用；成功后填入模型下拉框，并自动挑一个非 embedding / rerank 的模型；失败弹窗说明原因。
- **扫描预览**：只扫描不调用模型，先确认会处理哪些文件。
- **测试连接**：验证 Base URL / Key / 模型是否可用。

## 设计：为什么小模型也能稳定出片

**核心不是提示词，而是把"格式责任"从模型身上拿走。**

模型只看得到这种输入，也只被要求输出这种东西：

```
1|Hello, world!
2|Top line||second line
```

- 时间轴、序号、样式头、Dialogue 字段、HTML/ASS 标签 —— 全部由程序掌管，模型碰不到。
- 每条字幕先被剥掉所有格式标签，换行统一成 `||`（模型能理解"这是换行"，实测保留率 100%）。
- 标签按它在原文中的**位置比例**回填，`{\i1}你好{\i0}`、`<i>你好</i>`、`第一行\N第二行` 都能还原。
- 返回后逐条校验：缺行 / 空行 / 闲聊前缀 / 时间码污染 / 原样复制 / 明显漏译（译文长度 < 原文 12%）。
- 坏行只重发坏行；整批坏就二分拆批；单条坏就重试 3 次；仍失败**保留原文**，绝不写出结构损坏的文件。

实测（本机 LM Studio，`qwen3.8-2b-uncensored`，2B）：

| 场景 | 结果 |
|---|---|
| 60 条纯文本 SRT | 60/60 成功，约 2 秒 |
| SRT+ASS+VTT 混合（含 `<i>`、`{\i1}`、`\N`、双行） | 9/10 成功，1 条 `OK.` 保留原样 |

## 参数

| 参数 | 说明 |
|---|---|
| `--to` | 目标语言，默认 `zh-CN` |
| `--from` | 源语言，默认 `auto` |
| `--provider` | `openai`（兼容接口）或 `ollama` |
| `--base-url` / `--api-key` / `--model` | 接口配置 |
| `--batch` / `--batch-chars` | 每批条数 / 字符上限（小模型建议 10–20） |
| `--workers` | 文件级并发（本地模型建议 1） |
| `--suffix` | 输出后缀，默认等于目标语言 |
| `--overwrite` | 允许覆盖已存在的输出 |
| `--no-cache` | 关闭翻译缓存（`%APPDATA%\SubTranslate\cache.json`） |
| `--exts` | 只处理指定扩展名，如 `.srt,.ass` |

## 提示词

默认系统提示词只有 6 条规则，可在 GUI 里直接改（会存进配置）。它**故意不写**"保留占位符""不要改时间码"之类——因为模型根本收不到时间码和标签。

```text
You are a professional subtitle translator. You translate subtitle lines and nothing else.

INPUT: one subtitle per line, as: <id>|<text>
OUTPUT: exactly one line per input line, as: <id>|<translation>

RULES:
1. Copy <id> exactly. Never renumber, reorder, merge, split or drop lines.
2. Output nothing else: no timestamps, no numbering, no quotes, no markdown, no explanations, no blank lines.
3. The marker || marks a line break inside one subtitle. Reproduce it exactly where the break belongs.
4. Translate everything else faithfully. Keep numbers, names, units and sound effects consistent with the source.
5. If a line is already in the target language, keep it unchanged.
6. Keep each translation short enough to read on screen; never drop half of the sentence.
```

想加术语表/人名表，改这里或在 `TransCfg.glossary` 里传（会拼进 user message）。
调小模型时优先降 `--batch`（10–15）而不是加长提示词。

## 打包成独立 exe

```bat
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

产物 `dist\SubTranslate.exe`（约 12.4 MB，单文件免安装，双击即用）。

- 只在 `.venv-build` 里装 PyInstaller（清华源），**不碰系统 Python**；
- 完全回退：删 `.venv-build`、`build`、`dist` 三个目录即可，无注册表、无全局包改动；
- 脚本最后自动跑两遍自检（本目录 + 干净临时目录），确认 exe 不依赖任何外部文件；
- `--onefile --windowed`：双击无控制台，冷启动先解压到临时目录，约 1–2 秒；
- 配置与缓存仍在 `%APPDATA%\SubTranslate\`（与源码运行共用）。

> 打包前先关掉正在运行的 `SubTranslate.exe`，否则文件被占用会导致 PyInstaller 失败（退出码 1）。
> 若 exe 启动异常，错误会写到 `%TEMP%\SubTranslate_error.log`。

## 已知限制

- 图像字幕（PGS/VobSub/DVB）不支持；只做文本字幕。
- TTML 只替换 `<p>` 内文本，`<span>` 样式会丢。
- SMI 属实验性支持，结构复杂的文件建议先转 SRT。
- LRC 一字幕多时间标签的行会合并成一条翻译。
- 目标语言与源语言相同时仍会送模型（除非模型原样返回）。
