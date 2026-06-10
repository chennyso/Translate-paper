from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import fitz
import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
STORAGE_DIR = ROOT / "storage"
JOBS_DIR = STORAGE_DIR / "jobs"
STATIC_DIR = ROOT / "web"
MAX_FILE_SIZE = 80 * 1024 * 1024

JOBS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Translate Paper", version="1.0.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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
            clean = normalize_text(str(text))
            if is_noise(clean):
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
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        raise ValueError("模型没有返回 JSON")
    return json.loads(match.group(0))


async def translate_chunk(client: httpx.AsyncClient, blocks: list[dict[str, Any]]) -> dict[str, str]:
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
    response = await client.post(endpoint, headers=headers, json=payload, timeout=120)
    if response.status_code >= 400 and "response_format" in response.text:
        payload.pop("response_format", None)
        response = await client.post(endpoint, headers=headers, json=payload, timeout=120)
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    parsed = parse_json_object(content)
    translations = parsed.get("translations", parsed)
    return {str(key): str(value).strip() for key, value in translations.items()}


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
                translations = await translate_chunk(client, chunk)
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
    except Exception as exc:
        meta = read_metadata(job_id)
        meta["status"] = "error"
        meta["error"] = str(exc)
        meta["active_pages"] = []
        write_metadata(job_id, meta)


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


@app.get("/api/jobs/{job_id}/pages/{image_name}")
async def get_page_image(job_id: str, image_name: str) -> FileResponse:
    if not re.fullmatch(r"page-\d+\.png", image_name):
        raise HTTPException(status_code=404, detail="页面不存在")
    path = job_dir(job_id) / "pages" / image_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="页面不存在")
    return FileResponse(path)
