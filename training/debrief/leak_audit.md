# 反泄漏 / 提示核查报告

**盲包泄漏核查：✅ 未发现泄漏**

## 平凡提示分类器

| 所谓捷径 | 在声画组上的表现 |
| --- | --- |
| `always_pick_first_label_jia` | 2/4 (答案甲乙各半) |
| `always_pick_yi` | 2/4 |
| `pick_by_smaller_file_size` | all ties -> 无法选择 |
| `pick_by_fewer_leading_zeros` | all ties |
| `pick_by_earlier_first_onset_(opening_alignment)` | all ties |
| `pick_by_all_zero_waveform_platforms` | identical x2 each |

## 文件 / 内容卫生

- 扫描文本文件 5 个；
- 越界文件名：`无`；
- zip 成员 989 个，越界：`无`；
- 内容命中：`无`；
- 主持路径提示：`无`；
- 外链：`无`。

## 声画组平凡特征矩阵

| 盲号 | 密封答案 | 甲字节 | 乙字节 | 大小相同 | 前导零相同 | 首起振相同 | 零平台一致 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| BL-01 | yi | 204844 | 204844 | True | True | True | True |
| BL-03 | jia | 204844 | 204844 | True | True | True | True |
| BL-05 | yi | 204844 | 204844 | True | True | True | True |
| BL-06 | jia | 204844 | 204844 | True | True | True | True |
