---
name: markdown-to-obsidian-paper-card
description: 将已有论文 Markdown 快速转换为可阅读的 Obsidian 论文卡，支持裸 Markdown 或 source package、非正文格式自动恢复、图片归档、引文、表格、公式、完整中英对照翻译和安全概念双链。适用于网页抓取、Pandoc、手工 Markdown、MinerU Markdown，以及按题名从 Clippings、论文或 Zotero 启动的端到端双语论文卡任务；该 skill 本身不调用 MinerU API。
---

# Markdown to Obsidian Paper Card

## 作用边界

本 skill 接收已有论文 Markdown，输出明确路径下的 Obsidian 论文阅读卡。它不上传文件到 MinerU、不轮询 MinerU、不需要 MinerU token，也不依赖解析临时目录。双语翻译会通过 Codex 将冻结的论文单元发送给所选模型；可选 Spark 审查会发送当前论文 Markdown，远程图片下载会连接原图片站点。安装条件、数据边界与本地会话访问范围见 [README.md](README.md)。

## 自然语言端到端入口

将“将 Zotero 中的《论文题名》转化为中英对照版”“把某篇论文做成双语阅读卡”等表述视为一个端到端论文请求，而不是要求用户先给出 PDF 或 Markdown 路径。严格按以下顺序解析来源：先在当前 vault 的 `Clippings/` 中按 frontmatter `title` 精确匹配或规范化文件名匹配既有 Markdown；未找到时再以同一规则搜索 `论文/`；两处均未找到时，才从 Zotero 本地库只读定位精确题名对应的 PDF。不得只搜索 `论文/` 后就开始联网，也不得把整个 vault 的模糊全文搜索替代这三个有序、可审计的来源步骤。Zotero 命中后优先复用 PDF SHA-256 与解析指纹完全匹配的 source package；三个来源均未命中后，只有用户明确提供或当前任务已明确授权取得外部文献时，才允许使用外部 PDF/source package，最后才加载 `mineru-api-markdown` 解析并归档。两个 skill 的职责不得混合。

当用户没有另行指定时，使用当前 vault 作为 `vault-root`，将最终笔记写到 `论文/<清理后的论文题名>.md`，将中间来源包写到 `<vault-root>/.tmp/mineru-zotero/<Zotero 条目键>/`；实际调用脚本时仍显式传入这些路径。默认使用 `translation-mode=bilingual`、`concept-links=off`，并在全部校验通过后写入。概念链接是论文卡晋升后的可选、可失败步骤，不得阻塞翻译或交付。

用 `scripts/resolve_zotero_paper.py` 一次执行完整来源解析：确定性扫描 `Clippings/`，再扫描 `论文/`，最后才复制 `zotero.sqlite`、WAL、SHM 并在只读快照中完成题名、附件与 PDF 定位。脚本输出 `source_search_order`、`source_kind` 与 `source_scope`；Vault 命中时不得创建 Zotero 快照。任一层级精确题名不唯一、Zotero 没有或存在多个 PDF 附件、或不同源文件将覆盖既有目标笔记时退出非零，不得猜测或覆盖。成功后才进入 MinerU；完全匹配 PDF hash 与解析指纹的 source package 可直接复用。使用 MinerU 前必须取得当前用户对相关文件和目标服务的上传授权；本 skill 不携带任何历史或默认上传许可。只有当前连续工作流中已经明确授予且范围匹配的许可才可沿用。

输入可以是：

- 单独的 Markdown 文件，图片路径相对于该 Markdown 可解析；
- 符合 schema_version 1 的 source package。source package 提供附件映射、来源 PDF 与布局 JSON 时，可额外完成图片归档、来源追溯和 figure crop。

第一版只服务论文阅读卡，不把任意 Markdown 自动泛化为通用知识卡。

## 公开命令

    python scripts\start_obsidian_paper_card.py --title <exact-title> --zotero-data-dir <path> --vault-root <path> --target-directory 论文 --snapshot-directory <workflow-temp> --workflow-dir <paper-workflow>

    python scripts\resolve_zotero_paper.py --title <exact-title> --zotero-data-dir <path> --vault-root <path> --target-directory 论文 --snapshot-directory <workflow-temp>

    python scripts\build_obsidian_paper_card.py --input-markdown <path> --vault-root <path> --output-note <path> --translation-mode <bilingual|none> [--concept-links <off|report|write>] [--source-package <directory>] [--bibtex-path <references.bib>] [--resource-directory <vault-relative-dir>] [--stable-resource-root <vault-relative-dir>] [--workflow-dir <directory>] [--translation-stage <run|prepare|layout|finalize>] [--worker-backend <native-subagent|direct-cli>] [--translation-agent-role-file <path>] [--image-converter-layout <center|off>] [--overwrite-image-converter-alignments] [--in-place] [--write]

    python scripts\run_native_paper_translation_worker.py import --assignment <native-assignment.json> --workflow-dir <workflow-directory> --discover-rollout

    python scripts\sync_image_converter_alignments.py --vault-root <vault-path> --markdown-path <paper-note> [--layout-dir <mineru-layout-directory>] [--overwrite-existing] [--write]

    python scripts\normalize_obsidian_figure_links.py --vault-root <vault-path> --markdown-path <paper-note>

    python scripts\normalize_obsidian_figure_links.py --vault-root <vault-path> --directory <paper-directory> [--recursive]

The figure-link migration command is a read-only preflight unless `--write` is explicit. It preflights every selected note before writing, creates a timestamped per-note backup, preserves legacy image wikilink style, and skips ambiguous short wikilinks.

规则：

- input-markdown、vault-root、output-note、translation-mode 必须显式提供；不使用硬编码 vault 路径。concept-links 默认 off。
- 不带 write 时只输出 dry-run JSON；带 write 才写入 Markdown、图片和 card report。
- output-note 必须位于 vault-root 内。输入输出相同必须显式传入 in-place，并自动生成时间戳备份。
- 默认把资源归档到 `_resources`；`--resource-directory` 可选择其他 vault 内稳定目录，validator 默认允许 `_resources`、`_附件` 及其任意子目录，也可用重复的 `--stable-resource-root` 显式配置。绝对路径、越出 vault 和临时目录仍硬失败。
- translation-mode=none 只做结构与渲染整理。bilingual 用于完整中英对照流程。
- concept-links=write 只自动写入可信候选；report 只生成候选报告；off 关闭双链。
- `image-converter-layout=center` 是默认值：仅在目标 vault 已启用 Image Converter 时，为最终图片合并显式 `center + no-wrap` 缓存；插件未启用则安全跳过。已有手工 alignment 默认保留，只有显式 `--overwrite-image-converter-alignments` 才覆盖。

## 标准流程

1. 项目 `AGENTS.md` 与本 `SKILL.md` 各完整读取一次即可；允许在同一个只读工具调用中读取，禁止把内容打印给用户、重复读取、先扫描全文、先统计文件或为了“熟悉论文”加载正文。历史 memory 只有出现具体复用问题时才做一次关键词命中读取。
2. 自然语言题名默认只运行一次 `start_obsidian_paper_card.py`；它按 `Clippings -> 论文 -> Zotero` 解析来源，并在 Vault Markdown 命中时直接执行一次 bilingual `run`。不得再手工 `rg` 全库、单独执行 resolver、打印源文、额外 dry-run 或先做人工 QA。只有 bootstrap 返回 `requires_markdown_parse` 时才切换 MinerU；显式 Markdown 路径则直接调用 builder。
3. translation-mode=none 时，使用 write 完成 frontmatter、标题、图片、citation、References、表格、公式、concept report 与 validator。
4. bilingual 的标准入口只调用一次 `run`。它确定性过滤网页 chrome、修复可识别的非正文格式、冻结 schema v5 正文 packet 并计算 cache；远程图片、图片对齐、Figure 悬停和不改变正文单元的排版工作与翻译 worker 并行。cache 全命中时直接完成；存在 pending 单元时返回唯一 native handoff，绝不启动本机 `codex exec`。
5. preflight 只对正文内容完整性和安全不变量设硬门禁：正文丢失或语义结构无法保全、公式/引用/脚注/表格数据的 token 契约损坏、图片目标错误或越出 Vault、无可恢复乱码、编码/路径/哈希/覆盖不可信。纯编号、统计值、脚注编号标签、作者或机构身份、URL/DOI/ORCID、独立缩写等“精确复制即正确”的非正文单元必须在 packet 冻结前确定性标记为 passthrough，不得发送给 worker，也不得计入中文缺失 gate。frontmatter、标题层级、网页 chrome、References 附加元数据、作者别名、图注的重复 TeX 显示层、图片对齐、缺失 Figure 悬停等其他非正文问题，应依次确定性修复、由主代理对 workflow-local 副本做最小局部修复、或降级为 warning；不得仅因此延迟正文 worker。图注自然语言本身仍是语义内容，不得删改或跳过翻译。
6. 只按一个 `status` 字段分派：`completed` 直接交付；`awaiting_native_subagent` 则按返回的 `spawn_agent` 对象创建唯一 `paper-translation-worker`，立即并行执行一次 `layout`，只等待一次，再 import；默认 task name 必须由论文工作流 slug、规范化路径短哈希和 attempt 共同生成，不得只使用末级 `workflow` 目录名。import 为 `completed` 时 finalize，`partial_success` 或 `failed` 按第 9 条处理。不得使用 generic role、覆盖固定的 `gpt-5.6-terra/high`、回退 direct CLI 或轮询状态。
7. 权威翻译规范仍来自 `references/paper-translation-prompt-fragment.md`，由同步生成的 `paper-translation-worker` 角色承载。子代理只读取 handoff assignment 与其指向的 compact packet；不得读取完整 Markdown、项目 AGENTS、父线程历史、ai-research-writing、humanizer、其他 skill、插件或 connector，也不得写文件。它只返回一个符合 frozen JSON Schema 的最终 JSON object；父线程的确定性 bridge 校验角色、父子线程、实际模型、effort、session、unit 顺序和哈希后，以 UTF-8 无 BOM 原子写入 assignment 指定的 JSONL。
8. 公式、引用、脚注和强调 token 在 worker transport packet 中先替换成唯一短占位符，模型只翻译周围语义；bridge 要求每个占位符恰好出现一次并恢复冻结原 token，随后再执行原始 token 数量验证。引用 normalizer 必须保护 frontmatter、代码、既有链接和 `$...$` / `$$...$$`，不能把公式区间 `[1,5]` 当成引用。
9. bridge 逐单元验收并立即缓存合法译文。若旧 packet 或未知剪藏结构仍把可确定识别的身份、编号、标识符单元送入 worker，worker 对该单元精确复制源文时 bridge 必须直接验收为非正文 passthrough，不得把它计入失败阈值或触发 recovery。`partial_success` 只处理真正缺少中文语义的正文单元；正文单元哈希未变时必须复用正文 cache，不得重启全文 worker。只有确实缺少中文语义的 1–4 个正文单元才创建一次小型 recovery worker；全局 token/运行证明错误仍不得缓存或绕过。
10. workflow-state 与 packet 的新任务继续使用 schema v5，并记录 passthrough、model、cache、runtime 与 References 契约。passthrough 是开放的语义判定，不是封闭白名单：单元若仅承担身份、定位、标识、符号、格式或机器可执行结构功能，且精确复制是其正确呈现，可由主代理判定 passthrough。常见但非穷举示例包括作者/单位等文前元数据、邮箱/URL/ORCID/DOI、版本号与账号、纯公式或冻结 token、无自然语言注释的代码/伪代码/命令/路径、面板标签与分隔符。论文题名、章节标题、摘要与正文论述、定义/命题/证明、图表说明及有语义的表格单元、解释性脚注、作品题名仍须翻译；元数据中若含影响理解的说明句也须翻译。passthrough 单元确定性复制到双语对应位置，必须保持 token、链接、换行与版式；历史 schema v1-v4 只读兼容。
11. prepare 必须先校验当前角色文件与权威 prompt 一致，再把角色 TOML 原子冻结为 workflow 内 `translation-agent-role-snapshot.toml`。handoff、import 和 finalize 校验该快照及其哈希，不再读取可能随后变化的全局角色文件。实际模型、effort、Codex 版本、child session ID、parent thread ID、agent path 与 agent role 仍必须来自唯一匹配的原生子代理 rollout；当前 attempt 的 requested/actual 不一致、packet/快照变化、attestation 缺失、rollout hash 或输出 hash 不一致时拒绝 finalize。
12. pending、partial、failed workflow 保留完整 artifact 以便恢复。成功晋升后执行 `completed_compact_v1`：保留 workflow-state、逐单元 cache、translation-validation-report、layout-asset-report、远程资源/crop cache、冻结角色快照和最后一次 runtime attestation；删除 packet、assignment、compact transport、merge template、临时输出与旧 attempt 过程文件。对最终笔记只在内容确实变化时创建晋升前备份，并只保留最近 2 个同类备份；不清理用户其他命名的备份。

### 运行效率硬约束

- 普通按题名转换使用一次 bootstrap；显式 Markdown 使用一次 `translation-stage=run`。命中 handoff 后立即创建唯一角色 worker，并行执行一次 layout，只做一次 `wait_agent`、一次 import 和一次 finalize。除返回的失败指标外不读取长日志、完整 packet、全文或逐项报告；格式 warning 不触发人工全文检查。启动阶段目标是脚本计算用时数秒级，总墙钟时间主要由唯一翻译 worker 决定。
- 模型 transport 使用紧凑行：正文只传 `id/type/text`，确定性题名只传 `id/type/title`，歧义题名只传 `id/type/entry`。同一英文题名或完整书目不得在同一输入行重复。
- cache 只接受 `runtime_verified=true` 且达到 `publication` quality tier 的结果；默认不要求缓存的 exact model/effort 与当前请求字符串相同，因此同质量的已验证模型升级可直接复用。只有用户显式要求精确 runtime 时才启用 strict runtime cache。cache 全命中时不创建 subagent；存在 pending 单元时每个 native attempt 只创建一个 Terra High `paper-translation-worker` 并只等待一次。
- worker 输入 packet 使用 Windows PowerShell 5.1 可明确识别的 UTF-8 with BOM，提示词要求 `Get-Content -Encoding UTF8`；worker 输出、cache、attestation、workflow-state 与最终 Markdown 保持 UTF-8 无 BOM，换行固定为 LF，并记录 schema、字段计数与 SHA-256。
- 主线程只读取 builder/bridge 的紧凑 JSON 状态和子代理完成事件；transport 字节数、单位分类、耗时和运行证明写入 workflow-state/attestation。不要读取全文、完整 packet、完整 worker rollout 或反复统计最终卡片。
- 确定性格式由脚本处理；脚本暂未覆盖的非正文问题允许主代理在 workflow-local 副本上做最小修复并记录行号、前后文本和理由，无需再次征求许可。只有语义错误、数据丢失、正文引用/公式/表格损坏、错误图片目标、越出 Vault、乱码、运行证明、契约或覆盖安全不可信才阻断。样式和增强项修复失败一律 warning。
- layout 与 worker 并行时必须完成网页剪藏的非正文链接清理：只含 `#fig-*` 的 Figure 网页片段链接若对应 HTML id/name 或 Obsidian block anchor 未随剪藏保留，则去除链接标记、保留可见 Figure 文本并记录计数。此类孤立网页锚点不得等到 finalize 才修复，也不得作为断图硬 gate；真实图片附件的 missing/out-of-vault/not-image/wrong-target 仍保持硬失败。

用户对完整双语论文阅读卡的明确请求，同时构成创建一个受控 `paper-translation-worker` 子代理的授权；授权仅限当前论文、当前 frozen packet 和一次 native attempt，不得扩展为无关子任务、其他文件、generic agent 或外部翻译引擎调用。

## Spark 只读排版审查

当排版问题需要少量泛化判断但不值得占用 Terra High 时，可使用 GPT-5.3-Codex-Spark。只在用户已明确允许把当前论文 Markdown 发送给该外部模型时启用；不要把授权扩展到其他文档。

Spark 与首次 Terra attempt 必须并行，而不是串行门禁：确定性预处理冻结翻译 packet 后立即启动当前唯一的 Terra High worker；同时在独立 per-paper workflow 目录对原始 Markdown 启动 Spark。已知且可确定的规则（例如 `Abstract` 必须为 H2、数字脚注引用之间的逗号必须进入 `<sup>,</sup>`）仍由 normalizer 处理，不得等待 Spark。Spark 结果只在 merge/finalize 前审查和应用，不得修改正在翻译的 packet，也不得与当前 Terra attempt 并发启动另一个翻译 worker。

使用 `references/paper-formatting-prompt-fragment.md` 与 `references/paper-formatting-final.schema.json`。模型只读源文件并返回完整行级 edit 建议；父进程再调用应用器：

    python scripts\run_spark_format_review.py --source <workflow-source.md> --guide references\paper-formatting-prompt-fragment.md --schema references\paper-formatting-final.schema.json --result <workflow-dir>\spark-review.json --log <workflow-dir>\spark-worker.log --model gpt-5.3-codex-spark

    python scripts\apply_spark_format_review.py --source <workflow-source.md> --review <workflow-dir>\spark-review.json --output <workflow-dir>\spark-normalized.md

Terra 翻译标准路径不再启动嵌套 Codex CLI，因此不得用 `codex login`、修改 `.codex/config.toml`、修改认证文件、代理环境或 `danger-full-access` 处理翻译网络问题。可选 Spark 审查仍是独立 backend；若其外层 workspace 沙箱阻止嵌套 CLI 读取本机登录状态，只能在告知论文会发送给外部模型并取得明确授权后，对 Spark 父 runner 请求受限提权，且 Spark 子进程仍须证明受限沙箱。Spark 的网络或认证问题不得改变 Terra translation worker 的 native-subagent 编排。

应用器必须校验源 SHA-256、精确 `before` 行、允许的 edit type、行数、引用编号序列、图片、URL、脚注定义、代码围栏、表格管道、UTF-8 无 BOM 与 CRLF/LF 保持。任何 mismatch、越界编辑或 unresolved 项都拒绝写入。应用后仍运行 prepared/final validator；Spark 自报通过不能替代确定性验证。

## 网页剪藏噪声预过滤

在 frontmatter、引文、图片与翻译分包之前运行确定性过滤器。只删除可审计的站点 chrome：`nav/script/style/noscript` 块、已知导航标签的独立链接、H1 题名前指向站内片段/根路径的独立导航链接，以及 title 含 `‣`/`›` 的剪藏目录链接。不得删除正文句子中的链接、H1 后普通的独立章节锚点、代码围栏或 frontmatter。把删除行号、原因、截断后的原文和总数写入 preflight/workflow-state；过滤规则无法确定时保留原文，不交给翻译模型猜测。

## 资源与来源包

source package 的 source-manifest.json 只允许相对路径。处理器优先读取 artifacts.asset_map；裸 Markdown 中可从输入目录解析的本地图片复制到配置的稳定资源目录。裸 Markdown 中的 `http(s)`/data 图片先按上下文分级：正文实际引用、图注相邻或 Figure/Fig./图标识的论文图为 `critical`，下载失败阻止 finalize；明确的 logo/icon/avatar/badge/banner/social/navigation/tracking 等站点装饰为 `noncritical`，直接从成品移除并记录 warning，不进入下载队列。无法确定时按关键资源保留。下载与 worker 并行，失败不丢弃已通过逐单元验证的译文。

来源包资源的 SHA-256 可直接复用；资源计划先完成，再执行可选 crop，最后只复制一次。远程资源缓存只保存 URL SHA-256、目标文件名和内容 SHA-256，不把绝对路径写入可复用协议；命中时必须重新核对文件哈希。concept report 使用 vault `.tmp/paper-card-cache/concept-index-v1.json` 的增量索引，但 stem/alias、歧义与泛词拒绝规则不变。

当来源包同时提供 source_pdf 与布局 JSON 时，postprocess_mineru_figure_crops.py 可以在本地生成完整 figure crop。crop 必须先写入同目录临时 PNG，完成 Pillow 解码校验后再原子晋升；以来源 PDF、布局 JSON、图块映射、bbox、渲染参数和裁剪算法版本组成 fingerprint cache，命中时仍核对目标文件 SHA-256。来源信息不完整、映射失败或图片跨页时必须跳过并在 report 中写明原因，不得重跑 MinerU。

最终图片使用标准 Markdown 相对路径，解析后必须位于配置允许的 vault 内稳定资源根；默认允许 `_resources`、`_附件` 及其子目录。不使用临时目录、MinerU images 路径或 Obsidian 图片 wikilink。builder 自动补入 `paper-card-centered-images` cssclass；posthoc 审查发现缺失时给 warning，不因居中样式阻断可用成品。

Image Converter 1.4.4 的宽高可以写进 Markdown 图片 alt 区中的 `|宽x高`，但 `left / center / right` 与 `wrap` 不属于 Markdown 语法，而是保存在 `.obsidian/image-converter-image-alignments.json`。因此不得伪造不存在的图片引用参数。builder 在论文卡原子晋升后调用 `sync_image_converter_alignments.py`，用与 Image Converter 1.4.4 一致的 MurmurHash3 x64 128 键合并显式居中状态；Windows 渲染路径按插件实际的 `/_resources/...` 规范计算。缓存写入采用 UTF-8 无 BOM、并发变更检测、时间戳备份和原子替换，card report 会记录 `reload_required`；若 Obsidian 正在运行，用户需执行 Image Converter 的 `Reload plugin` 命令或重启 Obsidian，才能让内存缓存重新载入。

当 source package 或显式 `--layout-dir` 提供 MinerU `*_content_list.json` 时，同步器会用 image/text bbox 检测“窄图位于一侧且另一侧有纵向重叠正文”的浮动候选，并写入报告的 `layout_recommendation`。本阶段不得自动应用 `left/right + wrap`：MinerU 线性 Markdown 通常把原本重叠的正文排在图片之前，而 CSS float 只能影响图片之后的节点，独立图注也会被错误绕排。只有后续同时完成图片前移、双语正文边界、图注同容器和 clear-float 规则后，才能开启自动浮动。

After final resource anchoring, the builder links body references such as `Fig. 4`, `Figure 4(a)`, `Fig. S1`, and their Chinese equivalents directly to the final image attachment. It also recognizes LaTeXML/arXiv clipping separators such as literal `~` and U+02DC `˜`, normalizes the visible label to one ordinary space, and rejects any residual malformed separator. In bilingual mode this runs only after translation merge, so translation packets, protected-token transport, and caches remain unchanged.

Only a unique archived image or a successful full PDF crop is linkable. A multi-asset figure without a recovered crop is reported as `ambiguous/multi_asset` and is never linked to the first fragment. Explicit enumerations are linked item by item; numeric ranges are preserved and reported. Captions, frontmatter, headings, code, math, tables, References, image alt text, URLs, and existing links are protected. Repeated normalization is byte-idempotent.

## 引文、表格和公式

- 在构建 preflight、生成翻译 packet 之前运行 `normalize_obsidian_citations.py`，直接将可核对的数字引用转换为 References block link；不要等 validator 报错后再补做。
- 对正文中的 `\[citation_key, ...\]` 命名引文，必须从唯一 BibTeX 解析；正文 key 缺失仍硬失败。References 区域不参加命名引文扫描，其中 `\[cs.RO\]` 等 arXiv 学科标签确定性规范为普通 `[cs.RO]` 元数据，不要求 BibTeX。
- 先区分原生 Markdown 脚注与参考文献型脚注：命名脚注和解释性数字脚注保持 `[^label]`；位于 References 区域、具有作者/年份/期刊/会议/arXiv/DOI 等书目结构，或属于连续书目编号组的数字脚注，转换为 `[[#^ref-n|ⁿ]]` 与 `[n] ... ^ref-n`。
- 规范化后立即检查“参考文献型脚注残留数必须为 0”，再进入图片处理和翻译分包。validator 允许原生脚注，但必须拒绝残留的参考文献型脚注，以及对已有 `^ref-n` 仍使用 `[^n]` 的正文引用。
- `## References` 下的书目条目先由 normalizer 结构化为 `[n] 英文书目 ^ref-n`。保持完整英文书目正文，只翻译作品题名；不翻译作者、期刊、会议、出版社、年份、页码、DOI 或 URL。高置信格式只把抽取出的英文题名作为 `reference_title` 单元交给当前 Terra High attempt；歧义格式才把该条完整英文书目交给 worker，并要求返回原条目中的精确 `source_title` 与中文 `zh`。只有作者、软件或项目名、arXiv 元数据、DOI、日期和 URL 而没有自然语言作品题名的条目标记为 `skipped`，不生成 `reference_title` 单元，也不强制添加 `《中文题名》`。
- layout 将中文题名作为 `《中文题名》` 直接附在英文书目末尾、`^ref-n` 之前，固定为 `[n] English entry.《中文题名》 ^ref-n`；不得生成独立中文段落、引用块或整条中文书目翻译。已有规范题名尾注保持幂等，不重复生成。
- prepare 记录移除中文题名尾注后的英文 References 基底哈希；final validator 从最终条目确定性移除 `《……》` 后重建英文基底并核对哈希，同时校验每个编号、题名尾注、block ID 与正文链接一一对应。普通解释性 Markdown 脚注仍然合法。
- preflight 中转换 HTML 表格；简单表格直接转换，rowspan/colspan 确定性展开为矩形 Markdown 网格并重复跨越单元内容。嵌套、列数无法闭合或不可无损处理的结构必须在生成翻译 packet 前失败。
- 表格单元中的 Obsidian 引文链接必须把别名分隔符写成 `\|`，例如 `[[#^ref-6\|⁶]]`；normalizer、翻译 token 保护和 validator 均须同时识别该表格安全形式，且表格每行列数在规范化前后保持一致。
- 标题层级先走确定性规范化：首个 H1 保留为论文题名，后续 H1 降为 H2，任意层级且标题精确为 `Abstract` 的章节固定规范为 `## Abstract`；不得改写 Proposition、Proof 等局部语义标题。builder 自动修复空 aliases、Abstract 层级和图片 cssclass；posthoc 审查发现残留时给 warning。此类无歧义格式不调用模型。
- 表格内短公式使用 $...$；不在最终卡片保留 HTML table 以牺牲 Obsidian 的公式渲染。
- 在翻译分包前只对 Figure/Fig./图图注行修复“已渲染文本 + 重复 TeX fallback”；不得改写正文或图注自然语言。未知残留记录行号并 warning，不阻止正文 packet、worker 或晋升；主代理可在不改变图注意义时对 workflow-local 图注做最小显示修复。
- 使用 `validate_full_paper_card.py --stage prepared|final --mode pipeline_finalize|posthoc_audit` 一次检查。`pipeline_finalize` 是新流水线晋升门禁，必须提供 frozen packet/workflow provenance；`posthoc_audit` 用于既有或后处理成品，packet 缺失只报告 `provenance_unverified` warning，仍独立检查正文、图片、引用、References、表格、公式、路径、占位符与乱码。`validate_obsidian_paper_note.py` 仅保留为兼容包装器。

- Final validation reports `figure_targets`, `figure_mentions`, `figure_links_written`, `unmatched_figure_mentions`, `ambiguous_figure_targets`, and `broken_figure_links`. builder 自动补齐唯一目标的正文 Figure hover 链接；仍缺失的 hover 增强只给 warning。missing/out-of-vault/non-image target、figure-to-target mismatch 仍硬失败；unavailable figures、ranges 和 unresolved multi-asset figures 保持 warning。

## 安全的 existing concept auto-link

auto_link_existing_concepts.py 只扫描已有 vault note：

- 文件名 stem 与 frontmatter aliases 是可信候选，且只有无歧义、非泛词、通过保护区检查时才能自动写入。
- 标题、括号和正文推导词是 review candidate，永远不自动写入。
- 内置泛词拒绝表包含信息量等已知误链；可用 denylist-file 补充 vault 特有拒绝词。
- Unitree 等从较长标题派生的短词只能报告，不能自动链接到具体数据集或论文卡。
- 新链接只允许写入中文阅读层，不能进入英文 blockquote、公式、代码、图片、表格、References 或 citation。

若确实希望某个推导词自动链接，应将它显式加入目标 note 的 aliases，而不是依赖标题推断。
