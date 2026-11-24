"""生成参训者文档与主持复盘手册（Markdown）。"""
from __future__ import annotations

import json

from . import model


def trainee_readme() -> str:
    return """# Slatewatch 声画关系盲标练习 · 参训者说明

这是一套**盲标**练习。发放给你的材料只有：

- `index.html`：双击即可在浏览器离线打开的练习页（画面、甲 / 乙两条等长试听、波形、制作记录都已内嵌）；
- `assets/<编号>/`：同样内容的散文件，供你想拖进剪辑 / 音频工具时使用：
  - `picture.mp4`：**无伴音**短段画面；
  - `jia.wav` / `yi.wav`：与画面等长的两条候选试听（匿名，不对应任何真实流号）；
  - `filmstrip.png`、`frame_*.png`：画格序列 / 胶片条；
- `制作记录.md`：说明“哪个时间的哪个画面动作，应当和哪种现场声同步”；
- `答题卡.html` / `答题卡.md`：作答用题卡。

## 你要判断什么

母带里有**两条流名完全相同**的音轨：一条是主混，另一条是同名参考轨，还可能存在换轨。
但在盲包里，真实流号、流名标签和覆盖情况都被拿掉了，两条候选只叫 **甲 / 乙**。
请只依据**声画关系**判断：

1. 逐条核对制作记录中的动作时点，白闪 / 接触瞬间是否正好出现对应音色的现场打点；
2. 给出本小题结论：**甲对得上 / 乙对得上 / 像是换错轨 / 无法确认**；
3. 如果盲材内没有任何可区分甲、乙的声画关系，合理的判定就是 **“无法确认”**，不要猜流号，
   也不要猜“制作样本的人可能用了哪个文件”。

## 明确不要使用的“捷径”

这些线索要么不可靠，要么已被刻意做成甲、乙一致，**不能**据此选轨：

- 谁排在前面、谁先被播放；
- 谁的开头和零点 / 第一个画格对齐；
- 波形是否出现全零平台（全零既可能是“录到的安静”，也可能是“没有覆盖、播放时补零”）；
- WAV / 画面的文件长度或字节大小。

每一小题的画面与两条试听都严格等长、同采样率、同声道数、同样本数，并且都含两段相同的全零平台。
哪段零是实录、哪段零是无覆盖，**只有复盘时用真实扫描覆盖区间才能区分**。

## 运行环境

直接双击 `index.html` 即可，推荐使用当前版本的 Chrome / Edge / Firefox / Safari。
若浏览器限制本地音频，可改用 `assets/` 中的散文件，把 `picture.mp4` 与 `jia.wav` / `yi.wav`
同时从零播放核对。请独立完成；真实流号与覆盖安排由培训主持在复盘时统一公布，答完前不要提前索要答案材料。
"""


def production_record_md() -> str:
    out = ["# 制作记录（盲包版）", "",
           "本记录只描述画面动作与**应当**听到的现场声，不记载流号、流名归属或覆盖安排。",
           "所有片段时长均为 6.40 s。两处留白窗口在试听中都是全零平台，记录不说明其声音来源。", ""]
    for item in model.ITEMS:
        out.append(f"## {item.bid}")
        out.append("")
        out.append(f"**画面：** {item.scene}")
        out.append("")
        out.append("| 时间 | 画面动作 | 应同步听到的现场声 |")
        out.append("| ---: | --- | --- |")
        for hit in item.correct:
            st = model.STATIONS[hit.station]
            out.append(f"| {hit.t:0.2f} s | 接触{st['shape']}，接触瞬间白闪 | {st['sound']} |")
        for extra in item.extra_record:
            out.append(f"| — | {extra} | |")
        out.append("")
        out.append(f"**留白窗口：** {model.PLATEAUS['L1'][0]:.2f}–{model.PLATEAUS['L1'][1]:.2f} s、"
                   f"{model.PLATEAUS['L2'][0]:.2f}–{model.PLATEAUS['L2'][1]:.2f} s，"
                   "无规定动作；可能是静默，也可能是未收录，仅凭试听无法区分。")
        out.append("")
    return "\n".join(out)


def answer_sheet_md() -> str:
    out = ["# 盲标签题卡（可打印）", "",
           "参训者：____________　日期：__________　组别 / 工位：__________", "",
           "> 每一小题都允许且应当在证据不足时选择 **无法确认**。", ""]
    for item in model.ITEMS:
        times = "、".join(f"{h.t:0.2f}s" for h in item.correct)
        out += [
            f"## {item.bid}", "",
            f"规定动作时点：{times}", "",
            "- 甲：□ 同步　□ 不同步　□ 不确定",
            "- 乙：□ 同步　□ 不同步　□ 不确定", "",
            "本小题判定（单选）：",
            "□ 甲是应对齐的主混　□ 乙是应对齐的主混　□ 存在换错轨　□ 无法确认", "",
            "依据 / 听到的具体时点：", "",
            "```", "（请填写）", "```", "",
        ]
    return "\n".join(out)


def debrief_manual_md(truth: dict) -> str:
    lines = [
        "# 复盘手册 · 答案、真实流号与覆盖真值（主持 / 复盘专用，禁止进盲包）",
        "",
        "本文件由 `training/lab/builder.py` 依据密封设计生成；`training/verify_truth.py`",
        "会从母带字节**独立重算**并运行真实 Slatewatch 扫描，结果写入",
        "`verification_report.md` 与 `leak_audit.md`。若核对未通过，应以核查报告为准并重新生成。",
        "",
        "## 这组练习到底在测什么",
        "",
        "测的是审片员是否会因为**两条音轨流名完全相同**（都叫 "
        f"`{model.SHARED_STREAM_NAME}`）而把同名参考轨当成主混，而不是测能否猜出",
        "出题人用了哪个文件。盲包已经拿掉流号与覆盖，只留下声画关系；因此：",
        "",
        "- **声画组（code A1–A4，盲号 BL-01/03/05/06）**：恰好一条候选的打点与画面白闪、",
        "  制作记录一一对应；另一条保留相同的第一个打点（开头对齐不泄漏），后两个打点错位，",
        "  参训者应能靠多点声画关系识别“换错轨 / 哪条才对”。",
        "- **存疑组（code B1–B2，盲号 BL-02/04）**：甲、乙 WAV 字节完全相同，所有平凡线索一致，",
        "  盲材在数学上无法区分两条物理流；正确结论是 **无法确认**。差异只存在于被扣留的",
        "  真实流号与交叉覆盖里。",
        "",
        "## 密封答案总表",
        "",
        "| 盲号 | code | 组 | 盲材正确判定 | 甲=? | 乙=? | 主混物理流 | 主混空洞(无覆盖) | 主混实录静音 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in truth["items"]:
        amap = row["blind_to_role"]
        jia = "主混" if amap["jia"] == "main" else "参考"
        yi = "主混" if amap["yi"] == "main" else "参考"
        verdict = {"jia": "甲对（=主混）", "yi": "乙对（=主混）",
                   None: "无法确认"}[row["correct_label"]]
        main_idx = row["role_to_stream"]["main"]
        other = model.plateau_other(row["main_hole"])
        lines.append(
            f"| {row['bid']} | {row['code']} | {row['group']} | **{verdict}** | {jia} | {yi} "
            f"| 音频流 index={main_idx} | {row['main_hole']} {_span(row, row['main_hole'])} "
            f"| {other} {_span(row, other)} |"
        )
    lines += [
        "",
        "两条物理音轨的 `title` 标签都被写成同一个字符串 "
        f"`{model.SHARED_STREAM_NAME}`；流名不提供任何区分信息。物理流 index 是容器真实流号，",
        "由 PyAV 探测并经真实扫描配对确认。",
        "",
        "## 交叉的“相同静音外观”",
        "",
        "每个母带都有两段 0.80 s 的全零平台 L1（1.60–2.40 s）与 L2（3.20–4.00 s）。",
        "两条音轨、甲 / 乙 WAV 在这两处都是逐采样全零，看起来 / 听起来完全一样；但真值交叉：",
        "",
        "- 对同一条物理流，一个平台是**有覆盖的实录数字静音**（Slatewatch 报 `silence`），",
        "  另一个平台是**整块缺采样的无覆盖空洞**（Slatewatch 报 `sound_gap`，盲试听补零）。",
        "- 主混与参考轨的空洞平台互换，因此“哪段零是空洞”既不能从波形判断，也与选轨答案无关。",
        "",
        "## 建议复盘流程",
        "",
        "1. 先统计答题卡：多少人在 A 组被“同名 / 默认第一条 / 开头对齐”带偏；",
        "2. 打开 `index.html` 复看 A 组，只看白闪与打点，证明多点对齐能识轨；",
        "3. 打开 B 组并直接比对 `assets/BL-02|BL-04/jia.wav` 与 `yi.wav` 的 SHA-256（完全相同），",
        "   说明盲材内不存在可判定函数，答“无法确认”才对；",
        "4. 公布 `truth.json` 的流号与流名，播放 / 展示 `debrief/masters/*.mkv` 的双流；",
        "5. 用 `scan_pairing.json` / `verification_report.md` 展示真实扫描的覆盖区间，",
        "   指出同一全零外观下 `silence` 与 `sound_gap` 的差别；",
        "6. 宣读 `leak_audit.md`：顺序、开头、全零、长度四类提示均不构成答案泄漏。",
        "",
        "## 重新生成与独立核查",
        "",
        "```sh",
        ".venv/bin/python training/build_blind_lab.py     # 可复跑：重建母带与盲包",
        ".venv/bin/python training/verify_truth.py         # 独立重算 + 真实扫描配对 + 反泄漏核查",
        "```",
        "",
        "独立重算只信任母带与盲包媒体字节（PyAV 解码 + 自带采样 / 画格分析），不引用生成器的",
        "判定函数；它会和本底册逐项比对，并调用与 HTTP 服务同一个 `slatewatch.service.Slatewatch`",
        "扫描入口（临时保管库）完成真实扫描配对。",
        "",
        "## truth.json（完整机器可读真值）",
        "",
        "```json",
        json.dumps(truth, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)


def _span(row: dict, plateau: str) -> str:
    a, b = model.PLATEAUS[plateau]
    return f"({a:.2f}–{b:.2f}s)"
