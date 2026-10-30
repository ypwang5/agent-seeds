# 评测集

> **公开仓库里没有真实评测数据**：17 份 JD 原文（第三方招聘内容）、对它们的人工标注（含对真实公司的风险判断）、真实简历都只留在本地。
> 仓库里只有合成示例 `jds/00_示例_合成.txt` + `labels/00_示例_合成.json`、两份虚构简历 `mock_resumes/`、虚构的编造用例 `fabrication_cases.json`，用来说明格式；
> 想复现评测，按下表放入自己的 JD 和简历即可。README 里的评测数字来自本地真实数据。

| 目录 | 放什么 | 命名 |
|---|---|---|
| `jds/` | 每个 JD 一个 txt，直接粘贴招聘页面上的全文（含公司名、薪资） | `01_公司_岗位.txt` |
| `labels/` | 挑 5 个 JD 人工标注，作为标准答案（格式参考 `00_示例_合成.json`） | 与 JD 同名，后缀 `.json` |
| `resumes/` | 测试用的简历（PDF 或 txt）。**含个人信息，已加入 .gitignore，不会提交** | 随意 |
| `out/` | 运行结果和 `report.md` 汇总（不提交） | 自动生成 |

运行：
```powershell
uv run python evals/run_parse_eval.py                          # 用默认快模型
uv run python evals/run_parse_eval.py --model qwen:qwen-plus   # 换模型对比
```

标注原则：

| 字段 | 怎么标 |
|---|---|
| `education` / `years_required` | 学历只写"本科""硕士"；年限取数字，标题和正文矛盾时取正文 |
| `hard_requirements` | 一条只写一个独立要求（"熟悉 A 和 B"拆成两条）；**学历不写这里**（已有单独字段）；年龄限制照写，同时记入 `risks` |
| `employment_signals` | 只摘原文里直接写出的外包、驻场、大小周、加班等短语；需要推理才能看出来的（如"协助DR"）不算 |
| `risks` | 给第 3 周风险检查用：年龄限制、猎头未公开企业、名不副实、疑似外包、JD 自相矛盾等，允许推理 |
| `_备注` | 标注时的判断理由 |
| `risk_labels.json` | `risks` 归到风险类型（外包驻场、加班强度、名不副实、信息矛盾、歧视性条件、公司未公开），供 `run_screen_eval.py` 自动对比 |

其他评测：

| 脚本 | 评什么 |
|---|---|
| `run_gap_eval.py [--model]` | 差距分析（明细供人工抽查） |
| `run_fabrication_eval.py [--model]` | 防编造两层校验的漏检、误报 |
| `run_screen_eval.py [--concurrency 4]` | 岗位筛选：17 个 JD 一次跑完，看排序、风险类型准确率、并行省下的时间 |
