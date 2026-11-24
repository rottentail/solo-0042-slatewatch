# 独立核查报告（复盘材料）

**总体结论：✅ 全部关键核查通过**

通过 188/188 项。

真值从母带 / 盲包字节独立重算（`recomputed_truth.json`），真实 Slatewatch 
扫描配对见 `scan_pairing.json`。逐项结果：

| 结果 | 核查项 | 说明 |
| --- | --- | --- |
| ✅ | BL-01 母带存在 | /workspace/training/debrief/masters/BL-01_A1_dual.mkv |
| ✅ | BL-01 母带 SHA-256 与底册一致 | recomputed=a94821c7b089d74e5314de4dd7ea46e0a7e130244a6f8573b09b6ec198dcde1d |
| ✅ | BL-01 两条音轨流名完全相同且为密封流名 | titles=['主混 MIX', '主混 MIX'] |
| ✅ | BL-01 物理音频流号与底册一致 | observed=[1, 2] truth=[1, 2] |
| ✅ | BL-01 流1(main) 独立判定无覆盖平台=L1 | uncovered=[(1600000, 2400000)] |
| ✅ | BL-01 流1(main) 独立判定实录静音平台=L2 | recorded=[(3200000, 4000000)] |
| ✅ | BL-01 流1 起点为 0 且无前置偏移 | first=0 leading0=0 |
| ✅ | BL-01 流2(ref) 独立判定无覆盖平台=L2 | uncovered=[(3200000, 4000000)] |
| ✅ | BL-01 流2(ref) 独立判定实录静音平台=L1 | recorded=[(1600000, 2400000)] |
| ✅ | BL-01 流2 起点为 0 且无前置偏移 | first=0 leading0=0 |
| ✅ | BL-01 母带白闪独立重算与制作记录一致 | flashes=[800000, 2800000, 4400000] target=[800000, 2800000, 4400000] |
| ✅ | BL-01 jia.wav 与构建底册一致 | 5e8c2cd1250c22fa |
| ✅ | BL-01 jia.wav 可完整解码 | decoded=102400 header=102400 |
| ✅ | BL-01 yi.wav 与构建底册一致 | cc3ab54eb44e0548 |
| ✅ | BL-01 yi.wav 可完整解码 | decoded=102400 header=102400 |
| ✅ | BL-01 甲/乙格式、样本数、字节大小完全相同 | jia=(1,16000,102400,204844) yi=(1,16000,102400,204844) |
| ✅ | BL-01 两条试听都不是全零文件 | peaks=10426,10334 |
| ✅ | BL-01 甲/乙开头前导零样本数相同 | 0 vs 0 |
| ✅ | BL-01 甲/乙第一个起振时刻相同（不能只靠开头对齐取轨） | first=[0.8] vs [0.8] Δ=0.000s |
| ✅ | BL-01 甲/乙长全零平台位置与长度相同（全零波形不泄漏选轨） | jia=[(1600000, 2400000), (3200000, 4000000)] yi=[(1600000, 2400000), (3200000, 4000000)] |
| ✅ | BL-01 独立声画重算唯一锁定 yi 且与密封答案一致 | jia=1/3 yi=3/3 derived=yi truth=yi other_matches_only_first=True |
| ✅ | BL-01 盲包胶片条白闪时点与母带一致 | film=[0.8, 2.8, 4.4] |
| ✅ | BL-01 picture.mp4 可解码且为等长画面 | {'frames_decoded': 160, 'container_duration_us': 6400000} |
| ✅ | BL-02 母带存在 | /workspace/training/debrief/masters/BL-02_B1_dual.mkv |
| ✅ | BL-02 母带 SHA-256 与底册一致 | recomputed=e89292399257f96dd01f1359b3440743c14fcfbe30a5d3c234cc377b240c5e05 |
| ✅ | BL-02 两条音轨流名完全相同且为密封流名 | titles=['主混 MIX', '主混 MIX'] |
| ✅ | BL-02 物理音频流号与底册一致 | observed=[1, 2] truth=[1, 2] |
| ✅ | BL-02 流1(main) 独立判定无覆盖平台=L1 | uncovered=[(1600000, 2400000)] |
| ✅ | BL-02 流1(main) 独立判定实录静音平台=L2 | recorded=[(3200000, 4000000)] |
| ✅ | BL-02 流1 起点为 0 且无前置偏移 | first=0 leading0=0 |
| ✅ | BL-02 流2(ref) 独立判定无覆盖平台=L2 | uncovered=[(3200000, 4000000)] |
| ✅ | BL-02 流2(ref) 独立判定实录静音平台=L1 | recorded=[(1600000, 2400000)] |
| ✅ | BL-02 流2 起点为 0 且无前置偏移 | first=0 leading0=0 |
| ✅ | BL-02 母带白闪独立重算与制作记录一致 | flashes=[1000000, 4600000] target=[1000000, 4600000] |
| ✅ | BL-02 jia.wav 与构建底册一致 | 53d5e295ac56f004 |
| ✅ | BL-02 jia.wav 可完整解码 | decoded=102400 header=102400 |
| ✅ | BL-02 yi.wav 与构建底册一致 | 53d5e295ac56f004 |
| ✅ | BL-02 yi.wav 可完整解码 | decoded=102400 header=102400 |
| ✅ | BL-02 甲/乙格式、样本数、字节大小完全相同 | jia=(1,16000,102400,204844) yi=(1,16000,102400,204844) |
| ✅ | BL-02 两条试听都不是全零文件 | peaks=10413,10413 |
| ✅ | BL-02 甲/乙开头前导零样本数相同 | 0 vs 0 |
| ✅ | BL-02 甲/乙第一个起振时刻相同（不能只靠开头对齐取轨） | first=[1.0] vs [1.0] Δ=0.000s |
| ✅ | BL-02 甲/乙长全零平台位置与长度相同（全零波形不泄漏选轨） | jia=[(1600000, 2400000), (3200000, 4000000)] yi=[(1600000, 2400000), (3200000, 4000000)] |
| ✅ | BL-02 甲/乙字节相同且声画匹配相同 → 盲材必须判无法确认 | sha_equal=True matches jia=2 yi=2 |
| ✅ | BL-02 盲包胶片条白闪时点与母带一致 | film=[1.0, 4.6] |
| ✅ | BL-02 picture.mp4 可解码且为等长画面 | {'frames_decoded': 160, 'container_duration_us': 6400000} |
| ✅ | BL-03 母带存在 | /workspace/training/debrief/masters/BL-03_A2_dual.mkv |
| ✅ | BL-03 母带 SHA-256 与底册一致 | recomputed=db8b7d47f8e14d0c25e8162f67b704e3b3fea8c5fcf2b3e8a6e497e9cf530175 |
| ✅ | BL-03 两条音轨流名完全相同且为密封流名 | titles=['主混 MIX', '主混 MIX'] |
| ✅ | BL-03 物理音频流号与底册一致 | observed=[1, 2] truth=[1, 2] |
| ✅ | BL-03 流1(ref) 独立判定无覆盖平台=L1 | uncovered=[(1600000, 2400000)] |
| ✅ | BL-03 流1(ref) 独立判定实录静音平台=L2 | recorded=[(3200000, 4000000)] |
| ✅ | BL-03 流1 起点为 0 且无前置偏移 | first=0 leading0=0 |
| ✅ | BL-03 流2(main) 独立判定无覆盖平台=L2 | uncovered=[(3200000, 4000000)] |
| ✅ | BL-03 流2(main) 独立判定实录静音平台=L1 | recorded=[(1600000, 2400000)] |
| ✅ | BL-03 流2 起点为 0 且无前置偏移 | first=0 leading0=0 |
| ✅ | BL-03 母带白闪独立重算与制作记录一致 | flashes=[1000000, 3000000, 4800000] target=[1000000, 3000000, 4800000] |
| ✅ | BL-03 jia.wav 与构建底册一致 | 6f845a82df541496 |
| ✅ | BL-03 jia.wav 可完整解码 | decoded=102400 header=102400 |
| ✅ | BL-03 yi.wav 与构建底册一致 | e02981a70ea8bf72 |
| ✅ | BL-03 yi.wav 可完整解码 | decoded=102400 header=102400 |
| ✅ | BL-03 甲/乙格式、样本数、字节大小完全相同 | jia=(1,16000,102400,204844) yi=(1,16000,102400,204844) |
| ✅ | BL-03 两条试听都不是全零文件 | peaks=10426,10295 |
| ✅ | BL-03 甲/乙开头前导零样本数相同 | 0 vs 0 |
| ✅ | BL-03 甲/乙第一个起振时刻相同（不能只靠开头对齐取轨） | first=[1.0] vs [1.0] Δ=0.000s |
| ✅ | BL-03 甲/乙长全零平台位置与长度相同（全零波形不泄漏选轨） | jia=[(1600000, 2400000), (3200000, 4000000)] yi=[(1600000, 2400000), (3200000, 4000000)] |
| ✅ | BL-03 独立声画重算唯一锁定 jia 且与密封答案一致 | jia=3/3 yi=1/3 derived=jia truth=jia other_matches_only_first=True |
| ✅ | BL-03 盲包胶片条白闪时点与母带一致 | film=[1.0, 3.0, 4.8] |
| ✅ | BL-03 picture.mp4 可解码且为等长画面 | {'frames_decoded': 160, 'container_duration_us': 6400000} |
| ✅ | BL-04 母带存在 | /workspace/training/debrief/masters/BL-04_B2_dual.mkv |
| ✅ | BL-04 母带 SHA-256 与底册一致 | recomputed=83a8373e8d8867c46847d5b952390512d5e4bc8f73b4ecc4f390a3fa269a30ec |
| ✅ | BL-04 两条音轨流名完全相同且为密封流名 | titles=['主混 MIX', '主混 MIX'] |
| ✅ | BL-04 物理音频流号与底册一致 | observed=[1, 2] truth=[1, 2] |
| ✅ | BL-04 流1(ref) 独立判定无覆盖平台=L1 | uncovered=[(1600000, 2400000)] |
| ✅ | BL-04 流1(ref) 独立判定实录静音平台=L2 | recorded=[(3200000, 4000000)] |
| ✅ | BL-04 流1 起点为 0 且无前置偏移 | first=0 leading0=0 |
| ✅ | BL-04 流2(main) 独立判定无覆盖平台=L2 | uncovered=[(3200000, 4000000)] |
| ✅ | BL-04 流2(main) 独立判定实录静音平台=L1 | recorded=[(1600000, 2400000)] |
| ✅ | BL-04 流2 起点为 0 且无前置偏移 | first=0 leading0=0 |
| ✅ | BL-04 母带白闪独立重算与制作记录一致 | flashes=[800000, 2800000, 5200000] target=[800000, 2800000, 5200000] |
| ✅ | BL-04 jia.wav 与构建底册一致 | df82f3d55be7461d |
| ✅ | BL-04 jia.wav 可完整解码 | decoded=102400 header=102400 |
| ✅ | BL-04 yi.wav 与构建底册一致 | df82f3d55be7461d |
| ✅ | BL-04 yi.wav 可完整解码 | decoded=102400 header=102400 |
| ✅ | BL-04 甲/乙格式、样本数、字节大小完全相同 | jia=(1,16000,102400,204844) yi=(1,16000,102400,204844) |
| ✅ | BL-04 两条试听都不是全零文件 | peaks=10334,10334 |
| ✅ | BL-04 甲/乙开头前导零样本数相同 | 0 vs 0 |
| ✅ | BL-04 甲/乙第一个起振时刻相同（不能只靠开头对齐取轨） | first=[0.8] vs [0.8] Δ=0.000s |
| ✅ | BL-04 甲/乙长全零平台位置与长度相同（全零波形不泄漏选轨） | jia=[(1600000, 2400000), (3200000, 4000000)] yi=[(1600000, 2400000), (3200000, 4000000)] |
| ✅ | BL-04 甲/乙字节相同且声画匹配相同 → 盲材必须判无法确认 | sha_equal=True matches jia=3 yi=3 |
| ✅ | BL-04 盲包胶片条白闪时点与母带一致 | film=[0.8, 2.8, 5.2] |
| ✅ | BL-04 picture.mp4 可解码且为等长画面 | {'frames_decoded': 160, 'container_duration_us': 6400000} |
| ✅ | BL-05 母带存在 | /workspace/training/debrief/masters/BL-05_A3_dual.mkv |
| ✅ | BL-05 母带 SHA-256 与底册一致 | recomputed=9fcbffae68910569fb6fd757d88308501bc4be8fac80cc9aa2b30768933bb39b |
| ✅ | BL-05 两条音轨流名完全相同且为密封流名 | titles=['主混 MIX', '主混 MIX'] |
| ✅ | BL-05 物理音频流号与底册一致 | observed=[1, 2] truth=[1, 2] |
| ✅ | BL-05 流1(ref) 独立判定无覆盖平台=L2 | uncovered=[(3200000, 4000000)] |
| ✅ | BL-05 流1(ref) 独立判定实录静音平台=L1 | recorded=[(1600000, 2400000)] |
| ✅ | BL-05 流1 起点为 0 且无前置偏移 | first=0 leading0=0 |
| ✅ | BL-05 流2(main) 独立判定无覆盖平台=L1 | uncovered=[(1600000, 2400000)] |
| ✅ | BL-05 流2(main) 独立判定实录静音平台=L2 | recorded=[(3200000, 4000000)] |
| ✅ | BL-05 流2 起点为 0 且无前置偏移 | first=0 leading0=0 |
| ✅ | BL-05 母带白闪独立重算与制作记录一致 | flashes=[600000, 2600000, 5000000] target=[600000, 2600000, 5000000] |
| ✅ | BL-05 jia.wav 与构建底册一致 | d1f454fa80b1254d |
| ✅ | BL-05 jia.wav 可完整解码 | decoded=102400 header=102400 |
| ✅ | BL-05 yi.wav 与构建底册一致 | 7f8dd6d5b51d772d |
| ✅ | BL-05 yi.wav 可完整解码 | decoded=102400 header=102400 |
| ✅ | BL-05 甲/乙格式、样本数、字节大小完全相同 | jia=(1,16000,102400,204844) yi=(1,16000,102400,204844) |
| ✅ | BL-05 两条试听都不是全零文件 | peaks=10353,10353 |
| ✅ | BL-05 甲/乙开头前导零样本数相同 | 0 vs 0 |
| ✅ | BL-05 甲/乙第一个起振时刻相同（不能只靠开头对齐取轨） | first=[0.6] vs [0.6] Δ=0.000s |
| ✅ | BL-05 甲/乙长全零平台位置与长度相同（全零波形不泄漏选轨） | jia=[(1600000, 2400000), (3200000, 4000000)] yi=[(1600000, 2400000), (3200000, 4000000)] |
| ✅ | BL-05 独立声画重算唯一锁定 yi 且与密封答案一致 | jia=1/3 yi=3/3 derived=yi truth=yi other_matches_only_first=True |
| ✅ | BL-05 盲包胶片条白闪时点与母带一致 | film=[0.6, 2.6, 5.0] |
| ✅ | BL-05 picture.mp4 可解码且为等长画面 | {'frames_decoded': 160, 'container_duration_us': 6400000} |
| ✅ | BL-06 母带存在 | /workspace/training/debrief/masters/BL-06_A4_dual.mkv |
| ✅ | BL-06 母带 SHA-256 与底册一致 | recomputed=aa974f3227980375c52674024dc139ad6a096202bae838948059589b5110f299 |
| ✅ | BL-06 两条音轨流名完全相同且为密封流名 | titles=['主混 MIX', '主混 MIX'] |
| ✅ | BL-06 物理音频流号与底册一致 | observed=[1, 2] truth=[1, 2] |
| ✅ | BL-06 流1(main) 独立判定无覆盖平台=L2 | uncovered=[(3200000, 4000000)] |
| ✅ | BL-06 流1(main) 独立判定实录静音平台=L1 | recorded=[(1600000, 2400000)] |
| ✅ | BL-06 流1 起点为 0 且无前置偏移 | first=0 leading0=0 |
| ✅ | BL-06 流2(ref) 独立判定无覆盖平台=L1 | uncovered=[(1600000, 2400000)] |
| ✅ | BL-06 流2(ref) 独立判定实录静音平台=L2 | recorded=[(3200000, 4000000)] |
| ✅ | BL-06 流2 起点为 0 且无前置偏移 | first=0 leading0=0 |
| ✅ | BL-06 母带白闪独立重算与制作记录一致 | flashes=[1200000, 2800000, 4200000] target=[1200000, 2800000, 4200000] |
| ✅ | BL-06 jia.wav 与构建底册一致 | 606eb02e086fce61 |
| ✅ | BL-06 jia.wav 可完整解码 | decoded=102400 header=102400 |
| ✅ | BL-06 yi.wav 与构建底册一致 | 236b6b15a4c83303 |
| ✅ | BL-06 yi.wav 可完整解码 | decoded=102400 header=102400 |
| ✅ | BL-06 甲/乙格式、样本数、字节大小完全相同 | jia=(1,16000,102400,204844) yi=(1,16000,102400,204844) |
| ✅ | BL-06 两条试听都不是全零文件 | peaks=10295,10353 |
| ✅ | BL-06 甲/乙开头前导零样本数相同 | 0 vs 0 |
| ✅ | BL-06 甲/乙第一个起振时刻相同（不能只靠开头对齐取轨） | first=[1.2] vs [1.2] Δ=0.000s |
| ✅ | BL-06 甲/乙长全零平台位置与长度相同（全零波形不泄漏选轨） | jia=[(1600000, 2400000), (3200000, 4000000)] yi=[(1600000, 2400000), (3200000, 4000000)] |
| ✅ | BL-06 独立声画重算唯一锁定 jia 且与密封答案一致 | jia=3/3 yi=1/3 derived=jia truth=jia other_matches_only_first=True |
| ✅ | BL-06 盲包胶片条白闪时点与母带一致 | film=[1.2, 2.8, 4.2] |
| ✅ | BL-06 picture.mp4 可解码且为等长画面 | {'frames_decoded': 160, 'container_duration_us': 6400000} |
| ✅ | 声画组密封答案在甲/乙间平衡（不存在‘总选第一个’规律） | {'yi': 2, 'jia': 2} |
| ✅ | 主混物理流号在 1/2 间平衡（不存在‘流号小=主混’规律） | {'1': 2, '2': 2} |
| ✅ | 盲标签与物理流号无固定共现关系 | [('jia', 1), ('jia', 2), ('yi', 1), ('yi', 2)] |
| ✅ | BL-01 流 1：真实扫描覆盖与独立重算一致 | scan=[{'start_us': 0, 'end_us': 1600000}, {'start_us': 2400000, 'end_us': 6400000}] ind=[{'start_us': 0, 'end_us': 1600000}, {'start_us': 2400000, 'end_us': 6400000}] |
| ✅ | BL-01 流 1：sound_gap 配对无覆盖平台 L1 | gaps=[{'id': 'hit-1384b387f67c67fccccba28b', 'start_us': 1600000, 'end_us': 2400000}] expect=[1600000, 2400000] |
| ✅ | BL-01 流 1：silence 配对实录静音平台 L2 | silences=[{'id': 'hit-90133e06392cc23ff7c91fb9', 'channel': 1, 'start_us': 3200000, 'end_us': 4000000}, {'id': 'hit-cf52f6b641831f753901c17e', 'channel': 2, 'start_us': 3200000, 'end_us': 4000000}] expect=[3200000, 4000000] |
| ✅ | BL-01 流 2：真实扫描覆盖与独立重算一致 | scan=[{'start_us': 0, 'end_us': 3200000}, {'start_us': 4000000, 'end_us': 6400000}] ind=[{'start_us': 0, 'end_us': 3200000}, {'start_us': 4000000, 'end_us': 6400000}] |
| ✅ | BL-01 流 2：sound_gap 配对无覆盖平台 L2 | gaps=[{'id': 'hit-3b1df287f720b319b8305bc9', 'start_us': 3200000, 'end_us': 4000000}] expect=[3200000, 4000000] |
| ✅ | BL-01 流 2：silence 配对实录静音平台 L1 | silences=[{'id': 'hit-5b267bdf3fc7933295bbb115', 'channel': 1, 'start_us': 1600000, 'end_us': 2400000}, {'id': 'hit-02c826b7ed2d7e2089cfacd7', 'channel': 2, 'start_us': 1600000, 'end_us': 2400000}] expect=[1600000, 2400000] |
| ✅ | BL-02 流 1：真实扫描覆盖与独立重算一致 | scan=[{'start_us': 0, 'end_us': 1600000}, {'start_us': 2400000, 'end_us': 6400000}] ind=[{'start_us': 0, 'end_us': 1600000}, {'start_us': 2400000, 'end_us': 6400000}] |
| ✅ | BL-02 流 1：sound_gap 配对无覆盖平台 L1 | gaps=[{'id': 'hit-b92db6109f6a9097ec180525', 'start_us': 1600000, 'end_us': 2400000}] expect=[1600000, 2400000] |
| ✅ | BL-02 流 1：silence 配对实录静音平台 L2 | silences=[{'id': 'hit-e651e68c4fa31de5636522b0', 'channel': 1, 'start_us': 3200000, 'end_us': 4000000}, {'id': 'hit-fdb4eaeab926875fe17923ed', 'channel': 2, 'start_us': 3200000, 'end_us': 4000000}] expect=[3200000, 4000000] |
| ✅ | BL-02 流 2：真实扫描覆盖与独立重算一致 | scan=[{'start_us': 0, 'end_us': 3200000}, {'start_us': 4000000, 'end_us': 6400000}] ind=[{'start_us': 0, 'end_us': 3200000}, {'start_us': 4000000, 'end_us': 6400000}] |
| ✅ | BL-02 流 2：sound_gap 配对无覆盖平台 L2 | gaps=[{'id': 'hit-cd050695a596ded16250f2bd', 'start_us': 3200000, 'end_us': 4000000}] expect=[3200000, 4000000] |
| ✅ | BL-02 流 2：silence 配对实录静音平台 L1 | silences=[{'id': 'hit-57b64f905133b643deb4a663', 'channel': 1, 'start_us': 1600000, 'end_us': 2400000}, {'id': 'hit-ca4a31dd7f3fec85a420434e', 'channel': 2, 'start_us': 1600000, 'end_us': 2400000}] expect=[1600000, 2400000] |
| ✅ | BL-03 流 1：真实扫描覆盖与独立重算一致 | scan=[{'start_us': 0, 'end_us': 1600000}, {'start_us': 2400000, 'end_us': 6400000}] ind=[{'start_us': 0, 'end_us': 1600000}, {'start_us': 2400000, 'end_us': 6400000}] |
| ✅ | BL-03 流 1：sound_gap 配对无覆盖平台 L1 | gaps=[{'id': 'hit-57d2d1a6e7ab4205a64598dd', 'start_us': 1600000, 'end_us': 2400000}] expect=[1600000, 2400000] |
| ✅ | BL-03 流 1：silence 配对实录静音平台 L2 | silences=[{'id': 'hit-4a745dd83f4653569951e8b1', 'channel': 1, 'start_us': 3200000, 'end_us': 4000000}, {'id': 'hit-b1560be492def67bb9631895', 'channel': 2, 'start_us': 3200000, 'end_us': 4000000}] expect=[3200000, 4000000] |
| ✅ | BL-03 流 2：真实扫描覆盖与独立重算一致 | scan=[{'start_us': 0, 'end_us': 3200000}, {'start_us': 4000000, 'end_us': 6400000}] ind=[{'start_us': 0, 'end_us': 3200000}, {'start_us': 4000000, 'end_us': 6400000}] |
| ✅ | BL-03 流 2：sound_gap 配对无覆盖平台 L2 | gaps=[{'id': 'hit-a0f8b6440e76aa9627d08da8', 'start_us': 3200000, 'end_us': 4000000}] expect=[3200000, 4000000] |
| ✅ | BL-03 流 2：silence 配对实录静音平台 L1 | silences=[{'id': 'hit-7ac83239b8f39e18dfd71ce8', 'channel': 1, 'start_us': 1600000, 'end_us': 2400000}, {'id': 'hit-d8b1d05a4d884dc0786423c1', 'channel': 2, 'start_us': 1600000, 'end_us': 2400000}] expect=[1600000, 2400000] |
| ✅ | BL-04 流 1：真实扫描覆盖与独立重算一致 | scan=[{'start_us': 0, 'end_us': 1600000}, {'start_us': 2400000, 'end_us': 6400000}] ind=[{'start_us': 0, 'end_us': 1600000}, {'start_us': 2400000, 'end_us': 6400000}] |
| ✅ | BL-04 流 1：sound_gap 配对无覆盖平台 L1 | gaps=[{'id': 'hit-eaae0ed7f027a968598922b7', 'start_us': 1600000, 'end_us': 2400000}] expect=[1600000, 2400000] |
| ✅ | BL-04 流 1：silence 配对实录静音平台 L2 | silences=[{'id': 'hit-1386f96d2579b91e640c8539', 'channel': 1, 'start_us': 3200000, 'end_us': 4000000}, {'id': 'hit-ea12057b41a4738d20d002a3', 'channel': 2, 'start_us': 3200000, 'end_us': 4000000}] expect=[3200000, 4000000] |
| ✅ | BL-04 流 2：真实扫描覆盖与独立重算一致 | scan=[{'start_us': 0, 'end_us': 3200000}, {'start_us': 4000000, 'end_us': 6400000}] ind=[{'start_us': 0, 'end_us': 3200000}, {'start_us': 4000000, 'end_us': 6400000}] |
| ✅ | BL-04 流 2：sound_gap 配对无覆盖平台 L2 | gaps=[{'id': 'hit-00c151c573b0b18ac8166e2d', 'start_us': 3200000, 'end_us': 4000000}] expect=[3200000, 4000000] |
| ✅ | BL-04 流 2：silence 配对实录静音平台 L1 | silences=[{'id': 'hit-3fbf8eb0850b2d9a0a212ddd', 'channel': 1, 'start_us': 1600000, 'end_us': 2400000}, {'id': 'hit-f18684d1875725e7a3e47155', 'channel': 2, 'start_us': 1600000, 'end_us': 2400000}] expect=[1600000, 2400000] |
| ✅ | BL-05 流 1：真实扫描覆盖与独立重算一致 | scan=[{'start_us': 0, 'end_us': 3200000}, {'start_us': 4000000, 'end_us': 6400000}] ind=[{'start_us': 0, 'end_us': 3200000}, {'start_us': 4000000, 'end_us': 6400000}] |
| ✅ | BL-05 流 1：sound_gap 配对无覆盖平台 L2 | gaps=[{'id': 'hit-e80581787e3db4a47ff7264f', 'start_us': 3200000, 'end_us': 4000000}] expect=[3200000, 4000000] |
| ✅ | BL-05 流 1：silence 配对实录静音平台 L1 | silences=[{'id': 'hit-072b3780ee9fe3175fb1a674', 'channel': 1, 'start_us': 1600000, 'end_us': 2400000}, {'id': 'hit-67d7cb49615542dff024bfc5', 'channel': 2, 'start_us': 1600000, 'end_us': 2400000}] expect=[1600000, 2400000] |
| ✅ | BL-05 流 2：真实扫描覆盖与独立重算一致 | scan=[{'start_us': 0, 'end_us': 1600000}, {'start_us': 2400000, 'end_us': 6400000}] ind=[{'start_us': 0, 'end_us': 1600000}, {'start_us': 2400000, 'end_us': 6400000}] |
| ✅ | BL-05 流 2：sound_gap 配对无覆盖平台 L1 | gaps=[{'id': 'hit-a5606231d2d0dd73e3a6a392', 'start_us': 1600000, 'end_us': 2400000}] expect=[1600000, 2400000] |
| ✅ | BL-05 流 2：silence 配对实录静音平台 L2 | silences=[{'id': 'hit-3e2fa2fa958113191360be57', 'channel': 1, 'start_us': 3200000, 'end_us': 4000000}, {'id': 'hit-831e22effc18d791566783aa', 'channel': 2, 'start_us': 3200000, 'end_us': 4000000}] expect=[3200000, 4000000] |
| ✅ | BL-06 流 1：真实扫描覆盖与独立重算一致 | scan=[{'start_us': 0, 'end_us': 3200000}, {'start_us': 4000000, 'end_us': 6400000}] ind=[{'start_us': 0, 'end_us': 3200000}, {'start_us': 4000000, 'end_us': 6400000}] |
| ✅ | BL-06 流 1：sound_gap 配对无覆盖平台 L2 | gaps=[{'id': 'hit-fc5b2367d72f660b7e2f2c94', 'start_us': 3200000, 'end_us': 4000000}] expect=[3200000, 4000000] |
| ✅ | BL-06 流 1：silence 配对实录静音平台 L1 | silences=[{'id': 'hit-7a0ee2c6a3e037af69876147', 'channel': 1, 'start_us': 1600000, 'end_us': 2400000}, {'id': 'hit-e3b2d367514cbd8f3a1c800f', 'channel': 2, 'start_us': 1600000, 'end_us': 2400000}] expect=[1600000, 2400000] |
| ✅ | BL-06 流 2：真实扫描覆盖与独立重算一致 | scan=[{'start_us': 0, 'end_us': 1600000}, {'start_us': 2400000, 'end_us': 6400000}] ind=[{'start_us': 0, 'end_us': 1600000}, {'start_us': 2400000, 'end_us': 6400000}] |
| ✅ | BL-06 流 2：sound_gap 配对无覆盖平台 L1 | gaps=[{'id': 'hit-fa5da1ddd8e613d156833027', 'start_us': 1600000, 'end_us': 2400000}] expect=[1600000, 2400000] |
| ✅ | BL-06 流 2：silence 配对实录静音平台 L2 | silences=[{'id': 'hit-2ebc89ba7aed8b1e739773db', 'channel': 1, 'start_us': 3200000, 'end_us': 4000000}, {'id': 'hit-57d4b12abba7b5c6671461bb', 'channel': 2, 'start_us': 3200000, 'end_us': 4000000}] expect=[3200000, 4000000] |
| ✅ | 盲包目录无 truth/debrief/母带/脚本/报告等越界文件 | bad=[] |
| ✅ | 盲包 zip 仅含参训者文件，无答案 / 母带 / 越界路径 | members=989 bad=[] |
| ✅ | 盲包文本无密封哈希 / 真实流名 / 答案赋值 / 真值键 |  |
| ✅ | 盲包文本不回指 debrief / truth / 生成核查脚本等主持路径 |  |
| ✅ | 盲包页面无 http(s) 外链（离线自包含） | remote=[] |
| ✅ | 内嵌盲数据仅含媒体 / 时序键，无 role/stream/answer | per-item keys=["cols", "duration", "film", "fps", "frames", "h", "jia", "rows", "w", "yi"] |
| ✅ | index.html 的本地链接目标均在盲包内 | missing=[] |
| ✅ | 文件长度 / 字节大小不能选轨（全部相等） |  |
| ✅ | 开头对齐 / 前导零不能选轨（全部相同） |  |
| ✅ | 全零波形不能选轨（平台数量 / 位置全部相同） |  |
| ✅ | 固定选第一个候选不能全对（答案甲乙各半） |  |
