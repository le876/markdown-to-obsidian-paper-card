from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from normalize_obsidian_citations import (  # noqa: E402
    classify_citation_footnote_labels,
    count_named_citation_groups,
    normalize,
    normalize_arxiv_subject_labels,
)
from build_translation_worker_assignment import metrics  # noqa: E402
from paper_translation_packet import protected_tokens  # noqa: E402
from validate_obsidian_paper_note import validate_references  # noqa: E402


class CitationFootnoteNormalizationTests(unittest.TestCase):
    def test_named_latexml_citations_use_bibtex_and_are_idempotent(self) -> None:
        source = (
            "Prior work \\[rt1, bridgev2\\] supports the claim. "
            "The translation repeats \\[rt1\\].\n"
        )
        bibtex = (
            "@article{rt1, author={Alice Smith and Bob Jones}, title={RT One}, "
            "journal={Robotics}, year={2024}, doi={10.1000/rt1}}\n"
            "@inproceedings{bridgev2, author={Chen Li}, title={Bridge V2}, "
            "booktitle={Conference on Robots}, year={2023}}\n"
        )
        with tempfile.TemporaryDirectory() as raw_dir:
            bib_path = Path(raw_dir) / "references.bib"
            bib_path.write_text(bibtex, encoding="utf-8", newline="\n")
            normalized = normalize(source, bib_path)

        self.assertEqual(count_named_citation_groups(normalized), 0)
        self.assertIn(
            "Prior work [[#^ref-1|¹]]<sup>,</sup>[[#^ref-2|²]] supports",
            normalized,
        )
        self.assertIn("The translation repeats [[#^ref-1|¹]]", normalized)
        self.assertIn("## References", normalized)
        self.assertIn("[1] Smith, Alice; Jones, Bob. RT one. Robotics. 2024.", normalized)
        self.assertIn("https://doi.org/10.1000/rt1", normalized)
        self.assertIn("^ref-2", normalized)
        self.assertEqual(normalize(normalized), normalized)
        self.assertEqual(validate_references(normalized), [])

    def test_named_citations_require_a_complete_bibtex_source(self) -> None:
        source = "Claim \\[known, missing\\].\n"
        bibtex = "@article{known, author={A Author}, title={Known}, year={2024}}\n"
        with tempfile.TemporaryDirectory() as raw_dir:
            bib_path = Path(raw_dir) / "references.bib"
            bib_path.write_text(bibtex, encoding="utf-8", newline="\n")
            with self.assertRaisesRegex(ValueError, "missing"):
                normalize(source, bib_path)

    def test_named_citation_syntax_inside_code_and_math_is_protected(self) -> None:
        source = "Inline `$x=\\[rt1\\]$` and code `\\[rt1\\]`.\n"
        self.assertEqual(count_named_citation_groups(source), 0)
        self.assertEqual(normalize(source), source)

    def test_escaped_web_clipping_labels_followed_by_urls_are_not_citations(self) -> None:
        source = "\\[Website\\]https://example.com \\[Dataset\\]https://example.com/data\n"
        self.assertEqual(count_named_citation_groups(source), 0)
        self.assertEqual(normalize(source), source)

    def test_arxiv_subject_metadata_in_references_is_not_a_named_citation(self) -> None:
        source = (
            "Claim text.\n\n## References\n\n"
            "[1] A. Author. A paper, March 2026. arXiv:2603.00001 \\[cs.RO\\]. ^ref-1\n"
        )
        self.assertEqual(count_named_citation_groups(source), 0)
        normalized, replacements = normalize_arxiv_subject_labels(source)
        self.assertEqual(replacements, 1)
        self.assertIn("arXiv:2603.00001 [cs.RO]", normalized)
        self.assertEqual(normalize(source), normalized)

    def test_native_numeric_footnote_is_preserved(self) -> None:
        source = (
            "A local implementation detail uses a native footnote[^1].\n\n"
            "[^1]: This note explains an implementation choice and is not a publication.\n"
        )
        self.assertEqual(classify_citation_footnote_labels(source), set())
        self.assertEqual(normalize(source), source)
        self.assertEqual(validate_references(source), [])

    def test_mixed_native_and_bibliographic_footnotes(self) -> None:
        source = (
            "The published result[^1] also needs a local explanation[^2].\n\n"
            "[^1]: A. Author (2024) Reliable citation normalization. Journal of Tests 3 (2), pp. 1–9.\n\n"
            "[^2]: This is an explanatory Markdown footnote, not a bibliography entry.\n"
        )
        normalized = normalize(source)
        self.assertEqual(classify_citation_footnote_labels(source), {1})
        self.assertIn("[[#^ref-1|¹]]", normalized)
        self.assertIn("[^2]", normalized)
        self.assertIn("[^2]: This is an explanatory Markdown footnote", normalized)
        self.assertIn("[1] A. Author (2024)", normalized)
        self.assertIn("^ref-1", normalized)
        self.assertNotIn("[^1]", normalized)
        self.assertEqual(validate_references(normalized), [])

    def test_bilingual_reference_pairs_move_under_existing_heading(self) -> None:
        source = (
            "Main claim[^1].\n\n"
            "## References\n\n"
            "## Appendix\n\n"
            "Appendix claim[^2].\n\n"
            "> [^1]: A. Author (2024) English title. Journal of Tests 1, pp. 1–2.\n\n"
            "[^1]: A. Author (2024) 中文标题。测试期刊 1，第 1–2 页。\n\n"
            "> [^2]: B. Author (2025) Another title. Proceedings of TestConf, pp. 3–4.\n\n"
            "[^2]: B. Author (2025) 另一标题。测试会议，第 3–4 页。\n"
        )
        normalized = normalize(source)
        self.assertIn("Main claim[[#^ref-1|¹]].", normalized)
        self.assertIn("Appendix claim[[#^ref-2|²]].", normalized)
        self.assertIn("> [1] A. Author (2024) English title.", normalized)
        self.assertIn("[1] A. Author (2024) 中文标题。测试期刊 1，第 1–2 页。 ^ref-1", normalized)
        self.assertLess(normalized.index("^ref-2"), normalized.index("## Appendix"))
        self.assertEqual(normalize(normalized), normalized)
        self.assertEqual(validate_references(normalized), [])

    def test_contiguous_bibliography_includes_weak_web_entry(self) -> None:
        source = (
            "Claims[^1] [^2] [^3].\n\n"
            "[^1]: A. Author (2024) First paper. Journal of Tests.\n\n"
            "[^2]: Generic documentation, URL https://example.com/tool.\n\n"
            "[^3]: B. Author (2025) Third paper. Proceedings of TestConf.\n"
        )
        self.assertEqual(classify_citation_footnote_labels(source), {1, 2, 3})
        normalized = normalize(source)
        self.assertNotRegex(normalized, r"\[\^[123]\]")
        self.assertIn("[2] Generic documentation, URL https://example.com/tool. ^ref-2", normalized)

    def test_contiguous_group_does_not_absorb_native_note(self) -> None:
        source = (
            "Claims[^1] need a local caveat[^2] and another source[^3].\n\n"
            "[^1]: A. Author (2024) First paper. Journal of Tests.\n\n"
            "[^2]: This is a local explanatory note, not a publication.\n\n"
            "[^3]: B. Author (2025) Third paper. Proceedings of TestConf.\n"
        )
        self.assertEqual(classify_citation_footnote_labels(source), {1, 3})
        normalized = normalize(source)
        self.assertIn("[[#^ref-1|¹]]", normalized)
        self.assertIn("[[#^ref-3|³]]", normalized)
        self.assertIn("[^2]", normalized)
        self.assertIn("[^2]: This is a local explanatory note", normalized)

    def test_arxiv_clipping_bibliography_after_appendix_is_rehomed_by_strong_group_signal(self) -> None:
        source = (
            "Claims[^1] [^3] [^5].\n\n"
            "## References\n\n"
            "## Appendix A Details\n\n"
            "Appendix text.\n\n"
            "[^1]: First model. arXiv preprint arXiv:2503.14734. Cited by: §1.\n\n"
            "[^2]: Second model. In Robotics: Science and Systems. Cited by: §1.\n\n"
            "[^3]: Third model. External Links: [Link](https://example.com/3) Cited by: §2.\n\n"
            "[^4]: Fourth model. In Proceedings of TestConf. Cited by: §2.\n\n"
            "[^5]: Fifth model. arXiv preprint arXiv:2506.00005. Cited by: §3.\n"
        )
        self.assertEqual(classify_citation_footnote_labels(source), {1, 2, 3, 4, 5})
        normalized = normalize(source)
        self.assertNotRegex(normalized, r"\[\^[1-5]\]")
        self.assertIn(
            "Claims[[#^ref-1|¹]]<sup>,</sup>[[#^ref-3|³]]<sup>,</sup>[[#^ref-5|⁵]].",
            normalized,
        )
        self.assertIn("[5] Fifth model. arXiv preprint arXiv:2506.00005. Cited by: §3. ^ref-5", normalized)
        self.assertLess(normalized.index("^ref-5"), normalized.index("## Appendix A Details"))
        self.assertEqual(validate_references(normalized), [])

    def test_numeric_footnote_cluster_moves_baseline_commas_into_superscript(self) -> None:
        source = (
            "Prior work[^15], [^6], [^16] supports the claim.\n\n"
            "[^6]: B. Author (2024) Second paper. Journal of Tests.\n\n"
            "[^15]: A. Author (2023) First paper. Proceedings of TestConf.\n\n"
            "[^16]: C. Author (2025) Third paper. arXiv:2501.00001.\n"
        )
        normalized = normalize(source)
        self.assertIn(
            "Prior work[[#^ref-15|¹⁵]]<sup>,</sup>[[#^ref-6|⁶]]<sup>,</sup>[[#^ref-16|¹⁶]] supports",
            normalized,
        )
        self.assertNotRegex(normalized, r"\]\]\s*,\s*\[\[")

    def test_validator_rejects_bibliographic_footnote_but_allows_native(self) -> None:
        bibliographic = (
            "Claim[^1].\n\n"
            "[^1]: A. Author (2024) Paper title. Journal of Tests.\n"
        )
        errors = validate_references(bibliographic)
        self.assertTrue(any("bibliographic footnotes remain" in error for error in errors))

        native = "Claim[^1].\n\n[^1]: A short explanatory note.\n"
        self.assertEqual(validate_references(native), [])

    def test_validator_rejects_footnote_syntax_for_existing_ref_block(self) -> None:
        source = (
            "Claim[^1].\n\n"
            "## References\n\n"
            "[1] A. Author (2024) Paper title. ^ref-1\n"
        )
        errors = validate_references(source)
        self.assertTrue(any("footnote syntax for existing References blocks" in error for error in errors))

    def test_escaped_figure_number_is_not_treated_as_citation(self) -> None:
        source = (
            "See Fig. \\[2\\] and citation [2].\n\n"
            "## References\n\n"
            "[2] A. Author (2024) Paper title. ^ref-2\n"
        )
        normalized = normalize(source)
        self.assertIn(r"Fig. \[2\]", normalized)
        self.assertIn("citation [[#^ref-2|²]]", normalized)
        self.assertEqual(normalize(normalized), normalized)

    def test_wrapped_numbered_references_are_coalesced_before_block_ids(self) -> None:
        source = (
            "Claim [7] and another claim [8].\n\n"
            "## References\n\n"
            "[7] Anthony Author and Keerthana\n\n"
            "Gopalakrishnan. Rt-1: A complete title. 2022.\n\n"
            "[8] B. Author. Next paper. 2023.\n"
        )
        normalized = normalize(source)
        self.assertIn(
            "[7] Anthony Author and Keerthana Gopalakrishnan. Rt-1: A complete title. 2022. ^ref-7",
            normalized,
        )
        self.assertNotIn("Keerthana ^ref-7", normalized)
        self.assertEqual(normalize(normalized), normalized)
        self.assertEqual(validate_references(normalized), [])

    def test_complete_reference_does_not_absorb_orphan_paragraph(self) -> None:
        source = (
            "Claim [59] and another claim [60].\n\n"
            "## References\n\n"
            "[59] A. Author. A complete paper. 2022.\n\n"
            "review. Journal of Sport and Health Science, 2022.\n\n"
            "[60] B. Author. Next paper. 2023.\n"
        )
        normalized = normalize(source)
        self.assertIn("[59] A. Author. A complete paper. 2022. ^ref-59", normalized)
        self.assertNotIn("2022. review.", normalized)

    def test_math_code_frontmatter_and_links_are_protected_from_numeric_conversion(self) -> None:
        source = (
            "---\nrange: '[1,5]'\n---\n"
            "Outside [1,5], inline $a_{q,t}\\in[1,5]$, and link [1,5](target).\n\n"
            "```text\n[1,5]\n```\n\n"
            "$$\nq\\in[1,5]\n$$\n\n"
            "## References\n\n"
            + "\n".join(f"[{number}] A. Author. Paper {number}. 2024. ^ref-{number}" for number in range(1, 6))
            + "\n"
        )
        normalized = normalize(source)
        self.assertIn("Outside [[#^ref-1|¹]]<sup>,</sup>[[#^ref-5|⁵]]", normalized)
        self.assertIn("$a_{q,t}\\in[1,5]$", normalized)
        self.assertIn("[1,5](target)", normalized)
        self.assertIn("range: '[1,5]'", normalized)
        self.assertIn("```text\n[1,5]\n```", normalized)
        self.assertIn("$$\nq\\in[1,5]\n$$", normalized)
        self.assertEqual(validate_references(normalized), [])

    def test_table_citations_escape_alias_pipes_without_changing_columns(self) -> None:
        source = (
            "| System | Error |\n"
            "| --- | --- |\n"
            "| UMI [6] | ${\\sim}6$ |\n"
            "| FastUMI [7] | ${\\sim}10$ |\n\n"
            "```markdown\n"
            "| Literal [[#^ref-6|⁶]] | code |\n"
            "```\n\n"
            "## References\n\n"
            "[6] A. Author. UMI paper. 2024. ^ref-6\n\n"
            "[7] B. Author. FastUMI paper. 2025. ^ref-7\n"
        )
        normalized = normalize(source)

        self.assertIn("| UMI [[#^ref-6\\|⁶]] | ${\\sim}6$ |", normalized)
        self.assertIn("| FastUMI [[#^ref-7\\|⁷]] | ${\\sim}10$ |", normalized)
        self.assertIn("| Literal [[#^ref-6|⁶]] | code |", normalized)
        table_rows = normalized.split("\n\n", 1)[0].splitlines()
        self.assertTrue(all(len(re.findall(r"(?<!\\)\|", row)) == 3 for row in table_rows))
        self.assertEqual(normalize(normalized), normalized)
        self.assertEqual(validate_references(normalized), [])

        escaped_link = "[[#^ref-6\\|⁶]]"
        self.assertEqual(protected_tokens(escaped_link)["citation_tokens"], [escaped_link])
        self.assertEqual(metrics([escaped_link])["citation_links"], 1)

        missing_reference = normalized.replace(
            "[6] A. Author. UMI paper. 2024. ^ref-6\n\n",
            "",
        )
        self.assertTrue(
            any("citation link points to missing reference block: ^ref-6" in error for error in validate_references(missing_reference))
        )


if __name__ == "__main__":
    unittest.main()
