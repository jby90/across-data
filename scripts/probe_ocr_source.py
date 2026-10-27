"""Bounded local OCR proposals with exact image/word boxes; never activate text.

Two page segmentation modes are compared without spelling correction, inferred
letters, watermark erasure or models. Agreement is not legal/human approval.
"""

import argparse
import csv
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pypdfium2 as pdfium

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.ingestion.archive import ArchiveStore


def agreement(first, second):
    return bool(first.strip()) and "".join(first.split()) == "".join(second.split())


def run(args):
    if args.output.exists():
        raise ValueError("refuse to overwrite old probe")
    blob = ArchiveStore(args.archive_root).read(
        args.content_hash[:2] + "/" + args.content_hash
    )
    weights = args.tessdata / (args.language + ".traineddata")
    if hashlib.sha256(weights.read_bytes()).hexdigest() != args.weights_sha256:
        raise ValueError("OCR weights hash mismatch")
    args.output.mkdir(parents=True, exist_ok=False)
    pdf = pdfium.PdfDocument(blob)
    if len(pdf) > args.max_pages:
        raise ValueError("document exceeds bounded probe, not silently truncated")
    started = time.perf_counter()
    result = {
        "source_sha256": args.content_hash,
        "dpi": args.dpi,
        "engine": "tesseract-lstm",
        "weights_sha256": args.weights_sha256,
        "language": args.language,
        "pages": [],
        "model_calls": 0,
        "active": False,
        "review_status": "pending_review",
        "usable_as_legal_basis": False,
        "human_reviewed": False,
    }
    try:
        for n in range(len(pdf)):
            page = pdf[n]
            bitmap = page.render(scale=args.dpi / 72)
            image = bitmap.to_pil().convert("L")
            image_path = args.output / f"page-{n + 1}.png"
            image.save(image_path)
            bitmap.close()
            page.close()
            item = {
                "page_number": n + 1,
                "image_sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
                "width": image.width,
                "height": image.height,
                "attempts": [],
            }
            for mode in [3, 6]:
                output = args.output / f"page-{n + 1}-psm{mode}"
                env = dict(os.environ, TESSDATA_PREFIX=str(args.tessdata))
                call = subprocess.run(
                    [
                        str(args.tesseract),
                        str(image_path),
                        str(output),
                        "-l",
                        args.language,
                        "--oem",
                        "1",
                        "--psm",
                        str(mode),
                        "-c",
                        "tessedit_create_txt=1",
                        "-c",
                        "tessedit_create_tsv=1",
                    ],
                    env=env,
                    capture_output=True,
                    timeout=120,
                    check=False,
                )
                if call.returncode:
                    raise RuntimeError("OCR process failed on page " + str(n + 1))
                text = output.with_suffix(".txt").read_text(encoding="utf-8")
                with output.with_suffix(".tsv").open(encoding="utf-8", newline="") as f:
                    words = [
                        r
                        for r in csv.DictReader(f, delimiter="\t")
                        if r.get("text", "").strip()
                    ]
                confidences = [float(r["conf"]) for r in words if float(r["conf"]) >= 0]
                item["attempts"].append(
                    {
                        "psm": mode,
                        "text": text,
                        "words": words,
                        "mean_confidence": round(statistics.fmean(confidences), 2)
                        if confidences
                        else 0,
                    }
                )
            item["exact_nonwhitespace_agreement"] = agreement(
                *[r["text"] for r in item["attempts"]]
            )
            result["pages"].append(item)
            print(
                json.dumps(
                    {"page": n + 1, "agreement": item["exact_nonwhitespace_agreement"]}
                ),
                flush=True,
            )
    finally:
        pdf.close()
    result["elapsed_seconds"] = round(time.perf_counter() - started, 2)
    (args.output / "proposal.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "pages": len(result["pages"]),
                "exact_agreement_pages": sum(
                    r["exact_nonwhitespace_agreement"] for r in result["pages"]
                ),
                "model_calls": 0,
                "active": False,
            }
        )
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive-root", type=Path, required=True)
    p.add_argument("--content-hash", required=True)
    p.add_argument("--tesseract", type=Path, required=True)
    p.add_argument("--tessdata", type=Path, required=True)
    p.add_argument("--weights-sha256", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--language", default="ara")
    p.add_argument("--dpi", type=int, default=300)
    p.add_argument("--max-pages", type=int, default=40)
    run(p.parse_args())
