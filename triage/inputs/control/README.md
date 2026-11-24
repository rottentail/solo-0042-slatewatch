# 对照：完整的浏览映射长什么样

这两份 JSON 都带 `target_us`（页面显示时标）和 `source_us`（源帧时标）。
它们不是本次争议的答案，只用来展示“缺了哪一项就无法归属”。

- `frozen-card-target-2.100.json`：冻结包内的映射。target 2.100 s → source 1.000 s。
- `current-long-target-2.100.json`：在当前母带上重新摘片。同一 target 2.100 s，
  但 source 是 1.040 s。可见相同页面时标在两版上对应不同源帧。

若网页另存时多抄一行 `source_us`：1.000 s 指向归档版，1.040 s 指向当前版，
同一像素的归属即可判。若只有 `target_us`，同一像素在两版、不同源帧上都出现过，
归属就只能停在未知。
