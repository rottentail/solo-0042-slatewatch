# R045 随单源地址清单

所有时间为整数微秒，区间按左闭右开 `[start_us,end_us)` 解释。
本清单与三个格位、一个 WAV 一起离线使用；不需要母带或服务端。

## 画格格位

| 格位 | 文件 | 目标地址 | 唯一源格 | 源覆盖区间 | PNG SHA-256 前16位 |
|---:|---|---|---|---|---|
| 1 | `frames/slot-1.png` | T-L1@3700000 → V08-03500ms<br>T-L2@4200000 → V08-03500ms | `V08-03500ms` | `3500000..4500000` | `1414c9a36eab6a3f` |
| 2 | `frames/slot-2.png` | T-B0@4750000 → V09-04500ms | `V09-04500ms` | `4500000..5000000` | `1414c9a36eab6a3f` |
| 3 | `frames/slot-3.png` | T-A0@6250000 → V10-06000ms | `V10-06000ms` | `6000000..6500000` | `1414c9a36eab6a3f` |

## WAV 地址

- 文件：`audio/R045-primary-mono.wav`
- 格式：1 声道 / 8000 Hz / 16-bit PCM
- 绝对源采样：`[35200, 60800)`
- 源时间：`00:00:04.400000` → `00:00:07.600000`
- 摘片补零区间（不是源内实录静音）：
  - 采样 `[44800,46400)`，时间 `00:00:05.600000–00:00:05.800000`：No source PCM coverage; evidence extractor inserted zero samples.
  - 采样 `[54400,56800)`，时间 `00:00:06.800000–00:00:07.100000`：No source PCM coverage; evidence extractor inserted zero samples.

| 区段 | 类型 | 覆盖 | 源采样 | RMS | Peak |
|---|---|---|---:|---:|---:|
| L-BEFORE 洞前源内标记音 | `source_tone` | `source_covered` | `36000..37600` | 8485 | 12000 |
| SIL-REC 实录数字零声 | `recorded_silence` | `source_covered` | `37600..40000` | 0 | 0 |
| BED-IN-HOLE 无图段内源内低鸣 | `source_tone` | `source_covered` | `40000..44800` | 3535 | 5000 |
| FILL-INNER 摘片补零（内部声洞） | `extract_fill` | `uncovered_by_source_extract_zero_fill` | `44800..46400` | 0 | 0 |
| R-AFTER 洞后源内标记音 | `source_tone` | `source_covered` | `48000..49600` | 8485 | 12000 |
| SIL-REC-2 第二段实录零声 | `recorded_silence` | `source_covered` | `50400..52800` | 0 | 0 |
| FILL-LATER 摘片补零（后段声洞） | `extract_fill` | `uncovered_by_source_extract_zero_fill` | `54400..56800` | 0 | 0 |
| CHIRP-RESUME 补零后源内恢复音 | `source_tone` | `source_covered` | `56800..58400` | 7071 | 10000 |

## 判定地址

```json
{
  "same_long_cell": {
    "source_frame_id": "V08-03500ms",
    "targets": [
      {
        "label": "T-L1",
        "program_us": 3700000,
        "program_timecode": "00:00:03.700000",
        "source_frame_id": "V08-03500ms"
      },
      {
        "label": "T-L2",
        "program_us": 4200000,
        "program_timecode": "00:00:04.200000",
        "source_frame_id": "V08-03500ms"
      }
    ],
    "note": "Both target addresses are inside the same decoded source coverage cell."
  },
  "cross_hole_twins": {
    "before_slot": 2,
    "after_slot": 3,
    "before_slot_key": "before-boundary",
    "after_slot_key": "after-boundary",
    "before_frame_id": "V09-04500ms",
    "after_frame_id": "V10-06000ms",
    "same_raw_gray_hash": true,
    "same_source_frame": false,
    "conclusion": "Equal pixels are two source addresses; no single source cell covers the picture hole."
  },
  "audio_both_sides_in_source": {
    "before_region_id": "L-BEFORE",
    "after_region_id": "R-AFTER",
    "conclusion": "Both immediate boundary markers are covered native-rate source PCM."
  }
}
```
