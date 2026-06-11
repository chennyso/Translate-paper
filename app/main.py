from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import fitz
import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles


load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
STORAGE_DIR = ROOT / "storage"
JOBS_DIR = STORAGE_DIR / "jobs"
LOGS_DIR = STORAGE_DIR / "logs"
STATIC_DIR = ROOT / "web"
MAX_FILE_SIZE = 80 * 1024 * 1024

JOBS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "app.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("translate-paper")

app = FastAPI(title="Translate Paper", version="1.0.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass
class TextBlock:
    id: str
    page: int
    index: int
    bbox: list[float]
    text: str
    translation: str = ""


def job_dir(job_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9-]{36}", job_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    return JOBS_DIR / job_id


def metadata_path(job_id: str) -> Path:
    return job_dir(job_id) / "metadata.json"


def read_metadata(job_id: str) -> dict[str, Any]:
    path = metadata_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="任务不存在")
    return json.loads(path.read_text(encoding="utf-8"))


def write_metadata(job_id: str, data: dict[str, Any]) -> None:
    path = metadata_path(job_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def default_layout_pdf_state() -> dict[str, Any]:
    return {
        "status": "idle",
        "progress": 0,
        "stage": "",
        "error": "",
        "dual_pdf": "",
        "mono_pdf": "",
        "updated_at": "",
    }


def set_layout_pdf_state(job_id: str, **updates: Any) -> dict[str, Any]:
    meta = read_metadata(job_id)
    state = default_layout_pdf_state()
    state.update(meta.get("layout_pdf") or {})
    state.update(updates)
    meta["layout_pdf"] = state
    write_metadata(job_id, meta)
    return state


def stable_layout_pdf_path(job_id: str, kind: str) -> Path:
    if kind not in {"dual", "mono"}:
        raise HTTPException(status_code=404, detail="PDF 不存在")
    return job_dir(job_id) / f"layout-{kind}.pdf"


def copy_pdf_output(source: Any, target: Path) -> str:
    if not source:
        return ""
    source_path = Path(str(source))
    if not source_path.exists():
        return ""
    if source_path.resolve() != target.resolve():
        shutil.copyfile(source_path, target)
    return str(target)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def append_job_log(job_id: str, message: str) -> None:
    line = f"{now_iso()} {message}\n"
    try:
        with (job_dir(job_id) / "translation.log").open("a", encoding="utf-8") as handle:
            handle.write(line)
    except Exception:
        logger.exception("failed to write job log for %s", job_id)


def normalize_text(text: str) -> str:
    text = re.sub(r"-\s*\n\s*", "", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_noise(text: str) -> bool:
    compact = text.strip()
    if len(compact) < 3:
        return True
    if re.fullmatch(r"[\d\s.\-–—_/|:;()[\]]+", compact):
        return True
    return False


def is_formula_like(text: str) -> bool:
    compact = text.strip()
    if not compact:
        return True
    math_chars = set("=<>±×÷∑∏√∞≈≠≤≥∂∆∇∫∈∉∩∪⊂⊃⊆⊇∀∃→←↔⇒⇔αβγδθλμνπρσφχψωΓΔΘΛΞΠΣΦΨΩ")
    symbol_count = sum(1 for char in compact if char in math_chars)
    operator_count = len(re.findall(r"(?<!\w)[+\-*/^=<>](?!\w)|[_{}]", compact))
    alpha_words = re.findall(r"[A-Za-z]{3,}", compact)
    if symbol_count + operator_count >= 4 and len(alpha_words) <= 8:
        return True
    if re.search(r"\b(eq\.?|equation)\s*\(?\d+", compact, re.I):
        return False
    if len(compact) < 120 and symbol_count + operator_count >= 3 and len(alpha_words) <= 4:
        return True
    return False


def is_table_like(raw_text: str, clean: str) -> bool:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if len(lines) < 4:
        return False
    short_lines = sum(1 for line in lines if len(line) <= 24)
    numeric_tokens = len(re.findall(r"\b\d+(?:\.\d+)?%?\b", raw_text))
    alpha_words = len(re.findall(r"[A-Za-z]{3,}", clean))
    columnish_lines = sum(1 for line in lines if len(re.split(r"\s{2,}|\t", line)) >= 3)
    if columnish_lines >= 3:
        return True
    if short_lines / len(lines) > 0.65 and numeric_tokens >= 3:
        return True
    if len(lines) >= 8 and numeric_tokens > alpha_words * 0.45:
        return True
    return False


def should_translate_block(raw_text: str, clean: str) -> bool:
    if is_noise(clean):
        return False
    if is_table_like(raw_text, clean):
        return False
    if is_formula_like(clean):
        return False
    return True


def extract_pdf(pdf_path: Path, output_dir: Path) -> tuple[list[dict[str, Any]], list[TextBlock]]:
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    pages: list[dict[str, Any]] = []
    blocks: list[TextBlock] = []

    for page_index, page in enumerate(doc):
        rect = page.rect
        pix = page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
        image_name = f"page-{page_index + 1}.png"
        pix.save(pages_dir / image_name)
        pages.append(
            {
                "page": page_index + 1,
                "width": rect.width,
                "height": rect.height,
                "image": f"/api/jobs/{output_dir.name}/pages/{image_name}",
            }
        )

        raw_blocks = page.get_text("blocks", sort=True)
        block_index = 0
        for raw in raw_blocks:
            x0, y0, x1, y1, text = raw[:5]
            raw_text = str(text)
            clean = normalize_text(raw_text)
            if not should_translate_block(raw_text, clean):
                continue
            blocks.append(
                TextBlock(
                    id=f"p{page_index + 1}-b{block_index + 1}",
                    page=page_index + 1,
                    index=block_index,
                    bbox=[round(float(x0), 2), round(float(y0), 2), round(float(x1), 2), round(float(y1), 2)],
                    text=clean,
                )
            )
            block_index += 1

    doc.close()
    return pages, blocks


def chunk_blocks(blocks: list[dict[str, Any]], max_chars: int = 5000) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_len = 0

    for block in blocks:
        item_len = len(block["text"])
        if current and current_len + item_len > max_chars:
            chunks.append(current)
            current = []
            current_len = 0
        current.append(block)
        current_len += item_len

    if current:
        chunks.append(current)
    return chunks


def parse_json_object(content: str) -> dict[str, Any]:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    decoder = json.JSONDecoder()
    parsed_objects: list[dict[str, Any]] = []

    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    for match in re.finditer(r"\{", content):
        try:
            parsed, _ = decoder.raw_decode(content[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            parsed_objects.append(parsed)
            if "translations" in parsed:
                return parsed

    if parsed_objects:
        return parsed_objects[0]
    raise ValueError("模型没有返回 JSON")


async def translate_chunk(client: httpx.AsyncClient, blocks: list[dict[str, Any]], job_id: str = "") -> dict[str, str]:
    api_key = os.getenv("LLM_API_KEY", "").strip()
    base_url = os.getenv("LLM_BASE_URL", "https://token-plan-cn.xiaomimimo.com/v1").rstrip("/")
    model = os.getenv("LLM_MODEL", "mimo-v2.5-pro")
    if not api_key:
        raise RuntimeError("请先在 .env 或环境变量中设置 LLM_API_KEY")

    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是严谨的论文翻译助手。把英文学术论文文本翻译成自然、准确的简体中文。"
                    "保留公式、变量名、引用编号、专有名词和缩写。只返回 JSON。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "instruction": "翻译 blocks 中每个 text，返回格式必须是 {\"translations\":{\"block-id\":\"中文译文\"}}。",
                        "blocks": [{"id": item["id"], "text": item["text"]} for item in blocks],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "response_format": {"type": "json_object"},
    }

    endpoint = f"{base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    block_ids = [str(item["id"]) for item in blocks]
    char_count = sum(len(str(item.get("text", ""))) for item in blocks)
    started = time.perf_counter()
    if job_id:
        append_job_log(job_id, f"LLM request start model={model} blocks={len(blocks)} chars={char_count} ids={block_ids[0]}..{block_ids[-1]}")
    logger.info("LLM request start job=%s model=%s blocks=%s chars=%s", job_id or "-", model, len(blocks), char_count)

    try:
        response = await client.post(endpoint, headers=headers, json=payload, timeout=120)
    except Exception as exc:
        elapsed = time.perf_counter() - started
        if job_id:
            append_job_log(job_id, f"LLM request transport_error elapsed={elapsed:.1f}s error={exc}")
        logger.exception("LLM request transport_error job=%s elapsed=%.1fs", job_id or "-", elapsed)
        raise

    if response.status_code >= 400 and "response_format" in response.text:
        payload.pop("response_format", None)
        if job_id:
            append_job_log(job_id, f"LLM retry without response_format status={response.status_code}")
        response = await client.post(endpoint, headers=headers, json=payload, timeout=120)
    elapsed = time.perf_counter() - started
    if job_id:
        append_job_log(job_id, f"LLM response status={response.status_code} elapsed={elapsed:.1f}s bytes={len(response.content)}")
    logger.info("LLM response job=%s status=%s elapsed=%.1fs bytes=%s", job_id or "-", response.status_code, elapsed, len(response.content))
    if response.status_code >= 400:
        snippet = response.text[:500].replace("\n", " ")
        if job_id:
            append_job_log(job_id, f"LLM error status={response.status_code} body={snippet}")
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    try:
        parsed = parse_json_object(content)
    except Exception as exc:
        snippet = str(content)[:1000].replace("\n", "\\n")
        if job_id:
            append_job_log(job_id, f"LLM parse_error error={exc} content={snippet}")
        logger.warning("LLM parse_error job=%s error=%s content=%s", job_id or "-", exc, snippet)
        raise
    translations = parsed.get("translations", parsed)
    result = {str(key): str(value).strip() for key, value in translations.items()}
    missing = [block_id for block_id in block_ids if block_id not in result]
    if missing and job_id:
        append_job_log(job_id, f"LLM missing translations count={len(missing)} ids={missing[:12]}")
    return result


def page_status(blocks: list[dict[str, Any]], pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    status = []
    for page in pages:
        page_blocks = [block for block in blocks if block["page"] == page["page"]]
        translated = sum(1 for block in page_blocks if block.get("translation"))
        status.append(
            {
                "page": page["page"],
                "total": len(page_blocks),
                "translated": translated,
                "done": bool(page_blocks) and translated == len(page_blocks),
            }
        )
    return status


async def translate_job(job_id: str, pages: list[int] | None = None) -> None:
    meta = read_metadata(job_id)
    try:
        append_job_log(job_id, f"job start pages={pages or 'all'}")
        meta["status"] = "translating"
        meta["error"] = ""
        meta["active_pages"] = pages or []
        write_metadata(job_id, meta)

        blocks = meta["blocks"]
        selected_blocks = [
            block for block in blocks if (pages is None or block["page"] in pages) and not block.get("translation")
        ]
        translated = 0
        meta["progress"] = {"done": 0, "total": len(selected_blocks)}
        write_metadata(job_id, meta)

        if not selected_blocks:
            meta["status"] = "done" if all(block.get("translation") for block in blocks) else "ready"
            meta["active_pages"] = []
            meta["page_status"] = page_status(blocks, meta["pages"])
            write_metadata(job_id, meta)
            return

        async with httpx.AsyncClient() as client:
            for chunk in chunk_blocks(selected_blocks):
                translations = await translate_chunk(client, chunk, job_id)
                for block in blocks:
                    if block["id"] in translations:
                        block["translation"] = translations[block["id"]]
                translated += len(chunk)
                meta["blocks"] = blocks
                meta["progress"] = {"done": min(translated, len(selected_blocks)), "total": len(selected_blocks)}
                meta["page_status"] = page_status(blocks, meta["pages"])
                write_metadata(job_id, meta)

        meta["status"] = "done" if all(block.get("translation") for block in blocks) else "ready"
        meta["active_pages"] = []
        meta["progress"] = {"done": len(selected_blocks), "total": len(selected_blocks)}
        meta["page_status"] = page_status(blocks, meta["pages"])
        write_metadata(job_id, meta)
        append_job_log(job_id, f"job finish status={meta['status']} translated={sum(1 for block in blocks if block.get('translation'))}/{len(blocks)}")
    except Exception as exc:
        meta = read_metadata(job_id)
        meta["status"] = "error"
        meta["error"] = str(exc)
        meta["active_pages"] = []
        write_metadata(job_id, meta)
        append_job_log(job_id, f"job error {type(exc).__name__}: {exc}")
        logger.exception("translation job error job=%s", job_id)


def build_layout_settings(output_dir: Path) -> Any:
    api_key = os.getenv("LLM_API_KEY", "").strip()
    base_url = os.getenv("LLM_BASE_URL", "https://token-plan-cn.xiaomimimo.com/v1").rstrip("/")
    model = os.getenv("LLM_MODEL", "mimo-v2.5-pro")
    if not api_key:
        raise RuntimeError("请先在 .env 或环境变量中设置 LLM_API_KEY")

    try:
        from pdf2zh_next.config.model import PDFSettings
        from pdf2zh_next.config.model import SettingsModel
        from pdf2zh_next.config.model import TranslationSettings
        from pdf2zh_next.config.translate_engine_model import OpenAICompatibleSettings
    except ImportError as exc:
        raise RuntimeError("缺少 pdf2zh-next，请先运行 start.ps1 或 pip install -r requirements.txt") from exc

    return SettingsModel(
        report_interval=0.5,
        translation=TranslationSettings(
            lang_in=os.getenv("PDF2ZH_LANG_IN", "en"),
            lang_out=os.getenv("PDF2ZH_LANG_OUT", "zh-CN"),
            output=str(output_dir),
            qps=env_int("PDF2ZH_QPS", 2),
            pool_max_workers=env_int("PDF2ZH_WORKERS", 2),
            no_auto_extract_glossary=True,
        ),
        pdf=PDFSettings(
            no_mono=False,
            no_dual=False,
            watermark_output_mode="no_watermark",
            translate_table_text=False,
            figure_table_protection_threshold=0.95,
            enhance_compatibility=True,
            split_short_lines=True,
        ),
        translate_engine_settings=OpenAICompatibleSettings(
            openai_compatible_model=model,
            openai_compatible_base_url=base_url,
            openai_compatible_api_key=api_key,
            openai_compatible_timeout=os.getenv("PDF2ZH_TIMEOUT", "180"),
            openai_compatible_temperature=os.getenv("PDF2ZH_TEMPERATURE", "0.2"),
            openai_compatible_send_temperature=True,
            openai_compatible_enable_json_mode=True,
        ),
    )


async def generate_layout_pdf_job(job_id: str) -> None:
    output_dir = job_dir(job_id) / "layout"
    output_dir.mkdir(parents=True, exist_ok=True)
    source_pdf = job_dir(job_id) / "source.pdf"

    set_layout_pdf_state(
        job_id,
        status="running",
        progress=0,
        stage="准备 BabelDOC 布局管线",
        error="",
        updated_at=now_iso(),
    )

    try:
        from pdf2zh_next.high_level import do_translate_async_stream

        settings = build_layout_settings(output_dir)
        settings.validate_settings()
        settings.basic.input_files = set()

        async for event in do_translate_async_stream(settings, source_pdf):
            event_type = event.get("type")
            if event_type in {"progress_start", "progress_update", "progress_end"}:
                progress = float(event.get("overall_progress") or 0)
                stage = str(event.get("stage") or "处理中")
                part_index = event.get("part_index")
                total_parts = event.get("total_parts")
                if part_index and total_parts:
                    stage = f"{stage} ({part_index}/{total_parts})"
                set_layout_pdf_state(
                    job_id,
                    status="running",
                    progress=round(max(0, min(100, progress)), 1),
                    stage=stage,
                    updated_at=now_iso(),
                )
            elif event_type == "error":
                message = str(event.get("error") or "BabelDOC 生成失败")
                set_layout_pdf_state(
                    job_id,
                    status="error",
                    error=message,
                    stage=str(event.get("error_type") or "错误"),
                    updated_at=now_iso(),
                )
                raise RuntimeError(message)
            elif event_type == "finish":
                result = event["translate_result"]
                dual_source = getattr(result, "no_watermark_dual_pdf_path", None) or getattr(result, "dual_pdf_path", None)
                mono_source = getattr(result, "no_watermark_mono_pdf_path", None) or getattr(result, "mono_pdf_path", None)
                dual_pdf = copy_pdf_output(dual_source, stable_layout_pdf_path(job_id, "dual"))
                mono_pdf = copy_pdf_output(mono_source, stable_layout_pdf_path(job_id, "mono"))
                if not dual_pdf:
                    raise RuntimeError("BabelDOC 没有生成双语 PDF")
                set_layout_pdf_state(
                    job_id,
                    status="done",
                    progress=100,
                    stage="双语 PDF 已生成",
                    error="",
                    dual_pdf=dual_pdf,
                    mono_pdf=mono_pdf,
                    updated_at=now_iso(),
                )
                return

        raise RuntimeError("BabelDOC 任务结束但没有返回完成事件")
    except Exception as exc:
        state = set_layout_pdf_state(
            job_id,
            status="error",
            error=str(exc),
            stage="生成失败",
            updated_at=now_iso(),
        )
        if not state.get("dual_pdf"):
            for kind in ("dual", "mono"):
                stable_layout_pdf_path(job_id, kind).unlink(missing_ok=True)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    return {
        "base_url": os.getenv("LLM_BASE_URL", "https://token-plan-cn.xiaomimimo.com/v1"),
        "model": os.getenv("LLM_MODEL", "mimo-v2.5-pro"),
        "has_key": bool(os.getenv("LLM_API_KEY", "").strip()),
    }


@app.post("/api/jobs")
async def create_job(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    auto_translate: bool = Form(True),
) -> dict[str, str]:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="请上传 PDF 文件")

    job_id = str(uuid.uuid4())
    out_dir = JOBS_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / "source.pdf"

    size = 0
    with pdf_path.open("wb") as handle:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_FILE_SIZE:
                shutil.rmtree(out_dir, ignore_errors=True)
                raise HTTPException(status_code=413, detail="PDF 不能超过 80MB")
            handle.write(chunk)

    try:
        pages, blocks = extract_pdf(pdf_path, out_dir)
    except Exception as exc:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"PDF 解析失败：{exc}") from exc

    meta = {
        "id": job_id,
        "filename": file.filename,
        "status": "queued" if auto_translate else "ready",
        "error": "",
        "pages": pages,
        "blocks": [asdict(block) for block in blocks],
        "active_pages": [],
        "progress": {"done": 0, "total": len(blocks)},
        "layout_pdf": default_layout_pdf_state(),
    }
    meta["page_status"] = page_status(meta["blocks"], pages)
    write_metadata(job_id, meta)
    if auto_translate:
        background_tasks.add_task(translate_job, job_id)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    return read_metadata(job_id)


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str, background_tasks: BackgroundTasks) -> dict[str, str]:
    meta = read_metadata(job_id)
    for block in meta["blocks"]:
        block["translation"] = ""
    meta["status"] = "queued"
    meta["error"] = ""
    meta["progress"] = {"done": 0, "total": len(meta["blocks"])}
    write_metadata(job_id, meta)
    background_tasks.add_task(translate_job, job_id)
    return {"status": "queued"}


@app.post("/api/jobs/{job_id}/translate")
async def translate_pages(
    job_id: str,
    background_tasks: BackgroundTasks,
    payload: dict[str, Any] | None = Body(default=None),
) -> dict[str, str]:
    meta = read_metadata(job_id)
    if meta["status"] in {"queued", "translating"}:
        raise HTTPException(status_code=409, detail="已有翻译任务正在运行")

    requested_pages = None
    if payload and payload.get("pages"):
        requested_pages = [int(page) for page in payload["pages"]]
        available = {page["page"] for page in meta["pages"]}
        if any(page not in available for page in requested_pages):
            raise HTTPException(status_code=400, detail="页码超出范围")

    meta["status"] = "queued"
    meta["error"] = ""
    meta["progress"] = {"done": 0, "total": 0}
    write_metadata(job_id, meta)
    background_tasks.add_task(translate_job, job_id, requested_pages)
    return {"status": "queued"}


@app.post("/api/jobs/{job_id}/layout-pdf")
async def start_layout_pdf(job_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
    meta = read_metadata(job_id)
    layout_state = default_layout_pdf_state()
    layout_state.update(meta.get("layout_pdf") or {})
    if layout_state["status"] in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="布局双语 PDF 正在生成")

    for kind in ("dual", "mono"):
        stable_layout_pdf_path(job_id, kind).unlink(missing_ok=True)
    meta["layout_pdf"] = {
        **default_layout_pdf_state(),
        "status": "queued",
        "stage": "等待启动 BabelDOC",
        "updated_at": now_iso(),
    }
    write_metadata(job_id, meta)
    background_tasks.add_task(generate_layout_pdf_job, job_id)
    return meta["layout_pdf"]


@app.get("/api/jobs/{job_id}/layout-pdf")
async def get_layout_pdf_status(job_id: str) -> dict[str, Any]:
    meta = read_metadata(job_id)
    state = default_layout_pdf_state()
    state.update(meta.get("layout_pdf") or {})
    return state


@app.get("/api/jobs/{job_id}/layout-pdf/{kind}")
async def get_layout_pdf(job_id: str, kind: str) -> FileResponse:
    path = stable_layout_pdf_path(job_id, kind)
    if not path.exists():
        raise HTTPException(status_code=404, detail="PDF 尚未生成")
    filename = f"{read_metadata(job_id)['filename'].removesuffix('.pdf')}.{kind}.pdf"
    return FileResponse(path, media_type="application/pdf", filename=filename, content_disposition_type="inline")


@app.get("/api/jobs/{job_id}/logs")
async def get_job_logs(job_id: str) -> PlainTextResponse:
    path = job_dir(job_id) / "translation.log"
    if not path.exists():
        return PlainTextResponse("暂无翻译调用日志\n")
    return PlainTextResponse(path.read_text(encoding="utf-8"))


@app.get("/api/jobs/{job_id}/pages/{image_name}")
async def get_page_image(job_id: str, image_name: str) -> FileResponse:
    if not re.fullmatch(r"page-\d+\.png", image_name):
        raise HTTPException(status_code=404, detail="页面不存在")
    path = job_dir(job_id) / "pages" / image_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="页面不存在")
    return FileResponse(path)
