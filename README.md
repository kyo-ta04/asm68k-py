# asm68k-py

Python で書いた EhBASIC-68k専用 MC68000 アセンブラ。C 言語の配列ソースと生バイナリ (.bin) を出力します。

もともとは、EMUC68000-PGA2350B（RP2350B 上の MC68000 エミュレータ）向けに
EhBASIC 上で動くセルフホスト・アセンブラを設計するための試作として生まれました。
設計検証が目的だったはずが、実用的なEhBASIC専用クロスアセンブラになりました。

## 特徴

- パス構成: 整形（INCLUDE 展開・EQU 収集）→ 固定長中間レコード化 → シンボル解決（収束まで反復）→ コード生成
- 出力形式: `const uint8_t NAME[] = {...}` 形式の C ソース、および生バイナリ (.bin)
- `--check` で既存の C 配列とバイト単位で照合できる（回帰確認用）
- オリジナルの Macro Assembler 1.42 の符号化規則を再現:
  - 絶対アドレスは符号付き16ビットに収まれば abs.W、そうでなければ abs.L（前方参照は反復で確定）
  - サフィックスなし分岐も最終距離で .S を自動選択
  - 即値＋Dn 宛の ALU 命令は即値グループではなく通常オペコード＋EA 形式で符号化、など

## 検証実績

EhBASIC v3.54 のソースから、リファレンスバイナリと **バイト単位で完全一致** する
16,384 バイトのイメージを生成できることを確認しています（素の設定と、
EMUC68000-PGA2350B 向け設定＝RAM 256KB / ACIA $80E000 の両方）。

検証用の EhBASIC ソース一式は `third_party/ehbasic/` に無改変で同梱しています
（Lee Davison 氏の EhBASIC、satoshiokue/EhBASIC68k-EMU 収録版）。
再現コマンドは同フォルダの NOTICE.md を参照してください。
なお、このフォルダのファイルは本リポジトリの MIT ライセンスの対象外で、
作者の条件（教育・個人利用のみ自由、商用権利は作者に留保）が適用されます。

## 必要環境

- Python 3.10 以上（標準ライブラリのみ、追加インストール不要）
- Windows / Linux / macOS

## 使い方

```
python asm68k.py SOURCE.asm [-o OUT.c] [--name NAME] [--bin OUT.bin]
python asm68k.py SOURCE.asm --check TARGET.c [--array NAME]
```

- INCLUDE はソースファイルのあるフォルダから相対で解決されます
- `--bin` は C 配列と同一範囲の生バイト列を書きます（隙間は $FF 詰め、ヘッダなし）
- 終了コード: 0 = 正常（--check 時は一致）/ 1 = 照合不一致 / 2 = アセンブルエラー

### 動作確認（tests/ の4本）

```
python asm68k.py tests/test1.asm -o _check1.c --check tests/test1.c
python asm68k.py tests/test2.asm -o _check2.c --check tests/test2.c
python asm68k.py tests/test3.asm -o _check3.c --check tests/test3.c
python asm68k.py tests/test4.asm -o _check4.c --check tests/test4.c
```

4つとも `check OK: identical` と出れば正常です。
tests/ のソースは EMUC68000-PGA2350B ファームのテストプログラム（test1〜test4）を
切り出したもので、ゴールデンはファーム内のバイト配列です。

## 制限（試作由来）

- 対応命令は EhBASIC ソースで使われる範囲（75 基本ニーモニック）が中心です
- EQU の式は (項)±(項) まで。それ以外の複雑な式は定数畳み込みと
  後置記法レコードで一部対応します
- マクロ、条件アセンブルには対応しません

## ライセンス

MIT License（LICENSE ファイル参照）。
ただし `third_party/ehbasic/` のファイルは対象外で、Lee Davison 氏の条件が
適用されます（詳細は同フォルダの NOTICE.md）。
