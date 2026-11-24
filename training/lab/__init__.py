"""Slatewatch 盲标练习 · 主持侧实验室（生成 / 真值 / 核查）。

这个包属于仓库内的“出题与复盘工具”，**不会**整体进入可发放盲包。
- ``model``  ：练习设计（含密封的流号 / 角色映射，属答案）。
- ``render`` ：确定性画格、PCM、双音轨母带与盲包媒体。
- ``builder``：生成母带、盲包与 ``truth.json`` 答案底册。
- ``probe``  ：只依赖 PyAV/标准库，从媒体字节独立重算观测真值。
- ``audit``  ：真实 Slatewatch 扫描配对、真值比对与反泄漏核查。

发放边界见 ``training/README.md``：只有 ``training/blind_pack/``（或其 zip）
可以发给参训者；``training/debrief/`` 与 ``lab/`` 均为主持 / 复盘材料。
"""
