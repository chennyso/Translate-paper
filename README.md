# Translate Paper

本地 PDF 论文沉浸式翻译工具。上传 PDF 后，工具会抽取每页文本块，调用 OpenAI 兼容接口翻译，并提供左侧原文 PDF 预览、右侧中文对照译文。点击任意原文块或译文块可以互相定位。

## 功能

- PDF 页面渲染预览，保留原版排版视觉。
- 中文译文按页、按栏重排，尽量贴近论文版式，同时保证中文能完整展开阅读。
- 文本块级抽取、翻译和中英双栏对照。
- 原文和译文点击联动，高亮对应块。
- 支持上传后自动翻译全文，也支持先解析、阅读时按当前页翻译。
- 页面级进度、全文重试、搜索、当前段复制和 Markdown 导出。
- BabelDOC/pdf2zh-next 布局管线生成真正的双语 PDF，可在浏览器查看或下载。
- 生成布局 PDF 时保护公式、图片和表格；表格文字默认不翻译，只翻译正文文本。
- 翻译任务缓存，刷新页面不丢结果。
- 支持 OpenAI 兼容 API Base URL 和模型配置。
- 不把 API Key 写进代码，使用 `.env` 或环境变量。

## 前端参考和取舍

实现前参考了这些项目的交互思路：

- `davideuler/pdf-translator-for-human`：双栏阅读、按页翻译、读到哪里翻到哪里。
- `PDFMathTranslate/PDFMathTranslate-next`：任务进度、双语产物、翻译流程和配置管理。
- `mengxi-ream/read-frog`：沉浸式翻译的模式切换、卡片操作、重试/复制等细粒度状态。

本项目没有直接引入 React/Vite/Gradio/Streamlit 前端，是为了降低本地运行和构建故障概率。前端使用原生 HTML/CSS/JS，但结构按阅读器拆分为上传、任务状态、页面导航、PDF 原文层、译文排版页和操作区。

## 快速启动

```powershell
cd E:\python\Translate-paper
Copy-Item .env.example .env
```

编辑 `.env`，填入你的 `LLM_API_KEY`。然后启动：

```powershell
.\start.ps1
```

`start.ps1` 会自动创建 `.venv`，安装 `uv`，并使用清华源安装依赖。

浏览器打开：

```text
http://127.0.0.1:8088
```

## Token Plan 配置

OpenAI 兼容接口：

```env
LLM_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1
LLM_MODEL=mimo-v2.5-pro
LLM_API_KEY=你的KEY
```

注意：不要把真实 Key 提交到 Git。

## 使用建议

论文页数很多时，建议取消“上传后自动翻译全文”，先解析 PDF，然后按当前页翻译。当前版本按文本块分批调用模型，便于稳定重试和对照定位。

如果要最终阅读版式，点击“生成双语 PDF”。这会调用 pdf2zh-next/BabelDOC 的 layout pipeline，第一次运行可能需要下载或加载版面模型，耗时会明显长于普通逐段翻译。扫描版 PDF 没有可抽取文字时，需要先 OCR 后再上传。
