"""可复跑构建：双音轨母带 + 可发放盲包 + 密封 truth.json + 复盘手册。

用法：
    .venv/bin/python training/build_blind_lab.py
"""
from __future__ import annotations

from array import array
import hashlib
import json
import shutil
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import av

from . import docs, model, page, render


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def audio_indices(path: Path) -> list[int]:
    with av.open(str(path)) as container:
        return sorted(s.index for s in container.streams if s.type == "audio")


def blind_roles(item: model.Item) -> dict[str, str]:
    """返回 {盲标签 jia/yi: 物理角色 main/ref}。"""
    if item.correct_label == "jia":
        return {"jia": "main", "yi": "ref"}
    if item.correct_label == "yi":
        return {"jia": "ref", "yi": "main"}
    # ambiguous：按 main_first 做一个确定但平衡的密封映射（盲材字节相同）。
    return {"jia": "main", "yi": "ref"} if item.main_first else {"jia": "ref", "yi": "main"}


def build() -> dict:
    for d in (model.BLIND, model.ASSETS, model.DEBRIEF, model.MASTERS):
        d.mkdir(parents=True, exist_ok=True)
    if model.ASSETS.exists():
        shutil.rmtree(model.ASSETS)
    model.ASSETS.mkdir(parents=True)
    if model.MASTERS.exists():
        shutil.rmtree(model.MASTERS)
    model.MASTERS.mkdir(parents=True)
    if model.BLIND_ZIP.exists():
        model.BLIND_ZIP.unlink()

    view: dict[str, dict] = {}
    truth_items: list[dict] = []

    for item in model.ITEMS:
        item_dir = model.ASSETS / item.bid
        frames_dir = item_dir / "frames"
        frames_dir.mkdir(parents=True)

        frames = render.all_frames(item)
        mono_main = render.build_mono(item.correct)
        mono_ref = render.build_mono(item.wrong)
        role_mono = {"main": mono_main, "ref": mono_ref}

        label_to_role = blind_roles(item)
        role_to_label = {role: label for label, role in label_to_role.items()}

        main_hole = item.main_hole
        role_hole = {"main": main_hole, "ref": model.plateau_other(main_hole)}

        # 物理添加顺序（刻意在条目间交替，防止“音轨号小=主混”成为规律）。
        role_order = ["main", "ref"] if item.main_first else ["ref", "main"]
        audio_tracks = [(role_mono[r], role_hole[r]) for r in role_order]

        master_rel = Path("debrief") / "masters" / f"{item.bid}_{item.code}_dual.mkv"
        master_path = model.TRAIN / master_rel
        render.mux_master(master_path, item, audio_tracks)
        indices = audio_indices(master_path)
        if len(indices) != 2:
            raise RuntimeError(f"{item.bid}: expected 2 audio streams, got {indices}")
        # audio 流按 index 排序后的顺序 == mux 添加顺序。
        role_to_stream = {role_order[k]: indices[k] for k in range(2)}

        # 盲候选 WAV：始终写“补零后的完整长度”单声道，空洞平台本来就是零。
        wav_bytes: dict[str, bytes] = {}
        wav_hash: dict[str, str] = {}
        for label in ("jia", "yi"):
            mono = role_mono[label_to_role[label]]
            payload = render.wav_mono(mono)
            (item_dir / f"{label}.wav").write_bytes(payload)
            wav_bytes[label] = payload
            wav_hash[label] = sha256_bytes(payload)

        # 画格 + 胶片条 + 无音轨 MP4。
        for fi, frame in enumerate(frames, start=1):
            (frames_dir / f"frame_{fi:04d}.png").write_bytes(render.png_gray(
                model.WIDTH, model.HEIGHT, frame, level=6))
        strip, cols, rows = render.filmstrip(frames)
        (item_dir / "filmstrip.png").write_bytes(strip)
        render.encode_picture_mp4(item_dir / "picture.mp4", frames)

        svg = {label: render.waveform_svg(role_mono[label_to_role[label]])
               for label in ("jia", "yi")}

        view[item.bid] = {
            "fps": model.FPS, "frames": model.FRAME_COUNT,
            "cols": cols, "rows": rows, "w": model.WIDTH, "h": model.HEIGHT,
            "duration": model.DURATION_S,
            "film": render.data_uri("image/png", strip),
            "jia": render.data_uri("audio/wav", wav_bytes["jia"]),
            "yi": render.data_uri("audio/wav", wav_bytes["yi"]),
            "svg_jia": svg["jia"], "svg_yi": svg["yi"],
        }

        truth_items.append({
            "bid": item.bid,
            "code": item.code,
            "group": item.group,
            "scene": item.scene,
            "duration_s": model.DURATION_S,
            "fps": model.FPS,
            "width": model.WIDTH,
            "height": model.HEIGHT,
            "rate": model.RATE,
            "channels_master": model.CHANNELS,
            "total_samples_per_channel": model.TOTAL_SAMPLES,
            "frame_count": model.FRAME_COUNT,
            "shared_stream_name": model.SHARED_STREAM_NAME,
            "master_rel": master_rel.as_posix(),
            "master_sha256": sha256_file(master_path),
            "master_size": master_path.stat().st_size,
            "physical_audio_indices": indices,
            "role_order": role_order,
            "role_to_stream": role_to_stream,
            "stream_to_role": {str(v): k for k, v in role_to_stream.items()},
            "blind_to_role": label_to_role,
            "role_to_blind": role_to_label,
            "correct_label": item.correct_label,
            "expected_verdict": ("cannot_confirm" if item.correct_label is None
                                 else f"{item.correct_label}_is_synced_main"),
            "main_hole": main_hole,
            "role_hole": role_hole,
            "plateau_us": {k: [int(a * 1e6), int(b * 1e6)]
                           for k, (a, b) in model.PLATEAUS.items()},
            "actions": [{"t_s": h.t, "t_us": int(round(h.t * 1e6)),
                         "station": h.station, "freq": h.freq,
                         "shape": model.STATIONS[h.station]["shape"],
                         "expected_sound": model.STATIONS[h.station]["sound"]}
                        for h in item.correct],
            "wrong_onsets": [{"t_s": h.t, "t_us": int(round(h.t * 1e6)),
                              "station": h.station} for h in item.wrong],
            "sync_tol_us": model.SYNC_TOL_US,
            "assets": {
                "dir_rel": (Path("blind_pack") / "assets" / item.bid).as_posix(),
                "jia_wav_rel": (Path("blind_pack") / "assets" / item.bid / "jia.wav").as_posix(),
                "yi_wav_rel": (Path("blind_pack") / "assets" / item.bid / "yi.wav").as_posix(),
                "jia_sha256": wav_hash["jia"],
                "yi_sha256": wav_hash["yi"],
                "wav_bytes_equal": wav_hash["jia"] == wav_hash["yi"],
                "picture_mp4_rel": (Path("blind_pack") / "assets" / item.bid / "picture.mp4").as_posix(),
                "filmstrip_rel": (Path("blind_pack") / "assets" / item.bid / "filmstrip.png").as_posix(),
                "frame_count_on_disk": len(list(frames_dir.glob("frame_*.png"))),
            },
        })

    truth = {
        "schema": "slatewatch-blind-lab-truth/1",
        "warning": "ANSWER KEY / HOST-ONLY. Never distribute blind_pack with this file.",
        "shared_stream_name": model.SHARED_STREAM_NAME,
        "constants": {
            "rate": model.RATE, "channels": model.CHANNELS, "fps": model.FPS,
            "width": model.WIDTH, "height": model.HEIGHT,
            "duration_s": model.DURATION_S,
            "total_samples_per_channel": model.TOTAL_SAMPLES,
            "plateaus_s": model.PLATEAUS,
            "sync_tol_us": model.SYNC_TOL_US,
        },
        "items": truth_items,
    }
    model.TRUTH_JSON.write_text(json.dumps(truth, ensure_ascii=False, indent=2),
                               encoding="utf-8")

    # 盲包文档与页面（全部由“参训者视图”生成，不含 truth）。
    model.BLIND_HTML.write_text(page.practice_html(view), encoding="utf-8")
    (model.BLIND / "答题卡.html").write_text(page.answer_sheet_html(), encoding="utf-8")
    (model.BLIND / "参训者说明.md").write_text(docs.trainee_readme(), encoding="utf-8")
    (model.BLIND / "制作记录.md").write_text(docs.production_record_md(), encoding="utf-8")
    (model.BLIND / "答题卡.md").write_text(docs.answer_sheet_md(), encoding="utf-8")

    model.DEBRIEF_DOC.write_text(docs.debrief_manual_md(truth), encoding="utf-8")

    _make_zip()
    return truth


def _make_zip() -> None:
    root = model.BLIND
    prefix = "Slatewatch-blind-lab"
    with ZipFile(model.BLIND_ZIP, "w", ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_dir():
                continue
            if path.resolve() == model.BLIND_ZIP.resolve():
                continue
            if path.name == ".DS_Store":
                continue
            arc = Path(prefix) / path.relative_to(root)
            zf.write(path, arc.as_posix())


def main() -> None:
    truth = build()
    n = len(truth["items"])
    same = sum(1 for i in truth["items"] if i["assets"]["wav_bytes_equal"])
    print(f"[build] {n} items -> {model.BLIND}")
    print(f"[build] masters -> {model.MASTERS}")
    print(f"[build] byte-identical candidate pairs: {same} (expected 2 ambiguous)")
    print(f"[build] truth -> {model.TRUTH_JSON}")
    print(f"[build] blind zip -> {model.BLIND_ZIP} "
          f"({model.BLIND_ZIP.stat().st_size / 1024:.0f} KiB)")


if __name__ == "__main__":
    main()
