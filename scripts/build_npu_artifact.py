"""Furiosa-LLM artifact builder for Qwen3 Embedding (RNGD NPU).

PVC `/artifacts/qwen3-embed-0.6b/` 디렉토리 비어 있으면 HuggingFace에서 받아
embed task로 컴파일 → ENF 저장. 이미 존재하면 skip.
"""
import argparse
import logging
import os
import time
from pathlib import Path

from furiosa_llm.artifact import ArtifactBuilder
from furiosa_llm.artifact.types.config import ModelConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("build_npu_artifact")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="Qwen/Qwen3-Embedding-0.6B",
                        help="HuggingFace model id")
    parser.add_argument("--name", default="qwen3-embed-0.6b")
    parser.add_argument("--save-dir", default=os.getenv("ARTIFACT_DIR",
                        "/artifacts/qwen3-embed-0.6b"))
    parser.add_argument("--pipeline-workers", type=int, default=4)
    parser.add_argument("--compile-workers", type=int, default=8)
    parser.add_argument("--force", action="store_true",
                        help="기존 산출물 무시하고 재빌드")
    args = parser.parse_args()

    target = Path(args.save_dir)
    if target.exists() and any(target.iterdir()) and not args.force:
        logger.info("artifact already exists at %s — skip", target)
        return 0

    target.mkdir(parents=True, exist_ok=True)
    logger.info("building %s → %s (task=embed)", args.model, target)
    t0 = time.time()

    builder = ArtifactBuilder(
        model_id_or_path=args.model,
        name=args.name,
        model_config=ModelConfig(task="embed"),
    )
    builder.build(
        save_dir=str(target),
        num_pipeline_builder_workers=args.pipeline_workers,
        num_compile_workers=args.compile_workers,
    )

    logger.info("done in %.1fs — %s", time.time() - t0, target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
