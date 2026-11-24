# 回件收件箱

这两个 CSV 是本次可复现演练中班组交回的实际回报。真实外发时只发送两个 ZIP；
班组填回自己的 CSV 后，可替换本目录文件并重新运行：

```sh
python3 -m slatewatch.handoff_lab reconcile --root handoff/case-r044
python3 -m slatewatch.handoff_lab verify --root handoff/case-r044
```

交付方不得把一方回报转发给另一方。汇合和开单只在 `coordinator/` 中进行。
