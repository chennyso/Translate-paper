# Translate Paper

本地 PDF 论文翻译与对照阅读工具。

上传论文后，服务会抽取页面文本块，调用 OpenAI 兼容接口翻译，并提供：

- 左侧原文 PDF 预览
- 右侧中文译文对照阅读
- 原文块和译文块双向定位
- 按页翻译、全文翻译、失败重试
- 历史论文记录恢复
- Markdown 导出
- 基于 `pdf2zh-next` / BabelDOC 的双语 PDF 生成

## 适用场景

- 本地阅读英文论文，不希望先等整篇翻译完成
- 边看原文边看中文，快速定位对应段落
- 需要保留版面预览，而不是只看纯文本翻译
- 需要导出双语 PDF 或 Markdown

## 当前实现

后端：

- FastAPI
- PyMuPDF (`pymupdf`)
- `pdf2zh-next`

前端：

- 原生 HTML / CSS / JavaScript

运行方式：

- 本地单机运行
- 默认地址 `http://127.0.0.1:8088`

## 功能

- PDF 页面渲染预览，保留原论文版面
- 文本块级抽取与翻译
- 原文热区与译文块点击联动
- 支持上传后自动翻译全文
- 支持按当前页单独翻译
- 支持全文重试
- 支持历史论文列表恢复
- 支持搜索原文或译文
- 支持复制当前段落
- 支持导出 Markdown
- 支持生成双语 PDF
- 翻译任务结果保存在本地 `storage/jobs`

## 目录结构

```text
.
├── app/                 # FastAPI 服务
├── web/                 # 前端静态文件
├── run.py               # 服务入口
├── requirements.txt     # Python 依赖
├── .env.example         # 环境变量示例
└── start.ps1            # Windows 启动脚本
```

## 环境要求

- Python 3.11 推荐
- macOS / Linux / Windows 均可
- 可访问 OpenAI 兼容接口

## 配置

先复制环境变量文件：

```bash
cp .env.example .env
```

最少需要配置：

```env
LLM_API_KEY=你的KEY
LLM_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1
LLM_MODEL=mimo-v2.5-pro
APP_HOST=127.0.0.1
APP_PORT=8088
```

不要把真实 `.env` 提交到仓库。

## 启动方式

### 方式 1：使用 uv

推荐。

安装 `uv` 后执行：

```bash
uv venv .venv --python 3.11
uv pip install --python .venv/bin/python -r requirements.txt
uv run --python .venv/bin/python run.py
```

启动后访问：

```text
http://127.0.0.1:8088
```

### 方式 2：Windows PowerShell

仓库内提供了一个简单脚本：

```powershell
Copy-Item .env.example .env
.\start.ps1
```

注意：

- `start.ps1` 当前使用 `venv + uv`
- 如果你已经手动创建了环境，可以直接运行 `python run.py`

## 使用流程

1. 打开首页，上传 PDF
2. 选择是否“上传后自动翻译全文”
3. 在左侧查看原文 PDF
4. 在右侧查看中文译文
5. 点击任意原文块或译文块进行双向定位
6. 需要时可：
   - 翻译当前页
   - 翻译全文
   - 重试失败任务
   - 导出 Markdown
   - 生成双语 PDF

## 历史记录

历史论文记录基于本地任务目录：

```text
storage/jobs/<job_id>/
```

会保存：

- 原始 PDF
- 每页渲染图片
- 任务元数据
- 翻译日志
- 生成后的双语 PDF

前端左侧会显示历史论文列表，可直接恢复之前的阅读任务。

## 双语 PDF

“生成双语 PDF” 使用 `pdf2zh-next` / BabelDOC 的布局管线。

特点：

- 比普通逐段翻译更慢
- 会额外消耗接口请求额度
- 首次运行可能需要加载模型或做额外初始化
- 对扫描版 PDF 效果依赖 OCR 质量

当前默认做了较保守的并发控制，以降低接口 `429 Too Many Requests` 的概率。

## 可调配置

### 服务

```env
APP_HOST=127.0.0.1
APP_PORT=8088
```

### 普通翻译重试

```env
LLM_RETRY_ATTEMPTS=4
LLM_RETRY_BASE_DELAY=2
```

含义：

- `LLM_RETRY_ATTEMPTS`：遇到 `429` 时最大重试次数
- `LLM_RETRY_BASE_DELAY`：指数退避起始秒数

### pdf2zh-next 并发

```env
PDF2ZH_QPS=1
PDF2ZH_WORKERS=1
PDF2ZH_TIMEOUT=180
PDF2ZH_TEMPERATURE=0.2
PDF2ZH_LANG_IN=en
PDF2ZH_LANG_OUT=zh-CN
```

如果你的接口额度比较紧，保持 `PDF2ZH_QPS=1` 和 `PDF2ZH_WORKERS=1`。

## 常见问题

### 1. 页面提示 `429 Too Many Requests`

原因：

- 上游 OpenAI 兼容接口触发限流

当前处理：

- 普通逐段翻译已经加了 `429` 自动退避重试
- 双语 PDF 默认并发已降到更保守的水平

如果仍然频繁出现：

- 降低使用频率
- 先按页翻译，不要直接整篇跑
- 保持 `PDF2ZH_QPS=1`
- 更换额度更高的接口

### 2. 上传后任务失败

优先检查：

- `.env` 里的 `LLM_API_KEY` 是否正确
- `LLM_BASE_URL` 是否可访问
- PDF 是否可正常解析

可查看本地日志：

```text
storage/logs/app.log
storage/jobs/<job_id>/translation.log
```

### 3. 扫描版 PDF 没有内容

原因：

- 扫描版通常没有可抽取文字层

处理：

- 先 OCR，再上传

### 4. 生成双语 PDF 很慢

这是预期行为。

因为布局管线比普通块翻译更重，且会做版面处理。

## API 概览

主要接口：

- `GET /api/config`
- `GET /api/jobs`
- `POST /api/jobs`
- `GET /api/jobs/{job_id}`
- `POST /api/jobs/{job_id}/retry`
- `POST /api/jobs/{job_id}/translate`
- `POST /api/jobs/{job_id}/layout-pdf`
- `GET /api/jobs/{job_id}/layout-pdf`
- `GET /api/jobs/{job_id}/layout-pdf/{kind}`
- `GET /api/jobs/{job_id}/logs`

## 开发说明

前端是原生静态文件：

- `web/index.html`
- `web/app.js`
- `web/styles.css`

后端入口：

- `run.py`
- `app/main.py`

## 已知限制

- 当前依赖 OpenAI 兼容接口稳定返回 JSON
- 上游限流会直接影响翻译速度和成功率
- 扫描版 PDF 依赖 OCR
- 表格和公式默认以保护版面为优先，不保证全部逐字翻译

## License

当前仓库未单独声明许可证。发布前建议补充。
