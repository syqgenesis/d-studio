ChineseSimplified.isl
=====================

Inno Setup 6.x does not install a Chinese (Simplified) translation by default
(it lives in the "Unofficial" folder of the Inno Setup sources), so a copy is
kept here and passed to the compiler by installer/build_windows.ps1.

Source : https://github.com/jrsoftware/issrc/blob/is-6_7_4/Files/Languages/Unofficial/ChineseSimplified.isl
         (git blob d6a11c4490de07dad443ade668289fc954dfa1ed, header "Inno Setup version 6.5.0+")
Change : a UTF-8 byte-order mark was prepended so every Unicode Inno Setup
         version reads it as UTF-8. No other edits.
License: distributed with Inno Setup under the Inno Setup license.

If the Inno Setup compiler on the build machine ships its own
Languages\ChineseSimplified.isl (Inno Setup 7 made it official), the build
script uses that one instead so the messages always match the compiler.
