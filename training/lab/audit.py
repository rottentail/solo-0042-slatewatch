"""独立核查 + 真实扫描配对 + 反泄漏审计。

入口：``.venv/bin/python training/verify_truth.py``

核查分三块：
1. 用 :mod:`lab.probe` 从母带 / 盲包字节独立重算覆盖、静音归属、起振、白闪；
2. 走与 HTTP 服务相同的 ``slatewatch.service.Slatewatch`` 真实扫描入口，
   对每条物理音轨扫描并配对覆盖 / ``silence`` / ``sound_gap``；
3. 对盲包做反泄漏审计（顺序、开头、全零、长度 + 密钥 / 答案 / 文件卫生）。
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import hashlib
import json
import re
import sys
import tempfile
import zipfile

from . import model, probe

if str(model.REPO) not in sys.path:
    sys.path.insert(0, str(model.REPO))

RECOMPUTED_JSON = model.DEBRIEF / "recomputed_truth.json"

TEXT_SUFFIX = {".html", ".md", ".js", ".css", ".svg", ".json", ".txt"}
FORBIDDEN_NAME = re.compile(
    r"(truth|debrief|pairing|verification|leak|recomputed|master|\.mkv$|\.py$|"
    r"复盘|真值|答案与真值|__pycache__)", re.IGNORECASE)
# 盲包正文中不允许出现的“给指定候选泄底”模式。
ANSWER_PATTERNS = [
    re.compile(r"(正确答案|参考答案|标准答案|密封答案)"),
    re.compile(r"答案\s*[是为：:]\s*[甲乙]"),
    re.compile(r"correct[_a-z]*\s*[:=]\s*[\"']?(jia|yi)", re.IGNORECASE),
    re.compile(r"[\"']correct_label[\"']\s*:"),
    re.compile(r"\b(role_to_stream|blind_to_role|main_hole|stream_to_role)\b"),
]
DATA_URI = re.compile(r"data:[^;\"')]+;base64,[A-Za-z0-9+/=\s]+")
HEX64 = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
REMOTE_URL = re.compile(r"https?://")
# 盲包正文不应回指主持侧路径 / 文件名（即使不含答案，也会提示学员去翻仓库）。
HOST_HINTS = [re.compile(p, re.IGNORECASE) for p in
              (r"\bdebrief\b", r"truth\.json", r"training[\\/]lab",
               r"build_blind_lab", r"verify_truth", r"recompute_truth")]


class Report:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def check(self, cid: str, title: str, ok: bool, detail: str = "",
              severity: str = "critical") -> bool:
        self.checks.append({"id": cid, "title": title, "ok": bool(ok),
                            "severity": severity, "detail": detail})
        return bool(ok)

    @property
    def ok(self) -> bool:
        return all(c["ok"] for c in self.checks if c["severity"] == "critical")


def _near(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


def _plateau_match(run: dict, se: list[int], tol_us: int = 80_000) -> bool:
    return _near(run["start_us"], se[0], tol_us) and _near(run["end_us"], se[1], tol_us)


# ---------------------------------------------------------------- 独立重算
def recompute_all(truth: dict, rep: Report) -> dict:
    observed: dict = {"items": {}}
    for it in truth["items"]:
        bid = it["bid"]
        mp = model.TRAIN / it["master_rel"]
        entry: dict = {"master_rel": it["master_rel"], "audio": {}, "wav": {}}

        if not rep.check(f"master-exists-{bid}", f"{bid} 母带存在", mp.exists(), str(mp)):
            continue

        digest = hashlib.sha256(mp.read_bytes()).hexdigest()
        rep.check(f"master-sha-{bid}", f"{bid} 母带 SHA-256 与底册一致",
                  digest == it["master_sha256"], f"recomputed={digest}")

        tags = probe.probe_stream_tags(mp)
        entry["container_duration_us"] = tags["container_duration_us"]
        audio_streams = [s for s in tags["streams"] if s["type"] == "audio"]
        titles = {s["title"] for s in audio_streams}
        rep.check(f"shared-name-{bid}",
                  f"{bid} 两条音轨流名完全相同且为密封流名",
                  len(audio_streams) == 2
                  and titles == {model.SHARED_STREAM_NAME, it["shared_stream_name"]}
                  and model.SHARED_STREAM_NAME == it["shared_stream_name"],
                  f"titles={[s['title'] for s in audio_streams]}")
        entry["streams"] = tags["streams"]

        indices = sorted(s["index"] for s in audio_streams)
        rep.check(f"indices-{bid}", f"{bid} 物理音频流号与底册一致",
                  indices == sorted(it["physical_audio_indices"]),
                  f"observed={indices} truth={it['physical_audio_indices']}")

        for idx in indices:
            o = probe.probe_master_audio(mp, idx)
            entry["audio"][str(idx)] = {
                "rate": o.rate, "channels": o.channels,
                "first_sample": o.first_sample, "sample_count": o.sample_count,
                "coverage_us": o.coverage_us, "gaps_us": o.gaps_us,
                "zero_runs": o.zero_runs, "onsets_s": o.onsets_s,
                "leading_zero_samples": o.leading_zero_samples, "peak": o.peak,
            }
            role = it["stream_to_role"][str(idx)]
            hole = it["role_hole"][role]
            hole_se = it["plateau_us"][hole]
            other = model.plateau_other(hole)
            other_se = it["plateau_us"][other]
            unc = [r for r in o.zero_runs if r["kind"] == "uncovered"]
            rec = [r for r in o.zero_runs if r["kind"] == "recorded_silence"]
            rep.check(f"hole-{bid}-s{idx}",
                      f"{bid} 流{idx}({role}) 独立判定无覆盖平台={hole}",
                      any(_plateau_match(r, hole_se) for r in unc),
                      f"uncovered={[(r['start_us'], r['end_us']) for r in unc]}")
            rep.check(f"realsil-{bid}-s{idx}",
                      f"{bid} 流{idx}({role}) 独立判定实录静音平台={other}",
                      any(_plateau_match(r, other_se) for r in rec),
                      f"recorded={[(r['start_us'], r['end_us']) for r in rec]}")
            rep.check(f"startzero-{bid}-s{idx}",
                      f"{bid} 流{idx} 起点为 0 且无前置偏移",
                      o.first_sample == 0 and o.leading_zero_samples == 0,
                      f"first={o.first_sample} leading0={o.leading_zero_samples}")

        flashes = probe.probe_master_flashes(mp)
        entry["flashes"] = flashes
        target_us = [a["t_us"] for a in it["actions"]]
        flash_times = [f["time_us"] for f in flashes]
        flash_ok = len(flashes) == len(target_us) and all(
            any(_near(ft, tu, 60_000) for ft in flash_times) for tu in target_us)
        rep.check(f"flash-{bid}", f"{bid} 母带白闪独立重算与制作记录一致",
                  flash_ok, f"flashes={flash_times} target={target_us}")

        wav_obs = {}
        wav_broken = False
        for label in ("jia", "yi"):
            wp = model.TRAIN / it["assets"][f"{label}_wav_rel"]
            actual_sha = hashlib.sha256(wp.read_bytes()).hexdigest() if wp.exists() else None
            rep.check(f"wav-sha-{bid}-{label}", f"{bid} {label}.wav 与构建底册一致",
                      actual_sha == it["assets"][f"{label}_sha256"],
                      (actual_sha or "missing")[:16])
            try:
                w = probe.probe_wav(wp)
                rep.check(f"wav-readable-{bid}-{label}", f"{bid} {label}.wav 可完整解码",
                          w["decoded_frames"] == w["nframes"],
                          f"decoded={w['decoded_frames']} header={w['nframes']}")
            except (OSError, ValueError, EOFError) as exc:
                rep.check(f"wav-readable-{bid}-{label}", f"{bid} {label}.wav 可完整解码",
                          False, repr(exc))
                w = None
                wav_broken = True
            wav_obs[label] = w
            if w is not None:
                entry["wav"][label] = {k: w[k] for k in
                                       ("channels", "sampwidth", "rate", "nframes",
                                        "decoded_frames", "duration_s", "onsets_s",
                                        "leading_zero_samples", "peak", "sha256",
                                        "size", "zero_runs")}

        if wav_broken or wav_obs["jia"] is None or wav_obs["yi"] is None:
            continue
        j, y = wav_obs["jia"], wav_obs["yi"]
        rep.check(
            f"wav-format-{bid}", f"{bid} 甲/乙格式、样本数、字节大小完全相同",
            (j["channels"], j["sampwidth"], j["rate"], j["nframes"], j["size"])
            == (y["channels"], y["sampwidth"], y["rate"], y["nframes"], y["size"])
            and j["nframes"] == it["total_samples_per_channel"],
            f"jia=({j['channels']},{j['rate']},{j['nframes']},{j['size']}) "
            f"yi=({y['channels']},{y['rate']},{y['nframes']},{y['size']})")
        rep.check(f"wav-notzero-{bid}", f"{bid} 两条试听都不是全零文件",
                  j["peak"] > 3000 and y["peak"] > 3000,
                  f"peaks={j['peak']},{y['peak']}")
        rep.check(f"leading-tie-{bid}", f"{bid} 甲/乙开头前导零样本数相同",
                  j["leading_zero_samples"] == y["leading_zero_samples"],
                  f"{j['leading_zero_samples']} vs {y['leading_zero_samples']}")

        jo, yo = j["onsets_s"], y["onsets_s"]
        first_delta = abs(jo[0] - yo[0]) if jo and yo else 9.9
        rep.check(f"first-onset-tie-{bid}",
                  f"{bid} 甲/乙第一个起振时刻相同（不能只靠开头对齐取轨）",
                  first_delta <= 0.02, f"first={jo[:1]} vs {yo[:1]} Δ={first_delta:.3f}s")

        jr = [(r["start_us"], r["end_us"]) for r in j["zero_runs"]]
        yr = [(r["start_us"], r["end_us"]) for r in y["zero_runs"]]
        runs_same = len(jr) == len(yr) == 2 and all(
            _near(a[0], b[0], 2000) and _near(a[1], b[1], 2000)
            for a, b in zip(jr, yr))
        rep.check(f"zeroruns-tie-{bid}",
                  f"{bid} 甲/乙长全零平台位置与长度相同（全零波形不泄漏选轨）",
                  runs_same, f"jia={jr} yi={yr}")

        tol = it["sync_tol_us"]
        mj, ej = probe.match_count(jo, flash_times, tol)
        my, ey = probe.match_count(yo, flash_times, tol)
        k = len(flash_times)
        entry["sync_score"] = {"targets": k, "jia_matches": mj, "yi_matches": my,
                               "jia_max_err_us": ej, "yi_max_err_us": ey}
        if it["group"] == "sync":
            derived = ("jia" if (mj == k and my < k)
                       else "yi" if (my == k and mj < k) else None)
            weak_other = (my if derived == "jia" else mj) == 1
            err_ok = (ej if derived == "jia" else ey) <= tol
            rep.check(f"verdict-{bid}",
                      f"{bid} 独立声画重算唯一锁定 {it['correct_label']} 且与密封答案一致",
                      derived == it["correct_label"] and weak_other and err_ok,
                      f"jia={mj}/{k} yi={my}/{k} derived={derived} "
                      f"truth={it['correct_label']} other_matches_only_first={weak_other}")
        else:
            identical = j["sha256"] == y["sha256"]
            rep.check(f"ambiguous-{bid}",
                      f"{bid} 甲/乙字节相同且声画匹配相同 → 盲材必须判无法确认",
                      identical and mj == my == k and it["correct_label"] is None,
                      f"sha_equal={identical} matches jia={mj} yi={my}")

        fs = probe.probe_filmstrip_flashes(
            model.TRAIN / it["assets"]["filmstrip_rel"], 16,
            it["width"], it["height"])
        rep.check(f"film-{bid}", f"{bid} 盲包胶片条白闪时点与母带一致",
                  len(fs) == k and all(any(_near(f, t / 1e6, 0.06) for t in flash_times)
                                       for f in fs),
                  f"film={fs}")
        mp4 = probe.probe_mp4_basic(model.TRAIN / it["assets"]["picture_mp4_rel"])
        rep.check(f"mp4-{bid}", f"{bid} picture.mp4 可解码且为等长画面",
                  mp4["frames_decoded"] >= it["frame_count"] - 1
                  and bool(mp4["container_duration_us"])
                  and _near(mp4["container_duration_us"], int(it["duration_s"] * 1e6), 120_000),
                  str(mp4), severity="check")

        observed["items"][bid] = entry

    sync_items = [it for it in truth["items"] if it["group"] == "sync"]
    labels = Counter(it["correct_label"] for it in sync_items)
    rep.check("balance-labels",
              "声画组密封答案在甲/乙间平衡（不存在‘总选第一个’规律）",
              labels.get("jia") == 2 and labels.get("yi") == 2, dict(labels))
    main_indices = Counter(str(it["role_to_stream"]["main"]) for it in sync_items)
    rep.check("balance-stream-index",
              "主混物理流号在 1/2 间平衡（不存在‘流号小=主混’规律）",
              set(main_indices) == {"1", "2"} and len(set(main_indices.values())) == 1,
              dict(main_indices))
    combos = {(it["correct_label"], it["role_to_stream"]["main"]) for it in sync_items}
    rep.check("balance-cross", "盲标签与物理流号无固定共现关系",
              len(combos) == 4, str(sorted(combos)))
    return observed


# ---------------------------------------------------------------- 真实扫描
def service_scans(truth: dict, rep: Report, observed: dict) -> dict:
    from slatewatch.service import Slatewatch

    pairing = {"policy_label": "盲标复盘-默认规则", "items": {}}
    with tempfile.TemporaryDirectory(prefix="slatewatch-blind-verify-") as tmp:
        svc = Slatewatch(Path(tmp))
        policy = svc.policy({"label": pairing["policy_label"]})
        pairing["policy_id"] = policy["id"]
        for it in truth["items"]:
            data = (model.TRAIN / it["master_rel"]).read_bytes()
            streams = {}
            for idx in it["physical_audio_indices"]:
                master = svc.master(data, f"{it['bid']}-audio-{idx}", None, int(idx))
                scan = svc.scan({"master_id": master["id"], "policy_id": policy["id"]})
                streams[str(idx)] = {
                    "master_id": master["id"], "scan_id": scan["id"],
                    "coverage_sound": scan["coverage"]["sound"],
                    "sound_gaps": [{"id": h["id"], "start_us": h["start_us"],
                                    "end_us": h["end_us"]}
                                   for h in scan["hits"] if h["kind"] == "sound_gap"],
                    "silences": [{"id": h["id"], "channel": h.get("channel"),
                                  "start_us": h["start_us"], "end_us": h["end_us"]}
                                 for h in scan["hits"] if h["kind"] == "silence"],
                }
            pairing["items"][it["bid"]] = {"streams": streams}

    for it in truth["items"]:
        bid = it["bid"]
        for idx_s, scan_s in pairing["items"][bid]["streams"].items():
            ind = observed["items"][bid]["audio"][idx_s]
            cov_ok = len(scan_s["coverage_sound"]) == len(ind["coverage_us"]) and all(
                _near(a["start_us"], b["start_us"], 5_000)
                and _near(a["end_us"], b["end_us"], 5_000)
                for a, b in zip(scan_s["coverage_sound"], ind["coverage_us"]))
            rep.check(f"scan-coverage-{bid}-s{idx_s}",
                      f"{bid} 流 {idx_s}：真实扫描覆盖与独立重算一致", cov_ok,
                      f"scan={scan_s['coverage_sound']} ind={ind['coverage_us']}")
            role = it["stream_to_role"][idx_s]
            hole = it["role_hole"][role]
            hole_se = it["plateau_us"][hole]
            other = model.plateau_other(hole)
            other_se = it["plateau_us"][other]
            gap_ok = any(_near(g["start_us"], hole_se[0], 120_000)
                         and _near(g["end_us"], hole_se[1], 120_000)
                         for g in scan_s["sound_gaps"])
            rep.check(f"scan-gap-{bid}-s{idx_s}",
                      f"{bid} 流 {idx_s}：sound_gap 配对无覆盖平台 {hole}", gap_ok,
                      f"gaps={scan_s['sound_gaps']} expect={hole_se}")
            sil_ok = any(_near(s["start_us"], other_se[0], 120_000)
                         and _near(s["end_us"], other_se[1], 120_000)
                         for s in scan_s["silences"])
            rep.check(f"scan-silence-{bid}-s{idx_s}",
                      f"{bid} 流 {idx_s}：silence 配对实录静音平台 {other}", sil_ok,
                      f"silences={scan_s['silences']} expect={other_se}")
    return pairing


# ---------------------------------------------------------------- 反泄漏
def leak_audit(truth: dict, rep: Report) -> dict:
    result: dict = {"files_scanned": [], "content_hits": [], "remote_refs": [],
                    "trivial_cues": [], "classifiers": {}}

    bad_names, text_files = [], []
    for path in model.BLIND.rglob("*"):
        if path.is_dir() or path.name == model.BLIND_ZIP.name:
            continue
        rel = path.relative_to(model.BLIND).as_posix()
        if FORBIDDEN_NAME.search(rel):
            bad_names.append(rel)
        if path.suffix.lower() in TEXT_SUFFIX:
            text_files.append(path)
    rep.check("leak-filenames", "盲包目录无 truth/debrief/母带/脚本/报告等越界文件",
              not bad_names, f"bad={bad_names}")
    result["forbidden_names"] = bad_names

    zip_bad: list[str] = []
    with zipfile.ZipFile(model.BLIND_ZIP) as zf:
        zip_names = zf.namelist()
        for n in zip_names:
            if n.startswith("/") or ".." in Path(n).parts or FORBIDDEN_NAME.search(n):
                zip_bad.append(n)
    rep.check("leak-zip", "盲包 zip 仅含参训者文件，无答案 / 母带 / 越界路径",
              bool(zip_names) and not zip_bad, f"members={len(zip_names)} bad={zip_bad}")
    result["zip_member_count"] = len(zip_names)
    result["zip_bad"] = zip_bad

    secret_values = [it["master_sha256"] for it in truth["items"]]
    hits, remote, host_hits = [], [], []
    for path in text_files:
        rel = path.relative_to(model.BLIND).as_posix()
        raw = path.read_text(encoding="utf-8", errors="replace")
        if REMOTE_URL.search(raw):
            remote.append(rel)
        stripped = DATA_URI.sub("", raw)
        for sv in secret_values:
            if sv in raw:
                hits.append(f"{rel}: contains master sha {sv[:12]}…")
        if model.SHARED_STREAM_NAME in raw:
            hits.append(f"{rel}: contains literal shared stream tag")
        if HEX64.search(stripped):
            hits.append(f"{rel}: contains 64-hex token")
        for pat in ANSWER_PATTERNS:
            m = pat.search(stripped)
            if m:
                hits.append(f"{rel}: /{pat.pattern}/ -> {m.group(0)!r}")
        for pat in HOST_HINTS:
            m = pat.search(stripped)
            if m:
                host_hits.append(f"{rel}: {m.group(0)!r}")
    rep.check("leak-content", "盲包文本无密封哈希 / 真实流名 / 答案赋值 / 真值键",
              not hits, " | ".join(hits[:8]))
    rep.check("leak-host-hints", "盲包文本不回指 debrief / truth / 生成核查脚本等主持路径",
              not host_hits, " | ".join(host_hits[:8]))
    rep.check("leak-no-remote", "盲包页面无 http(s) 外链（离线自包含）",
              not remote, f"remote={remote}")
    result["content_hits"] = hits
    result["host_hint_hits"] = host_hits
    result["remote_refs"] = remote
    result["files_scanned"] = [p.relative_to(model.BLIND).as_posix() for p in text_files]

    html = model.BLIND_HTML.read_text(encoding="utf-8")
    m = re.search(r'<script type="application/json" id="lab-data">(.*?)</script>', html, re.S)
    keys_ok, key_detail = False, "lab-data not found"
    if m:
        data = json.loads(m.group(1))
        allowed = {"fps", "frames", "cols", "rows", "w", "h", "duration",
                   "film", "jia", "yi"}
        top_ok = set(data) == {it["bid"] for it in truth["items"]}
        key_sets = {bid: set(cfg) for bid, cfg in data.items()}
        keys_ok = top_ok and all(k <= allowed for k in key_sets.values())
        key_detail = "per-item keys=" + json.dumps(sorted(next(iter(key_sets.values()))),
                                                   ensure_ascii=False)
    rep.check("leak-viewmodel-keys", "内嵌盲数据仅含媒体 / 时序键，无 role/stream/answer",
              keys_ok, key_detail)

    missing_links = []
    for target in re.findall(r'(?:href|src)="([^"#][^"]*)"', html):
        if target.startswith(("data:", "http:", "https:", "mailto:")):
            continue
        if not (model.BLIND / target.split("#")[0]).exists():
            missing_links.append(target)
    rep.check("blind-links", "index.html 的本地链接目标均在盲包内",
              not missing_links, f"missing={missing_links}")

    matrix = []
    for it in truth["items"]:
        if it["group"] != "sync":
            continue
        j = probe.probe_wav(model.TRAIN / it["assets"]["jia_wav_rel"])
        y = probe.probe_wav(model.TRAIN / it["assets"]["yi_wav_rel"])
        matrix.append({
            "bid": it["bid"], "correct": it["correct_label"],
            "jia_size": j["size"], "yi_size": y["size"],
            "size_tie": j["size"] == y["size"],
            "jia_leading0": j["leading_zero_samples"],
            "yi_leading0": y["leading_zero_samples"],
            "leading_tie": j["leading_zero_samples"] == y["leading_zero_samples"],
            "jia_first_onset": j["onsets_s"][0] if j["onsets_s"] else None,
            "yi_first_onset": y["onsets_s"][0] if y["onsets_s"] else None,
            "first_onset_tie": bool(j["onsets_s"] and y["onsets_s"]
                                    and abs(j["onsets_s"][0] - y["onsets_s"][0]) <= 0.02),
            "zero_platforms_equal": len(j["zero_runs"]) == len(y["zero_runs"]) == 2,
        })
    always_jia = sum(1 for r in matrix if r["correct"] == "jia")
    always_yi = sum(1 for r in matrix if r["correct"] == "yi")
    size_tie = all(r["size_tie"] for r in matrix)
    lead_tie = all(r["leading_tie"] for r in matrix)
    onset_tie = all(r["first_onset_tie"] for r in matrix)
    zero_tie = all(r["zero_platforms_equal"] for r in matrix)
    result["trivial_cues"] = matrix
    result["classifiers"] = {
        "always_pick_first_label_jia": f"{always_jia}/{len(matrix)} (答案甲乙各半)",
        "always_pick_yi": f"{always_yi}/{len(matrix)}",
        "pick_by_smaller_file_size": "all ties -> 无法选择" if size_tie else "LEAK",
        "pick_by_fewer_leading_zeros": "all ties" if lead_tie else "differs",
        "pick_by_earlier_first_onset_(opening_alignment)": "all ties" if onset_tie else "differs",
        "pick_by_all_zero_waveform_platforms": "identical x2 each" if zero_tie else "differs",
    }
    rep.check("cue-size", "文件长度 / 字节大小不能选轨（全部相等）", size_tie)
    rep.check("cue-leading", "开头对齐 / 前导零不能选轨（全部相同）", lead_tie and onset_tie)
    rep.check("cue-zero", "全零波形不能选轨（平台数量 / 位置全部相同）", zero_tie)
    rep.check("cue-order", "固定选第一个候选不能全对（答案甲乙各半）",
              always_jia == always_yi == len(matrix) / 2)
    result["leak_checks_ok"] = all(
        c["ok"] for c in rep.checks if c["id"].startswith("leak") or c["id"].startswith("cue"))
    return result


# ---------------------------------------------------------------- 报告
def write_reports(truth: dict, rep: Report, observed: dict,
                  pairing: dict, leak: dict) -> None:
    model.DEBRIEF.mkdir(parents=True, exist_ok=True)
    RECOMPUTED_JSON.write_text(json.dumps(observed, ensure_ascii=False, indent=2), "utf-8")
    model.PAIRING_JSON.write_text(json.dumps(pairing, ensure_ascii=False, indent=2), "utf-8")
    verdict = {"schema": "slatewatch-blind-verify/1", "ok": rep.ok,
               "total_checks": len(rep.checks),
               "passed": sum(c["ok"] for c in rep.checks),
               "critical_failed": [c for c in rep.checks if not c["ok"]
                                   and c["severity"] == "critical"],
               "checks": rep.checks}
    model.VERIFY_JSON.write_text(json.dumps(verdict, ensure_ascii=False, indent=2), "utf-8")
    model.LEAK_JSON.write_text(json.dumps(leak, ensure_ascii=False, indent=2), "utf-8")

    md = ["# 独立核查报告（复盘材料）", "",
          f"**总体结论：{'✅ 全部关键核查通过' if rep.ok else '❌ 存在未通过的关键核查'}**", "",
          f"通过 {verdict['passed']}/{verdict['total_checks']} 项。", "",
          "真值从母带 / 盲包字节独立重算（`recomputed_truth.json`），真实 Slatewatch ",
          "扫描配对见 `scan_pairing.json`。逐项结果：", "",
          "| 结果 | 核查项 | 说明 |", "| --- | --- | --- |"]
    for c in rep.checks:
        md.append(f"| {'✅' if c['ok'] else '❌'} | {c['title']} | "
                  f"{str(c['detail']).replace('|', '／')[:280]} |")
    md.append("")
    model.VERIFY_MD.write_text("\n".join(md), "utf-8")

    lm = ["# 反泄漏 / 提示核查报告", "",
          f"**盲包泄漏核查：{'✅ 未发现泄漏' if leak['leak_checks_ok'] else '❌ 发现问题'}**", "",
          "## 平凡提示分类器", "",
          "| 所谓捷径 | 在声画组上的表现 |", "| --- | --- |"]
    for k, v in leak["classifiers"].items():
        lm.append(f"| `{k}` | {v} |")
    lm += ["", "## 文件 / 内容卫生", "",
           f"- 扫描文本文件 {len(leak['files_scanned'])} 个；",
           f"- 越界文件名：`{leak['forbidden_names'] or '无'}`；",
           f"- zip 成员 {leak.get('zip_member_count', 0)} 个，越界：`{leak['zip_bad'] or '无'}`；",
           f"- 内容命中：`{leak['content_hits'] or '无'}`；",
           f"- 主持路径提示：`{leak.get('host_hint_hits') or '无'}`；",
           f"- 外链：`{leak['remote_refs'] or '无'}`。", "",
           "## 声画组平凡特征矩阵", "",
           "| 盲号 | 密封答案 | 甲字节 | 乙字节 | 大小相同 | 前导零相同 | 首起振相同 | 零平台一致 |",
           "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in leak["trivial_cues"]:
        lm.append(f"| {r['bid']} | {r['correct']} | {r['jia_size']} | {r['yi_size']} | "
                  f"{r['size_tie']} | {r['leading_tie']} | {r['first_onset_tie']} | "
                  f"{r['zero_platforms_equal']} |")
    lm.append("")
    model.LEAK_MD.write_text("\n".join(lm), "utf-8")


def main() -> int:
    truth = json.loads(model.TRUTH_JSON.read_text("utf-8"))
    rep = Report()
    observed = recompute_all(truth, rep)
    pairing = service_scans(truth, rep, observed)
    leak = leak_audit(truth, rep)
    write_reports(truth, rep, observed, pairing, leak)
    print(f"[verify] {sum(c['ok'] for c in rep.checks)}/{len(rep.checks)} checks passed; ok={rep.ok}")
    if not rep.ok:
        for c in rep.checks:
            if not c["ok"] and c["severity"] == "critical":
                print("  FAIL", c["id"], "::", c["detail"][:220])
    print(f"[verify] reports: {model.VERIFY_MD.name}, {model.LEAK_MD.name} in {model.DEBRIEF}")
    return 0 if rep.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
