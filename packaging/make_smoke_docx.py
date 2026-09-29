# -*- mode: python ; coding: utf-8 -*-
"""生成最小合法 docx (便携版 CI 冒烟用, 本地亦可).

用法: python packaging/make_smoke_docx.py [out.docx]
默认输出 smoke.docx。内容为一段中文测试文本, 供 frozen exe 的
add → anydoc 转换 → FTS search 全链路断言。
"""

import sys
import zipfile

DOC = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
<w:p><w:r><w:t>小書房便携版 office 冒烟测试段落。</w:t></w:r></w:p>
<w:p><w:r><w:t>Second paragraph for frozen bundle verification.</w:t></w:r></w:p>
</w:body></w:document>'''

CT = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>'''

RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else 'smoke.docx'
    with zipfile.ZipFile(out, 'w') as z:
        z.writestr('[Content_Types].xml', CT)
        z.writestr('_rels/.rels', RELS)
        z.writestr('word/document.xml', DOC)
    print(f'{out} written')


if __name__ == '__main__':
    main()
