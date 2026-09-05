# Markdown to Obsidian Paper Card

以**论文排版与阅读体验**为核心的 Codex skill，支持图片超链接、文献上角标引用、公式校对，以及 subagent 中英对照翻译。

## 效果展示

<a href="docs/images/paper-card-preview.png">
  <img src="docs/images/paper-card-preview.png" alt="转换后效果图" width="560">
</a>

转换后效果图

## 能做什么

### 核心：论文排版

- **图片超链接**：正文图号链接到对应图片，支持点击跳转与悬停预览。
- **文献上角标超链接引用**：点击正文中的上角标，跳转至文末对应参考文献。
- **公式校对**：检查公式定界符、修复可识别的重复显示问题，校验翻译前后的公式完整性。
- **整体版式整理**：规范标题、摘要、图注、表格和参考文献，整理图片路径，清理网页剪藏噪声。

### Subagent 中英对照翻译

使用专用翻译子代理生成逐段对应的英文原文与中文译文，保留公式和引用。参考文献保留英文书目信息，并附上中文题名。

## 文件来源

本 skill 处理 **Markdown 及其配套图片**。

| 论文来源 | 准备方式 |
| --- | --- |
| **PDF** | 先通过 OCR/文档解析转为 Markdown，推荐 [MinerU 线上转换](https://mineru.net/)，保留导出的图片附件。 |
| **HTML 全文**，如 arXiv HTML 论文 | 使用 [Obsidian Web Clipper](https://help.obsidian.md/web-clipper) 直接剪藏到 vault 的 `Clippings/` 目录。 |
| **已有 Markdown** | 直接提供文件及配套图片。 |
| **Zotero** | 按题名定位论文；PDF 附件先完成 Markdown 转换。 |

## 安装

需要 **Python 3.11+、Obsidian 和支持自定义子代理的 Codex**。当前双语翻译使用 `gpt-5.6-terra`，推理强度为 `high`。

在 PowerShell 中运行：

```powershell
$skillRoot = Join-Path $env:USERPROFILE '.agents/skills/markdown-to-obsidian-paper-card'
git clone https://github.com/le876/markdown-to-obsidian-paper-card.git $skillRoot
Set-Location -LiteralPath $skillRoot
py -3 -m venv .venv
$paperPython = Join-Path $skillRoot '.venv/Scripts/python.exe'
& $paperPython -I -X utf8 -m pip install -r requirements.txt

# 改为自己的 Obsidian vault 路径，生成翻译子代理配置
$vaultRoot = 'D:/Notes/MyVault'
& $paperPython -X utf8 scripts/sync_paper_translation_agent_prompt.py --agent-path "$vaultRoot/.codex/agents/paper-translation-worker.toml" --write
```

安装后，在该 vault 中重新打开 Codex。

可选依赖：PDF 图片裁剪使用 `requirements-pdf.txt`；命名 BibTeX 引文解析需要 [Pandoc](https://pandoc.org/installing.html)。

## 使用

在 Codex 中指定 skill 和论文：

> 使用 $markdown-to-obsidian-paper-card，将 Clippings 中的《论文题名》整理为完整中英对照论文卡，保存到论文目录，使用翻译子代理完成翻译。

只需要排版时：

> 使用 $markdown-to-obsidian-paper-card，整理这篇 Markdown 的图片、文献上角标引用和公式排版，不翻译正文。

也可以直接提供 Markdown 文件路径。默认输出到 vault 的 `论文/` 目录。

## 许可

[MIT](LICENSE) · [第三方声明](THIRD_PARTY_NOTICES.md)
