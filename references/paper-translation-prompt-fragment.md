# Faithful academic paper translation

Translate every effective claim. Do not omit, collapse, summarize, speculate, add reading notes, or introduce facts that are absent from the source.

Preserve the author's person, voice, modality, certainty, qualifications, causal relations, contrasts, and conclusion strength. Prefer precise, natural academic Chinese over literal English word order, but never improve the argument by changing it.

Use one consistent term for one concept throughout the packet. Keep method names, models, datasets, metrics, variables, architectures, training paradigms, and established retrieval terms in English unless an unambiguous conventional Chinese term exists. Use concrete, checkable technical wording; avoid vague pronouns, inflated claims, generic transitions, and empty promotional language.

Preserve every supplied formula, citation, emphasis, and Markdown protection token exactly and place it near the same supported Chinese claim. Never add, remove, fabricate, renumber, or replace citations. Do not guess damaged OCR mathematics; keep the supplied protected token unchanged.

Keep figure and table captions aligned with the experiment, figure, table, and result described by the source. Removing filler is allowed only when it carries no propositional content; every effective source statement must remain represented in the translation.

For a `reference_title` unit, translate only the cited work's title. Do not translate or repeat authors, venue, publisher, year, pages, DOI, URL, reference number, or block ID. Return the exact source-language title substring as `source_title` and the Chinese title text as `zh`; omit book-title brackets, Markdown, commentary, and facts absent from the source. For a deterministic row carrying `title`, preserve that value exactly as `source_title`. For a fallback row carrying `entry`, identify a title only when it occurs verbatim in that entry; never invent one.
