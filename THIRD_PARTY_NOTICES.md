# Third-party notices

The repository's original code and documentation are provided under MIT.
Dependency packages and external tools retain their own licenses; they are not
bundled into this source repository or relicensed by its LICENSE file.

- Pillow and PyYAML: installed separately; consult each installed distribution's license.
- Optional PyMuPDF/MuPDF: AGPL or a commercial license. See the [upstream licensing statement](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright). Enabling or redistributing the PDF feature does not turn these dependencies into MIT software.
- Pandoc: external executable, not distributed here. See its [upstream repository](https://github.com/jgm/pandoc).
- Codex, Obsidian, Zotero, MinerU, and their services are separate products. Names identify interoperability, not endorsement or a bundled license.

## Image Converter interoperability

`scripts/sync_image_converter_alignments.py` reproduces the cache-key and path
behavior used by [Image Converter](https://github.com/xRyul/obsidian-image-converter)
1.4.4. The plugin itself is not included. Its MIT notice is retained below for the
interoperability implementation. MurmurHash3 originates with Austin Appleby;
the [original implementation](https://github.com/aappleby/smhasher/blob/master/src/MurmurHash3.cpp)
is dedicated to the public domain. This project preserves the plugin-specific
behavior rather than claiming its cache keys are a general MurmurHash API.

MIT License

Copyright (c) 2023 xRyul

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
